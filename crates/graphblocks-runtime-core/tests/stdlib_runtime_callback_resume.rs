use std::path::{Path, PathBuf};
use std::sync::{Arc, Barrier};
use std::time::{SystemTime, UNIX_EPOCH};

use graphblocks_compiler::canonical::canonical_hash;
use graphblocks_runtime_core::stdlib_runtime::run_stdlib_graph_with_options_json;
use hmac::{Hmac, Mac};
use rusqlite::Connection;
use serde_json::{Value, json};
use sha2::Sha256;

const CALLBACK_ADMISSION_HMAC_KEY: &str = "native-callback-test-admission-key-material-v1";

fn sqlite_path(label: &str) -> PathBuf {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "graphblocks-native-callback-{label}-{unique}.sqlite3"
    ))
}

fn callback_graph() -> Value {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/native-callback-runtime.json"))
            .expect("native callback TCK fixture is valid JSON");
    fixture["graph"].clone()
}

fn callback_receipt(waiting: &Value, admitted: bool) -> Value {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/native-callback-runtime.json"))
            .expect("native callback TCK fixture is valid JSON");
    let mut receipt = fixture["receipt"].clone();
    receipt["resume_admission"] = json!({
        "contract": "graphblocks.trusted-callback-resume-admission.v1",
        "outcome": if admitted { "authorized" } else { "denied" },
        "authentication_decision_id": "authentication-decision-1",
        "policy_decision_id": "policy-decision-1",
        "budget_reservation_id": "budget-reservation-1",
        "compatible_release_digest": waiting["graphHash"],
        "run_id": waiting["runId"],
        "operation_id": receipt["operation_id"],
        "node_id": receipt["node_id"],
        "attempt_id": receipt["attempt_id"],
        "checkpoint_id": waiting["checkpoint"]["checkpoint_id"],
        "checkpoint_state_digest": waiting["checkpoint"]["state_digest"],
        "ownership": {
            "owner_id": "worker-1",
            "lease_id": "lease-1",
            "fencing_epoch": 7,
            "fence_token": "ownership-fence-7"
        },
        "schema_verification": {
            "verification_id": "schema-verification-1",
            "schema_id": receipt["schema_id"],
            "payload_digest": receipt["payload_digest"],
            "verified_by": receipt["verified_by"]
        }
    });
    sign_callback_admission(&mut receipt);
    receipt
}

fn sign_callback_admission(receipt: &mut Value) {
    let admission = receipt["resume_admission"]
        .as_object_mut()
        .expect("callback admission is an object");
    admission.remove("signature");
    let admission_digest = canonical_hash(&Value::Object(admission.clone()));
    let message = format!("graphblocks.trusted-callback-resume-admission.v1\n{admission_digest}");
    let mut mac = Hmac::<Sha256>::new_from_slice(CALLBACK_ADMISSION_HMAC_KEY.as_bytes())
        .expect("test callback admission HMAC key is valid");
    mac.update(message.as_bytes());
    let signature = mac
        .finalize()
        .into_bytes()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    admission.insert(
        "signature".to_owned(),
        json!(format!("hmac-sha256:{signature}")),
    );
}

fn callback_receipt_with_denied_decision(waiting: &Value) -> Value {
    let mut receipt = callback_receipt(waiting, false);
    receipt["resume_admission"]["policy_decision_id"] = json!("policy-denial-1");
    sign_callback_admission(&mut receipt);
    receipt
}

fn async_operation_state(path: &Path) -> Result<String, String> {
    let connection = Connection::open(path).map_err(|error| error.to_string())?;
    let operation_json: String = connection
        .query_row(
            "SELECT operation_json FROM async_operations WHERE operation_id = ?1",
            ["operation-native-1"],
            |row| row.get(0),
        )
        .map_err(|error| error.to_string())?;
    let operation: Value =
        serde_json::from_str(&operation_json).map_err(|error| error.to_string())?;
    operation["state"]
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| "stored operation state is not a string".to_owned())
}

fn force_async_operation_state(path: &Path, state: &str) -> Result<(), String> {
    let connection = Connection::open(path).map_err(|error| error.to_string())?;
    let operation_json: String = connection
        .query_row(
            "SELECT operation_json FROM async_operations WHERE operation_id = ?1",
            ["operation-native-1"],
            |row| row.get(0),
        )
        .map_err(|error| error.to_string())?;
    let mut operation: Value =
        serde_json::from_str(&operation_json).map_err(|error| error.to_string())?;
    operation["state"] = json!(state);
    connection
        .execute(
            "UPDATE async_operations SET operation_json = ?2 WHERE operation_id = ?1",
            ["operation-native-1", &operation.to_string()],
        )
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn accepted_callback_event_counts(path: &Path) -> Result<(i64, i64), String> {
    let connection = Connection::open(path).map_err(|error| error.to_string())?;
    connection
        .query_row(
            "SELECT
                 SUM(CASE WHEN json_extract(event_json, '$.type') = 'ExternalCallbackReceived' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN json_extract(event_json, '$.type') = 'CallbackResumeAuthorized' THEN 1 ELSE 0 END)
             FROM async_operation_events
             WHERE operation_id = ?1",
            ["operation-native-1"],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|error| error.to_string())
}

fn inject_acceptance_ahead_crash(path: &Path, receipt: &Value) -> Result<(), String> {
    let connection = Connection::open(path).map_err(|error| error.to_string())?;
    connection
        .execute_batch(
            "CREATE TRIGGER inject_crash_before_callback_accepted_coordinator_update
             BEFORE UPDATE ON native_callback_checkpoints
             WHEN NEW.status = 'callback_accepted'
             BEGIN
                 SELECT RAISE(FAIL, 'injected crash after callback acceptance');
             END;",
        )
        .map_err(|error| error.to_string())?;
    drop(connection);

    let error = run(path, Some(receipt.clone()))
        .expect_err("callback-accepted coordinator persistence is fault-injected");
    assert!(
        error.contains("injected crash after callback acceptance"),
        "{error}"
    );
    let connection = Connection::open(path).map_err(|error| error.to_string())?;
    connection
        .execute_batch("DROP TRIGGER inject_crash_before_callback_accepted_coordinator_update;")
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn options(path: &Path, receipt: Option<Value>) -> Value {
    let mut options = json!({
        "runId": "run-native-callback-1",
        "checkpointStorePath": path,
        "asyncOperationStorePath": path,
        "runStorePath": path,
        "journalStorePath": path,
        "callbackAdmissionHmacKey": CALLBACK_ADMISSION_HMAC_KEY
    });
    if let Some(receipt) = receipt {
        options["callbackReceipt"] = receipt;
    }
    options
}

fn run(path: &Path, receipt: Option<Value>) -> Result<Value, String> {
    run_graph(path, &callback_graph(), receipt)
}

fn run_graph(path: &Path, graph: &Value, receipt: Option<Value>) -> Result<Value, String> {
    let result = run_stdlib_graph_with_options_json(
        &graph.to_string(),
        "{}",
        &options(path, receipt).to_string(),
    )
    .map_err(|error| error.to_string())?;
    serde_json::from_str(&result).map_err(|error| error.to_string())
}

#[test]
fn native_callback_run_suspends_and_returns_a_canonical_checkpoint() -> Result<(), String> {
    let path = sqlite_path("suspend");

    let waiting = run(&path, None)?;

    assert_eq!(waiting["status"], "waiting_callback");
    assert_eq!(waiting["checkpoint"]["run_id"], "run-native-callback-1");
    assert_eq!(waiting["checkpoint"]["wait_node"], "wait");
    assert_eq!(
        waiting["checkpoint"]["operation"]["operation_id"],
        "operation-native-1"
    );
    assert_eq!(
        waiting["checkpoint"]["state_digest"],
        canonical_hash(&Value::Object(
            waiting["checkpoint"]
                .as_object()
                .expect("checkpoint is an object")
                .iter()
                .filter(|(key, _)| key.as_str() != "state_digest")
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect::<serde_json::Map<_, _>>()
        ))
    );
    let waiting_position = waiting["journal"]
        .as_array()
        .expect("journal is an array")
        .len();
    let prefix_position = waiting_position - 1;
    assert_eq!(
        waiting["checkpoint"]["journal_binding"]["prefix_position"],
        json!(prefix_position)
    );
    assert_eq!(
        waiting["checkpoint"]["journal_binding"]["waiting_position"],
        json!(waiting_position)
    );
    assert_eq!(
        waiting["checkpoint"]["journal_binding"]["terminal_position"],
        Value::Null
    );
    assert_eq!(
        waiting["checkpoint"]["journal_binding"]["prefix_digest"],
        canonical_hash(&json!(
            waiting["journal"]
                .as_array()
                .expect("journal is an array")
                .iter()
                .take(prefix_position)
                .cloned()
                .collect::<Vec<_>>()
        ))
    );
    assert_eq!(
        waiting["journal"]
            .as_array()
            .and_then(|records| records.last())
            .and_then(|record| record.get("kind")),
        Some(&json!("run_waiting_callback"))
    );
    let repeat_path = sqlite_path("suspend-repeat");
    assert_eq!(run(&repeat_path, None)?, waiting);
    let _ = std::fs::remove_file(path);
    let _ = std::fs::remove_file(repeat_path);
    Ok(())
}

#[test]
fn native_callback_checkpoint_reports_accepted_depth_overflow_without_panicking() {
    let path = sqlite_path("accepted-depth");
    let mut graph = callback_graph();
    graph["spec"]["interface"]["inputs"] = json!({"deep": "graphblocks.ai/JsonValue@1"});
    let mut nested = Value::Null;
    for _ in 0..63 {
        nested = json!({"nested": nested});
    }
    let inputs = json!({"deep": nested});

    let error = run_stdlib_graph_with_options_json(
        &graph.to_string(),
        &inputs.to_string(),
        &options(&path, None).to_string(),
    )
    .expect_err("checkpoint wrapping beyond canonical depth must return an error");

    assert!(
        error.to_string().contains("native callback checkpoint"),
        "{error}"
    );
    assert!(
        error.to_string().contains("nesting must not exceed"),
        "{error}"
    );
    let _ = std::fs::remove_file(path);
}

#[test]
fn native_callback_suspension_requires_checkpoint_persistence() {
    let error = run_stdlib_graph_with_options_json(
        &callback_graph().to_string(),
        "{}",
        r#"{"runId":"run-native-callback-1"}"#,
    )
    .expect_err("callback suspension without a checkpoint store must fail closed");

    assert_eq!(
        error.to_string(),
        "native callback suspension requires checkpointStorePath"
    );
}

#[test]
fn native_callback_receipt_is_rejected_without_checkpoint_persistence() -> Result<(), String> {
    let path = sqlite_path("receipt-without-checkpoint");
    let waiting = run(&path, None)?;
    let options = json!({
        "runId": "run-native-callback-1",
        "callbackReceipt": callback_receipt(&waiting, true),
        "callbackAdmissionHmacKey": CALLBACK_ADMISSION_HMAC_KEY
    });

    let error = run_stdlib_graph_with_options_json(
        &callback_graph().to_string(),
        "{}",
        &options.to_string(),
    )
    .expect_err("callback receipt without a checkpoint store must fail closed");

    assert_eq!(
        error.to_string(),
        "runtime callbackReceipt requires checkpointStorePath"
    );
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn native_callback_wait_checkpoint_is_persisted_and_reloadable() -> Result<(), String> {
    let path = sqlite_path("persisted");

    let waiting = run(&path, None)?;
    let connection = Connection::open(&path).map_err(|error| error.to_string())?;
    let (checkpoint_json, state_digest): (String, String) = connection
        .query_row(
            "SELECT checkpoint_json, state_digest FROM native_callback_checkpoints WHERE run_id = ?1",
            ["run-native-callback-1"],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|error| error.to_string())?;
    let reloaded: Value =
        serde_json::from_str(&checkpoint_json).map_err(|error| error.to_string())?;

    assert_eq!(reloaded, waiting["checkpoint"]);
    assert_eq!(state_digest, waiting["checkpoint"]["state_digest"]);
    drop(connection);
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn admitted_native_callback_resumes_with_callback_and_operation_outputs() -> Result<(), String> {
    let path = sqlite_path("admitted");
    let waiting = run(&path, None)?;
    assert_eq!(waiting["status"], "waiting_callback");

    let resumed = run(&path, Some(callback_receipt(&waiting, true)))?;

    assert_eq!(resumed["status"], "succeeded", "{resumed:#}");
    assert_eq!(
        resumed["outputs"]["callback"],
        json!({"status": "completed", "conclusion": "success"})
    );
    assert_eq!(resumed["outputs"]["operation"]["state"], "resuming");
    let kinds = resumed["journal"]
        .as_array()
        .expect("journal is an array")
        .iter()
        .filter_map(|record| record["kind"].as_str())
        .collect::<Vec<_>>();
    let waiting_index = kinds
        .iter()
        .position(|kind| *kind == "run_waiting_callback")
        .expect("wait record exists");
    let callback_index = kinds
        .iter()
        .position(|kind| *kind == "external_callback_received")
        .expect("callback record exists");
    let resuming_index = kinds
        .iter()
        .position(|kind| *kind == "run_resuming")
        .expect("resuming record exists");
    assert!(waiting_index < callback_index && callback_index < resuming_index);
    let repeat_path = sqlite_path("admitted-repeat");
    let repeated_waiting = run(&repeat_path, None)?;
    assert_eq!(
        run(
            &repeat_path,
            Some(callback_receipt(&repeated_waiting, true))
        )?,
        resumed
    );
    let _ = std::fs::remove_file(path);
    let _ = std::fs::remove_file(repeat_path);
    Ok(())
}

#[test]
fn native_callback_resume_does_not_duplicate_guard_skip_journal_records() -> Result<(), String> {
    let path = sqlite_path("guard-skip-resume");
    let mut graph = callback_graph();
    graph["spec"]["nodes"]["aCondition"] = json!({
        "block": "check.run_suite@1",
        "config": {"checks": [{"checkId": "disabled", "status": "failed"}]}
    });
    graph["spec"]["nodes"]["bGuarded"] = json!({
        "block": "model.generate@1",
        "config": {"response": "must not execute"},
        "when": "aCondition.passed"
    });

    let waiting = run_graph(&path, &graph, None)?;
    assert_eq!(waiting["status"], "waiting_callback", "{waiting:#}");
    assert!(
        waiting["checkpoint"]["node_outputs"]["bGuarded"]
            .as_object()
            .is_some_and(|outputs| outputs.keys().any(|key| key.contains("checkpoint_skipped")))
    );

    let resumed = run_graph(&path, &graph, Some(callback_receipt(&waiting, true)))?;
    assert_eq!(resumed["status"], "succeeded", "{resumed:#}");
    for kind in ["node_started", "node_completed"] {
        let count = resumed["journal"]
            .as_array()
            .expect("journal is an array")
            .iter()
            .filter(|record| record["kind"] == kind && record["nodeId"] == "bGuarded")
            .count();
        assert_eq!(count, 1, "{kind} should appear once: {resumed:#}");
    }
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn denied_native_callback_does_not_resume_the_run() -> Result<(), String> {
    let mut expected_error = None;
    for evidence_field in [
        "authentication_decision_id",
        "policy_decision_id",
        "budget_reservation_id",
        "compatible_release_digest",
    ] {
        let path = sqlite_path(&format!("denied-{evidence_field}"));
        let waiting = run(&path, None)?;
        let mut denied = callback_receipt(&waiting, true);
        denied["resume_admission"][evidence_field] = Value::Null;
        let error = run(&path, Some(denied))
            .expect_err("a callback without complete resume admission must be denied");
        assert_eq!(error, "native async callback rejected");
        if let Some(expected_error) = &expected_error {
            assert_eq!(&error, expected_error);
        } else {
            expected_error = Some(error);
        }
        let connection = Connection::open(&path).map_err(|error| error.to_string())?;
        let status: String = connection
            .query_row(
                "SELECT status FROM runs WHERE run_id = ?1",
                ["run-native-callback-1"],
                |row| row.get(0),
            )
            .map_err(|error| error.to_string())?;
        assert_eq!(status, "waiting_callback");
        drop(connection);
        let _ = std::fs::remove_file(path);
    }
    let expected_error = expected_error.expect("all denied admission gates were exercised");
    let unknown_path = sqlite_path("denied-unknown");
    let unknown_waiting = run(&sqlite_path("denied-template"), None)?;
    let unknown_error = run(
        &unknown_path,
        Some(callback_receipt_with_denied_decision(&unknown_waiting)),
    )
    .expect_err("an unadmitted callback must be denied before operation lookup");
    assert_eq!(unknown_error, expected_error);
    let _ = std::fs::remove_file(unknown_path);

    let oracle_path = sqlite_path("non-oracle");
    let oracle_waiting = run(&oracle_path, None)?;
    let mut mismatched = callback_receipt(&oracle_waiting, true);
    mismatched["attempt_id"] = json!("attempt-other");
    let mismatch_error = run(&oracle_path, Some(mismatched))
        .expect_err("a known coordinator identity mismatch must be rejected");
    let authorized_unknown_path = sqlite_path("non-oracle-authorized-unknown");
    let authorized_unknown_error = run(
        &authorized_unknown_path,
        Some(callback_receipt(&oracle_waiting, true)),
    )
    .expect_err("an authorized callback for an unknown coordinator must be rejected");
    assert_eq!(mismatch_error, expected_error);
    assert_eq!(authorized_unknown_error, expected_error);
    let _ = std::fs::remove_file(oracle_path);
    let _ = std::fs::remove_file(authorized_unknown_path);
    Ok(())
}

#[test]
fn native_callback_requires_a_positive_trusted_schema_validation_assertion() -> Result<(), String> {
    let path = sqlite_path("schema-validation-false");
    let waiting = run(&path, None)?;
    let mut receipt = callback_receipt(&waiting, true);
    receipt["schema_validated"] = json!(false);

    let error = run(&path, Some(receipt))
        .expect_err("a trusted admission with schema_validated=false must fail closed");

    assert_eq!(error, "native async callback rejected");
    assert_eq!(async_operation_state(&path)?, "waiting_callback");
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn native_callback_admission_requires_a_host_trusted_signature() -> Result<(), String> {
    let path = sqlite_path("unsigned-admission");
    let waiting = run(&path, None)?;
    let mut unsigned = callback_receipt(&waiting, true);
    unsigned["resume_admission"]
        .as_object_mut()
        .expect("callback admission is an object")
        .remove("signature");

    let error = run(&path, Some(unsigned))
        .expect_err("payload-provided admission claims must not authorize a resume");

    assert_eq!(error, "native async callback rejected");
    assert_eq!(async_operation_state(&path)?, "waiting_callback");
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn native_callback_admission_signature_binds_schema_and_payload_claims() -> Result<(), String> {
    let path = sqlite_path("tampered-admission");
    let waiting = run(&path, None)?;
    let mut forged = callback_receipt(&waiting, true);
    forged["payload"] = json!({"unexpected": "shape"});
    let forged_digest = canonical_hash(&forged["payload"]);
    forged["payload_digest"] = json!(forged_digest);
    forged["resume_admission"]["schema_verification"]["payload_digest"] =
        forged["payload_digest"].clone();

    let error = run(&path, Some(forged))
        .expect_err("callback claims changed after trusted verification must fail closed");

    assert_eq!(error, "native async callback rejected");
    assert_eq!(async_operation_state(&path)?, "waiting_callback");
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn native_callback_receipt_requires_trusted_key_configuration() -> Result<(), String> {
    let path = sqlite_path("missing-admission-key");
    let waiting = run(&path, None)?;
    let mut untrusted_options = options(&path, Some(callback_receipt(&waiting, true)));
    untrusted_options
        .as_object_mut()
        .expect("runtime options are an object")
        .remove("callbackAdmissionHmacKey");

    let error = run_stdlib_graph_with_options_json(
        &callback_graph().to_string(),
        "{}",
        &untrusted_options.to_string(),
    )
    .expect_err("callback admission without trusted verifier key must fail closed");

    assert_eq!(error.to_string(), "native async callback rejected");
    assert_eq!(async_operation_state(&path)?, "waiting_callback");
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn retry_repairs_callback_accepted_before_coordinator_update_without_reaccepting()
-> Result<(), String> {
    for operation_state_after_crash in ["callback_received", "resuming"] {
        let path = sqlite_path(&format!("acceptance-ahead-{operation_state_after_crash}"));
        let waiting = run(&path, None)?;
        let receipt = callback_receipt(&waiting, true);
        inject_acceptance_ahead_crash(&path, &receipt)?;
        assert_eq!(async_operation_state(&path)?, "callback_received");
        assert_eq!(accepted_callback_event_counts(&path)?, (1, 1));
        let connection = Connection::open(&path).map_err(|error| error.to_string())?;
        let coordinator_phase: String = connection
            .query_row(
                "SELECT status FROM native_callback_checkpoints WHERE run_id = ?1",
                ["run-native-callback-1"],
                |row| row.get(0),
            )
            .map_err(|error| error.to_string())?;
        assert_eq!(coordinator_phase, "waiting_callback");
        drop(connection);
        if operation_state_after_crash == "resuming" {
            force_async_operation_state(&path, "resuming")?;
        }

        let completed = run(&path, Some(receipt))?;

        assert_eq!(completed["status"], "succeeded", "{completed:#}");
        assert_eq!(
            completed["outputs"]["callback"],
            json!({"status": "completed", "conclusion": "success"})
        );
        assert_eq!(accepted_callback_event_counts(&path)?, (1, 1));
        let _ = std::fs::remove_file(path);
    }
    Ok(())
}

#[test]
fn acceptance_ahead_rejects_tampered_persisted_receipt_binding() -> Result<(), String> {
    let path = sqlite_path("acceptance-ahead-tampered-receipt");
    let waiting = run(&path, None)?;
    let receipt = callback_receipt(&waiting, true);
    inject_acceptance_ahead_crash(&path, &receipt)?;
    let connection = Connection::open(&path).map_err(|error| error.to_string())?;
    let (event_index, event_json): (i64, String) = connection
        .query_row(
            "SELECT event_index, event_json
             FROM async_operation_events
             WHERE operation_id = ?1
               AND json_extract(event_json, '$.type') = 'ExternalCallbackReceived'",
            ["operation-native-1"],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|error| error.to_string())?;
    let mut event: Value = serde_json::from_str(&event_json).map_err(|error| error.to_string())?;
    let tampered_payload = json!({"status": "completed", "conclusion": "tampered"});
    event["receipt"]["payload_digest"] = json!(canonical_hash(&tampered_payload));
    event["receipt"]["payload"] = tampered_payload;
    connection
        .execute(
            "UPDATE async_operation_events
             SET event_json = ?3
             WHERE operation_id = ?1 AND event_index = ?2",
            rusqlite::params!["operation-native-1", event_index, event.to_string()],
        )
        .map_err(|error| error.to_string())?;
    drop(connection);

    let error = run(&path, Some(receipt))
        .expect_err("a persisted receipt that diverges from the incoming binding must fail closed");

    assert_eq!(error, "native async callback rejected");
    assert_eq!(async_operation_state(&path)?, "callback_received");
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn terminal_duplicate_rejects_tampered_persisted_acceptance_evidence() -> Result<(), String> {
    for operation_state in ["callback_received", "resuming"] {
        for event_type in ["ExternalCallbackReceived", "CallbackResumeAuthorized"] {
            let path = sqlite_path(&format!("terminal-tampered-{operation_state}-{event_type}"));
            let waiting = run(&path, None)?;
            let receipt = callback_receipt(&waiting, true);
            let completed = run(&path, Some(receipt.clone()))?;
            assert_eq!(completed["status"], "succeeded", "{completed:#}");
            if operation_state == "resuming" {
                force_async_operation_state(&path, operation_state)?;
            }

            let connection = Connection::open(&path).map_err(|error| error.to_string())?;
            let (event_index, event_json): (i64, String) = connection
                .query_row(
                    "SELECT event_index, event_json
                     FROM async_operation_events
                     WHERE operation_id = ?1
                       AND json_extract(event_json, '$.type') = ?2",
                    ["operation-native-1", event_type],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .map_err(|error| error.to_string())?;
            let mut event: Value =
                serde_json::from_str(&event_json).map_err(|error| error.to_string())?;
            match event_type {
                "ExternalCallbackReceived" => {
                    let tampered_payload = json!({"status": "completed", "conclusion": "tampered"});
                    event["receipt"]["payload_digest"] = json!(canonical_hash(&tampered_payload));
                    event["receipt"]["payload"] = tampered_payload;
                }
                "CallbackResumeAuthorized" => {
                    event["policy_decision_id"] = json!("policy-decision-tampered");
                }
                _ => unreachable!("the test only exercises acceptance evidence events"),
            }
            connection
                .execute(
                    "UPDATE async_operation_events
                     SET event_json = ?3
                     WHERE operation_id = ?1 AND event_index = ?2",
                    rusqlite::params!["operation-native-1", event_index, event.to_string()],
                )
                .map_err(|error| error.to_string())?;
            drop(connection);

            let error = run(&path, Some(receipt))
                .expect_err("terminal callback evidence divergence must fail closed");

            assert_eq!(error, "native async callback rejected");
            assert_eq!(async_operation_state(&path)?, operation_state);
            assert_eq!(accepted_callback_event_counts(&path)?, (1, 1));
            let _ = std::fs::remove_file(path);
        }
    }
    Ok(())
}

#[test]
fn duplicate_native_callback_is_idempotent() -> Result<(), String> {
    let path = sqlite_path("duplicate");
    let waiting = run(&path, None)?;
    let first = run(&path, Some(callback_receipt(&waiting, true)))?;

    let duplicate = run(&path, Some(callback_receipt(&waiting, true)))?;

    assert_eq!(duplicate, first);
    assert_eq!(
        duplicate["journal"]
            .as_array()
            .expect("journal is an array")
            .iter()
            .filter(|record| record["kind"] == "external_callback_received")
            .count(),
        1
    );
    let mut identity_conflict = callback_receipt(&waiting, true);
    identity_conflict["attempt_id"] = json!("attempt-other");
    let error = run(&path, Some(identity_conflict))
        .expect_err("a completed duplicate must still match checkpoint identity");
    assert!(error.contains("native async callback rejected"));
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn concurrent_native_callback_resumes_are_serialized_atomically() -> Result<(), String> {
    let path = sqlite_path("concurrent-resume");
    let waiting = run(&path, None)?;
    let receipt = callback_receipt(&waiting, true);
    let barrier = Arc::new(Barrier::new(2));
    let workers = (0..2)
        .map(|_| {
            let path = path.clone();
            let receipt = receipt.clone();
            let barrier = Arc::clone(&barrier);
            std::thread::spawn(move || {
                barrier.wait();
                run(&path, Some(receipt))
            })
        })
        .collect::<Vec<_>>();
    let results = workers
        .into_iter()
        .map(|worker| {
            worker
                .join()
                .map_err(|_| "callback resume worker panicked".to_owned())?
        })
        .collect::<Result<Vec<_>, String>>()?;

    assert_eq!(results[0], results[1]);
    assert_eq!(results[0]["status"], "succeeded");
    assert_eq!(accepted_callback_event_counts(&path)?, (1, 1));
    assert_eq!(
        results[0]["journal"]
            .as_array()
            .expect("journal is an array")
            .iter()
            .filter(|record| record["kind"] == "external_callback_received")
            .count(),
        1
    );
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn native_callback_resume_survives_process_state_restart() -> Result<(), String> {
    let path = sqlite_path("restart");

    let waiting_json = run_stdlib_graph_with_options_json(
        &callback_graph().to_string(),
        "{}",
        &options(&path, None).to_string(),
    )
    .map_err(|error| error.to_string())?;
    let waiting: Value = serde_json::from_str(&waiting_json).map_err(|error| error.to_string())?;

    // The public entry point is deliberately invoked again with no in-memory runtime
    // handle carried across calls, mirroring a fresh worker process after restart.
    let resumed_json = run_stdlib_graph_with_options_json(
        &callback_graph().to_string(),
        "{}",
        &options(&path, Some(callback_receipt(&waiting, true))).to_string(),
    )
    .map_err(|error| error.to_string())?;
    let resumed: Value = serde_json::from_str(&resumed_json).map_err(|error| error.to_string())?;

    assert_eq!(resumed["status"], "succeeded", "{resumed:#}");
    assert_eq!(resumed["checkpoint"], Value::Null);
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn denied_callback_after_completion_or_for_unknown_run_is_non_mutating() -> Result<(), String> {
    let path = sqlite_path("terminal-denial");
    let waiting = run(&path, None)?;
    let completed = run(&path, Some(callback_receipt(&waiting, true)))?;
    let connection = Connection::open(&path).map_err(|error| error.to_string())?;
    let before_checkpoint: (String, String, Option<String>, Option<String>) = connection
        .query_row(
            "SELECT status, result_json, rejected_idempotency_key, rejected_payload_digest FROM native_callback_checkpoints WHERE run_id = ?1",
            ["run-native-callback-1"],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .map_err(|error| error.to_string())?;
    let before_journal_count: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM journal_records WHERE run_id = ?1",
            ["run-native-callback-1"],
            |row| row.get(0),
        )
        .map_err(|error| error.to_string())?;
    drop(connection);

    let error = run(&path, Some(callback_receipt_with_denied_decision(&waiting)))
        .expect_err("denied callback after completion must fail closed");
    assert_eq!(error, "native async callback rejected");

    let connection = Connection::open(&path).map_err(|error| error.to_string())?;
    let after_checkpoint: (String, String, Option<String>, Option<String>) = connection
        .query_row(
            "SELECT status, result_json, rejected_idempotency_key, rejected_payload_digest FROM native_callback_checkpoints WHERE run_id = ?1",
            ["run-native-callback-1"],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .map_err(|error| error.to_string())?;
    let after_journal_count: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM journal_records WHERE run_id = ?1",
            ["run-native-callback-1"],
            |row| row.get(0),
        )
        .map_err(|error| error.to_string())?;
    assert_eq!(after_checkpoint, before_checkpoint);
    assert_eq!(after_journal_count, before_journal_count);
    assert_eq!(
        serde_json::from_str::<Value>(&after_checkpoint.1).map_err(|error| error.to_string())?,
        completed
    );
    drop(connection);

    let unknown_path = sqlite_path("terminal-denial-unknown");
    let unknown_error = run(
        &unknown_path,
        Some(callback_receipt_with_denied_decision(&waiting)),
    )
    .expect_err("denied callback for unknown run must fail closed");
    assert_eq!(unknown_error, error);
    assert!(!unknown_path.exists());
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn waiting_retry_reconciles_partial_operation_run_and_journal_commits() -> Result<(), String> {
    let path = sqlite_path("waiting-reconcile");
    let waiting = run(&path, None)?;
    let connection = Connection::open(&path).map_err(|error| error.to_string())?;
    connection
        .execute("DELETE FROM async_operations", [])
        .and_then(|_| connection.execute("DELETE FROM async_operation_events", []))
        .and_then(|_| connection.execute("DELETE FROM runs", []))
        .and_then(|_| connection.execute("DELETE FROM journal_records", []))
        .map_err(|error| error.to_string())?;
    drop(connection);

    assert_eq!(run(&path, None)?, waiting);
    let connection = Connection::open(&path).map_err(|error| error.to_string())?;
    let operation_count: i64 = connection
        .query_row("SELECT COUNT(*) FROM async_operations", [], |row| {
            row.get(0)
        })
        .map_err(|error| error.to_string())?;
    let run_status: String = connection
        .query_row(
            "SELECT status FROM runs WHERE run_id = ?1",
            ["run-native-callback-1"],
            |row| row.get(0),
        )
        .map_err(|error| error.to_string())?;
    let journal_count: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM journal_records WHERE run_id = ?1",
            ["run-native-callback-1"],
            |row| row.get(0),
        )
        .map_err(|error| error.to_string())?;
    assert_eq!(operation_count, 1);
    assert_eq!(run_status, "waiting_callback");
    assert_eq!(
        journal_count as usize,
        waiting["journal"]
            .as_array()
            .expect("waiting result journal is an array")
            .len()
    );
    drop(connection);
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn completed_duplicate_reconciles_missing_terminal_evidence() -> Result<(), String> {
    let path = sqlite_path("completed-reconcile");
    let waiting = run(&path, None)?;
    let completed = run(&path, Some(callback_receipt(&waiting, true)))?;
    let connection = Connection::open(&path).map_err(|error| error.to_string())?;
    connection
        .execute("DELETE FROM async_operations", [])
        .and_then(|_| connection.execute("DELETE FROM async_operation_events", []))
        .and_then(|_| connection.execute("DELETE FROM async_callback_receipts", []))
        .and_then(|_| connection.execute("DELETE FROM runs", []))
        .and_then(|_| connection.execute("DELETE FROM journal_records", []))
        .map_err(|error| error.to_string())?;
    drop(connection);

    assert_eq!(
        run(&path, Some(callback_receipt(&waiting, true)))?,
        completed
    );
    let connection = Connection::open(&path).map_err(|error| error.to_string())?;
    let run_status: String = connection
        .query_row(
            "SELECT status FROM runs WHERE run_id = ?1",
            ["run-native-callback-1"],
            |row| row.get(0),
        )
        .map_err(|error| error.to_string())?;
    let (journal_count, terminal_count): (i64, i64) = connection
        .query_row(
            "SELECT COUNT(*), SUM(terminal) FROM journal_records WHERE run_id = ?1",
            ["run-native-callback-1"],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|error| error.to_string())?;
    assert_eq!(run_status, "completed");
    assert_eq!(async_operation_state(&path)?, "callback_received");
    assert_eq!(accepted_callback_event_counts(&path)?, (1, 1));
    assert_eq!(
        journal_count as usize,
        completed["journal"]
            .as_array()
            .expect("completed result journal is an array")
            .len()
    );
    assert_eq!(terminal_count, 1);
    drop(connection);
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn resume_rejects_a_journal_prefix_that_no_longer_matches_checkpoint() -> Result<(), String> {
    let path = sqlite_path("journal-binding");
    let waiting = run(&path, None)?;
    let connection = Connection::open(&path).map_err(|error| error.to_string())?;
    connection
        .execute(
            "UPDATE journal_records SET kind = 'tampered' WHERE run_id = ?1 AND run_sequence = 1",
            ["run-native-callback-1"],
        )
        .map_err(|error| error.to_string())?;
    drop(connection);

    let error = run(&path, Some(callback_receipt(&waiting, true)))
        .expect_err("journal prefix mutation must be detected before callback admission");
    assert!(error.contains("journal prefix"), "{error}");
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn failed_resume_consumes_checkpoint_and_retries_deterministically() -> Result<(), String> {
    let path = sqlite_path("failed-resume");
    let mut graph = callback_graph();
    graph["spec"]["nodes"]["selectCallback"]["config"]["outputSchema"] = Value::Null;
    let waiting = run_graph(&path, &graph, None)?;

    let failed = run_graph(&path, &graph, Some(callback_receipt(&waiting, true)))?;
    assert_eq!(failed["status"], "failed", "{failed:#}");
    assert_eq!(failed["checkpoint"], Value::Null);
    assert_eq!(
        run_graph(&path, &graph, Some(callback_receipt(&waiting, true)))?,
        failed
    );
    let connection = Connection::open(&path).map_err(|error| error.to_string())?;
    let status: String = connection
        .query_row(
            "SELECT status FROM native_callback_checkpoints WHERE run_id = ?1",
            ["run-native-callback-1"],
            |row| row.get(0),
        )
        .map_err(|error| error.to_string())?;
    assert_eq!(status, "terminal");
    drop(connection);
    let _ = std::fs::remove_file(path);
    Ok(())
}

#[test]
fn inconsistent_operation_identity_is_rejected_before_any_initial_persistence() -> Result<(), String>
{
    let path = sqlite_path("invalid-initial-identity");
    let mut graph = callback_graph();
    graph["spec"]["nodes"]["start"]["config"]["runId"] = json!("run-other");

    let error = run_graph(&path, &graph, None)
        .expect_err("operation identity must be checked before coordinator persistence");
    assert!(error.contains("operation identity"), "{error}");
    let connection = Connection::open(&path).map_err(|error| error.to_string())?;
    for table in [
        "native_callback_checkpoints",
        "async_operations",
        "runs",
        "journal_records",
    ] {
        let count: i64 = connection
            .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
                row.get(0)
            })
            .unwrap_or(0);
        assert_eq!(count, 0, "{table} must remain empty");
    }
    drop(connection);
    let _ = std::fs::remove_file(path);
    Ok(())
}
