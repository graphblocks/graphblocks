use serde_json::{Map, Value};

pub const GRAPH_API_VERSION: &str = "graphblocks.ai/v1alpha3";
pub const PSEUDO_NODES: [&str; 5] = ["$input", "$output", "$state", "$context", "$execution"];

pub fn normalize_graph(document: &Value) -> Value {
    if document.get("kind").and_then(Value::as_str) != Some("Graph") {
        return document.clone();
    }

    let mut normalized = document.clone();
    let Some(root) = normalized.as_object_mut() else {
        return normalized;
    };

    root.insert(
        "apiVersion".to_owned(),
        Value::String(GRAPH_API_VERSION.to_owned()),
    );
    if !root.contains_key("spec") {
        root.insert("spec".to_owned(), Value::Object(Map::new()));
    }

    let Some(spec) = root.get_mut("spec").and_then(Value::as_object_mut) else {
        return normalized;
    };

    let mut edges = Vec::<(String, String)>::new();
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
                        edges.push((source, format!("{node_name}.{port_path}")));
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
                        edges.push((format!("{node_name}.{port_path}"), target));
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
