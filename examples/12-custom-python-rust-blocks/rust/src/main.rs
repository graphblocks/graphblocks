use std::collections::BTreeMap;
use std::io::{self, Read};

use graphblocks_protocol::{
    WorkerInvokeResult, WorkerProtocolMessage, WorkerProtocolMessagePayload,
};
use serde_json::{Value, json};

fn text_statistics(text: &str) -> Value {
    let mut checksum = 0xcbf2_9ce4_8422_2325_u64;
    for byte in text.as_bytes() {
        checksum ^= u64::from(*byte);
        checksum = checksum.wrapping_mul(0x0000_0100_0000_01b3);
    }
    json!({
        "text": text,
        "wordCount": text.split_whitespace().count(),
        "characterCount": text.chars().count(),
        "checksum": format!("fnv1a64:{checksum:016x}"),
    })
}

fn main() {
    let mut encoded = String::new();
    if let Err(error) = io::stdin().read_to_string(&mut encoded) {
        eprintln!("failed to read worker request: {error}");
        std::process::exit(1);
    }
    let message = match serde_json::from_str::<WorkerProtocolMessage>(&encoded) {
        Ok(message) => message,
        Err(error) => {
            eprintln!("invalid worker protocol message: {error}");
            std::process::exit(1);
        }
    };
    let request_message_id = message.message_id.clone();
    let request = match message.payload {
        WorkerProtocolMessagePayload::InvokeRequest(request) => *request,
        _ => {
            eprintln!("Rust custom block requires an invoke_request message");
            std::process::exit(1);
        }
    };
    if request.block != "examples.rust.text-stats@1" {
        eprintln!("unsupported Rust custom block {}", request.block);
        std::process::exit(1);
    }
    let text = match request.inputs.get("text").and_then(Value::as_str) {
        Some(text) => text,
        None => {
            eprintln!("text-stats input text must be a string");
            std::process::exit(1);
        }
    };
    let mut outputs = BTreeMap::new();
    outputs.insert("stats".to_owned(), text_statistics(text));
    let result = WorkerInvokeResult {
        invocation_id: request.invocation_id.clone(),
        node_attempt_id: request.node_attempt_id,
        lease_epoch: request.lease_epoch,
        outputs,
    };
    let response = WorkerProtocolMessage::invoke_result(
        format!("{}-result", request.invocation_id),
        message.sequence + 1,
        result,
    )
    .with_causation_id(request_message_id);
    let wire = match response.to_wire_value() {
        Ok(wire) => wire,
        Err(error) => {
            eprintln!("failed to validate worker result: {error:?}");
            std::process::exit(1);
        }
    };
    match serde_json::to_string(&wire) {
        Ok(rendered) => println!("{rendered}"),
        Err(error) => {
            eprintln!("failed to encode worker result: {error}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::text_statistics;
    use serde_json::json;

    #[test]
    fn statistics_are_unicode_aware_and_deterministic() {
        assert_eq!(
            text_statistics("hello 한글"),
            json!({
                "text": "hello 한글",
                "wordCount": 2,
                "characterCount": 8,
                "checksum": "fnv1a64:72d21c2a791e8fb5",
            })
        );
    }
}
