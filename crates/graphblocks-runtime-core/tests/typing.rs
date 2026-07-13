#[test]
fn typed_stdlib_ports_reject_mismatches_and_forging() {
    let cases = trybuild::TestCases::new();
    cases.compile_fail("tests/typing/stdlib_port_mismatch.rs");
    cases.compile_fail("tests/typing/port_forging.rs");
    cases.compile_fail("tests/typing/graph_value_schema_override.rs");
}
