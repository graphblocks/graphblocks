use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::marker::PhantomData;
use std::sync::atomic::{AtomicU64, Ordering};

use serde_json::{Map, Value, json};

use graphblocks_compiler::compiler::BlockCatalog;
use graphblocks_schema::SchemaId;

/// A marker for values that may flow through a typed block port.
pub trait PortType {
    const TYPE_REF: &'static str;
}

/// A value that may be exposed through a graph interface.
pub trait GraphValue: PortType {}

/// A stdlib block definition with statically typed inputs, configuration, and outputs.
pub trait Block {
    const ID: &'static str;

    type Inputs: NodeInputs;
    type Config: NodeConfig;
    type Outputs: NodeOutputs;

    fn config(&self) -> &Self::Config;

    #[doc(hidden)]
    fn registration(&self) -> BlockRegistration {
        BlockRegistration::custom()
    }
}

/// Opaque evidence that distinguishes crate-owned stdlib wrappers from custom blocks.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BlockRegistration {
    stdlib_block_id: Option<&'static str>,
}

impl BlockRegistration {
    fn custom() -> Self {
        Self {
            stdlib_block_id: None,
        }
    }

    pub(crate) fn stdlib(block_id: &'static str) -> Self {
        Self {
            stdlib_block_id: Some(block_id),
        }
    }
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
    fn port_types() -> BTreeMap<String, &'static str>;

    fn from_factory(factory: &NodeOutputFactory<'_>) -> Result<Self, TypedGraphError>;
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NodeInputReference {
    builder_id: u64,
    reference: String,
    origin: PortOrigin,
    type_ref: &'static str,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum PortOrigin {
    GraphInput { name: String },
    Node { node_id: String, port: String },
}

/// A graph reference whose Rust type records the schema flowing through the port.
#[derive(Debug, Eq, PartialEq)]
pub struct Port<T: PortType> {
    builder_id: u64,
    reference: String,
    origin: PortOrigin,
    marker: PhantomData<fn(T) -> T>,
}

impl<T: PortType> Clone for Port<T> {
    fn clone(&self) -> Self {
        Self {
            builder_id: self.builder_id,
            reference: self.reference.clone(),
            origin: self.origin.clone(),
            marker: PhantomData,
        }
    }
}

impl<T: PortType> Port<T> {
    pub fn reference(&self) -> &str {
        &self.reference
    }

    fn node_output(builder_id: u64, node_id: &str, port: &str) -> Self {
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
            origin: PortOrigin::GraphInput {
                name: name.to_owned(),
            },
            marker: PhantomData,
        }
    }

    pub fn into_input_reference(self) -> NodeInputReference {
        NodeInputReference {
            builder_id: self.builder_id,
            reference: self.reference,
            origin: self.origin,
            type_ref: T::TYPE_REF,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TypedPortDescriptor {
    name: String,
    type_ref: String,
    required: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TypedResourceSlotDescriptor {
    name: String,
    type_ref: String,
    optional: bool,
}

impl TypedResourceSlotDescriptor {
    pub fn required(name: impl Into<String>, type_ref: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            type_ref: type_ref.into(),
            optional: false,
        }
    }

    pub fn optional(name: impl Into<String>, type_ref: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            type_ref: type_ref.into(),
            optional: true,
        }
    }
}

impl TypedPortDescriptor {
    pub fn required<T: PortType>(name: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            type_ref: T::TYPE_REF.to_owned(),
            required: true,
        }
    }

    pub fn optional<T: PortType>(name: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            type_ref: T::TYPE_REF.to_owned(),
            required: false,
        }
    }

    pub fn required_any(name: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            type_ref: "Any".to_owned(),
            required: true,
        }
    }

    pub fn optional_any(name: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            type_ref: "Any".to_owned(),
            required: false,
        }
    }

    pub(crate) fn required_type(name: impl Into<String>, type_ref: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            type_ref: type_ref.into(),
            required: true,
        }
    }

    pub(crate) fn optional_type(name: impl Into<String>, type_ref: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            type_ref: type_ref.into(),
            required: false,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TypedBlockOrigin {
    Custom,
    Stdlib,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TypedBlockDescriptor {
    block_id: String,
    inputs: BTreeMap<String, TypedPortDescriptor>,
    outputs: BTreeMap<String, TypedPortDescriptor>,
    resource_slots: BTreeMap<String, TypedResourceSlotDescriptor>,
    origin: TypedBlockOrigin,
}

impl TypedBlockDescriptor {
    pub fn new(
        block_id: impl Into<String>,
        inputs: impl IntoIterator<Item = TypedPortDescriptor>,
        outputs: impl IntoIterator<Item = TypedPortDescriptor>,
    ) -> Result<Self, TypedGraphError> {
        Self::build(
            block_id,
            inputs,
            outputs,
            std::iter::empty(),
            TypedBlockOrigin::Custom,
        )
    }

    pub fn new_with_resource_slots(
        block_id: impl Into<String>,
        inputs: impl IntoIterator<Item = TypedPortDescriptor>,
        outputs: impl IntoIterator<Item = TypedPortDescriptor>,
        resource_slots: impl IntoIterator<Item = TypedResourceSlotDescriptor>,
    ) -> Result<Self, TypedGraphError> {
        Self::build(
            block_id,
            inputs,
            outputs,
            resource_slots,
            TypedBlockOrigin::Custom,
        )
    }

    fn build(
        block_id: impl Into<String>,
        inputs: impl IntoIterator<Item = TypedPortDescriptor>,
        outputs: impl IntoIterator<Item = TypedPortDescriptor>,
        resource_slots: impl IntoIterator<Item = TypedResourceSlotDescriptor>,
        origin: TypedBlockOrigin,
    ) -> Result<Self, TypedGraphError> {
        let block_id = block_id.into();
        let mut input_map = BTreeMap::new();
        let mut output_map = BTreeMap::new();
        let mut resource_slot_map = BTreeMap::new();
        for descriptor in inputs {
            if descriptor.name.trim().is_empty() {
                return Err(TypedGraphError::EmptyName {
                    kind: "block input",
                });
            }
            if input_map
                .insert(descriptor.name.clone(), descriptor)
                .is_some()
            {
                return Err(TypedGraphError::DuplicateName {
                    kind: "block input",
                    name: block_id,
                });
            }
        }
        for descriptor in outputs {
            if descriptor.name.trim().is_empty() {
                return Err(TypedGraphError::EmptyName {
                    kind: "block output",
                });
            }
            if output_map
                .insert(descriptor.name.clone(), descriptor)
                .is_some()
            {
                return Err(TypedGraphError::DuplicateName {
                    kind: "block output",
                    name: block_id,
                });
            }
        }
        for descriptor in resource_slots {
            if descriptor.name.trim().is_empty() {
                return Err(TypedGraphError::EmptyName {
                    kind: "block resource slot",
                });
            }
            if resource_slot_map
                .insert(descriptor.name.clone(), descriptor)
                .is_some()
            {
                return Err(TypedGraphError::DuplicateName {
                    kind: "block resource slot",
                    name: block_id,
                });
            }
        }
        let value = json!([{
            "typeId": block_id.clone(),
            "inputs": input_map.values().map(port_descriptor_value).collect::<Vec<_>>(),
            "outputs": output_map.values().map(port_descriptor_value).collect::<Vec<_>>(),
            "resourceSlots": resource_slot_map.values().map(resource_slot_descriptor_value).collect::<Vec<_>>(),
        }]);
        BlockCatalog::from_blocks(&value)
            .map_err(|message| TypedGraphError::InvalidCatalog { message })?;
        Ok(Self {
            block_id,
            inputs: input_map,
            outputs: output_map,
            resource_slots: resource_slot_map,
            origin,
        })
    }

    pub(crate) fn stdlib(
        block_id: impl Into<String>,
        inputs: impl IntoIterator<Item = TypedPortDescriptor>,
        outputs: impl IntoIterator<Item = TypedPortDescriptor>,
    ) -> Result<Self, TypedGraphError> {
        Self::build(
            block_id,
            inputs,
            outputs,
            std::iter::empty(),
            TypedBlockOrigin::Stdlib,
        )
    }

    pub(crate) fn stdlib_with_resource_slots(
        block_id: impl Into<String>,
        inputs: impl IntoIterator<Item = TypedPortDescriptor>,
        outputs: impl IntoIterator<Item = TypedPortDescriptor>,
        resource_slots: impl IntoIterator<Item = TypedResourceSlotDescriptor>,
    ) -> Result<Self, TypedGraphError> {
        Self::build(
            block_id,
            inputs,
            outputs,
            resource_slots,
            TypedBlockOrigin::Stdlib,
        )
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct TypedBlockCatalog {
    descriptors: BTreeMap<String, TypedBlockDescriptor>,
}

impl TypedBlockCatalog {
    pub fn from_descriptors(
        descriptors: impl IntoIterator<Item = TypedBlockDescriptor>,
    ) -> Result<Self, TypedGraphError> {
        let mut catalog = Self::default();
        catalog.extend(descriptors)?;
        Ok(catalog)
    }

    fn extend(
        &mut self,
        descriptors: impl IntoIterator<Item = TypedBlockDescriptor>,
    ) -> Result<(), TypedGraphError> {
        for descriptor in descriptors {
            if self.descriptors.contains_key(&descriptor.block_id) {
                return Err(TypedGraphError::DuplicateBlockDescriptor {
                    block_id: descriptor.block_id,
                });
            }
            self.descriptors
                .insert(descriptor.block_id.clone(), descriptor);
        }
        Ok(())
    }

    fn get(&self, block_id: &str) -> Option<&TypedBlockDescriptor> {
        self.descriptors.get(block_id)
    }

    pub fn compiler_catalog(&self) -> Result<BlockCatalog, TypedGraphError> {
        let descriptors = self
            .descriptors
            .values()
            .map(|descriptor| {
                json!({
                    "typeId": descriptor.block_id,
                    "inputs": descriptor.inputs.values().map(port_descriptor_value).collect::<Vec<_>>(),
                    "outputs": descriptor.outputs.values().map(port_descriptor_value).collect::<Vec<_>>(),
                    "resourceSlots": descriptor.resource_slots.values().map(resource_slot_descriptor_value).collect::<Vec<_>>(),
                })
            })
            .collect::<Vec<_>>();
        BlockCatalog::from_blocks(&Value::Array(descriptors))
            .map_err(|message| TypedGraphError::InvalidCatalog { message })
    }
}

pub struct NodeOutputFactory<'a> {
    builder_id: u64,
    node_id: &'a str,
    descriptor: &'a TypedBlockDescriptor,
}

impl NodeOutputFactory<'_> {
    pub fn port<T: PortType>(&self, name: &str) -> Result<Port<T>, TypedGraphError> {
        let Some(descriptor) = self.descriptor.outputs.get(name) else {
            return Err(TypedGraphError::UnknownBlockPort {
                block_id: self.descriptor.block_id.clone(),
                direction: "output",
                port: name.to_owned(),
            });
        };
        validate_type_ref(
            &self.descriptor.block_id,
            "output",
            name,
            &descriptor.type_ref,
            T::TYPE_REF,
        )?;
        Ok(Port::node_output(self.builder_id, self.node_id, name))
    }
}

fn port_descriptor_value(descriptor: &TypedPortDescriptor) -> Value {
    json!({
        "name": descriptor.name,
        "type": descriptor.type_ref,
        "required": descriptor.required,
    })
}

fn resource_slot_descriptor_value(descriptor: &TypedResourceSlotDescriptor) -> Value {
    json!({
        "name": descriptor.name,
        "type": descriptor.type_ref,
        "optional": descriptor.optional,
    })
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TypedGraphError {
    EmptyName {
        kind: &'static str,
    },
    DuplicateName {
        kind: &'static str,
        name: String,
    },
    InvalidOutputSource {
        output: String,
    },
    UnknownOutputNode {
        output: String,
        node_id: String,
    },
    DuplicateOutputPort {
        node_id: String,
        port: String,
    },
    CrossBuilderPort {
        reference: String,
    },
    InvalidSchema {
        schema: String,
        message: String,
    },
    InvalidCatalog {
        message: String,
    },
    DuplicateBlockDescriptor {
        block_id: String,
    },
    UnknownBlock {
        block_id: String,
    },
    UntrustedStdlibBlock {
        block_id: String,
    },
    UnknownBlockPort {
        block_id: String,
        direction: &'static str,
        port: String,
    },
    MissingRequiredBlockInput {
        block_id: String,
        port: String,
    },
    MissingRequiredBlockOutput {
        block_id: String,
        port: String,
    },
    BlockPortTypeMismatch {
        block_id: String,
        direction: &'static str,
        port: String,
        expected: String,
        actual: String,
    },
    UnknownInputSource {
        reference: String,
    },
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
            Self::InvalidCatalog { message } => {
                write!(formatter, "invalid typed block catalog: {message}")
            }
            Self::DuplicateBlockDescriptor { block_id } => {
                write!(formatter, "duplicate block descriptor {block_id:?}")
            }
            Self::UnknownBlock { block_id } => {
                write!(
                    formatter,
                    "block {block_id:?} is not registered in the typed catalog"
                )
            }
            Self::UntrustedStdlibBlock { block_id } => write!(
                formatter,
                "block {block_id:?} must use the crate-owned stdlib wrapper"
            ),
            Self::UnknownBlockPort {
                block_id,
                direction,
                port,
            } => write!(
                formatter,
                "block {block_id:?} has no registered {direction} port {port:?}"
            ),
            Self::MissingRequiredBlockInput { block_id, port } => write!(
                formatter,
                "block {block_id:?} is missing required input port {port:?}"
            ),
            Self::MissingRequiredBlockOutput { block_id, port } => write!(
                formatter,
                "block {block_id:?} is missing required output port {port:?}"
            ),
            Self::BlockPortTypeMismatch {
                block_id,
                direction,
                port,
                expected,
                actual,
            } => write!(
                formatter,
                "block {block_id:?} {direction} port {port:?} expects {expected}, got {actual}"
            ),
            Self::UnknownInputSource { reference } => {
                write!(
                    formatter,
                    "input reference {reference:?} has no known source port"
                )
            }
        }
    }
}

impl Error for TypedGraphError {}

/// A graph produced by [`GraphBuilder`].
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GraphDocument {
    value: Value,
    block_catalog: BlockCatalog,
}

impl GraphDocument {
    pub fn as_value(&self) -> &Value {
        &self.value
    }

    pub fn into_value(self) -> Value {
        self.value
    }

    pub(crate) fn block_catalog(&self) -> &BlockCatalog {
        &self.block_catalog
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
    node_output_types: BTreeMap<String, BTreeMap<String, &'static str>>,
    block_catalog: TypedBlockCatalog,
    compiler_catalog: BlockCatalog,
}

impl GraphBuilder {
    pub fn new(name: impl Into<String>) -> Result<Self, TypedGraphError> {
        Self::with_custom_blocks(name, std::iter::empty())
    }

    pub fn with_custom_blocks(
        name: impl Into<String>,
        descriptors: impl IntoIterator<Item = TypedBlockDescriptor>,
    ) -> Result<Self, TypedGraphError> {
        let name = require_name("graph", name.into())?;
        let mut block_catalog = crate::stdlib_blocks::stdlib_typed_block_catalog()?;
        block_catalog.extend(descriptors)?;
        let compiler_catalog = block_catalog.compiler_catalog()?;
        Ok(Self {
            builder_id: next_builder_id(),
            name,
            inputs: BTreeMap::new(),
            outputs: BTreeMap::new(),
            nodes: BTreeMap::new(),
            node_output_types: BTreeMap::new(),
            block_catalog,
            compiler_catalog,
        })
    }

    pub fn input<T: GraphValue>(
        &mut self,
        name: impl Into<String>,
    ) -> Result<Port<T>, TypedGraphError> {
        let name = require_name("input", name.into())?;
        validate_schema(T::TYPE_REF)?;
        if self.inputs.contains_key(&name) {
            return Err(TypedGraphError::DuplicateName {
                kind: "input",
                name,
            });
        }
        self.inputs.insert(name.clone(), T::TYPE_REF.to_owned());
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
        let descriptor =
            self.block_catalog
                .get(B::ID)
                .ok_or_else(|| TypedGraphError::UnknownBlock {
                    block_id: B::ID.to_owned(),
                })?;
        let registration = block.registration();
        if descriptor.origin == TypedBlockOrigin::Stdlib
            && registration.stdlib_block_id != Some(B::ID)
        {
            return Err(TypedGraphError::UntrustedStdlibBlock {
                block_id: B::ID.to_owned(),
            });
        }
        for (name, reference) in &inputs {
            let Some(port_descriptor) = descriptor.inputs.get(name) else {
                return Err(TypedGraphError::UnknownBlockPort {
                    block_id: B::ID.to_owned(),
                    direction: "input",
                    port: name.clone(),
                });
            };
            validate_input_source(&self.inputs, &self.node_output_types, reference)?;
            validate_type_ref(
                B::ID,
                "input",
                name,
                &port_descriptor.type_ref,
                reference.type_ref,
            )?;
        }
        for port_descriptor in descriptor.inputs.values() {
            if port_descriptor.required && !inputs.contains_key(&port_descriptor.name) {
                return Err(TypedGraphError::MissingRequiredBlockInput {
                    block_id: B::ID.to_owned(),
                    port: port_descriptor.name.clone(),
                });
            }
        }
        let declared_outputs = B::Outputs::port_types();
        for (name, type_ref) in &declared_outputs {
            let Some(port_descriptor) = descriptor.outputs.get(name) else {
                return Err(TypedGraphError::UnknownBlockPort {
                    block_id: B::ID.to_owned(),
                    direction: "output",
                    port: name.clone(),
                });
            };
            validate_type_ref(B::ID, "output", name, &port_descriptor.type_ref, type_ref)?;
        }
        for port_descriptor in descriptor.outputs.values() {
            if port_descriptor.required && !declared_outputs.contains_key(&port_descriptor.name) {
                return Err(TypedGraphError::MissingRequiredBlockOutput {
                    block_id: B::ID.to_owned(),
                    port: port_descriptor.name.clone(),
                });
            }
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
        let factory = NodeOutputFactory {
            builder_id: self.builder_id,
            node_id: &node_id,
            descriptor,
        };
        let outputs = B::Outputs::from_factory(&factory)?;
        self.nodes.insert(node_id.clone(), Value::Object(node));
        self.node_output_types.insert(node_id, declared_outputs);
        Ok(outputs)
    }

    pub fn bind_output<T: GraphValue>(
        &mut self,
        name: impl Into<String>,
        source: &Port<T>,
    ) -> Result<(), TypedGraphError> {
        let name = require_name("output", name.into())?;
        validate_schema(T::TYPE_REF)?;
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
        let Some(actual_type) = self
            .node_output_types
            .get(node_id)
            .and_then(|ports| ports.get(port))
        else {
            return Err(TypedGraphError::UnknownInputSource {
                reference: source.reference.clone(),
            });
        };
        if *actual_type != T::TYPE_REF {
            return Err(TypedGraphError::BlockPortTypeMismatch {
                block_id: node_id.clone(),
                direction: "output",
                port: port.clone(),
                expected: (*actual_type).to_owned(),
                actual: T::TYPE_REF.to_owned(),
            });
        }
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
        self.outputs.insert(name, T::TYPE_REF.to_owned());
        Ok(())
    }

    pub fn build(self) -> GraphDocument {
        GraphDocument {
            value: json!({
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
            }),
            block_catalog: self.compiler_catalog,
        }
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

fn validate_type_ref(
    block_id: &str,
    direction: &'static str,
    port: &str,
    expected: &str,
    actual: &str,
) -> Result<(), TypedGraphError> {
    if expected == "Any" || actual == "Any" || expected == actual {
        return Ok(());
    }
    Err(TypedGraphError::BlockPortTypeMismatch {
        block_id: block_id.to_owned(),
        direction,
        port: port.to_owned(),
        expected: expected.to_owned(),
        actual: actual.to_owned(),
    })
}

fn validate_input_source(
    graph_inputs: &BTreeMap<String, String>,
    node_outputs: &BTreeMap<String, BTreeMap<String, &'static str>>,
    reference: &NodeInputReference,
) -> Result<(), TypedGraphError> {
    let source_type = match &reference.origin {
        PortOrigin::GraphInput { name } => graph_inputs.get(name).map(String::as_str),
        PortOrigin::Node { node_id, port } => node_outputs
            .get(node_id)
            .and_then(|outputs| outputs.get(port))
            .copied(),
    };
    let Some(source_type) = source_type else {
        return Err(TypedGraphError::UnknownInputSource {
            reference: reference.reference.clone(),
        });
    };
    if source_type != reference.type_ref {
        return Err(TypedGraphError::BlockPortTypeMismatch {
            block_id: reference.reference.clone(),
            direction: "source",
            port: reference.reference.clone(),
            expected: source_type.to_owned(),
            actual: reference.type_ref.to_owned(),
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use serde_json::{Map, Value, json};

    use super::{
        Block, GraphBuilder, GraphValue, NodeConfig, NodeInputs, NodeOutputFactory, NodeOutputs,
        Port, PortType, TypedBlockDescriptor, TypedGraphError, TypedPortDescriptor,
    };

    struct Text;

    impl PortType for Text {
        const TYPE_REF: &'static str = "graphblocks.ai/Text@1";
    }

    impl GraphValue for Text {}

    struct InvalidText;

    impl PortType for InvalidText {
        const TYPE_REF: &'static str = "graphblocks.ai/Text";
    }

    impl GraphValue for InvalidText {}

    struct Other;

    impl PortType for Other {
        const TYPE_REF: &'static str = "graphblocks.ai/Other@1";
    }

    struct EchoConfig;

    impl NodeConfig for EchoConfig {
        fn to_config_object(&self) -> Map<String, Value> {
            Map::new()
        }
    }

    struct Echo;

    struct FakeFuse;

    struct IncompleteEcho;

    struct WrongOutputEcho;

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

    struct IncompleteEchoOutputs;

    impl NodeOutputs for IncompleteEchoOutputs {
        fn port_types() -> BTreeMap<String, &'static str> {
            BTreeMap::new()
        }

        fn from_factory(_factory: &NodeOutputFactory<'_>) -> Result<Self, TypedGraphError> {
            Ok(Self)
        }
    }

    struct WrongOutputEchoOutputs;

    impl NodeOutputs for WrongOutputEchoOutputs {
        fn port_types() -> BTreeMap<String, &'static str> {
            BTreeMap::from([("message".to_owned(), Other::TYPE_REF)])
        }

        fn from_factory(factory: &NodeOutputFactory<'_>) -> Result<Self, TypedGraphError> {
            let _message: Port<Other> = factory.port("message")?;
            Ok(Self)
        }
    }

    impl NodeOutputs for EchoOutputs {
        fn port_types() -> BTreeMap<String, &'static str> {
            BTreeMap::from([("message".to_owned(), Text::TYPE_REF)])
        }

        fn from_factory(factory: &NodeOutputFactory<'_>) -> Result<Self, TypedGraphError> {
            Ok(Self {
                message: factory.port("message")?,
            })
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

    impl Block for FakeFuse {
        const ID: &'static str = "retrieve.fuse@1";
        type Config = EchoConfig;
        type Inputs = EchoInputs;
        type Outputs = EchoOutputs;

        fn config(&self) -> &Self::Config {
            &EchoConfig
        }
    }

    impl Block for IncompleteEcho {
        const ID: &'static str = "test.incomplete_echo@1";
        type Config = EchoConfig;
        type Inputs = EchoInputs;
        type Outputs = IncompleteEchoOutputs;

        fn config(&self) -> &Self::Config {
            &EchoConfig
        }
    }

    impl Block for WrongOutputEcho {
        const ID: &'static str = "test.wrong_output_echo@1";
        type Config = EchoConfig;
        type Inputs = EchoInputs;
        type Outputs = WrongOutputEchoOutputs;

        fn config(&self) -> &Self::Config {
            &EchoConfig
        }
    }

    fn echo_graph(name: &str) -> GraphBuilder {
        GraphBuilder::with_custom_blocks(
            name,
            [TypedBlockDescriptor::new(
                Echo::ID,
                [TypedPortDescriptor::required::<Text>("message")],
                [TypedPortDescriptor::required::<Text>("message")],
            )
            .expect("echo descriptor is valid")],
        )
        .expect("echo catalog is valid")
    }

    #[test]
    fn builder_emits_portable_graph_document() {
        let mut graph = echo_graph("typed-echo");
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
        let mut second = echo_graph("second");

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

    #[test]
    fn graph_rejects_custom_block_contract_that_impersonates_stdlib() {
        let mut graph = GraphBuilder::new("typed-contract").expect("graph name is valid");
        let message = graph.input::<Text>("message").expect("input is unique");

        assert!(graph.add("fake", FakeFuse, EchoInputs { message }).is_err());
    }

    #[test]
    fn graph_rejects_block_wrapper_missing_a_required_output() {
        let mut graph = GraphBuilder::with_custom_blocks(
            "typed-incomplete",
            [TypedBlockDescriptor::new(
                IncompleteEcho::ID,
                [TypedPortDescriptor::required::<Text>("message")],
                [TypedPortDescriptor::required::<Text>("message")],
            )
            .expect("incomplete echo descriptor is valid")],
        )
        .expect("custom block catalog is valid");
        let message = graph.input::<Text>("message").expect("input is unique");

        assert!(matches!(
            graph.add("echo", IncompleteEcho, EchoInputs { message }),
            Err(TypedGraphError::MissingRequiredBlockOutput { block_id, port })
                if block_id == IncompleteEcho::ID && port == "message"
        ));
    }

    #[test]
    fn graph_rejects_unregistered_input_keys() {
        let mut graph = GraphBuilder::with_custom_blocks(
            "typed-input-key",
            [TypedBlockDescriptor::new(
                Echo::ID,
                [TypedPortDescriptor::required::<Text>("text")],
                [TypedPortDescriptor::required::<Text>("message")],
            )
            .expect("echo descriptor is valid")],
        )
        .expect("custom block catalog is valid");
        let message = graph.input::<Text>("message").expect("input is unique");

        assert!(matches!(
            graph.add("echo", Echo, EchoInputs { message }),
            Err(TypedGraphError::UnknownBlockPort {
                direction: "input",
                port,
                ..
            }) if port == "message"
        ));
    }

    #[test]
    fn graph_rejects_output_type_declarations_that_disagree_with_catalog() {
        let mut graph = GraphBuilder::with_custom_blocks(
            "typed-output-type",
            [TypedBlockDescriptor::new(
                WrongOutputEcho::ID,
                [TypedPortDescriptor::required::<Text>("message")],
                [TypedPortDescriptor::required::<Text>("message")],
            )
            .expect("echo descriptor is valid")],
        )
        .expect("custom block catalog is valid");
        let message = graph.input::<Text>("message").expect("input is unique");

        assert!(matches!(
            graph.add("echo", WrongOutputEcho, EchoInputs { message }),
            Err(TypedGraphError::BlockPortTypeMismatch {
                direction: "output",
                port,
                ..
            }) if port == "message"
        ));
    }

    #[test]
    fn graph_rejects_node_ports_without_a_known_source() {
        let mut graph = echo_graph("typed-source");
        let missing = Port::<Text>::node_output(graph.builder_id, "missing", "message");

        assert!(matches!(
            graph.add("echo", Echo, EchoInputs { message: missing }),
            Err(TypedGraphError::UnknownInputSource { reference })
                if reference == "missing.message"
        ));
    }
}
