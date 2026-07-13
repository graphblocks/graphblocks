use graphblocks_runtime_core::stdlib_blocks::{
    FederatedSourcesValue, RetrievalFusionAlgorithm, RetrieveExecutePlan,
    RetrieveExecutePlanConfig, RetrieveExecutePlanInputs, RetrieveFuse, RetrieveFuseConfig,
    RetrieveFuseInputs, SearchRequestValue,
};
use graphblocks_runtime_core::typed_graph::GraphBuilder;

fn main() {
    let mut graph = GraphBuilder::new("mismatched-ports").unwrap();
    let query = graph.input::<SearchRequestValue>("query").unwrap();
    let sources = graph.input::<FederatedSourcesValue>("sources").unwrap();
    let retrieve = graph
        .add(
            "retrieve",
            RetrieveExecutePlan::new(RetrieveExecutePlanConfig::new(1, 5).unwrap()),
            RetrieveExecutePlanInputs { query, sources },
        )
        .unwrap();
    let fuse = graph
        .add(
            "fuse",
            RetrieveFuse::new(
                RetrieveFuseConfig::new(RetrievalFusionAlgorithm::ReciprocalRankFusion, 60)
                    .unwrap(),
            ),
            RetrieveFuseInputs {
                sources: retrieve.sources,
            },
        )
        .unwrap();

    let _invalid = graph.add(
        "invalid-fuse",
        RetrieveFuse::new(
            RetrieveFuseConfig::new(RetrievalFusionAlgorithm::ReciprocalRankFusion, 60).unwrap(),
        ),
        RetrieveFuseInputs {
            sources: fuse.hits,
        },
    );
}
