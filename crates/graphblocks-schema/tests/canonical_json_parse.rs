use graphblocks_schema::{CanonicalJsonParseError, canonical_json, parse_canonical_json};

#[test]
fn canonical_json_parser_preserves_arbitrary_precision_numbers() {
    let value =
        parse_canonical_json(r#"{"decimal":1.00000000000000001,"integer":18446744073709551616}"#)
            .expect("arbitrary-precision canonical JSON parses");

    assert_eq!(
        canonical_json(&value),
        r#"{"decimal":1.00000000000000001,"integer":18446744073709551616}"#
    );
}

#[test]
fn canonical_json_parser_rejects_duplicate_keys_at_any_depth() {
    assert_eq!(
        parse_canonical_json(r#"{"outer":{"value":1,"value":2}}"#),
        Err(CanonicalJsonParseError::DuplicateObjectKey {
            key: "value".to_owned(),
        })
    );
}
