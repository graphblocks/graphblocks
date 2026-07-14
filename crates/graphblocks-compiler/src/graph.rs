use std::collections::BTreeSet;

use serde_json::{Map, Value};

use graphblocks_schema::resource_schema_errors;

pub const GRAPH_API_VERSION: &str = "graphblocks.ai/v1";
pub const PSEUDO_NODES: [&str; 5] = ["$input", "$output", "$state", "$context", "$execution"];

pub fn migrate_graph(document: &Value) -> Value {
    let mut migrated = document.clone();
    if migrated.get("kind").and_then(Value::as_str) != Some("Graph") {
        return migrated;
    }
    let Some(previous) = migrated
        .get("apiVersion")
        .and_then(Value::as_str)
        .filter(|version| {
            matches!(
                *version,
                "graphblocks.ai/v1alpha1" | "graphblocks.ai/v1alpha2" | "graphblocks.ai/v1alpha3"
            )
        })
        .map(str::to_owned)
    else {
        return migrated;
    };

    let Some(root) = migrated.as_object_mut() else {
        return migrated;
    };
    root.insert(
        "apiVersion".to_owned(),
        Value::String(GRAPH_API_VERSION.to_owned()),
    );
    if !root.contains_key("metadata") {
        root.insert("metadata".to_owned(), Value::Object(Map::new()));
    }
    if let Some(metadata) = root.get_mut("metadata").and_then(Value::as_object_mut) {
        if !metadata.contains_key("annotations") {
            metadata.insert("annotations".to_owned(), Value::Object(Map::new()));
        }
        if let Some(annotations) = metadata
            .get_mut("annotations")
            .and_then(Value::as_object_mut)
        {
            annotations.insert(
                "graphblocks.ai/migratedFrom".to_owned(),
                Value::String(previous),
            );
        }
    }
    match resource_schema_errors(&migrated) {
        Ok(violations) if violations.is_empty() => migrated,
        // Preview-only alpha fields and malformed legacy resources must never
        // be relabelled as stable v1 merely to enter compilation.
        Ok(_) | Err(_) => document.clone(),
    }
}

pub fn normalize_graph(document: &Value) -> Value {
    if document.get("kind").and_then(Value::as_str) != Some("Graph") {
        return document.clone();
    }

    let mut normalized = migrate_graph(document);
    let Some(root) = normalized.as_object_mut() else {
        return normalized;
    };
    if !root.contains_key("spec") {
        root.insert("spec".to_owned(), Value::Object(Map::new()));
    }

    let Some(spec) = root.get_mut("spec").and_then(Value::as_object_mut) else {
        return normalized;
    };

    let mut edges = Vec::<(String, String)>::new();
    let mut input_edges = Vec::<(String, String)>::new();
    let mut output_edges = Vec::<(String, String)>::new();
    if let Some(Value::Array(existing_edges)) = spec.remove("edges") {
        for edge in existing_edges {
            let Value::Object(edge) = edge else {
                continue;
            };
            let Some(Value::String(source)) = edge.get("from") else {
                continue;
            };
            let Some(Value::String(target)) = edge.get("to") else {
                continue;
            };
            edges.push((source.clone(), target.clone()));
        }
    }

    let mut nodes = match spec.remove("nodes") {
        Some(Value::Object(nodes)) => nodes,
        _ => Map::new(),
    };
    let mut node_names = nodes.keys().cloned().collect::<Vec<_>>();
    node_names.sort();

    for node_name in &node_names {
        let Some(Value::Object(node)) = nodes.get_mut(node_name) else {
            continue;
        };

        if let Some(Value::Object(inputs)) = node.remove("inputs") {
            let mut stack = inputs.into_iter().collect::<Vec<_>>();
            while let Some((port_path, value)) = stack.pop() {
                match value {
                    Value::String(source) => {
                        input_edges.push((source, format!("{node_name}.{port_path}")));
                    }
                    Value::Object(values) => {
                        for (key, nested) in values {
                            stack.push((format!("{port_path}.{key}"), nested));
                        }
                    }
                    Value::Array(values) => {
                        for (index, nested) in values.into_iter().enumerate() {
                            stack.push((format!("{port_path}.{index}"), nested));
                        }
                    }
                    _ => {}
                }
            }
        }

        if let Some(Value::Object(outputs)) = node.remove("outputs") {
            let mut stack = outputs.into_iter().collect::<Vec<_>>();
            while let Some((port_path, value)) = stack.pop() {
                match value {
                    Value::String(target) => {
                        output_edges.push((format!("{node_name}.{port_path}"), target));
                    }
                    Value::Object(values) => {
                        for (key, nested) in values {
                            stack.push((format!("{port_path}.{key}"), nested));
                        }
                    }
                    Value::Array(values) => {
                        for (index, nested) in values.into_iter().enumerate() {
                            stack.push((format!("{port_path}.{index}"), nested));
                        }
                    }
                    _ => {}
                }
            }
        }

        if let Some(connection) = node.remove("connection")
            && !node.contains_key("bindings")
        {
            let mut bindings = Map::new();
            bindings.insert("default".to_owned(), connection);
            node.insert("bindings".to_owned(), Value::Object(bindings));
        }
    }

    let input_edge_identities = input_edges.iter().cloned().collect::<BTreeSet<_>>();
    edges.extend(input_edges);
    edges.extend(
        output_edges
            .into_iter()
            .filter(|edge| !input_edge_identities.contains(edge)),
    );

    let mut sorted_nodes = Map::new();
    for node_name in node_names {
        if let Some(node) = nodes.remove(&node_name) {
            sorted_nodes.insert(node_name, node);
        }
    }

    edges.sort_by(|left, right| left.0.cmp(&right.0).then_with(|| left.1.cmp(&right.1)));
    spec.insert("nodes".to_owned(), Value::Object(sorted_nodes));
    spec.insert(
        "edges".to_owned(),
        Value::Array(
            edges
                .into_iter()
                .map(|(source, target)| {
                    let mut edge = Map::new();
                    edge.insert("from".to_owned(), Value::String(source));
                    edge.insert("to".to_owned(), Value::String(target));
                    Value::Object(edge)
                })
                .collect(),
        ),
    );

    normalized
}
