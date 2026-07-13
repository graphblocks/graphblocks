//! Typed definitions for blocks implemented by the in-process stdlib runtime.
//!
//! Payloads remain `serde_json::Value` at the execution boundary. Distinct marker
//! types on [`Port`](crate::typed_graph::Port) prevent accidentally wiring, for
//! example, search hits into a context-pack input.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::marker::PhantomData;

use serde_json::{Map, Value, json};

use crate::typed_graph::{Block, GraphValue, NodeConfig, NodeInputs, NodeOutputs, Port};

macro_rules! graph_value {
    ($name:ident, $schema:literal) => {
        #[derive(Clone, Copy, Debug, Eq, PartialEq)]
        pub struct $name;

        impl GraphValue for $name {
            const SCHEMA: &'static str = $schema;
        }
    };
}

macro_rules! port_value {
    ($name:ident) => {
        #[derive(Clone, Copy, Debug, Eq, PartialEq)]
        pub struct $name;
    };
}

graph_value!(SearchRequestValue, "graphblocks.ai/SearchRequest@1");
graph_value!(FederatedSourcesValue, "graphblocks.ai/FederatedSources@1");
graph_value!(ContextPackValue, "graphblocks.ai/ContextPack@1");
graph_value!(AnswerValue, "graphblocks.ai/Answer@1");
graph_value!(
    GroundingValidationValue,
    "graphblocks.ai/GroundingValidation@1"
);
port_value!(RetrievalSourcesValue);
port_value!(RetrievalResultValue);
port_value!(SearchHitsValue);

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StdlibBlockConfigError {
    Empty {
        field: &'static str,
    },
    Zero {
        field: &'static str,
    },
    InvalidRange {
        message: &'static str,
    },
    InvalidType {
        field: &'static str,
        expected: &'static str,
    },
}

impl fmt::Display for StdlibBlockConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Empty { field } => write!(formatter, "{field} must not be empty"),
            Self::Zero { field } => write!(formatter, "{field} must be positive"),
            Self::InvalidRange { message } => formatter.write_str(message),
            Self::InvalidType { field, expected } => {
                write!(formatter, "{field} must be {expected}")
            }
        }
    }
}

impl Error for StdlibBlockConfigError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RetrievalFusionAlgorithm {
    Concatenate,
    ReciprocalRankFusion,
    WeightedRank,
    NormalizedScore,
    Interleave,
}

impl RetrievalFusionAlgorithm {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Concatenate => "concatenate",
            Self::ReciprocalRankFusion => "reciprocal_rank_fusion",
            Self::WeightedRank => "weighted_rank",
            Self::NormalizedScore => "normalized_score",
            Self::Interleave => "interleave",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GroundingFailurePolicy {
    Warn,
    Fail,
    Abstain,
    Repair,
    RemoveInvalid,
}

impl GroundingFailurePolicy {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Warn => "warn",
            Self::Fail => "fail",
            Self::Abstain => "abstain",
            Self::Repair => "repair",
            Self::RemoveInvalid => "remove_invalid",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RetrieveExecutePlanConfig {
    minimum_successful_sources: u64,
    top_k: u64,
}

impl RetrieveExecutePlanConfig {
    pub fn new(
        minimum_successful_sources: u64,
        top_k: u64,
    ) -> Result<Self, StdlibBlockConfigError> {
        require_positive("minimum_successful_sources", minimum_successful_sources)?;
        require_positive("top_k", top_k)?;
        Ok(Self {
            minimum_successful_sources,
            top_k,
        })
    }
}

impl NodeConfig for RetrieveExecutePlanConfig {
    fn to_config_object(&self) -> Map<String, Value> {
        Map::from_iter([
            (
                "minimumSuccessfulSources".to_owned(),
                json!(self.minimum_successful_sources),
            ),
            ("topK".to_owned(), json!(self.top_k)),
        ])
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RetrieveExecutePlan {
    config: RetrieveExecutePlanConfig,
}

impl RetrieveExecutePlan {
    pub fn new(config: RetrieveExecutePlanConfig) -> Self {
        Self { config }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RetrieveExecutePlanInputs {
    pub query: Port<SearchRequestValue>,
    pub sources: Port<FederatedSourcesValue>,
}

impl NodeInputs for RetrieveExecutePlanInputs {
    fn into_node_inputs(self) -> BTreeMap<String, crate::typed_graph::NodeInputReference> {
        BTreeMap::from([
            ("query".to_owned(), self.query.into_input_reference()),
            ("sources".to_owned(), self.sources.into_input_reference()),
        ])
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RetrieveExecutePlanOutputs {
    pub result: Port<RetrievalResultValue>,
    pub sources: Port<RetrievalSourcesValue>,
}

impl NodeOutputs for RetrieveExecutePlanOutputs {
    fn for_node(builder_id: u64, node_id: &str) -> Self {
        Self {
            result: Port::node_output(builder_id, node_id, "result"),
            sources: Port::node_output(builder_id, node_id, "sources"),
        }
    }
}

impl Block for RetrieveExecutePlan {
    const ID: &'static str = "retrieve.execute_plan@1";
    type Config = RetrieveExecutePlanConfig;
    type Inputs = RetrieveExecutePlanInputs;
    type Outputs = RetrieveExecutePlanOutputs;

    fn config(&self) -> &Self::Config {
        &self.config
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RetrieveFuseConfig {
    algorithm: RetrievalFusionAlgorithm,
    k: u64,
}

impl RetrieveFuseConfig {
    pub fn new(
        algorithm: RetrievalFusionAlgorithm,
        k: u64,
    ) -> Result<Self, StdlibBlockConfigError> {
        require_positive("k", k)?;
        Ok(Self { algorithm, k })
    }
}

impl NodeConfig for RetrieveFuseConfig {
    fn to_config_object(&self) -> Map<String, Value> {
        Map::from_iter([
            ("algorithm".to_owned(), json!(self.algorithm.as_str())),
            ("k".to_owned(), json!(self.k)),
        ])
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RetrieveFuse {
    config: RetrieveFuseConfig,
}

impl RetrieveFuse {
    pub fn new(config: RetrieveFuseConfig) -> Self {
        Self { config }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RetrieveFuseInputs {
    pub sources: Port<RetrievalSourcesValue>,
}

impl NodeInputs for RetrieveFuseInputs {
    fn into_node_inputs(self) -> BTreeMap<String, crate::typed_graph::NodeInputReference> {
        BTreeMap::from([("sources".to_owned(), self.sources.into_input_reference())])
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RetrieveFuseOutputs {
    pub hits: Port<SearchHitsValue>,
}

impl NodeOutputs for RetrieveFuseOutputs {
    fn for_node(builder_id: u64, node_id: &str) -> Self {
        Self {
            hits: Port::node_output(builder_id, node_id, "hits"),
        }
    }
}

impl Block for RetrieveFuse {
    const ID: &'static str = "retrieve.fuse@1";
    type Config = RetrieveFuseConfig;
    type Inputs = RetrieveFuseInputs;
    type Outputs = RetrieveFuseOutputs;

    fn config(&self) -> &Self::Config {
        &self.config
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RankDocumentsConfig {
    reranker_id: String,
}

impl RankDocumentsConfig {
    pub fn new(reranker_id: impl Into<String>) -> Result<Self, StdlibBlockConfigError> {
        let reranker_id = reranker_id.into();
        require_nonempty("reranker_id", &reranker_id)?;
        Ok(Self { reranker_id })
    }
}

impl NodeConfig for RankDocumentsConfig {
    fn to_config_object(&self) -> Map<String, Value> {
        Map::from_iter([("rerankerId".to_owned(), json!(self.reranker_id))])
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RankDocuments {
    config: RankDocumentsConfig,
}

impl RankDocuments {
    pub fn new(config: RankDocumentsConfig) -> Self {
        Self { config }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RankDocumentsInputs {
    pub query: Port<SearchRequestValue>,
    pub hits: Port<SearchHitsValue>,
}

impl NodeInputs for RankDocumentsInputs {
    fn into_node_inputs(self) -> BTreeMap<String, crate::typed_graph::NodeInputReference> {
        BTreeMap::from([
            ("query".to_owned(), self.query.into_input_reference()),
            ("hits".to_owned(), self.hits.into_input_reference()),
        ])
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RankDocumentsOutputs {
    pub hits: Port<SearchHitsValue>,
}

impl NodeOutputs for RankDocumentsOutputs {
    fn for_node(builder_id: u64, node_id: &str) -> Self {
        Self {
            hits: Port::node_output(builder_id, node_id, "hits"),
        }
    }
}

impl Block for RankDocuments {
    const ID: &'static str = "rank.documents@1";
    type Config = RankDocumentsConfig;
    type Inputs = RankDocumentsInputs;
    type Outputs = RankDocumentsOutputs;

    fn config(&self) -> &Self::Config {
        &self.config
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ContextBuildConfig {
    context_id: String,
    max_tokens: u64,
    reserve_output_tokens: u64,
}

impl ContextBuildConfig {
    pub fn new(
        context_id: impl Into<String>,
        max_tokens: u64,
        reserve_output_tokens: u64,
    ) -> Result<Self, StdlibBlockConfigError> {
        let context_id = context_id.into();
        require_nonempty("context_id", &context_id)?;
        require_positive("max_tokens", max_tokens)?;
        if reserve_output_tokens > max_tokens {
            return Err(StdlibBlockConfigError::InvalidRange {
                message: "reserve_output_tokens must not exceed max_tokens",
            });
        }
        Ok(Self {
            context_id,
            max_tokens,
            reserve_output_tokens,
        })
    }
}

impl NodeConfig for ContextBuildConfig {
    fn to_config_object(&self) -> Map<String, Value> {
        Map::from_iter([
            ("contextId".to_owned(), json!(self.context_id)),
            ("maxTokens".to_owned(), json!(self.max_tokens)),
            (
                "reserveOutputTokens".to_owned(),
                json!(self.reserve_output_tokens),
            ),
        ])
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ContextBuild {
    config: ContextBuildConfig,
}

impl ContextBuild {
    pub fn new(config: ContextBuildConfig) -> Self {
        Self { config }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ContextBuildInputs {
    pub evidence: Port<SearchHitsValue>,
}

impl NodeInputs for ContextBuildInputs {
    fn into_node_inputs(self) -> BTreeMap<String, crate::typed_graph::NodeInputReference> {
        BTreeMap::from([("evidence".to_owned(), self.evidence.into_input_reference())])
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ContextBuildOutputs {
    pub pack: Port<ContextPackValue>,
}

impl NodeOutputs for ContextBuildOutputs {
    fn for_node(builder_id: u64, node_id: &str) -> Self {
        Self {
            pack: Port::node_output(builder_id, node_id, "pack"),
        }
    }
}

impl Block for ContextBuild {
    const ID: &'static str = "context.build@1";
    type Config = ContextBuildConfig;
    type Inputs = ContextBuildInputs;
    type Outputs = ContextBuildOutputs;

    fn config(&self) -> &Self::Config {
        &self.config
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StructuredGenerateConfig<T: GraphValue> {
    response: Value,
    marker: PhantomData<fn(T) -> T>,
}

impl<T: GraphValue> StructuredGenerateConfig<T> {
    pub fn new(response: Value) -> Result<Self, StdlibBlockConfigError> {
        if !response.is_object() && !response.is_array() {
            return Err(StdlibBlockConfigError::InvalidType {
                field: "response",
                expected: "an object or array",
            });
        }
        Ok(Self {
            response,
            marker: PhantomData,
        })
    }
}

impl<T: GraphValue> NodeConfig for StructuredGenerateConfig<T> {
    fn to_config_object(&self) -> Map<String, Value> {
        Map::from_iter([
            ("outputSchema".to_owned(), json!(T::SCHEMA)),
            ("response".to_owned(), self.response.clone()),
        ])
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StructuredGenerate<T: GraphValue> {
    config: StructuredGenerateConfig<T>,
}

impl<T: GraphValue> StructuredGenerate<T> {
    pub fn new(config: StructuredGenerateConfig<T>) -> Self {
        Self { config }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StructuredGenerateInputs {
    pub context: Port<ContextPackValue>,
}

impl NodeInputs for StructuredGenerateInputs {
    fn into_node_inputs(self) -> BTreeMap<String, crate::typed_graph::NodeInputReference> {
        BTreeMap::from([("context".to_owned(), self.context.into_input_reference())])
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StructuredGenerateOutputs<T: GraphValue> {
    pub response: Port<T>,
}

impl<T: GraphValue> NodeOutputs for StructuredGenerateOutputs<T> {
    fn for_node(builder_id: u64, node_id: &str) -> Self {
        Self {
            response: Port::node_output(builder_id, node_id, "response"),
        }
    }
}

impl<T: GraphValue> Block for StructuredGenerate<T> {
    const ID: &'static str = "model.structured_generate@1";
    type Config = StructuredGenerateConfig<T>;
    type Inputs = StructuredGenerateInputs;
    type Outputs = StructuredGenerateOutputs<T>;

    fn config(&self) -> &Self::Config {
        &self.config
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ValidateGroundingConfig {
    require_citation: bool,
    on_insufficient_evidence: GroundingFailurePolicy,
}

impl ValidateGroundingConfig {
    pub fn new(require_citation: bool, on_insufficient_evidence: GroundingFailurePolicy) -> Self {
        Self {
            require_citation,
            on_insufficient_evidence,
        }
    }
}

impl NodeConfig for ValidateGroundingConfig {
    fn to_config_object(&self) -> Map<String, Value> {
        Map::from_iter([
            ("requireCitation".to_owned(), json!(self.require_citation)),
            (
                "onInsufficientEvidence".to_owned(),
                json!(self.on_insufficient_evidence.as_str()),
            ),
        ])
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ValidateGrounding {
    config: ValidateGroundingConfig,
}

impl ValidateGrounding {
    pub fn new(config: ValidateGroundingConfig) -> Self {
        Self { config }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ValidateGroundingInputs {
    pub response: Port<AnswerValue>,
    pub context: Port<ContextPackValue>,
}

impl NodeInputs for ValidateGroundingInputs {
    fn into_node_inputs(self) -> BTreeMap<String, crate::typed_graph::NodeInputReference> {
        BTreeMap::from([
            ("response".to_owned(), self.response.into_input_reference()),
            ("context".to_owned(), self.context.into_input_reference()),
        ])
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ValidateGroundingOutputs {
    pub candidate: Port<AnswerValue>,
    pub validation: Port<GroundingValidationValue>,
}

impl NodeOutputs for ValidateGroundingOutputs {
    fn for_node(builder_id: u64, node_id: &str) -> Self {
        Self {
            candidate: Port::node_output(builder_id, node_id, "candidate"),
            validation: Port::node_output(builder_id, node_id, "validation"),
        }
    }
}

impl Block for ValidateGrounding {
    const ID: &'static str = "answer.validate_grounding@1";
    type Config = ValidateGroundingConfig;
    type Inputs = ValidateGroundingInputs;
    type Outputs = ValidateGroundingOutputs;

    fn config(&self) -> &Self::Config {
        &self.config
    }
}

fn require_positive(field: &'static str, value: u64) -> Result<(), StdlibBlockConfigError> {
    if value == 0 {
        return Err(StdlibBlockConfigError::Zero { field });
    }
    Ok(())
}

fn require_nonempty(field: &'static str, value: &str) -> Result<(), StdlibBlockConfigError> {
    if value.trim().is_empty() {
        return Err(StdlibBlockConfigError::Empty { field });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use crate::typed_graph::GraphBuilder;

    use super::{
        AnswerValue, ContextBuild, ContextBuildConfig, ContextBuildInputs, FederatedSourcesValue,
        RankDocuments, RankDocumentsConfig, RankDocumentsInputs, RetrievalFusionAlgorithm,
        RetrieveExecutePlan, RetrieveExecutePlanConfig, RetrieveExecutePlanInputs, RetrieveFuse,
        RetrieveFuseConfig, RetrieveFuseInputs, SearchRequestValue, StdlibBlockConfigError,
        StructuredGenerateConfig,
    };

    #[test]
    fn rag_definitions_emit_typed_block_nodes() {
        let mut graph = GraphBuilder::new("typed-rag").expect("name is valid");
        let query = graph
            .input::<SearchRequestValue>("query")
            .expect("input is unique");
        let sources = graph
            .input::<FederatedSourcesValue>("sources")
            .expect("input is unique");
        let retrieve = graph
            .add(
                "retrieve",
                RetrieveExecutePlan::new(
                    RetrieveExecutePlanConfig::new(2, 5).expect("positive limits are valid"),
                ),
                RetrieveExecutePlanInputs {
                    query: query.clone(),
                    sources,
                },
            )
            .expect("node is unique");
        let fuse = graph
            .add(
                "fuse",
                RetrieveFuse::new(
                    RetrieveFuseConfig::new(RetrievalFusionAlgorithm::ReciprocalRankFusion, 60)
                        .expect("positive k is valid"),
                ),
                RetrieveFuseInputs {
                    sources: retrieve.sources,
                },
            )
            .expect("node is unique");
        let rank = graph
            .add(
                "rank",
                RankDocuments::new(
                    RankDocumentsConfig::new("lexical").expect("reranker id is valid"),
                ),
                RankDocumentsInputs {
                    query,
                    hits: fuse.hits,
                },
            )
            .expect("node is unique");
        graph
            .add(
                "context",
                ContextBuild::new(
                    ContextBuildConfig::new("context-1", 1_000, 100)
                        .expect("token budget is valid"),
                ),
                ContextBuildInputs {
                    evidence: rank.hits,
                },
            )
            .expect("node is unique");

        let document = graph.build().into_value();
        assert_eq!(
            document.pointer("/spec/nodes/retrieve/block"),
            Some(&json!("retrieve.execute_plan@1"))
        );
        assert_eq!(
            document.pointer("/spec/nodes/fuse/inputs/sources"),
            Some(&json!("retrieve.sources"))
        );
        assert_eq!(
            document.pointer("/spec/nodes/context/inputs/evidence"),
            Some(&json!("rank.hits"))
        );
    }

    #[test]
    fn typed_configs_reject_invalid_limits() {
        assert_eq!(
            RetrieveExecutePlanConfig::new(0, 5),
            Err(StdlibBlockConfigError::Zero {
                field: "minimum_successful_sources",
            })
        );
        assert_eq!(
            RetrieveFuseConfig::new(RetrievalFusionAlgorithm::ReciprocalRankFusion, 0),
            Err(StdlibBlockConfigError::Zero { field: "k" })
        );
        assert_eq!(
            ContextBuildConfig::new("context-1", 100, 101),
            Err(StdlibBlockConfigError::InvalidRange {
                message: "reserve_output_tokens must not exceed max_tokens",
            })
        );
        assert_eq!(
            StructuredGenerateConfig::<AnswerValue>::new(json!("not structured")),
            Err(StdlibBlockConfigError::InvalidType {
                field: "response",
                expected: "an object or array",
            })
        );
        assert!(StructuredGenerateConfig::<AnswerValue>::new(json!([])).is_ok());
    }
}
