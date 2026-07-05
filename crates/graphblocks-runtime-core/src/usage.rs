use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};

use rusqlite::{Connection, OptionalExtension, Row, params};
use serde_json::{Map, Number, Value};

type UsageTotalsKey = (String, String, Vec<(String, String)>);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum UsageSource {
    ProviderReported,
    RuntimeMeasured,
    TokenizerEstimated,
    PricingEstimated,
    Reconciled,
}

impl UsageSource {
    fn as_str(self) -> &'static str {
        match self {
            Self::ProviderReported => "provider_reported",
            Self::RuntimeMeasured => "runtime_measured",
            Self::TokenizerEstimated => "tokenizer_estimated",
            Self::PricingEstimated => "pricing_estimated",
            Self::Reconciled => "reconciled",
        }
    }

    fn from_str(source: &str) -> Option<Self> {
        match source {
            "provider_reported" => Some(Self::ProviderReported),
            "runtime_measured" => Some(Self::RuntimeMeasured),
            "tokenizer_estimated" => Some(Self::TokenizerEstimated),
            "pricing_estimated" => Some(Self::PricingEstimated),
            "reconciled" => Some(Self::Reconciled),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum UsageConfidence {
    Exact,
    ProviderExact,
    Estimated,
    Unknown,
}

impl UsageConfidence {
    fn as_str(self) -> &'static str {
        match self {
            Self::Exact => "exact",
            Self::ProviderExact => "provider_exact",
            Self::Estimated => "estimated",
            Self::Unknown => "unknown",
        }
    }

    fn from_str(confidence: &str) -> Option<Self> {
        match confidence {
            "exact" => Some(Self::Exact),
            "provider_exact" => Some(Self::ProviderExact),
            "estimated" => Some(Self::Estimated),
            "unknown" => Some(Self::Unknown),
            _ => None,
        }
    }
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
    pub quota_window_id: Option<String>,
    pub execution_scope: Option<String>,
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
            quota_window_id: None,
            execution_scope: None,
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

    pub fn with_quota_window_id(mut self, quota_window_id: impl Into<String>) -> Self {
        self.quota_window_id = Some(quota_window_id.into());
        self
    }

    pub fn with_execution_scope(mut self, execution_scope: impl Into<String>) -> Self {
        self.execution_scope = Some(execution_scope.into());
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
    InvalidRecord { message: String },
    Storage { message: String },
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
        validate_usage_record(&record)?;

        if let Some(existing) = self.records.get(&record.record_id) {
            if existing == &record {
                return Ok(existing.clone());
            }
            return Err(UsageLedgerError::RecordConflict {
                record_id: record.record_id,
            });
        }

        if let Some(reconciliation_of) = &record.reconciliation_of
            && self.reconciliation_for(reconciliation_of).is_some()
        {
            return Err(UsageLedgerError::RecordConflict {
                record_id: reconciliation_of.clone(),
            });
        }

        if record.reconciliation_of.is_none()
            && let Some(provider_response_id) = &record.provider_response_id
        {
            let dedupe_key = (provider_response_id.clone(), record.attempt_id.clone());
            if let Some(existing_id) = self.provider_dedupe.get(&dedupe_key) {
                let existing = self
                    .records
                    .get(existing_id)
                    .expect("provider dedupe index points to an existing usage record");
                if usage_provider_duplicate_conflict(existing, &record) {
                    return Err(UsageLedgerError::RecordConflict {
                        record_id: provider_response_id.clone(),
                    });
                }
                return Ok(existing.clone());
            }
        }

        if record.reconciliation_of.is_none()
            && let Some(provider_response_id) = &record.provider_response_id
        {
            self.provider_dedupe.insert(
                (provider_response_id.clone(), record.attempt_id.clone()),
                record.record_id.clone(),
            );
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

    pub fn totals_for_run(&self, run_id: impl AsRef<str>) -> Vec<UsageAmount> {
        usage_totals(&self.records_for_run(run_id))
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
        if occurred_at_unix_ms < original.occurred_at_unix_ms {
            return Err(UsageLedgerError::InvalidRecord {
                message: "usage reconciliation occurred_at must not precede source usage"
                    .to_string(),
            });
        }
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
            quota_window_id: original.quota_window_id,
            execution_scope: original.execution_scope,
            reconciliation_of: Some(original.record_id),
            metadata: original.metadata,
        };

        self.append(reconciled)
    }

    fn reconciliation_for(&self, source_record_id: &str) -> Option<UsageRecord> {
        self.order
            .iter()
            .filter_map(|record_id| self.records.get(record_id))
            .find(|record| record.reconciliation_of.as_deref() == Some(source_record_id))
            .cloned()
    }
}

pub struct SqliteUsageLedger {
    connection: Connection,
}

impl SqliteUsageLedger {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, UsageLedgerError> {
        let connection = Connection::open(path).map_err(usage_storage_error)?;
        let ledger = Self { connection };
        ledger.initialize()?;
        Ok(ledger)
    }

    pub fn open_in_memory() -> Result<Self, UsageLedgerError> {
        let connection = Connection::open_in_memory().map_err(usage_storage_error)?;
        let ledger = Self { connection };
        ledger.initialize()?;
        Ok(ledger)
    }

    fn initialize(&self) -> Result<(), UsageLedgerError> {
        self.connection
            .execute_batch(
                "
                CREATE TABLE IF NOT EXISTS usage_records (
                    sequence INTEGER PRIMARY KEY,
                    record_id TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    amounts_json TEXT NOT NULL,
                    occurred_at_unix_ms INTEGER NOT NULL,
                    run_id TEXT,
                    attempt_id TEXT,
                    provider_response_id TEXT,
                    pricing_ref TEXT,
                    quota_window_id TEXT,
                    execution_scope TEXT,
                    reconciliation_of TEXT,
                    metadata_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS usage_records_run_id
                    ON usage_records(run_id, sequence);
                CREATE INDEX IF NOT EXISTS usage_records_provider_response
                    ON usage_records(provider_response_id, attempt_id, sequence)
                    WHERE provider_response_id IS NOT NULL AND reconciliation_of IS NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS usage_records_provider_dedupe_with_attempt
                    ON usage_records(provider_response_id, attempt_id)
                    WHERE provider_response_id IS NOT NULL
                        AND attempt_id IS NOT NULL
                        AND reconciliation_of IS NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS usage_records_provider_dedupe_without_attempt
                    ON usage_records(provider_response_id)
                    WHERE provider_response_id IS NOT NULL
                        AND attempt_id IS NULL
                        AND reconciliation_of IS NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS usage_records_single_reconciliation
                    ON usage_records(reconciliation_of)
                    WHERE reconciliation_of IS NOT NULL;
                ",
            )
            .map_err(usage_storage_error)?;
        self.ensure_usage_record_column("quota_window_id")?;
        self.ensure_usage_record_column("execution_scope")?;
        Ok(())
    }

    fn ensure_usage_record_column(&self, column: &'static str) -> Result<(), UsageLedgerError> {
        let mut statement = self
            .connection
            .prepare("PRAGMA table_info(usage_records)")
            .map_err(usage_storage_error)?;
        let rows = statement
            .query_map([], |row| row.get::<_, String>(1))
            .map_err(usage_storage_error)?;
        for row in rows {
            if row.map_err(usage_storage_error)? == column {
                return Ok(());
            }
        }

        let alter_sql = match column {
            "quota_window_id" => "ALTER TABLE usage_records ADD COLUMN quota_window_id TEXT",
            "execution_scope" => "ALTER TABLE usage_records ADD COLUMN execution_scope TEXT",
            _ => {
                return Err(UsageLedgerError::Storage {
                    message: format!("unsupported usage record column migration {column:?}"),
                });
            }
        };
        self.connection
            .execute(alter_sql, [])
            .map_err(usage_storage_error)?;
        Ok(())
    }

    pub fn append(&mut self, record: UsageRecord) -> Result<UsageRecord, UsageLedgerError> {
        validate_usage_record(&record)?;

        match self.get(&record.record_id) {
            Ok(existing) => {
                if existing == record {
                    return Ok(existing);
                }
                return Err(UsageLedgerError::RecordConflict {
                    record_id: record.record_id,
                });
            }
            Err(UsageLedgerError::RecordNotFound { .. }) => {}
            Err(error) => return Err(error),
        }

        if let Some(reconciliation_of) = &record.reconciliation_of
            && self.reconciliation_for(reconciliation_of)?.is_some()
        {
            return Err(UsageLedgerError::RecordConflict {
                record_id: reconciliation_of.clone(),
            });
        }

        if record.reconciliation_of.is_none()
            && let Some(provider_response_id) = &record.provider_response_id
            && let Some(existing) =
                self.provider_duplicate(provider_response_id, record.attempt_id.as_deref())?
        {
            if usage_provider_duplicate_conflict(&existing, &record) {
                return Err(UsageLedgerError::RecordConflict {
                    record_id: provider_response_id.clone(),
                });
            }
            return Ok(existing);
        }

        let transaction = self.connection.transaction().map_err(usage_storage_error)?;
        let next_sequence = transaction
            .query_row(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM usage_records",
                [],
                |row| row.get::<_, i64>(0),
            )
            .map_err(usage_storage_error)?;
        transaction
            .execute(
                "
                INSERT INTO usage_records (
                    sequence,
                    record_id,
                    source,
                    confidence,
                    amounts_json,
                    occurred_at_unix_ms,
                    run_id,
                    attempt_id,
                    provider_response_id,
                    pricing_ref,
                    quota_window_id,
                    execution_scope,
                    reconciliation_of,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ",
                params![
                    next_sequence,
                    &record.record_id,
                    record.source.as_str(),
                    record.confidence.as_str(),
                    usage_amounts_json(&record.amounts)?,
                    usage_u64_to_i64(record.occurred_at_unix_ms, "usage occurred_at")?,
                    &record.run_id,
                    &record.attempt_id,
                    &record.provider_response_id,
                    &record.pricing_ref,
                    &record.quota_window_id,
                    &record.execution_scope,
                    &record.reconciliation_of,
                    string_map_json(&record.metadata)?,
                ],
            )
            .map_err(usage_storage_error)?;
        transaction.commit().map_err(usage_storage_error)?;
        Ok(record)
    }

    pub fn get(&self, record_id: impl AsRef<str>) -> Result<UsageRecord, UsageLedgerError> {
        let record_id = record_id.as_ref();
        self.connection
            .query_row(
                "
                SELECT
                    record_id,
                    source,
                    confidence,
                    amounts_json,
                    occurred_at_unix_ms,
                    run_id,
                    attempt_id,
                    provider_response_id,
                    pricing_ref,
                    quota_window_id,
                    execution_scope,
                    reconciliation_of,
                    metadata_json
                FROM usage_records
                WHERE record_id = ?
                ",
                params![record_id],
                stored_usage_record_from_row,
            )
            .optional()
            .map_err(usage_storage_error)?
            .map(usage_record_from_storage)
            .transpose()?
            .ok_or_else(|| UsageLedgerError::RecordNotFound {
                record_id: record_id.to_string(),
            })
    }

    pub fn records_for_run(
        &self,
        run_id: impl AsRef<str>,
    ) -> Result<Vec<UsageRecord>, UsageLedgerError> {
        let mut statement = self
            .connection
            .prepare(
                "
                SELECT
                    record_id,
                    source,
                    confidence,
                    amounts_json,
                    occurred_at_unix_ms,
                    run_id,
                    attempt_id,
                    provider_response_id,
                    pricing_ref,
                    quota_window_id,
                    execution_scope,
                    reconciliation_of,
                    metadata_json
                FROM usage_records
                WHERE run_id = ?
                ORDER BY sequence
                ",
            )
            .map_err(usage_storage_error)?;
        let rows = statement
            .query_map(params![run_id.as_ref()], stored_usage_record_from_row)
            .map_err(usage_storage_error)?;
        let mut records = Vec::new();
        for row in rows {
            records.push(usage_record_from_storage(
                row.map_err(usage_storage_error)?,
            )?);
        }
        Ok(records)
    }

    pub fn totals_for_run(
        &self,
        run_id: impl AsRef<str>,
    ) -> Result<Vec<UsageAmount>, UsageLedgerError> {
        Ok(usage_totals(&self.records_for_run(run_id)?))
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
        if occurred_at_unix_ms < original.occurred_at_unix_ms {
            return Err(UsageLedgerError::InvalidRecord {
                message: "usage reconciliation occurred_at must not precede source usage"
                    .to_string(),
            });
        }
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
            quota_window_id: original.quota_window_id,
            execution_scope: original.execution_scope,
            reconciliation_of: Some(original.record_id),
            metadata: original.metadata,
        };

        self.append(reconciled)
    }

    fn reconciliation_for(
        &self,
        source_record_id: &str,
    ) -> Result<Option<UsageRecord>, UsageLedgerError> {
        self.connection
            .query_row(
                "
                SELECT
                    record_id,
                    source,
                    confidence,
                    amounts_json,
                    occurred_at_unix_ms,
                    run_id,
                    attempt_id,
                    provider_response_id,
                    pricing_ref,
                    quota_window_id,
                    execution_scope,
                    reconciliation_of,
                    metadata_json
                FROM usage_records
                WHERE reconciliation_of = ?
                ORDER BY sequence
                LIMIT 1
                ",
                params![source_record_id],
                stored_usage_record_from_row,
            )
            .optional()
            .map_err(usage_storage_error)?
            .map(usage_record_from_storage)
            .transpose()
    }

    fn provider_duplicate(
        &self,
        provider_response_id: &str,
        attempt_id: Option<&str>,
    ) -> Result<Option<UsageRecord>, UsageLedgerError> {
        self.connection
            .query_row(
                "
                SELECT
                    record_id,
                    source,
                    confidence,
                    amounts_json,
                    occurred_at_unix_ms,
                    run_id,
                    attempt_id,
                    provider_response_id,
                    pricing_ref,
                    quota_window_id,
                    execution_scope,
                    reconciliation_of,
                    metadata_json
                FROM usage_records
                WHERE provider_response_id = ?
                    AND ((attempt_id IS NULL AND ? IS NULL) OR attempt_id = ?)
                    AND reconciliation_of IS NULL
                ORDER BY sequence
                LIMIT 1
                ",
                params![provider_response_id, attempt_id, attempt_id],
                stored_usage_record_from_row,
            )
            .optional()
            .map_err(usage_storage_error)?
            .map(usage_record_from_storage)
            .transpose()
    }
}

fn validate_usage_record(record: &UsageRecord) -> Result<(), UsageLedgerError> {
    if record.record_id.trim().is_empty() {
        return Err(UsageLedgerError::InvalidRecord {
            message: "usage record_id must not be empty".to_string(),
        });
    }
    if record.occurred_at_unix_ms == 0 {
        return Err(UsageLedgerError::InvalidRecord {
            message: "usage occurred_at_unix_ms must be positive".to_string(),
        });
    }
    for (field, value) in [
        ("run_id", record.run_id.as_deref()),
        ("attempt_id", record.attempt_id.as_deref()),
        (
            "provider_response_id",
            record.provider_response_id.as_deref(),
        ),
        ("pricing_ref", record.pricing_ref.as_deref()),
        ("quota_window_id", record.quota_window_id.as_deref()),
        ("execution_scope", record.execution_scope.as_deref()),
        ("reconciliation_of", record.reconciliation_of.as_deref()),
    ] {
        if value.is_some_and(|value| value.trim().is_empty()) {
            return Err(UsageLedgerError::InvalidRecord {
                message: format!("usage {field} must not be empty"),
            });
        }
    }
    if record.amounts.is_empty() {
        return Err(UsageLedgerError::InvalidRecord {
            message: "usage amounts must not be empty".to_string(),
        });
    }
    for amount in &record.amounts {
        if amount.amount < 0 {
            return Err(UsageLedgerError::InvalidRecord {
                message: "usage amount must be non-negative".to_string(),
            });
        }
        if amount.kind.trim().is_empty() {
            return Err(UsageLedgerError::InvalidRecord {
                message: "usage amount kind must not be empty".to_string(),
            });
        }
        if amount.unit.trim().is_empty() {
            return Err(UsageLedgerError::InvalidRecord {
                message: "usage amount unit must not be empty".to_string(),
            });
        }
        for (key, value) in &amount.dimensions {
            if key.trim().is_empty() {
                return Err(UsageLedgerError::InvalidRecord {
                    message: "usage amount dimension keys must not be empty".to_string(),
                });
            }
            if value.trim().is_empty() {
                return Err(UsageLedgerError::InvalidRecord {
                    message: "usage amount dimension values must not be empty".to_string(),
                });
            }
        }
    }
    for (key, value) in &record.metadata {
        if key.trim().is_empty() {
            return Err(UsageLedgerError::InvalidRecord {
                message: "usage metadata keys must not be empty".to_string(),
            });
        }
        if value.trim().is_empty() {
            return Err(UsageLedgerError::InvalidRecord {
                message: "usage metadata values must not be empty".to_string(),
            });
        }
    }
    Ok(())
}

fn usage_provider_duplicate_conflict(existing: &UsageRecord, incoming: &UsageRecord) -> bool {
    existing.source != incoming.source
        || existing.confidence != incoming.confidence
        || existing.amounts != incoming.amounts
        || existing.run_id != incoming.run_id
        || existing.attempt_id != incoming.attempt_id
        || existing.provider_response_id != incoming.provider_response_id
        || existing.pricing_ref != incoming.pricing_ref
        || existing.quota_window_id != incoming.quota_window_id
        || existing.execution_scope != incoming.execution_scope
        || existing.reconciliation_of != incoming.reconciliation_of
        || existing.metadata != incoming.metadata
}

fn usage_totals(records: &[UsageRecord]) -> Vec<UsageAmount> {
    let superseded_record_ids = records
        .iter()
        .filter_map(|record| record.reconciliation_of.clone())
        .collect::<BTreeSet<_>>();
    let mut totals: BTreeMap<UsageTotalsKey, i64> = BTreeMap::new();
    for record in records {
        if superseded_record_ids.contains(&record.record_id) {
            continue;
        }
        for amount in &record.amounts {
            let key = (
                amount.kind.clone(),
                amount.unit.clone(),
                amount
                    .dimensions
                    .iter()
                    .map(|(key, value)| (key.clone(), value.clone()))
                    .collect::<Vec<_>>(),
            );
            *totals.entry(key).or_insert(0) += amount.amount;
        }
    }
    totals
        .into_iter()
        .filter_map(|((kind, unit, dimensions), amount)| {
            if amount == 0 {
                return None;
            }
            Some(UsageAmount {
                kind,
                amount,
                unit,
                dimensions: dimensions.into_iter().collect(),
            })
        })
        .collect()
}

struct StoredUsageRecord {
    record_id: String,
    source: String,
    confidence: String,
    amounts_json: String,
    occurred_at_unix_ms: i64,
    run_id: Option<String>,
    attempt_id: Option<String>,
    provider_response_id: Option<String>,
    pricing_ref: Option<String>,
    quota_window_id: Option<String>,
    execution_scope: Option<String>,
    reconciliation_of: Option<String>,
    metadata_json: String,
}

fn stored_usage_record_from_row(row: &Row<'_>) -> rusqlite::Result<StoredUsageRecord> {
    Ok(StoredUsageRecord {
        record_id: row.get(0)?,
        source: row.get(1)?,
        confidence: row.get(2)?,
        amounts_json: row.get(3)?,
        occurred_at_unix_ms: row.get(4)?,
        run_id: row.get(5)?,
        attempt_id: row.get(6)?,
        provider_response_id: row.get(7)?,
        pricing_ref: row.get(8)?,
        quota_window_id: row.get(9)?,
        execution_scope: row.get(10)?,
        reconciliation_of: row.get(11)?,
        metadata_json: row.get(12)?,
    })
}

fn usage_record_from_storage(stored: StoredUsageRecord) -> Result<UsageRecord, UsageLedgerError> {
    let source =
        UsageSource::from_str(&stored.source).ok_or_else(|| UsageLedgerError::Storage {
            message: format!("unknown usage source {:?}", stored.source),
        })?;
    let confidence =
        UsageConfidence::from_str(&stored.confidence).ok_or_else(|| UsageLedgerError::Storage {
            message: format!("unknown usage confidence {:?}", stored.confidence),
        })?;

    Ok(UsageRecord {
        record_id: stored.record_id,
        source,
        confidence,
        amounts: usage_amounts_from_json(&stored.amounts_json)?,
        occurred_at_unix_ms: usage_i64_to_u64(stored.occurred_at_unix_ms, "usage occurred_at")?,
        run_id: stored.run_id,
        attempt_id: stored.attempt_id,
        provider_response_id: stored.provider_response_id,
        pricing_ref: stored.pricing_ref,
        quota_window_id: stored.quota_window_id,
        execution_scope: stored.execution_scope,
        reconciliation_of: stored.reconciliation_of,
        metadata: string_map_from_json(&stored.metadata_json)?,
    })
}

fn usage_amounts_json(amounts: &[UsageAmount]) -> Result<String, UsageLedgerError> {
    let values = amounts
        .iter()
        .map(|amount| {
            let mut value = Map::new();
            value.insert("kind".to_string(), Value::String(amount.kind.clone()));
            value.insert(
                "amount".to_string(),
                Value::Number(Number::from(amount.amount)),
            );
            value.insert("unit".to_string(), Value::String(amount.unit.clone()));
            value.insert(
                "dimensions".to_string(),
                string_map_value(&amount.dimensions),
            );
            Value::Object(value)
        })
        .collect::<Vec<_>>();
    serde_json::to_string(&values).map_err(usage_storage_error)
}

fn usage_amounts_from_json(value: &str) -> Result<Vec<UsageAmount>, UsageLedgerError> {
    let Value::Array(values) = serde_json::from_str::<Value>(value).map_err(usage_storage_error)?
    else {
        return Err(UsageLedgerError::Storage {
            message: "usage amounts must be an array".to_string(),
        });
    };

    let mut amounts = Vec::new();
    for value in values {
        let Value::Object(mut object) = value else {
            return Err(UsageLedgerError::Storage {
                message: "usage amount must be an object".to_string(),
            });
        };
        let Some(kind) = object.remove("kind").and_then(|value| match value {
            Value::String(value) => Some(value),
            _ => None,
        }) else {
            return Err(UsageLedgerError::Storage {
                message: "usage amount kind must be a string".to_string(),
            });
        };
        let Some(amount) = object.remove("amount").and_then(|value| value.as_i64()) else {
            return Err(UsageLedgerError::Storage {
                message: "usage amount must be an integer".to_string(),
            });
        };
        let Some(unit) = object.remove("unit").and_then(|value| match value {
            Value::String(value) => Some(value),
            _ => None,
        }) else {
            return Err(UsageLedgerError::Storage {
                message: "usage amount unit must be a string".to_string(),
            });
        };
        let dimensions = match object.remove("dimensions") {
            Some(value) => string_map_from_value(value)?,
            None => BTreeMap::new(),
        };
        amounts.push(UsageAmount {
            kind,
            amount,
            unit,
            dimensions,
        });
    }
    Ok(amounts)
}

fn string_map_json(value: &BTreeMap<String, String>) -> Result<String, UsageLedgerError> {
    serde_json::to_string(&string_map_value(value)).map_err(usage_storage_error)
}

fn string_map_value(value: &BTreeMap<String, String>) -> Value {
    Value::Object(
        value
            .iter()
            .map(|(key, value)| (key.clone(), Value::String(value.clone())))
            .collect(),
    )
}

fn string_map_from_json(value: &str) -> Result<BTreeMap<String, String>, UsageLedgerError> {
    let value = serde_json::from_str(value).map_err(usage_storage_error)?;
    string_map_from_value(value)
}

fn string_map_from_value(value: Value) -> Result<BTreeMap<String, String>, UsageLedgerError> {
    let Value::Object(object) = value else {
        return Err(UsageLedgerError::Storage {
            message: "string map must be an object".to_string(),
        });
    };
    let mut result = BTreeMap::new();
    for (key, value) in object {
        let Value::String(value) = value else {
            return Err(UsageLedgerError::Storage {
                message: format!("string map value for {key:?} must be a string"),
            });
        };
        result.insert(key, value);
    }
    Ok(result)
}

fn usage_u64_to_i64(value: u64, label: &'static str) -> Result<i64, UsageLedgerError> {
    i64::try_from(value).map_err(|_| UsageLedgerError::Storage {
        message: format!("{label} exceeds SQLite integer range"),
    })
}

fn usage_i64_to_u64(value: i64, label: &'static str) -> Result<u64, UsageLedgerError> {
    u64::try_from(value).map_err(|_| UsageLedgerError::Storage {
        message: format!("{label} must be non-negative"),
    })
}

fn usage_storage_error(error: impl std::fmt::Display) -> UsageLedgerError {
    UsageLedgerError::Storage {
        message: error.to_string(),
    }
}
