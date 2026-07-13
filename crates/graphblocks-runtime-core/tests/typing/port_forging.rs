use graphblocks_runtime_core::stdlib_blocks::AnswerValue;
use graphblocks_runtime_core::typed_graph::Port;

fn main() {
    let _forged: Port<AnswerValue> = Port::node_output(7, "other-node", "candidate");
}
