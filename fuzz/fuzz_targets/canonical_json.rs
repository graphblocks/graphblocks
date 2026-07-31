#![no_main]

use graphblocks_schema::{
    CanonicalJsonError, CanonicalJsonParseError, MAX_CANONICAL_INTEGER_DIGITS,
    MAX_CANONICAL_JSON_DEPTH, SchemaId, TypedValue, canonical_json, parse_canonical_json,
};
use libfuzzer_sys::fuzz_target;

fn exercise_json(text: &str) {
    let Ok(value) = parse_canonical_json(text) else {
        return;
    };
    let encoded = canonical_json(&value)
        .unwrap_or_else(|error| panic!("parsed canonical value did not encode: {error}"));
    let reparsed = parse_canonical_json(&encoded)
        .unwrap_or_else(|error| panic!("canonical output did not parse: {error}"));
    let reencoded = canonical_json(&reparsed)
        .unwrap_or_else(|error| panic!("reparsed canonical value did not encode: {error}"));
    assert_eq!(reencoded, encoded);

    let schema = SchemaId::parse("schemas/FuzzValue@1")
        .unwrap_or_else(|error| panic!("fixed fuzz schema identity is invalid: {error}"));
    TypedValue::from_schema(schema, value)
        .unwrap_or_else(|error| panic!("parsed value did not enter TypedValue safely: {error}"));
}

fuzz_target!(|data: &[u8]| {
    let Some((&mode, payload)) = data.split_first() else {
        return;
    };

    match mode {
        b'D' => {
            let selector = payload.first().copied().unwrap_or_default() as usize;
            let depth = MAX_CANONICAL_JSON_DEPTH.saturating_sub(4) + selector % 9;
            let text = format!("{}0{}", "[".repeat(depth), "]".repeat(depth));
            let parsed = parse_canonical_json(&text);
            if depth <= MAX_CANONICAL_JSON_DEPTH {
                assert!(parsed.is_ok(), "bounded canonical depth was rejected");
                exercise_json(&text);
            } else {
                assert_eq!(
                    parsed,
                    Err(CanonicalJsonParseError::CanonicalJson(
                        CanonicalJsonError::NestingTooDeep {
                            max_depth: MAX_CANONICAL_JSON_DEPTH,
                        },
                    )),
                );
            }
        }
        b'I' => {
            let selector = payload.first().copied().unwrap_or_default() as usize;
            let digits = (MAX_CANONICAL_INTEGER_DIGITS.saturating_sub(4) + selector % 9).max(1);
            let sign = if payload.get(1).is_some_and(|value| value % 2 == 1) {
                "-"
            } else {
                ""
            };
            let text = format!("{sign}1{}", "7".repeat(digits - 1));
            let parsed = parse_canonical_json(&text);
            if digits <= MAX_CANONICAL_INTEGER_DIGITS {
                assert!(parsed.is_ok(), "bounded canonical integer was rejected");
                exercise_json(&text);
            } else {
                assert_eq!(
                    parsed,
                    Err(CanonicalJsonParseError::CanonicalJson(
                        CanonicalJsonError::IntegerTooLarge {
                            max_digits: MAX_CANONICAL_INTEGER_DIGITS,
                        },
                    )),
                );
            }
        }
        b'K' => {
            let key = payload
                .iter()
                .take(32)
                .map(|value| char::from(b'a' + value % 26))
                .collect::<String>();
            let text = format!(r#"{{"{key}":0,"{key}":1}}"#);
            assert_eq!(
                parse_canonical_json(&text),
                Err(CanonicalJsonParseError::DuplicateObjectKey { key }),
            );
        }
        b'R' => {
            if let Ok(text) = std::str::from_utf8(payload) {
                exercise_json(text);
            }
        }
        b'S' => {
            if let Ok(text) = std::str::from_utf8(payload) {
                let _ = SchemaId::parse(text);
            }
        }
        _ => {
            if let Ok(text) = std::str::from_utf8(data) {
                exercise_json(text);
                let _ = SchemaId::parse(text);
            }
        }
    }
});
