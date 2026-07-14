use std::mem;

use graphblocks_compiler::canonical::{
    canonical_hash, canonical_json, try_canonical_hash, try_canonical_json,
};
use graphblocks_schema::{CanonicalJsonError, MAX_CANONICAL_JSON_DEPTH};
use serde_json::{Value, json};

#[test]
fn canonical_json_sorts_object_keys_recursively() {
    let value = json!({
        "z": 1,
        "a": {
            "b": true,
            "a": ["text", {"d": null, "c": 3}]
        }
    });

    assert_eq!(
        canonical_json(&value),
        r#"{"a":{"a":["text",{"c":3,"d":null}],"b":true},"z":1}"#
    );
}

#[test]
fn canonical_hash_is_stable_for_map_ordering() {
    let left = json!({
        "kind": "Graph",
        "apiVersion": "graphblocks.ai/v1alpha3",
        "metadata": {"name": "ordered"},
        "spec": {
            "nodes": {
                "b": {"block": "text.join@1", "config": {"second": 2, "first": 1}},
                "a": {"block": "text.literal@1"}
            },
            "edges": [
                {"to": "b.value", "from": "a.value"},
                {"to": "$output.result", "from": "b.value"}
            ],
            "interface": {"outputs": {"result": "graphblocks.ai/Text@1"}}
        }
    });
    let right = json!({
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "spec": {
            "interface": {"outputs": {"result": "graphblocks.ai/Text@1"}},
            "edges": [
                {"from": "a.value", "to": "b.value"},
                {"from": "b.value", "to": "$output.result"}
            ],
            "nodes": {
                "a": {"block": "text.literal@1"},
                "b": {"config": {"first": 1, "second": 2}, "block": "text.join@1"}
            }
        },
        "metadata": {"name": "ordered"}
    });

    assert_eq!(canonical_hash(&left), canonical_hash(&right));
    assert_eq!(
        canonical_hash(&left),
        "sha256:4d121992be800bb056512aa26e834a45ee9efcba28e2ce8130d730f194ad97a2"
    );
}

#[test]
fn canonical_hash_preserves_array_order() {
    let left = json!({"items": [1, 2, 3]});
    let right = json!({"items": [3, 2, 1]});

    assert_ne!(canonical_hash(&left), canonical_hash(&right));
}

#[test]
fn canonical_json_preserves_large_integers_and_normalizes_exponents() {
    let value = serde_json::from_str(
        r#"{
            "fixed_high": 1000000000000000.0,
            "fixed_low": 0.0001,
            "large": 100000000000000000000,
            "negative_integer_zero": -0,
            "negative_zero": -0.0,
            "scientific_high": 10000000000000000.0,
            "small": 1e-7,
            "whole_float": 1.0
        }"#,
    )
    .expect("numeric fixture should parse");

    assert_eq!(
        canonical_json(&value),
        r#"{"fixed_high":1000000000000000.0,"fixed_low":0.0001,"large":100000000000000000000,"negative_integer_zero":0,"negative_zero":-0.0,"scientific_high":1e+16,"small":1e-07,"whole_float":1.0}"#
    );
}

#[test]
fn canonical_json_normalizes_numbers_beyond_binary64_range_without_panicking() {
    let value = serde_json::from_str(r#"{"equivalent":10e399,"huge":1e400,"negative":-0.01e402}"#)
        .expect("arbitrary precision numeric fixture should parse");

    assert_eq!(
        canonical_json(&value),
        r#"{"equivalent":1e+400,"huge":1e+400,"negative":-1e+400}"#
    );

    let enormous_left = serde_json::from_str(r#"10e999999"#).expect("large exponent should parse");
    let enormous_right =
        serde_json::from_str(r#"1e1000000"#).expect("equivalent large exponent should parse");

    assert_eq!(
        canonical_json(&enormous_left),
        canonical_json(&enormous_right)
    );
}

#[test]
fn canonical_json_keeps_distinct_decimals_above_binary64_integer_precision() {
    let left =
        serde_json::from_str(r#"9007199254740992.0"#).expect("exact binary64 decimal should parse");
    let right = serde_json::from_str(r#"9007199254740993.0"#)
        .expect("higher precision decimal should parse");
    let right_equivalent = serde_json::from_str(r#"90071992547409930e-1"#)
        .expect("equivalent higher precision decimal should parse");

    assert_eq!(canonical_json(&left), "9007199254740992.0");
    assert_eq!(canonical_json(&right), "9007199254740993.0");
    assert_eq!(canonical_json(&right_equivalent), "9007199254740993.0");
    assert_ne!(canonical_hash(&left), canonical_hash(&right));
    assert_eq!(canonical_hash(&right), canonical_hash(&right_equivalent));
}

#[test]
fn checked_compiler_identity_rejects_excessive_json_depth() {
    let mut at_limit = Value::Null;
    for _ in 0..MAX_CANONICAL_JSON_DEPTH {
        at_limit = Value::Array(vec![at_limit]);
    }
    let over_limit = Value::Array(vec![at_limit.clone()]);

    assert!(try_canonical_json(&at_limit).is_ok());
    assert!(try_canonical_hash(&at_limit).is_ok());
    assert_eq!(
        try_canonical_json(&over_limit),
        Err(CanonicalJsonError::NestingTooDeep {
            max_depth: MAX_CANONICAL_JSON_DEPTH,
        })
    );
    assert_eq!(
        try_canonical_hash(&over_limit),
        Err(CanonicalJsonError::NestingTooDeep {
            max_depth: MAX_CANONICAL_JSON_DEPTH,
        })
    );
}

#[test]
fn checked_compiler_hash_rejects_very_deep_values_without_recursive_abort() {
    let mut value = Value::Null;
    for _ in 0..100_000 {
        value = Value::Array(vec![value]);
    }

    assert_eq!(
        try_canonical_hash(&value),
        Err(CanonicalJsonError::NestingTooDeep {
            max_depth: MAX_CANONICAL_JSON_DEPTH,
        })
    );
    mem::forget(value);
}
