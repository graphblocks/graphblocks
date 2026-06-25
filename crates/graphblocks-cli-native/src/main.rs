use std::io::{self, Read};

use graphblocks_cli_native::{NativeCliMode, run_compiler_workflow};
use graphblocks_compiler::diagnostics::Severity;
use serde_json::{Value, json};

fn main() {
    let mut args = std::env::args().skip(1);
    let command = args.next();
    let mut expand = false;
    for arg in args {
        if arg == "--expand" {
            expand = true;
        } else {
            eprintln!("unsupported argument: {arg}");
            std::process::exit(2);
        }
    }

    let mode = match command.as_deref() {
        Some("validate") => NativeCliMode::Validate,
        Some("plan") => NativeCliMode::Plan { expand },
        _ => {
            eprintln!("usage: graphblocks-native <validate|plan> [--expand] < graph.json");
            std::process::exit(2);
        }
    };

    let mut input = String::new();
    if let Err(error) = io::stdin().read_to_string(&mut input) {
        eprintln!("failed to read stdin: {error}");
        std::process::exit(2);
    }
    let document: Value = match serde_json::from_str(&input) {
        Ok(value) => value,
        Err(error) => {
            eprintln!("failed to parse stdin as JSON: {error}");
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
