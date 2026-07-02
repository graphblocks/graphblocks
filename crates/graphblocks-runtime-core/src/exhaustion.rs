use std::collections::{BTreeMap, BTreeSet};

use crate::budget::{BudgetPermit, UsageAmount};

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum WorkKind {
    CurrentProviderCall,
    AlreadyAdmittedChildWork,
    DeclaredFinalization,
    Checkpoint,
    Cleanup,
    ReadOnlyTool,
    NewTurn,
    PlanExpansion,
    OptionalTask,
    NewTrial,
    StateChangingEffect,
    UnreservedProviderCall,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ExhaustionPreset {
    FinishCurrentTurn,
    FinishCurrentCall,
    FinishCurrentStep,
    CheckpointAndPause,
    HardStop,
    DegradeThenFinalize,
    RequestExtension,
}

impl ExhaustionPreset {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::FinishCurrentTurn => "finish_current_turn",
            Self::FinishCurrentCall => "finish_current_call",
            Self::FinishCurrentStep => "finish_current_step",
            Self::CheckpointAndPause => "checkpoint_and_pause",
            Self::HardStop => "hard_stop",
            Self::DegradeThenFinalize => "degrade_then_finalize",
            Self::RequestExtension => "request_extension",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InFlightPolicy {
    FinishCurrentUnit,
    CheckpointThenPause,
    DegradeAndContinue,
    RequestTopupOrApproval,
    CancelImmediately,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ExhaustionUnit {
    ProviderCall,
    Node,
    AgentStep,
    Turn,
    MapItem,
    Task,
    Trial,
    Run,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ClientDelivery {
    StopImmediately,
    ContinueToBoundary,
    BufferUntilCommit,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DurableResult {
    None,
    Retract,
    MarkIncomplete,
    CommitPartial,
    CommitWithExhaustionNotice,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EffectPolicy {
    PreserveAtomicity,
    CancelIfSafe,
    FinishCommittingEffect,
    CompensateIfCommitted,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AfterUnitPolicy {
    Reject,
    Pause,
    Fallback,
    Close,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ContinuationEnvelope {
    pub allowed_work: BTreeSet<WorkKind>,
    pub forbidden_work: BTreeSet<WorkKind>,
    pub max_additional_usage: Vec<UsageAmount>,
    pub max_additional_steps: Option<u32>,
    pub deadline: Option<String>,
}

impl ContinuationEnvelope {
    pub fn new() -> Self {
        Self {
            allowed_work: BTreeSet::new(),
            forbidden_work: BTreeSet::new(),
            max_additional_usage: Vec::new(),
            max_additional_steps: None,
            deadline: None,
        }
    }

    pub fn with_allowed_work<I>(mut self, work: I) -> Self
    where
        I: IntoIterator<Item = WorkKind>,
    {
        self.allowed_work.extend(work);
        self
    }

    pub fn with_forbidden_work<I>(mut self, work: I) -> Self
    where
        I: IntoIterator<Item = WorkKind>,
    {
        self.forbidden_work.extend(work);
        self
    }

    pub fn with_max_additional_usage<I>(mut self, amounts: I) -> Self
    where
        I: IntoIterator<Item = UsageAmount>,
    {
        self.max_additional_usage = amounts.into_iter().collect();
        self
    }

    pub fn with_max_additional_steps(mut self, steps: u32) -> Self {
        self.max_additional_steps = Some(steps);
        self
    }

    pub fn with_deadline(mut self, deadline: impl Into<String>) -> Self {
        self.deadline = Some(deadline.into());
        self
    }

    pub fn is_bounded(&self) -> bool {
        !self.max_additional_usage.is_empty()
            || self.max_additional_steps.is_some()
            || self.deadline.is_some()
    }
}

impl Default for ContinuationEnvelope {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PartialOutputPolicy {
    pub client_delivery: ClientDelivery,
    pub durable_result: DurableResult,
}

impl Default for PartialOutputPolicy {
    fn default() -> Self {
        Self {
            client_delivery: ClientDelivery::StopImmediately,
            durable_result: DurableResult::MarkIncomplete,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExhaustionPolicy {
    pub preset: Option<ExhaustionPreset>,
    pub in_flight: InFlightPolicy,
    pub unit: ExhaustionUnit,
    pub deny_new_work: bool,
    pub continuation: Option<ContinuationEnvelope>,
    pub max_overdraft: Vec<UsageAmount>,
    pub deadline: Option<String>,
    pub output: PartialOutputPolicy,
    pub effects: EffectPolicy,
    pub after_unit: AfterUnitPolicy,
}

impl ExhaustionPolicy {
    pub fn from_preset(
        preset: ExhaustionPreset,
        unit: ExhaustionUnit,
        continuation: Option<ContinuationEnvelope>,
    ) -> Self {
        match preset {
            ExhaustionPreset::FinishCurrentTurn => {
                let mut envelope = continuation.unwrap_or_default();
                envelope.allowed_work.extend([
                    WorkKind::AlreadyAdmittedChildWork,
                    WorkKind::DeclaredFinalization,
                    WorkKind::Checkpoint,
                    WorkKind::Cleanup,
                ]);
                envelope.forbidden_work.extend([
                    WorkKind::NewTurn,
                    WorkKind::PlanExpansion,
                    WorkKind::OptionalTask,
                    WorkKind::StateChangingEffect,
                ]);
                Self {
                    preset: Some(preset),
                    in_flight: InFlightPolicy::FinishCurrentUnit,
                    unit,
                    deny_new_work: true,
                    continuation: Some(envelope),
                    max_overdraft: Vec::new(),
                    deadline: None,
                    output: PartialOutputPolicy {
                        client_delivery: ClientDelivery::ContinueToBoundary,
                        durable_result: DurableResult::CommitWithExhaustionNotice,
                    },
                    effects: EffectPolicy::PreserveAtomicity,
                    after_unit: AfterUnitPolicy::Reject,
                }
            }
            ExhaustionPreset::HardStop => {
                let mut envelope = continuation.unwrap_or_default();
                envelope.allowed_work.insert(WorkKind::Cleanup);
                envelope.forbidden_work.extend([
                    WorkKind::NewTurn,
                    WorkKind::PlanExpansion,
                    WorkKind::UnreservedProviderCall,
                    WorkKind::StateChangingEffect,
                ]);
                Self {
                    preset: Some(preset),
                    in_flight: InFlightPolicy::CancelImmediately,
                    unit,
                    deny_new_work: true,
                    continuation: Some(envelope),
                    max_overdraft: Vec::new(),
                    deadline: None,
                    output: PartialOutputPolicy::default(),
                    effects: EffectPolicy::PreserveAtomicity,
                    after_unit: AfterUnitPolicy::Reject,
                }
            }
            ExhaustionPreset::CheckpointAndPause => {
                let mut envelope = continuation.unwrap_or_default();
                envelope
                    .allowed_work
                    .extend([WorkKind::Checkpoint, WorkKind::Cleanup]);
                envelope.forbidden_work.extend([
                    WorkKind::NewTurn,
                    WorkKind::OptionalTask,
                    WorkKind::NewTrial,
                ]);
                Self {
                    preset: Some(preset),
                    in_flight: InFlightPolicy::CheckpointThenPause,
                    unit,
                    deny_new_work: true,
                    continuation: Some(envelope),
                    max_overdraft: Vec::new(),
                    deadline: None,
                    output: PartialOutputPolicy {
                        client_delivery: ClientDelivery::StopImmediately,
                        durable_result: DurableResult::CommitPartial,
                    },
                    effects: EffectPolicy::PreserveAtomicity,
                    after_unit: AfterUnitPolicy::Pause,
                }
            }
            ExhaustionPreset::DegradeThenFinalize => {
                let mut envelope = continuation.unwrap_or_default();
                envelope
                    .allowed_work
                    .extend([WorkKind::DeclaredFinalization, WorkKind::Cleanup]);
                envelope
                    .forbidden_work
                    .extend([WorkKind::StateChangingEffect, WorkKind::OptionalTask]);
                Self {
                    preset: Some(preset),
                    in_flight: InFlightPolicy::DegradeAndContinue,
                    unit,
                    deny_new_work: true,
                    continuation: Some(envelope),
                    max_overdraft: Vec::new(),
                    deadline: None,
                    output: PartialOutputPolicy {
                        client_delivery: ClientDelivery::ContinueToBoundary,
                        durable_result: DurableResult::CommitWithExhaustionNotice,
                    },
                    effects: EffectPolicy::PreserveAtomicity,
                    after_unit: AfterUnitPolicy::Fallback,
                }
            }
            ExhaustionPreset::RequestExtension => {
                let mut envelope = continuation.unwrap_or_default();
                envelope
                    .allowed_work
                    .extend([WorkKind::Checkpoint, WorkKind::Cleanup]);
                envelope.forbidden_work.extend([
                    WorkKind::NewTurn,
                    WorkKind::PlanExpansion,
                    WorkKind::OptionalTask,
                    WorkKind::NewTrial,
                ]);
                Self {
                    preset: Some(preset),
                    in_flight: InFlightPolicy::RequestTopupOrApproval,
                    unit,
                    deny_new_work: true,
                    continuation: Some(envelope),
                    max_overdraft: Vec::new(),
                    deadline: None,
                    output: PartialOutputPolicy {
                        client_delivery: ClientDelivery::StopImmediately,
                        durable_result: DurableResult::CommitPartial,
                    },
                    effects: EffectPolicy::PreserveAtomicity,
                    after_unit: AfterUnitPolicy::Pause,
                }
            }
            ExhaustionPreset::FinishCurrentCall | ExhaustionPreset::FinishCurrentStep => {
                let mut envelope = continuation.unwrap_or_default();
                envelope
                    .allowed_work
                    .extend([WorkKind::CurrentProviderCall, WorkKind::Cleanup]);
                Self {
                    preset: Some(preset),
                    in_flight: InFlightPolicy::FinishCurrentUnit,
                    unit,
                    deny_new_work: true,
                    continuation: Some(envelope),
                    max_overdraft: Vec::new(),
                    deadline: None,
                    output: PartialOutputPolicy::default(),
                    effects: EffectPolicy::PreserveAtomicity,
                    after_unit: AfterUnitPolicy::Reject,
                }
            }
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct AdmissionDecision {
    pub allowed: bool,
    pub reason: &'static str,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExhaustionController {
    pub policy: ExhaustionPolicy,
    pub atomic_unit_id: String,
    pub admission_epoch: u64,
    pub continuation_permit: Option<BudgetPermit>,
    pub validation_time: Option<String>,
    pub used_additional_steps: u32,
    pub used_additional_usage: Vec<UsageAmount>,
}

impl ExhaustionController {
    pub fn new(
        policy: ExhaustionPolicy,
        atomic_unit_id: impl Into<String>,
        admission_epoch: u64,
    ) -> Self {
        Self {
            policy,
            atomic_unit_id: atomic_unit_id.into(),
            admission_epoch,
            continuation_permit: None,
            validation_time: None,
            used_additional_steps: 0,
            used_additional_usage: Vec::new(),
        }
    }

    pub fn with_continuation_permit(mut self, permit: BudgetPermit) -> Self {
        self.continuation_permit = Some(permit);
        self
    }

    pub fn with_validation_time(mut self, validation_time: impl Into<String>) -> Self {
        self.validation_time = Some(validation_time.into());
        self
    }

    pub fn admit(
        &mut self,
        work_kind: WorkKind,
        work_epoch: u64,
        permit: Option<&BudgetPermit>,
    ) -> AdmissionDecision {
        self.admit_with_usage(work_kind, work_epoch, permit, Vec::new())
    }

    pub fn admit_with_usage(
        &mut self,
        work_kind: WorkKind,
        work_epoch: u64,
        permit: Option<&BudgetPermit>,
        requested_usage: impl IntoIterator<Item = UsageAmount>,
    ) -> AdmissionDecision {
        let requested_usage = requested_usage.into_iter().collect::<Vec<_>>();
        let envelope = match &self.policy.continuation {
            Some(envelope) => envelope,
            None => {
                return AdmissionDecision {
                    allowed: false,
                    reason: "missing_continuation",
                };
            }
        };
        if envelope.forbidden_work.contains(&work_kind) {
            return AdmissionDecision {
                allowed: false,
                reason: "forbidden_work",
            };
        }
        if self.policy.deny_new_work && !envelope.allowed_work.contains(&work_kind) {
            return AdmissionDecision {
                allowed: false,
                reason: "new_work_denied",
            };
        }
        if work_kind == WorkKind::AlreadyAdmittedChildWork && work_epoch <= self.admission_epoch {
            return AdmissionDecision {
                allowed: true,
                reason: "already_admitted",
            };
        }
        if self.policy.preset == Some(ExhaustionPreset::HardStop) && work_kind != WorkKind::Cleanup
        {
            return AdmissionDecision {
                allowed: false,
                reason: "hard_stop",
            };
        }
        if let Some(permit) = permit
            && !self.valid_permit(permit)
        {
            return AdmissionDecision {
                allowed: false,
                reason: "invalid_permit",
            };
        }
        if work_epoch > self.admission_epoch
            && !matches!(
                work_kind,
                WorkKind::DeclaredFinalization | WorkKind::Checkpoint | WorkKind::Cleanup
            )
        {
            return AdmissionDecision {
                allowed: false,
                reason: "new_work_denied",
            };
        }
        let permits_checkpoint_pause_safety_work =
            self.policy.preset == Some(ExhaustionPreset::CheckpointAndPause)
                && matches!(work_kind, WorkKind::Checkpoint | WorkKind::Cleanup);
        let permits_degraded_finalization =
            self.policy.preset == Some(ExhaustionPreset::DegradeThenFinalize)
                && matches!(work_kind, WorkKind::DeclaredFinalization | WorkKind::Cleanup);
        if work_epoch > self.admission_epoch
            && permit.is_none()
            && !permits_checkpoint_pause_safety_work
            && !permits_degraded_finalization
        {
            match &self.continuation_permit {
                Some(stored_permit) if !self.valid_permit(stored_permit) => {
                    return AdmissionDecision {
                        allowed: false,
                        reason: "invalid_permit",
                    };
                }
                None => {
                    return AdmissionDecision {
                        allowed: false,
                        reason: "missing_continuation_permit",
                    };
                }
                Some(_) => {}
            }
        }
        if work_epoch > self.admission_epoch && !requested_usage.is_empty() {
            let effective_permit = match permit.or(self.continuation_permit.as_ref()) {
                Some(permit) => permit,
                None => {
                    return AdmissionDecision {
                        allowed: false,
                        reason: "missing_continuation_permit",
                    };
                }
            };
            if !effective_permit.allows(requested_usage.clone()) {
                return AdmissionDecision {
                    allowed: false,
                    reason: "usage_exceeds_permit",
                };
            }
            if !envelope.max_additional_usage.is_empty() {
                let mut allowed_usage = BTreeMap::new();
                for amount in &envelope.max_additional_usage {
                    let key = (
                        amount.kind.clone(),
                        amount.unit.clone(),
                        amount
                            .dimensions
                            .iter()
                            .map(|(key, value)| (key.clone(), value.clone()))
                            .collect::<Vec<_>>(),
                    );
                    *allowed_usage.entry(key).or_insert(0) += amount.amount;
                }
                let mut projected_usage = BTreeMap::new();
                for amount in self
                    .used_additional_usage
                    .iter()
                    .chain(requested_usage.iter())
                {
                    let key = (
                        amount.kind.clone(),
                        amount.unit.clone(),
                        amount
                            .dimensions
                            .iter()
                            .map(|(key, value)| (key.clone(), value.clone()))
                            .collect::<Vec<_>>(),
                    );
                    *projected_usage.entry(key).or_insert(0) += amount.amount;
                }
                if projected_usage
                    .iter()
                    .any(|(key, amount)| *amount > allowed_usage.get(key).copied().unwrap_or(0))
                {
                    return AdmissionDecision {
                        allowed: false,
                        reason: "max_additional_usage_exceeded",
                    };
                }
            }
        }
        if let Some(max_steps) = envelope.max_additional_steps
            && self.used_additional_steps >= max_steps
        {
            return AdmissionDecision {
                allowed: false,
                reason: "max_additional_steps_exceeded",
            };
        }
        if work_epoch > self.admission_epoch {
            self.used_additional_steps += 1;
            self.used_additional_usage.extend(requested_usage);
        }
        AdmissionDecision {
            allowed: true,
            reason: "allowed",
        }
    }

    fn valid_permit(&self, permit: &BudgetPermit) -> bool {
        permit.atomic_unit == self.atomic_unit_id
            && self
                .policy
                .preset
                .map(|preset| permit.continuation_profile == preset.as_str())
                .unwrap_or(false)
            && permit.admission_epoch == self.admission_epoch
            && self
                .validation_time
                .as_ref()
                .is_none_or(|validation_time| permit.is_active_at(validation_time))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ExhaustionPolicyError {
    MissingExhaustionBoundary,
}

pub fn validate_exhaustion_policy(
    policy: &ExhaustionPolicy,
    production: bool,
) -> Result<Vec<&'static str>, ExhaustionPolicyError> {
    if production && policy.preset == Some(ExhaustionPreset::FinishCurrentTurn) {
        let bounded = match &policy.continuation {
            Some(envelope) => envelope.is_bounded(),
            None => false,
        };
        if !bounded {
            return Err(ExhaustionPolicyError::MissingExhaustionBoundary);
        }
    }

    let mut issues = Vec::new();
    if policy.preset.is_none() {
        issues.push("missing_preset");
    }
    Ok(issues)
}
