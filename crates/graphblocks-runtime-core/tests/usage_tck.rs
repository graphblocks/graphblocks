use graphblocks_runtime_core::usage::{
    InMemoryUsageLedger, UsageAmount, UsageConfidence, UsageLedgerError, UsageRecord, UsageSource,
};
use serde_json::{Map, Value, json};

#[test]
fn rust_usage_ledger_matches_shared_tck_cases() -> Result<(), String> {
    let cases = serde_json::from_str::<Value>(include_str!("../../../tck/usage/cases.json"))
        .map_err(|error| error.to_string())?;
    let cases = cases
        .as_array()
        .ok_or_else(|| "usage TCK root must be an array".to_owned())?;

    for case in cases {
        run_case(case)?;
    }

    Ok(())
}

fn run_case(case: &Value) -> Result<(), String> {
    let case_name = required_str(case, "name")?;
    let operations = case
        .get("operations")
        .and_then(Value::as_array)
        .ok_or_else(|| format!("usage TCK case {case_name} is missing operations"))?;
    let expected = case
        .get("expected")
        .and_then(Value::as_object)
        .ok_or_else(|| format!("usage TCK case {case_name} is missing expected"))?;
    let mut ledger = InMemoryUsageLedger::new();
    let mut append_results = Vec::new();

    for operation in operations {
        match required_str(operation, "op")? {
            "append" => {
                let record = usage_record(
                    operation
                        .get("record")
                        .and_then(Value::as_object)
                        .ok_or_else(|| {
                            format!("usage TCK case {case_name} append missing record")
                        })?,
                )?;
                let appended = ledger
                    .append(record)
                    .map_err(|error| usage_error(case_name, error))?;
                append_results.push(appended.record_id);
            }
            "reconcile" => {
                let reconciled = ledger
                    .reconcile(
                        required_str(operation, "sourceRecordId")?,
                        usage_amounts(operation, "amounts")?,
                        required_u64(operation, "occurredAtUnixMs")?,
                        optional_str(operation, "recordId").map(str::to_owned),
                    )
                    .map_err(|error| usage_error(case_name, error))?;
                append_results.push(reconciled.record_id);
            }
            other => {
                return Err(format!(
                    "usage TCK case {case_name} has unknown operation {other}"
                ));
            }
        }
    }

    let expected_value = Value::Object(expected.clone());
    let run_id = required_str(&expected_value, "runId")?;
    let record_ids = ledger
        .records_for_run(run_id)
        .iter()
        .map(|record| record.record_id.clone())
        .collect::<Vec<_>>();
    let totals = ledger
        .totals_for_run(run_id)
        .iter()
        .map(usage_amount_contract)
        .collect::<Vec<_>>();

    assert_eq!(
        append_results,
        string_array(expected, "appendResults")?,
        "{case_name}",
    );
    assert_eq!(
        record_ids,
        string_array(expected, "recordIds")?,
        "{case_name}"
    );
    assert_eq!(
        Value::Array(totals),
        expected
            .get("totals")
            .cloned()
            .ok_or_else(|| format!("usage TCK case {case_name} missing expected totals"))?,
        "{case_name}",
    );

    Ok(())
}

fn usage_record(record: &Map<String, Value>) -> Result<UsageRecord, String> {
    let record_value = Value::Object(record.clone());
    let mut usage_record = UsageRecord::new(
        required_str(&record_value, "recordId")?,
        usage_source(required_str(&record_value, "source")?)?,
        usage_confidence(required_str(&record_value, "confidence")?)?,
        usage_amounts(&record_value, "amounts")?,
        required_u64(&record_value, "occurredAtUnixMs")?,
    );
    if let Some(run_id) = optional_str(&record_value, "runId") {
        usage_record = usage_record.with_run_id(run_id);
    }
    if let Some(attempt_id) = optional_str(&record_value, "attemptId") {
        usage_record = usage_record.with_attempt_id(attempt_id);
    }
    if let Some(provider_response_id) = optional_str(&record_value, "providerResponseId") {
        usage_record = usage_record.with_provider_response_id(provider_response_id);
    }
    if let Some(pricing_ref) = optional_str(&record_value, "pricingRef") {
        usage_record = usage_record.with_pricing_ref(pricing_ref);
    }
    if let Some(quota_window_id) = optional_str(&record_value, "quotaWindowId") {
        usage_record = usage_record.with_quota_window_id(quota_window_id);
    }
    if let Some(execution_scope) = optional_str(&record_value, "executionScope") {
        usage_record = usage_record.with_execution_scope(execution_scope);
    }
    if let Some(metadata) = record.get("metadata") {
        let metadata = metadata
            .as_object()
            .ok_or_else(|| "usage record metadata must be an object".to_owned())?;
        for (key, value) in metadata {
            usage_record = usage_record.with_metadata(
                key,
                value
                    .as_str()
                    .ok_or_else(|| "usage record metadata values must be strings".to_owned())?,
            );
        }
    }
    Ok(usage_record)
}

fn usage_amounts(value: &Value, key: &str) -> Result<Vec<UsageAmount>, String> {
    value
        .get(key)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("missing usage amounts array {key}"))?
        .iter()
        .map(|amount| {
            let mut usage_amount = UsageAmount::new(
                required_str(amount, "kind")?,
                required_i64(amount, "amount")?,
                required_str(amount, "unit")?,
            );
            if let Some(dimensions) = amount.get("dimensions") {
                let dimensions = dimensions
                    .as_object()
                    .ok_or_else(|| "usage amount dimensions must be an object".to_owned())?;
                for (key, value) in dimensions {
                    usage_amount = usage_amount.with_dimension(
                        key,
                        value.as_str().ok_or_else(|| {
                            "usage amount dimension values must be strings".to_owned()
                        })?,
                    );
                }
            }
            Ok(usage_amount)
        })
        .collect()
}

fn usage_amount_contract(amount: &UsageAmount) -> Value {
    json!({
        "kind": amount.kind,
        "amount": amount.amount,
        "unit": amount.unit,
        "dimensions": amount.dimensions,
    })
}

fn usage_source(source: &str) -> Result<UsageSource, String> {
    match source {
        "provider_reported" => Ok(UsageSource::ProviderReported),
        "runtime_measured" => Ok(UsageSource::RuntimeMeasured),
        "tokenizer_estimated" => Ok(UsageSource::TokenizerEstimated),
        "pricing_estimated" => Ok(UsageSource::PricingEstimated),
        "reconciled" => Ok(UsageSource::Reconciled),
        other => Err(format!("unknown usage source {other}")),
    }
}

fn usage_confidence(confidence: &str) -> Result<UsageConfidence, String> {
    match confidence {
        "exact" => Ok(UsageConfidence::Exact),
        "provider_exact" => Ok(UsageConfidence::ProviderExact),
        "estimated" => Ok(UsageConfidence::Estimated),
        "unknown" => Ok(UsageConfidence::Unknown),
        other => Err(format!("unknown usage confidence {other}")),
    }
}

fn usage_error(case_name: &str, error: UsageLedgerError) -> String {
    format!("usage TCK case {case_name} failed: {error:?}")
}

fn required_str<'a>(value: &'a Value, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing required string field {key}"))
}

fn optional_str<'a>(value: &'a Value, key: &str) -> Option<&'a str> {
    value.get(key).and_then(Value::as_str)
}

fn required_i64(value: &Value, key: &str) -> Result<i64, String> {
    value
        .get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("missing required i64 field {key}"))
}

fn required_u64(value: &Value, key: &str) -> Result<u64, String> {
    value
        .get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("missing required u64 field {key}"))
}

fn string_array(value: &Map<String, Value>, key: &str) -> Result<Vec<String>, String> {
    value
        .get(key)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("missing string array {key}"))?
        .iter()
        .map(|item| {
            item.as_str()
                .map(str::to_owned)
                .ok_or_else(|| format!("string array {key} contains non-string value"))
        })
        .collect()
}
