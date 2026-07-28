use graphblocks_schema::{
    CanonicalJsonError, CanonicalJsonParseError, MAX_CANONICAL_INTEGER_DIGITS, canonical_json,
    parse_canonical_json,
};

#[test]
fn canonical_json_parser_preserves_arbitrary_precision_numbers() {
    let value =
        parse_canonical_json(r#"{"decimal":1.00000000000000001,"integer":18446744073709551616}"#)
            .expect("arbitrary-precision canonical JSON parses");

    assert_eq!(
        canonical_json(&value).expect("bounded value should serialize"),
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

#[test]
fn canonical_json_integer_tokens_obey_the_decimal_digit_limit() {
    let accepted_digits = "9".repeat(MAX_CANONICAL_INTEGER_DIGITS);
    for token in [accepted_digits.clone(), format!("-{accepted_digits}")] {
        let value = parse_canonical_json(&token).expect("boundary integer should parse");
        assert_eq!(
            canonical_json(&value).expect("boundary integer should serialize"),
            token
        );
    }

    let oversized_digits = "9".repeat(MAX_CANONICAL_INTEGER_DIGITS + 1);
    let expected = CanonicalJsonParseError::CanonicalJson(CanonicalJsonError::IntegerTooLarge {
        max_digits: MAX_CANONICAL_INTEGER_DIGITS,
    });
    assert_eq!(
        parse_canonical_json(&oversized_digits),
        Err(expected.clone())
    );
    assert_eq!(
        parse_canonical_json(&format!("-{oversized_digits}")),
        Err(expected)
    );
    assert_eq!(
        parse_canonical_json(&format!("\"{oversized_digits}\""))
            .expect("digit strings are not integer tokens"),
        serde_json::Value::String(oversized_digits)
    );
}

#[test]
fn programmatic_canonical_values_obey_the_integer_digit_limit() {
    let oversized_digits = "9".repeat(MAX_CANONICAL_INTEGER_DIGITS + 1);
    let value = serde_json::from_str(&oversized_digits)
        .expect("serde arbitrary-precision value should parse before canonical admission");

    assert_eq!(
        canonical_json(&value),
        Err(CanonicalJsonError::IntegerTooLarge {
            max_digits: MAX_CANONICAL_INTEGER_DIGITS,
        })
    );
}
