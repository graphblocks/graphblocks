use std::collections::BTreeMap;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum UsageSource {
    ProviderReported,
    RuntimeMeasured,
    TokenizerEstimated,
    PricingEstimated,
    Reconciled,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum UsageConfidence {
    Exact,
    ProviderExact,
    Estimated,
    Unknown,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct UsageAmount {
    pub kind: String,
    pub amount: i64,
    pub unit: String,
    pub dimensions: BTreeMap<String, String>,
}

impl UsageAmount {
    pub fn new(kind: impl Into<String>, amount: i64, unit: impl Into<String>) -> Self {
        Self {
            kind: kind.into(),
            amount,
            unit: unit.into(),
            dimensions: BTreeMap::new(),
        }
    }

    pub fn with_dimension(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.dimensions.insert(key.into(), value.into());
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct UsageRecord {
    pub record_id: String,
    pub source: UsageSource,
    pub confidence: UsageConfidence,
    pub amounts: Vec<UsageAmount>,
    pub occurred_at_unix_ms: u64,
    pub run_id: Option<String>,
    pub attempt_id: Option<String>,
    pub provider_response_id: Option<String>,
    pub pricing_ref: Option<String>,
    pub reconciliation_of: Option<String>,
    pub metadata: BTreeMap<String, String>,
}

impl UsageRecord {
    pub fn new(
        record_id: impl Into<String>,
        source: UsageSource,
        confidence: UsageConfidence,
        amounts: impl IntoIterator<Item = UsageAmount>,
        occurred_at_unix_ms: u64,
    ) -> Self {
        Self {
            record_id: record_id.into(),
            source,
            confidence,
            amounts: amounts.into_iter().collect(),
            occurred_at_unix_ms,
            run_id: None,
            attempt_id: None,
            provider_response_id: None,
            pricing_ref: None,
            reconciliation_of: None,
            metadata: BTreeMap::new(),
        }
    }

    pub fn with_run_id(mut self, run_id: impl Into<String>) -> Self {
        self.run_id = Some(run_id.into());
        self
    }

    pub fn with_attempt_id(mut self, attempt_id: impl Into<String>) -> Self {
        self.attempt_id = Some(attempt_id.into());
        self
    }

    pub fn with_provider_response_id(mut self, provider_response_id: impl Into<String>) -> Self {
        self.provider_response_id = Some(provider_response_id.into());
        self
    }

    pub fn with_pricing_ref(mut self, pricing_ref: impl Into<String>) -> Self {
        self.pricing_ref = Some(pricing_ref.into());
        self
    }

    pub fn with_metadata(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.metadata.insert(key.into(), value.into());
        self
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum UsageLedgerError {
    RecordNotFound { record_id: String },
    RecordConflict { record_id: String },
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct InMemoryUsageLedger {
    records: BTreeMap<String, UsageRecord>,
    order: Vec<String>,
    provider_dedupe: BTreeMap<(String, Option<String>), String>,
}

impl InMemoryUsageLedger {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn append(&mut self, record: UsageRecord) -> Result<UsageRecord, UsageLedgerError> {
        if record.reconciliation_of.is_none() {
            if let Some(provider_response_id) = &record.provider_response_id {
                let dedupe_key = (provider_response_id.clone(), record.attempt_id.clone());
                if let Some(existing_id) = self.provider_dedupe.get(&dedupe_key) {
                    return Ok(self
                        .records
                        .get(existing_id)
                        .expect("provider dedupe index points to an existing usage record")
                        .clone());
                }
            }
        }

        if self.records.contains_key(&record.record_id) {
            return Err(UsageLedgerError::RecordConflict {
                record_id: record.record_id,
            });
        }

        if record.reconciliation_of.is_none() {
            if let Some(provider_response_id) = &record.provider_response_id {
                self.provider_dedupe.insert(
                    (provider_response_id.clone(), record.attempt_id.clone()),
                    record.record_id.clone(),
                );
            }
        }

        self.order.push(record.record_id.clone());
        self.records
            .insert(record.record_id.clone(), record.clone());
        Ok(record)
    }

    pub fn get(&self, record_id: impl AsRef<str>) -> Result<UsageRecord, UsageLedgerError> {
        let record_id = record_id.as_ref();
        self.records
            .get(record_id)
            .cloned()
            .ok_or_else(|| UsageLedgerError::RecordNotFound {
                record_id: record_id.to_string(),
            })
    }

    pub fn records_for_run(&self, run_id: impl AsRef<str>) -> Vec<UsageRecord> {
        let run_id = run_id.as_ref();
        self.order
            .iter()
            .filter_map(|record_id| self.records.get(record_id))
            .filter(|record| record.run_id.as_deref() == Some(run_id))
            .cloned()
            .collect()
    }

    pub fn reconcile(
        &mut self,
        source_record_id: impl AsRef<str>,
        amounts: impl IntoIterator<Item = UsageAmount>,
        occurred_at_unix_ms: u64,
        record_id: Option<String>,
    ) -> Result<UsageRecord, UsageLedgerError> {
        let source_record_id = source_record_id.as_ref();
        let original = self.get(source_record_id)?;
        let reconciled = UsageRecord {
            record_id: record_id.unwrap_or_else(|| format!("{source_record_id}:reconciled")),
            source: UsageSource::Reconciled,
            confidence: UsageConfidence::Exact,
            amounts: amounts.into_iter().collect(),
            occurred_at_unix_ms,
            run_id: original.run_id,
            attempt_id: original.attempt_id,
            provider_response_id: original.provider_response_id,
            pricing_ref: original.pricing_ref,
            reconciliation_of: Some(original.record_id),
            metadata: BTreeMap::new(),
        };

        self.append(reconciled)
    }
}
