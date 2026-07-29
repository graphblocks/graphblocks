use std::collections::BTreeMap;
use std::panic::{AssertUnwindSafe, catch_unwind};

use graphblocks_schema::{
    CanonicalJsonError, CanonicalJsonParseError, MAX_CANONICAL_INTEGER_DIGITS,
    MAX_CANONICAL_JSON_DEPTH, SchemaId, SchemaIdError, canonical_json, parse_canonical_json,
};
use proptest::prelude::*;
use serde_json::{Map, Number, Value};

fn json_value() -> BoxedStrategy<Value> {
    let leaf = prop_oneof![
        Just(Value::Null),
        any::<bool>().prop_map(Value::Bool),
        any::<i64>().prop_map(|value| Value::Number(value.into())),
        any::<f64>()
            .prop_filter("finite JSON number", |value| value.is_finite())
            .prop_map(|value| {
                Value::Number(Number::from_f64(value).expect("finite f64 is a JSON number"))
            }),
        prop::collection::vec(any::<char>(), 0..48)
            .prop_map(|characters| Value::String(characters.into_iter().collect())),
    ];

    leaf.prop_recursive(6, 128, 12, |children| {
        prop_oneof![
            prop::collection::vec(children.clone(), 0..8).prop_map(Value::Array),
            prop::collection::btree_map("[A-Za-z0-9_./-]{0,16}", children, 0..8).prop_map(
                |values: BTreeMap<String, Value>| {
                    Value::Object(values.into_iter().collect::<Map<_, _>>())
                }
            ),
        ]
    })
    .boxed()
}

proptest! {
    #![proptest_config(ProptestConfig {
        cases: 256,
        max_shrink_iters: 4_096,
        ..ProptestConfig::default()
    })]

    #[test]
    fn canonical_json_round_trips_generated_values(value in json_value()) {
        let encoded = canonical_json(&value).expect("bounded generated value should serialize");
        let decoded =
            parse_canonical_json(&encoded).expect("canonical output should parse canonically");

        prop_assert_eq!(
            canonical_json(&decoded).expect("round-tripped value should serialize"),
            encoded
        );
    }

    #[test]
    fn canonical_parser_handles_arbitrary_text_without_panicking(
        text in prop::collection::vec(any::<char>(), 0..512)
            .prop_map(|characters| characters.into_iter().collect::<String>())
    ) {
        let outcome = catch_unwind(AssertUnwindSafe(|| parse_canonical_json(&text)));

        prop_assert!(outcome.is_ok(), "canonical parser panicked for generated text");
    }

    #[test]
    fn canonical_parser_rejects_generated_duplicate_keys(
        key in "[A-Za-z0-9_./-]{0,24}",
        left in any::<i64>(),
        right in any::<i64>(),
    ) {
        let encoded_key = serde_json::to_string(&key).expect("generated key should serialize");
        let payload = format!("{{{encoded_key}:{left},{encoded_key}:{right}}}");

        prop_assert_eq!(
            parse_canonical_json(&payload),
            Err(CanonicalJsonParseError::DuplicateObjectKey { key })
        );
    }

    #[test]
    fn canonical_depth_budget_holds_for_generated_container_shapes(
        depth in (MAX_CANONICAL_JSON_DEPTH - 4)..=(MAX_CANONICAL_JSON_DEPTH + 4),
        use_objects in any::<bool>(),
    ) {
        let mut value = Value::Null;
        for _ in 0..depth {
            value = if use_objects {
                Value::Object(Map::from_iter([("value".to_owned(), value)]))
            } else {
                Value::Array(vec![value])
            };
        }

        let result = canonical_json(&value);
        if depth <= MAX_CANONICAL_JSON_DEPTH {
            prop_assert!(result.is_ok());
        } else {
            prop_assert_eq!(
                result,
                Err(CanonicalJsonError::NestingTooDeep {
                    max_depth: MAX_CANONICAL_JSON_DEPTH,
                })
            );
        }
    }
}

proptest! {
    #![proptest_config(ProptestConfig {
        cases: 24,
        max_shrink_iters: 256,
        ..ProptestConfig::default()
    })]

    #[test]
    fn canonical_integer_budget_holds_near_the_generated_boundary(
        digit_count in
            (MAX_CANONICAL_INTEGER_DIGITS - 4)..=(MAX_CANONICAL_INTEGER_DIGITS + 4),
        first_digit in 1_u8..=9,
        negative in any::<bool>(),
    ) {
        let token = format!(
            "{}{}{}",
            if negative { "-" } else { "" },
            first_digit,
            "7".repeat(digit_count - 1),
        );
        let result = parse_canonical_json(&token);

        if digit_count <= MAX_CANONICAL_INTEGER_DIGITS {
            let value = result.expect("bounded integer should parse");
            prop_assert_eq!(
                canonical_json(&value).expect("bounded integer should serialize"),
                token
            );
        } else {
            prop_assert_eq!(
                result,
                Err(CanonicalJsonParseError::CanonicalJson(
                    CanonicalJsonError::IntegerTooLarge {
                        max_digits: MAX_CANONICAL_INTEGER_DIGITS,
                    }
                ))
            );
        }
    }
}

proptest! {
    #![proptest_config(ProptestConfig {
        cases: 128,
        max_shrink_iters: 2_048,
        ..ProptestConfig::default()
    })]

    #[test]
    fn schema_id_accepts_generated_canonical_identities(
        name in "[A-Za-z][A-Za-z0-9_./-]{0,31}",
        major in 1_u32..=u32::MAX,
    ) {
        let raw = format!("{name}@{major}");
        let schema_id = SchemaId::parse(&raw).expect("generated schema id should be canonical");

        prop_assert_eq!(schema_id.as_str(), raw);
        prop_assert_eq!(schema_id.name(), name);
        prop_assert_eq!(schema_id.major_version(), major);
    }

    #[test]
    fn schema_id_rejects_generated_leading_zero_versions(
        name in "[A-Za-z][A-Za-z0-9_./-]{0,31}",
        major in 1_u32..=999_999,
    ) {
        let raw = format!("{name}@0{major}");

        prop_assert_eq!(
            SchemaId::parse(raw),
            Err(SchemaIdError::NonCanonicalVersion)
        );
    }
}
