use graphblocks_core::canonical::{canonical_hash, canonical_json};
use serde_json::json;

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
