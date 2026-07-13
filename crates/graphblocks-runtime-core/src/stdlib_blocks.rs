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

use graphblocks_compiler::compiler::BlockCatalog;

use crate::typed_graph::{
    Block, BlockRegistration, GraphValue, NodeConfig, NodeInputs, NodeOutputFactory, NodeOutputs,
    Port, PortType, TypedBlockCatalog, TypedBlockDescriptor, TypedGraphError, TypedPortDescriptor,
    TypedResourceSlotDescriptor,
};

pub const STDLIB_RUNTIME_BLOCK_IDS: [&str; 25] = [
    "prompt.render@1",
    "model.generate@1",
    "model.structured_generate@1",
    "tools.resolve@1",
    "agent.run@1",
    "conversation.begin_turn@1",
    "conversation.commit_turn@1",
    "control.map@2",
    "control.select@1",
    "retrieve.fuse@1",
    "retrieve.execute_plan@1",
    "rank.documents@1",
    "context.build@1",
    "answer.validate_grounding@1",
    "check.run_suite@1",
    "gate.evaluate@1",
    "review.request@1",
    "result.bundle@1",
    "async.start_operation@1",
    "async.await_callback@1",
    "async.poll_operation@1",
    "async.complete_operation@1",
    "async.cancel_operation@1",
    "async.expire_operation@1",
    "conversation.policy_stop_turn@1",
];

macro_rules! graph_value {
    ($name:ident, $schema:literal) => {
        #[derive(Clone, Copy, Debug, Eq, PartialEq)]
        pub struct $name;

        impl PortType for $name {
            const TYPE_REF: &'static str = $schema;
        }

        impl GraphValue for $name {}
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
graph_value!(PromptValue, "graphblocks.ai/Prompt@1");
graph_value!(ModelResponseValue, "graphblocks.ai/ModelResponse@1");
graph_value!(RetrievalSourcesValue, "graphblocks.ai/RetrievalSources@1");
graph_value!(RetrievalResultValue, "graphblocks.ai/RetrievalResult@1");
graph_value!(SearchHitsValue, "graphblocks.ai/SearchHits@1");
graph_value!(StructuredItemsValue, "graphblocks.ai/StructuredItems@1");
graph_value!(StringValue, "graphblocks.ai/String@1");

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelGenerateConfig {
    response: Value,
}

impl ModelGenerateConfig {
    pub fn new(response: Value) -> Self {
        Self { response }
    }
}

impl NodeConfig for ModelGenerateConfig {
    fn to_config_object(&self) -> Map<String, Value> {
        Map::from_iter([("response".to_owned(), self.response.clone())])
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelGenerate {
    config: ModelGenerateConfig,
}

impl ModelGenerate {
    pub fn new(config: ModelGenerateConfig) -> Self {
        Self { config }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelGenerateInputs {
    pub prompt: Port<PromptValue>,
}

impl NodeInputs for ModelGenerateInputs {
    fn into_node_inputs(self) -> BTreeMap<String, crate::typed_graph::NodeInputReference> {
        BTreeMap::from([("prompt".to_owned(), self.prompt.into_input_reference())])
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ModelGenerateOutputs {
    pub response: Port<ModelResponseValue>,
}

impl NodeOutputs for ModelGenerateOutputs {
    fn port_types() -> BTreeMap<String, &'static str> {
        BTreeMap::from([("response".to_owned(), ModelResponseValue::TYPE_REF)])
    }

    fn from_factory(factory: &NodeOutputFactory<'_>) -> Result<Self, TypedGraphError> {
        Ok(Self {
            response: factory.port("response")?,
        })
    }
}

impl Block for ModelGenerate {
    const ID: &'static str = "model.generate@1";
    type Config = ModelGenerateConfig;
    type Inputs = ModelGenerateInputs;
    type Outputs = ModelGenerateOutputs;

    fn config(&self) -> &Self::Config {
        &self.config
    }

    fn registration(&self) -> BlockRegistration {
        BlockRegistration::stdlib(Self::ID)
    }
}

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
    fn port_types() -> BTreeMap<String, &'static str> {
        BTreeMap::from([
            ("result".to_owned(), RetrievalResultValue::TYPE_REF),
            ("sources".to_owned(), RetrievalSourcesValue::TYPE_REF),
        ])
    }

    fn from_factory(factory: &NodeOutputFactory<'_>) -> Result<Self, TypedGraphError> {
        Ok(Self {
            result: factory.port("result")?,
            sources: factory.port("sources")?,
        })
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

    fn registration(&self) -> BlockRegistration {
        BlockRegistration::stdlib(Self::ID)
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
    fn port_types() -> BTreeMap<String, &'static str> {
        BTreeMap::from([("hits".to_owned(), SearchHitsValue::TYPE_REF)])
    }

    fn from_factory(factory: &NodeOutputFactory<'_>) -> Result<Self, TypedGraphError> {
        Ok(Self {
            hits: factory.port("hits")?,
        })
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

    fn registration(&self) -> BlockRegistration {
        BlockRegistration::stdlib(Self::ID)
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
    fn port_types() -> BTreeMap<String, &'static str> {
        BTreeMap::from([("hits".to_owned(), SearchHitsValue::TYPE_REF)])
    }

    fn from_factory(factory: &NodeOutputFactory<'_>) -> Result<Self, TypedGraphError> {
        Ok(Self {
            hits: factory.port("hits")?,
        })
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

    fn registration(&self) -> BlockRegistration {
        BlockRegistration::stdlib(Self::ID)
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
    fn port_types() -> BTreeMap<String, &'static str> {
        BTreeMap::from([("pack".to_owned(), ContextPackValue::TYPE_REF)])
    }

    fn from_factory(factory: &NodeOutputFactory<'_>) -> Result<Self, TypedGraphError> {
        Ok(Self {
            pack: factory.port("pack")?,
        })
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

    fn registration(&self) -> BlockRegistration {
        BlockRegistration::stdlib(Self::ID)
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
            ("outputSchema".to_owned(), json!(T::TYPE_REF)),
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
    pub value: Port<T>,
    pub response: Port<T>,
    pub items: Port<StructuredItemsValue>,
    pub schema_id: Port<StringValue>,
    pub schema_ref: Port<StringValue>,
    pub content_digest: Port<StringValue>,
}

impl<T: GraphValue> NodeOutputs for StructuredGenerateOutputs<T> {
    fn port_types() -> BTreeMap<String, &'static str> {
        BTreeMap::from([
            ("value".to_owned(), T::TYPE_REF),
            ("response".to_owned(), T::TYPE_REF),
            ("items".to_owned(), StructuredItemsValue::TYPE_REF),
            ("schemaId".to_owned(), StringValue::TYPE_REF),
            ("schemaRef".to_owned(), StringValue::TYPE_REF),
            ("contentDigest".to_owned(), StringValue::TYPE_REF),
        ])
    }

    fn from_factory(factory: &NodeOutputFactory<'_>) -> Result<Self, TypedGraphError> {
        Ok(Self {
            value: factory.port("value")?,
            response: factory.port("response")?,
            items: factory.port("items")?,
            schema_id: factory.port("schemaId")?,
            schema_ref: factory.port("schemaRef")?,
            content_digest: factory.port("contentDigest")?,
        })
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

    fn registration(&self) -> BlockRegistration {
        BlockRegistration::stdlib(Self::ID)
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
    pub response: Port<AnswerValue>,
    pub result: Port<GroundingValidationValue>,
    pub validation: Port<GroundingValidationValue>,
}

impl NodeOutputs for ValidateGroundingOutputs {
    fn port_types() -> BTreeMap<String, &'static str> {
        BTreeMap::from([
            ("candidate".to_owned(), AnswerValue::TYPE_REF),
            ("response".to_owned(), AnswerValue::TYPE_REF),
            ("result".to_owned(), GroundingValidationValue::TYPE_REF),
            ("validation".to_owned(), GroundingValidationValue::TYPE_REF),
        ])
    }

    fn from_factory(factory: &NodeOutputFactory<'_>) -> Result<Self, TypedGraphError> {
        Ok(Self {
            candidate: factory.port("candidate")?,
            response: factory.port("response")?,
            result: factory.port("result")?,
            validation: factory.port("validation")?,
        })
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

    fn registration(&self) -> BlockRegistration {
        BlockRegistration::stdlib(Self::ID)
    }
}

/// Returns the port contracts used by both typed graph construction and JSON compilation.
pub fn stdlib_block_catalog() -> Result<BlockCatalog, TypedGraphError> {
    stdlib_typed_block_catalog()?.compiler_catalog()
}

/// Returns the typed form of the official stdlib block catalog.
pub fn stdlib_typed_block_catalog() -> Result<TypedBlockCatalog, TypedGraphError> {
    let required = |name, type_ref| TypedPortDescriptor::required_type(name, type_ref);
    let optional = |name, type_ref| TypedPortDescriptor::optional_type(name, type_ref);
    TypedBlockCatalog::from_descriptors([
        TypedBlockDescriptor::stdlib(
            "prompt.render@1",
            [required("message", "graphblocks.ai/Message@1")],
            [required("prompt", PromptValue::TYPE_REF)],
        )?,
        TypedBlockDescriptor::stdlib_with_resource_slots(
            "model.generate@1",
            [
                optional("prompt", PromptValue::TYPE_REF),
                optional("context", ContextPackValue::TYPE_REF),
            ],
            [required("response", ModelResponseValue::TYPE_REF)],
            [TypedResourceSlotDescriptor::optional(
                "model",
                "resources/Model@1",
            )],
        )?,
        TypedBlockDescriptor::stdlib_with_resource_slots(
            "model.structured_generate@1",
            [
                optional("response", "Any"),
                optional("diagnosis", "Any"),
                optional("prompt", "Any"),
                optional("context", ContextPackValue::TYPE_REF),
                optional("candidates", "Any"),
                optional("questions", "Any"),
                optional("reference", "Any"),
            ],
            [
                required("value", "Any"),
                required("response", "Any"),
                required("items", StructuredItemsValue::TYPE_REF),
                required("schemaId", StringValue::TYPE_REF),
                required("schemaRef", StringValue::TYPE_REF),
                required("contentDigest", StringValue::TYPE_REF),
                optional("questions", "Any"),
                optional("scores", "Any"),
            ],
            [TypedResourceSlotDescriptor::optional(
                "model",
                "resources/Model@1",
            )],
        )?,
        TypedBlockDescriptor::stdlib(
            "tools.resolve@1",
            [
                optional("principal", "graphblocks.ai/Principal@1"),
                optional("conversation", "graphblocks.ai/ConversationSnapshot@1"),
                optional("policySnapshot", "graphblocks.ai/PolicySnapshot@1"),
            ],
            [required("tools", "graphblocks.ai/ResolvedTools@1")],
        )?,
        TypedBlockDescriptor::stdlib_with_resource_slots(
            "agent.run@1",
            [
                optional("messages", "graphblocks.ai/Messages@1"),
                optional("tools", "graphblocks.ai/ResolvedTools@1"),
                optional("context", "Any"),
                optional("objective", "Any"),
                optional("diagnostics", "Any"),
                optional("conversation", "graphblocks.ai/ConversationSnapshot@1"),
            ],
            [
                required("candidate", "graphblocks.ai/TurnCandidate@1"),
                optional("result", "Any"),
                optional("message", "graphblocks.ai/TurnCandidate@1"),
            ],
            [TypedResourceSlotDescriptor::optional(
                "model",
                "resources/Model@1",
            )],
        )?,
        TypedBlockDescriptor::stdlib(
            "conversation.begin_turn@1",
            [
                optional("conversationId", "graphblocks.ai/ConversationId@1"),
                optional("conversation", "graphblocks.conversation/ConversationRef@1"),
                optional("message", "graphblocks.conversation/Message@1"),
            ],
            [
                required("transaction", "graphblocks.ai/ConversationTransaction@1"),
                optional("snapshot", "graphblocks.ai/ConversationSnapshot@1"),
                optional("conversation", "graphblocks.ai/ConversationSnapshot@1"),
                optional("turn", "graphblocks.ai/ConversationTransaction@1"),
            ],
        )?,
        TypedBlockDescriptor::stdlib(
            "conversation.commit_turn@1",
            [
                optional("transaction", "graphblocks.ai/ConversationTransaction@1"),
                optional("candidate", "Any"),
                optional("turn", "graphblocks.ai/ConversationTransaction@1"),
                optional("response", "graphblocks.ai/TurnCandidate@1"),
            ],
            [
                required("answer", AnswerValue::TYPE_REF),
                required("result", "graphblocks.ai/TurnCandidate@1"),
            ],
        )?,
        TypedBlockDescriptor::stdlib(
            "control.map@2",
            [required("items", "graphblocks.ai/Items@1")],
            [
                required("values", "graphblocks.ai/Values@1"),
                optional("outcomes", "graphblocks.ai/Outcomes@1"),
            ],
        )?,
        TypedBlockDescriptor::stdlib(
            "control.select@1",
            [required("cases", "graphblocks.ai/Cases@1")],
            [
                required("value", "graphblocks.ai/SelectedValue@1"),
                required("selected", "graphblocks.ai/SelectedKey@1"),
            ],
        )?,
        TypedBlockDescriptor::stdlib(
            "retrieve.fuse@1",
            [required("sources", RetrievalSourcesValue::TYPE_REF)],
            [
                required("hits", SearchHitsValue::TYPE_REF),
                optional("metadata", "Any"),
            ],
        )?,
        TypedBlockDescriptor::stdlib_with_resource_slots(
            "retrieve.execute_plan@1",
            [
                optional("query", SearchRequestValue::TYPE_REF),
                optional("request", SearchRequestValue::TYPE_REF),
                optional("auth", "Any"),
                optional("sources", FederatedSourcesValue::TYPE_REF),
            ],
            [
                required("result", RetrievalResultValue::TYPE_REF),
                required("sources", RetrievalSourcesValue::TYPE_REF),
            ],
            [
                TypedResourceSlotDescriptor::optional("retrievers", "resources/RetrieverSet@1"),
                TypedResourceSlotDescriptor::optional("embedding", "resources/EmbeddingModel@1"),
            ],
        )?,
        TypedBlockDescriptor::stdlib_with_resource_slots(
            "rank.documents@1",
            [
                required("query", SearchRequestValue::TYPE_REF),
                required("hits", SearchHitsValue::TYPE_REF),
            ],
            [
                required("hits", SearchHitsValue::TYPE_REF),
                optional("result", "Any"),
            ],
            [TypedResourceSlotDescriptor::optional(
                "reranker",
                "resources/Reranker@1",
            )],
        )?,
        TypedBlockDescriptor::stdlib(
            "context.build@1",
            [
                optional("history", "graphblocks.ai/Messages@1"),
                optional("evidence", SearchHitsValue::TYPE_REF),
                optional("hits", SearchHitsValue::TYPE_REF),
                optional("currentMessage", "graphblocks.ai/Message@1"),
            ],
            [required("pack", ContextPackValue::TYPE_REF)],
        )?,
        TypedBlockDescriptor::stdlib(
            "answer.validate_grounding@1",
            [
                optional("response", "Any"),
                optional("answer", AnswerValue::TYPE_REF),
                required("context", ContextPackValue::TYPE_REF),
            ],
            [
                required("candidate", AnswerValue::TYPE_REF),
                required("response", AnswerValue::TYPE_REF),
                required("result", GroundingValidationValue::TYPE_REF),
                required("validation", GroundingValidationValue::TYPE_REF),
            ],
        )?,
        TypedBlockDescriptor::stdlib_with_resource_slots(
            "check.run_suite@1",
            [
                optional("subject", "Any"),
                optional("evidence", "Any"),
                optional("results", "Any"),
                optional("lease", "Any"),
            ],
            [
                required("results", "Any"),
                required("checks", "Any"),
                required("diagnostics", "Any"),
                required("passed", "graphblocks.ai/Boolean@1"),
                required("hardGatePassed", "graphblocks.ai/Boolean@1"),
            ],
            [TypedResourceSlotDescriptor::optional(
                "checks",
                "resources/CheckProviderSet@1",
            )],
        )?,
        TypedBlockDescriptor::stdlib(
            "gate.evaluate@1",
            [
                required("checks", "Any"),
                optional("metrics", "Any"),
                optional("subject", "Any"),
            ],
            [
                required("result", "Any"),
                required("decision", StringValue::TYPE_REF),
                required("passed", "graphblocks.ai/Boolean@1"),
                required("violations", "Any"),
            ],
        )?,
        TypedBlockDescriptor::stdlib_with_resource_slots(
            "review.request@1",
            [
                required("subject", "Any"),
                optional("gate", "Any"),
                optional("review", "Any"),
                optional("requestedBy", "graphblocks.ai/Principal@1"),
                optional("requested_by", "graphblocks.ai/Principal@1"),
            ],
            [
                required("request", "Any"),
                optional("requestDigest", StringValue::TYPE_REF),
                optional("record", "Any"),
                required("accepted", "graphblocks.ai/Boolean@1"),
                required("approved", "graphblocks.ai/Boolean@1"),
                required("status", StringValue::TYPE_REF),
                optional("waitMode", StringValue::TYPE_REF),
            ],
            [TypedResourceSlotDescriptor::optional(
                "reviewer",
                "resources/Reviewer@1",
            )],
        )?,
        TypedBlockDescriptor::stdlib(
            "result.bundle@1",
            [
                optional("inputs", "Any"),
                required("outputs", "Any"),
                optional("evidence", "Any"),
                optional("checks", "Any"),
                optional("metrics", "Any"),
                optional("diagnostics", "Any"),
                optional("reviews", "Any"),
                optional("gate", "Any"),
                optional("artifacts", "graphblocks.core/ArtifactRefList@1"),
                optional("usage", "Any"),
                optional("usageRecords", "Any"),
                optional("policyDecisionRefs", "Any"),
            ],
            [
                required("result", "Any"),
                required("bundle", "Any"),
                required("contentDigest", StringValue::TYPE_REF),
            ],
        )?,
        TypedBlockDescriptor::stdlib(
            "async.start_operation@1",
            [optional("subject", "Any"), optional("changeset", "Any")],
            [required("operation", "graphblocks.ai/AsyncOperation@1")],
        )?,
        TypedBlockDescriptor::stdlib(
            "async.await_callback@1",
            [required("operation", "graphblocks.ai/AsyncOperation@1")],
            [
                required("wait", "graphblocks.ai/AsyncWait@1"),
                optional("callback", "Any"),
                optional("operation", "graphblocks.ai/AsyncOperation@1"),
            ],
        )?,
        TypedBlockDescriptor::stdlib(
            "async.poll_operation@1",
            [required("operation", "graphblocks.ai/AsyncOperation@1")],
            [required("poll", "graphblocks.ai/AsyncPoll@1")],
        )?,
        TypedBlockDescriptor::stdlib(
            "async.complete_operation@1",
            [
                required("operation", "graphblocks.ai/AsyncOperation@1"),
                optional("output", "Any"),
            ],
            [required("result", "graphblocks.ai/AsyncOperationResult@1")],
        )?,
        TypedBlockDescriptor::stdlib(
            "async.cancel_operation@1",
            [required("operation", "graphblocks.ai/AsyncOperation@1")],
            [required("result", "graphblocks.ai/AsyncOperationResult@1")],
        )?,
        TypedBlockDescriptor::stdlib(
            "async.expire_operation@1",
            [required("operation", "graphblocks.ai/AsyncOperation@1")],
            [required("result", "graphblocks.ai/AsyncOperationResult@1")],
        )?,
        TypedBlockDescriptor::stdlib(
            "conversation.policy_stop_turn@1",
            [required(
                "transaction",
                "graphblocks.ai/ConversationTransaction@1",
            )],
            [
                required("transaction", "graphblocks.ai/ConversationTransaction@1"),
                required("turn", "graphblocks.ai/ConversationTransaction@1"),
            ],
        )?,
    ])
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
    use std::collections::BTreeSet;
    use std::fs;
    use std::path::Path;

    use graphblocks_compiler::compiler::{PortDescriptor, ResourceSlotDescriptor};
    use serde_json::{Value, json};

    use crate::typed_graph::GraphBuilder;

    use super::{
        AnswerValue, ContextBuild, ContextBuildConfig, ContextBuildInputs, FederatedSourcesValue,
        RankDocuments, RankDocumentsConfig, RankDocumentsInputs, RetrievalFusionAlgorithm,
        RetrieveExecutePlan, RetrieveExecutePlanConfig, RetrieveExecutePlanInputs, RetrieveFuse,
        RetrieveFuseConfig, RetrieveFuseInputs, STDLIB_RUNTIME_BLOCK_IDS, SearchRequestValue,
        StdlibBlockConfigError, StructuredGenerateConfig, stdlib_block_catalog,
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

    #[test]
    fn stdlib_catalog_covers_every_in_process_runtime_block() {
        let catalog = stdlib_block_catalog().expect("stdlib catalog is valid");

        for block_id in STDLIB_RUNTIME_BLOCK_IDS {
            assert!(
                catalog.get(block_id).is_some(),
                "missing runtime block descriptor for {block_id}"
            );
        }
    }

    #[test]
    fn rust_stdlib_catalog_matches_builtin_plugin_manifest() {
        let manifest_path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../src/graphblocks/data/builtin-plugin.yaml");
        let manifest_source = fs::read_to_string(&manifest_path);
        assert!(
            manifest_source.is_ok(),
            "failed to read {}: {:?}",
            manifest_path.display(),
            manifest_source.as_ref().err()
        );
        let manifest: Value = serde_yaml::from_str(
            &manifest_source.expect("builtin plugin manifest source is readable"),
        )
        .expect("builtin plugin manifest is valid YAML");
        let manifest_blocks = manifest
            .pointer("/spec/blocks")
            .and_then(Value::as_array)
            .expect("builtin plugin manifest has spec.blocks");
        let manifest_catalog = graphblocks_compiler::compiler::BlockCatalog::from_blocks(
            &Value::Array(manifest_blocks.clone()),
        )
        .expect("builtin plugin block catalog is valid");
        let rust_catalog = stdlib_block_catalog().expect("Rust stdlib catalog is valid");

        assert_eq!(manifest_blocks.len(), STDLIB_RUNTIME_BLOCK_IDS.len());
        for manifest_block in manifest_blocks {
            let type_id = manifest_block
                .get("typeId")
                .and_then(Value::as_str)
                .expect("manifest block has typeId");
            let version = manifest_block
                .get("version")
                .and_then(Value::as_u64)
                .expect("manifest block has version");
            let block_id = format!("{type_id}@{version}");
            assert!(
                STDLIB_RUNTIME_BLOCK_IDS.contains(&block_id.as_str()),
                "manifest block {block_id} is not dispatched by the Rust runtime"
            );

            let expected = manifest_catalog
                .get(&block_id)
                .expect("manifest catalog contains its block");
            let actual = rust_catalog
                .get(&block_id)
                .expect("Rust catalog contains every manifest block");
            assert_eq!(actual.type_id, expected.type_id, "{block_id} typeId");
            assert_eq!(actual.version, expected.version, "{block_id} version");
            assert_eq!(
                port_contracts(&actual.inputs),
                port_contracts(&expected.inputs),
                "{block_id} inputs"
            );
            assert_eq!(
                port_contracts(&actual.outputs),
                port_contracts(&expected.outputs),
                "{block_id} outputs"
            );
            assert_eq!(
                resource_slot_contracts(&actual.resource_slots),
                resource_slot_contracts(&expected.resource_slots),
                "{block_id} resource slots"
            );
        }
    }

    fn port_contracts(ports: &[PortDescriptor]) -> BTreeSet<(String, Option<String>, bool)> {
        ports
            .iter()
            .map(|port| (port.name.clone(), port.type_ref.clone(), port.required))
            .collect()
    }

    fn resource_slot_contracts(
        slots: &[ResourceSlotDescriptor],
    ) -> BTreeSet<(String, Option<String>, bool)> {
        slots
            .iter()
            .map(|slot| (slot.name.clone(), slot.type_ref.clone(), slot.optional))
            .collect()
    }
}
