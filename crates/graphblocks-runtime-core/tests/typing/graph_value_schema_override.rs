use graphblocks_runtime_core::typed_graph::{GraphValue, PortType};

struct ForgedAnswer;

impl PortType for ForgedAnswer {
    const TYPE_REF: &'static str = "Any";
}

impl GraphValue for ForgedAnswer {
    const SCHEMA: &'static str = "graphblocks.ai/Answer@1";
}

fn main() {}
