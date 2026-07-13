use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::marker::PhantomData;
use std::sync::atomic::{AtomicU64, Ordering};

use serde_json::{Map, Value, json};

use graphblocks_schema::SchemaId;

/// A value that may be exposed through a graph interface.
pub trait GraphValue {
    const SCHEMA: &'static str;
}

/// A stdlib block definition with statically typed inputs, configuration, and outputs.
pub trait Block {
    const ID: &'static str;

    type Inputs: NodeInputs;
    type Config: NodeConfig;
    type Outputs: NodeOutputs;

    fn config(&self) -> &Self::Config;
}

/// Converts a block-specific input struct to canonical graph input references.
pub trait NodeInputs {
    fn into_node_inputs(self) -> BTreeMap<String, NodeInputReference>;
}

/// Converts a block-specific configuration struct at the graph serialization boundary.
pub trait NodeConfig {
    fn to_config_object(&self) -> Map<String, Value>;
}

/// Constructs a block-specific collection of typed output ports.
pub trait NodeOutputs: Sized {
    fn for_node(builder_id: u64, node_id: &str) -> Self;
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NodeInputReference {
    builder_id: u64,
    reference: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum PortOrigin {
    GraphInput,
    Node { node_id: String, port: String },
}

/// A graph reference whose Rust type records the schema flowing through the port.
#[derive(Debug, Eq, PartialEq)]
pub struct Port<T> {
    builder_id: u64,
    reference: String,
    origin: PortOrigin,
    marker: PhantomData<fn(T) -> T>,
}

impl<T> Clone for Port<T> {
    fn clone(&self) -> Self {
        Self {
            builder_id: self.builder_id,
            reference: self.reference.clone(),
            origin: self.origin.clone(),
            marker: PhantomData,
        }
    }
}

impl<T> Port<T> {
    pub fn reference(&self) -> &str {
        &self.reference
    }

    pub fn node_output(builder_id: u64, node_id: &str, port: &str) -> Self {
        Self {
            builder_id,
            reference: format!("{node_id}.{port}"),
            origin: PortOrigin::Node {
                node_id: node_id.to_owned(),
                port: port.to_owned(),
            },
            marker: PhantomData,
        }
    }

    fn graph_input(builder_id: u64, name: &str) -> Self {
        Self {
            builder_id,
            reference: format!("$input.{name}"),
            origin: PortOrigin::GraphInput,
            marker: PhantomData,
        }
    }

    pub fn into_input_reference(self) -> NodeInputReference {
        NodeInputReference {
            builder_id: self.builder_id,
            reference: self.reference,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TypedGraphError {
    EmptyName { kind: &'static str },
    DuplicateName { kind: &'static str, name: String },
    InvalidOutputSource { output: String },
    UnknownOutputNode { output: String, node_id: String },
    DuplicateOutputPort { node_id: String, port: String },
    CrossBuilderPort { reference: String },
    InvalidSchema { schema: String, message: String },
}

impl fmt::Display for TypedGraphError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyName { kind } => write!(formatter, "{kind} name must not be empty"),
            Self::DuplicateName { kind, name } => {
                write!(formatter, "duplicate {kind} name {name:?}")
            }
            Self::InvalidOutputSource { output } => write!(
                formatter,
                "graph output {output:?} must be bound to a node output"
            ),
            Self::UnknownOutputNode { output, node_id } => write!(
                formatter,
                "graph output {output:?} references unknown node {node_id:?}"
            ),
            Self::DuplicateOutputPort { node_id, port } => write!(
                formatter,
                "node output {node_id}.{port} is already bound to a graph output"
            ),
            Self::CrossBuilderPort { reference } => write!(
                formatter,
                "port reference {reference:?} belongs to a different graph builder"
            ),
            Self::InvalidSchema { schema, message } => {
                write!(formatter, "invalid graph schema {schema:?}: {message}")
            }
        }
    }
}

impl Error for TypedGraphError {}

/// A graph produced by [`GraphBuilder`].
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GraphDocument(Value);

impl GraphDocument {
    pub fn as_value(&self) -> &Value {
        &self.0
    }

    pub fn into_value(self) -> Value {
        self.0
    }
}

impl AsRef<Value> for GraphDocument {
    fn as_ref(&self) -> &Value {
        self.as_value()
    }
}

/// Builds the portable Graph document while keeping node wiring typed in Rust.
#[derive(Debug)]
pub struct GraphBuilder {
    builder_id: u64,
    name: String,
    inputs: BTreeMap<String, String>,
    outputs: BTreeMap<String, String>,
    nodes: BTreeMap<String, Value>,
}

impl GraphBuilder {
    pub fn new(name: impl Into<String>) -> Result<Self, TypedGraphError> {
        let name = require_name("graph", name.into())?;
        Ok(Self {
            builder_id: next_builder_id(),
            name,
            inputs: BTreeMap::new(),
            outputs: BTreeMap::new(),
            nodes: BTreeMap::new(),
        })
    }

    pub fn input<T: GraphValue>(
        &mut self,
        name: impl Into<String>,
    ) -> Result<Port<T>, TypedGraphError> {
        let name = require_name("input", name.into())?;
        validate_schema(T::SCHEMA)?;
        if self.inputs.contains_key(&name) {
            return Err(TypedGraphError::DuplicateName {
                kind: "input",
                name,
            });
        }
        self.inputs.insert(name.clone(), T::SCHEMA.to_owned());
        Ok(Port::graph_input(self.builder_id, &name))
    }

    pub fn add<B: Block>(
        &mut self,
        node_id: impl Into<String>,
        block: B,
        inputs: B::Inputs,
    ) -> Result<B::Outputs, TypedGraphError> {
        let node_id = require_name("node", node_id.into())?;
        if self.nodes.contains_key(&node_id) {
            return Err(TypedGraphError::DuplicateName {
                kind: "node",
                name: node_id,
            });
        }
        let inputs = inputs.into_node_inputs();
        if let Some(reference) = inputs
            .values()
            .find(|reference| reference.builder_id != self.builder_id)
        {
            return Err(TypedGraphError::CrossBuilderPort {
                reference: reference.reference.clone(),
            });
        }
        let inputs = inputs
            .into_iter()
            .map(|(name, reference)| (name, Value::String(reference.reference)))
            .collect::<Map<_, _>>();
        let config = block.config().to_config_object();
        let mut node = Map::from_iter([
            ("block".to_owned(), Value::String(B::ID.to_owned())),
            ("inputs".to_owned(), Value::Object(inputs)),
        ]);
        if !config.is_empty() {
            node.insert("config".to_owned(), Value::Object(config));
        }
        self.nodes.insert(node_id.clone(), Value::Object(node));
        Ok(B::Outputs::for_node(self.builder_id, &node_id))
    }

    pub fn bind_output<T: GraphValue>(
        &mut self,
        name: impl Into<String>,
        source: &Port<T>,
    ) -> Result<(), TypedGraphError> {
        let name = require_name("output", name.into())?;
        validate_schema(T::SCHEMA)?;
        if self.outputs.contains_key(&name) {
            return Err(TypedGraphError::DuplicateName {
                kind: "output",
                name,
            });
        }
        if source.builder_id != self.builder_id {
            return Err(TypedGraphError::CrossBuilderPort {
                reference: source.reference.clone(),
            });
        }
        let PortOrigin::Node { node_id, port } = &source.origin else {
            return Err(TypedGraphError::InvalidOutputSource { output: name });
        };
        let Some(node) = self.nodes.get_mut(node_id).and_then(Value::as_object_mut) else {
            return Err(TypedGraphError::UnknownOutputNode {
                output: name,
                node_id: node_id.clone(),
            });
        };
        let outputs = node
            .entry("outputs".to_owned())
            .or_insert_with(|| json!({}));
        let Some(outputs) = outputs.as_object_mut() else {
            return Err(TypedGraphError::DuplicateOutputPort {
                node_id: node_id.clone(),
                port: port.clone(),
            });
        };
        if outputs.contains_key(port) {
            return Err(TypedGraphError::DuplicateOutputPort {
                node_id: node_id.clone(),
                port: port.clone(),
            });
        }
        outputs.insert(port.clone(), Value::String(format!("$output.{name}")));
        self.outputs.insert(name, T::SCHEMA.to_owned());
        Ok(())
    }

    pub fn build(self) -> GraphDocument {
        GraphDocument(json!({
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": self.name},
            "spec": {
                "interface": {
                    "inputs": self.inputs,
                    "outputs": self.outputs,
                },
                "nodes": self.nodes,
            },
        }))
    }
}

fn require_name(kind: &'static str, name: String) -> Result<String, TypedGraphError> {
    if name.trim().is_empty() {
        return Err(TypedGraphError::EmptyName { kind });
    }
    Ok(name)
}

fn next_builder_id() -> u64 {
    static NEXT_BUILDER_ID: AtomicU64 = AtomicU64::new(1);
    NEXT_BUILDER_ID.fetch_add(1, Ordering::Relaxed)
}

fn validate_schema(schema: &str) -> Result<(), TypedGraphError> {
    SchemaId::parse(schema)
        .map(|_| ())
        .map_err(|error| TypedGraphError::InvalidSchema {
            schema: schema.to_owned(),
            message: error.to_string(),
        })
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use serde_json::{Map, Value, json};

    use super::{
        Block, GraphBuilder, GraphValue, NodeConfig, NodeInputs, NodeOutputs, Port, TypedGraphError,
    };

    struct Text;

    impl GraphValue for Text {
        const SCHEMA: &'static str = "graphblocks.ai/Text@1";
    }

    struct InvalidText;

    impl GraphValue for InvalidText {
        const SCHEMA: &'static str = "graphblocks.ai/Text";
    }

    struct EchoConfig;

    impl NodeConfig for EchoConfig {
        fn to_config_object(&self) -> Map<String, Value> {
            Map::new()
        }
    }

    struct Echo;

    struct EchoInputs {
        message: Port<Text>,
    }

    impl NodeInputs for EchoInputs {
        fn into_node_inputs(self) -> BTreeMap<String, super::NodeInputReference> {
            BTreeMap::from([("message".to_owned(), self.message.into_input_reference())])
        }
    }

    struct EchoOutputs {
        message: Port<Text>,
    }

    impl NodeOutputs for EchoOutputs {
        fn for_node(builder_id: u64, node_id: &str) -> Self {
            Self {
                message: Port::node_output(builder_id, node_id, "message"),
            }
        }
    }

    impl Block for Echo {
        const ID: &'static str = "test.echo@1";
        type Config = EchoConfig;
        type Inputs = EchoInputs;
        type Outputs = EchoOutputs;

        fn config(&self) -> &Self::Config {
            &EchoConfig
        }
    }

    #[test]
    fn builder_emits_portable_graph_document() {
        let mut graph = GraphBuilder::new("typed-echo").expect("graph name is valid");
        let input = graph.input::<Text>("message").expect("input is unique");
        let echo = graph
            .add("echo", Echo, EchoInputs { message: input })
            .expect("node is unique");
        graph
            .bind_output("message", &echo.message)
            .expect("node output may be exposed");

        assert_eq!(
            graph.build().into_value(),
            json!({
                "apiVersion": "graphblocks.ai/v1alpha3",
                "kind": "Graph",
                "metadata": {"name": "typed-echo"},
                "spec": {
                    "interface": {
                        "inputs": {"message": "graphblocks.ai/Text@1"},
                        "outputs": {"message": "graphblocks.ai/Text@1"},
                    },
                    "nodes": {
                        "echo": {
                            "block": "test.echo@1",
                            "inputs": {"message": "$input.message"},
                            "outputs": {"message": "$output.message"},
                        }
                    }
                }
            })
        );
    }

    #[test]
    fn graph_interface_names_are_unique() {
        let mut graph = GraphBuilder::new("typed-echo").expect("graph name is valid");
        graph.input::<Text>("message").expect("input is unique");

        assert!(matches!(
            graph.input::<Text>("message"),
            Err(TypedGraphError::DuplicateName { kind: "input", name })
                if name == "message"
        ));
    }

    #[test]
    fn graph_rejects_ports_from_another_builder() {
        let mut first = GraphBuilder::new("first").expect("graph name is valid");
        let foreign_input = first.input::<Text>("message").expect("input is unique");
        let mut second = GraphBuilder::new("second").expect("graph name is valid");

        assert!(matches!(
            second.add(
                "echo",
                Echo,
                EchoInputs {
                    message: foreign_input,
                },
            ),
            Err(TypedGraphError::CrossBuilderPort { reference })
                if reference == "$input.message"
        ));
    }

    #[test]
    fn graph_rejects_noncanonical_interface_schema() {
        let mut graph = GraphBuilder::new("typed-echo").expect("graph name is valid");

        assert!(matches!(
            graph.input::<InvalidText>("message"),
            Err(TypedGraphError::InvalidSchema { schema, .. })
                if schema == "graphblocks.ai/Text"
        ));
    }
}
