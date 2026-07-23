use std::io::{self, Read};

use graphblocks_cli_native::{
    NativeCliMode, load_graph_document, run_compiler_workflow, run_stdlib_workflow_with_options,
};
use graphblocks_compiler::diagnostics::Severity;
use graphblocks_schema::parse_canonical_json;
use serde_json::{Value, json};

fn main() {
    let mut args = std::env::args().skip(1);
    let command = args.next();
    let mut expand = false;
    let mut graph_name: Option<String> = None;
    let mut input_json = "{}".to_owned();
    let mut runtime_options = serde_json::Map::new();
    while let Some(arg) = args.next() {
        match (command.as_deref(), arg.as_str()) {
            (Some("plan"), "--expand") => {
                expand = true;
            }
            (Some("validate" | "plan" | "run"), "--graph") => {
                let Some(value) = args.next() else {
                    eprintln!("--graph requires a graph metadata.name argument");
                    std::process::exit(2);
                };
                graph_name = Some(value);
            }
            (Some("run"), "--input-json") => {
                let Some(value) = args.next() else {
                    eprintln!("--input-json requires a JSON object argument");
                    std::process::exit(2);
                };
                input_json = value;
            }
            (
                Some("run"),
                "--run-id"
                | "--checkpoint-store-path"
                | "--async-operation-store-path"
                | "--run-store-path"
                | "--journal-store-path"
                | "--application-event-store-path",
            ) => {
                let Some(value) = args.next() else {
                    eprintln!("{arg} requires a string argument");
                    std::process::exit(2);
                };
                let option_name = match arg.as_str() {
                    "--run-id" => "runId",
                    "--checkpoint-store-path" => "checkpointStorePath",
                    "--async-operation-store-path" => "asyncOperationStorePath",
                    "--run-store-path" => "runStorePath",
                    "--journal-store-path" => "journalStorePath",
                    "--application-event-store-path" => "applicationEventStorePath",
                    _ => {
                        eprintln!("unsupported runtime path option: {arg}");
                        std::process::exit(2);
                    }
                };
                runtime_options.insert(option_name.to_owned(), Value::String(value));
            }
            (Some("run"), "--callback-receipt-json") => {
                let Some(value) = args.next() else {
                    eprintln!("--callback-receipt-json requires a JSON object argument");
                    std::process::exit(2);
                };
                match parse_canonical_json(&value) {
                    Ok(receipt) if receipt.is_object() => {
                        runtime_options.insert("callbackReceipt".to_owned(), receipt);
                    }
                    Ok(_) => {
                        eprintln!("--callback-receipt-json must decode to a JSON object");
                        std::process::exit(2);
                    }
                    Err(error) => {
                        eprintln!("failed to parse --callback-receipt-json as JSON: {error}");
                        std::process::exit(2);
                    }
                }
            }
            (Some("run"), "--callback-admission-hmac-key-env") => {
                let Some(variable) = args.next() else {
                    eprintln!(
                        "--callback-admission-hmac-key-env requires an environment variable name"
                    );
                    std::process::exit(2);
                };
                let key = match std::env::var(&variable) {
                    Ok(key) => key,
                    Err(error) => {
                        eprintln!(
                            "failed to read callback admission HMAC key from {variable:?}: {error}"
                        );
                        std::process::exit(2);
                    }
                };
                runtime_options.insert("callbackAdmissionHmacKey".to_owned(), Value::String(key));
            }
            _ => {
                eprintln!("unsupported argument: {arg}");
                std::process::exit(2);
            }
        }
    }

    let mut input = String::new();
    if let Err(error) = io::stdin().read_to_string(&mut input) {
        eprintln!("failed to read stdin: {error}");
        std::process::exit(2);
    }
    let document: Value = match load_graph_document(&input, graph_name.as_deref()) {
        Ok(value) => value,
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(2);
        }
    };

    if command.as_deref() == Some("run") {
        let inputs: Value = match parse_canonical_json(&input_json) {
            Ok(value) if value.is_object() => value,
            Ok(_) => {
                eprintln!("--input-json must decode to a JSON object");
                std::process::exit(2);
            }
            Err(error) => {
                eprintln!("failed to parse --input-json as JSON: {error}");
                std::process::exit(2);
            }
        };
        let report =
            run_stdlib_workflow_with_options(&document, &inputs, &Value::Object(runtime_options));
        if let Some(error) = report.error {
            eprintln!("native runtime execution failed: {error}");
            std::process::exit(1);
        }
        let Some(result) = report.result else {
            eprintln!("native runtime execution did not produce a result");
            std::process::exit(1);
        };
        match serde_json::to_string_pretty(&result) {
            Ok(rendered) => println!("{rendered}"),
            Err(error) => {
                eprintln!("failed to render runtime result JSON: {error}");
                std::process::exit(2);
            }
        }
        std::process::exit(if report.ok { 0 } else { 1 });
    }

    let mode = match command.as_deref() {
        Some("validate") => NativeCliMode::Validate,
        Some("plan") => NativeCliMode::Plan { expand },
        _ => {
            eprintln!(
                "usage: graphblocks-native <validate|plan|run> [--expand] [--graph NAME] [--input-json JSON] [--run-id ID] [--checkpoint-store-path PATH] [--async-operation-store-path PATH] [--run-store-path PATH] [--journal-store-path PATH] [--application-event-store-path PATH] [--callback-receipt-json JSON] [--callback-admission-hmac-key-env NAME] < graph.(json|yaml)"
            );
            std::process::exit(2);
        }
    };

    let report = run_compiler_workflow(&document, mode);
    let diagnostics = report
        .diagnostics
        .iter()
        .map(|diagnostic| {
            let severity = match diagnostic.severity {
                Severity::Error => "error",
                Severity::Warning => "warning",
                Severity::Info => "info",
            };
            json!({
                "code": diagnostic.code,
                "message": diagnostic.message,
                "path": diagnostic.path,
                "severity": severity,
            })
        })
        .collect::<Vec<_>>();
    let mut output = json!({
        "ok": report.ok,
        "graphHash": report.graph_hash,
        "diagnostics": diagnostics,
    });
    if let Some(normalized) = report.normalized
        && let Some(output_object) = output.as_object_mut()
    {
        output_object.insert("normalized".to_owned(), normalized);
    }

    match serde_json::to_string_pretty(&output) {
        Ok(rendered) => println!("{rendered}"),
        Err(error) => {
            eprintln!("failed to render report JSON: {error}");
            std::process::exit(2);
        }
    }
    std::process::exit(if report.ok { 0 } else { 1 });
}
