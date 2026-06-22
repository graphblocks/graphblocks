use std::collections::VecDeque;

use serde_json::{Value, json};

use crate::journal::{ExecutionJournal, JournalError, JournalMetadata};
use crate::outcome::{BlockError, Outcome};
use crate::readiness::PortRef;
use crate::scheduler::{
    LocalScheduler, NodeExecutionState, ScheduledNode, SchedulerError, StartedNode,
};

pub trait NodeExecutor {
    fn execute(&mut self, node: StartedNode) -> Result<Vec<(PortRef, Outcome<Value>)>, BlockError>;
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TestRunStatus {
    Succeeded,
    Failed,
}

#[derive(Clone, Debug, PartialEq)]
pub struct TestRunResult {
    pub status: TestRunStatus,
    pub journal: ExecutionJournal,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TestRuntimeError {
    Scheduler(SchedulerError),
    Journal(JournalError),
}

impl From<SchedulerError> for TestRuntimeError {
    fn from(error: SchedulerError) -> Self {
        Self::Scheduler(error)
    }
}

impl From<JournalError> for TestRuntimeError {
    fn from(error: JournalError) -> Self {
        Self::Journal(error)
    }
}

#[derive(Clone, Debug)]
pub struct InProcessTestRuntime {
    scheduler: LocalScheduler,
    journal: ExecutionJournal,
}

impl InProcessTestRuntime {
    pub fn new<I>(run_id: impl Into<String>, nodes: I) -> Result<Self, SchedulerError>
    where
        I: IntoIterator<Item = ScheduledNode>,
    {
        Ok(Self {
            scheduler: LocalScheduler::new(nodes)?,
            journal: ExecutionJournal::new(run_id),
        })
    }

    pub fn journal(&self) -> &ExecutionJournal {
        &self.journal
    }

    pub fn run<E>(&mut self, executor: &mut E) -> Result<TestRunResult, TestRuntimeError>
    where
        E: NodeExecutor,
    {
        self.journal
            .append_with_metadata("run_started", JournalMetadata::new(), None)?;
        let mut ready = VecDeque::from(self.scheduler.admit_run()?);

        while let Some(node_id) = ready.pop_front() {
            let started = self.scheduler.start_node(&node_id)?;
            let metadata = JournalMetadata::new().with_node_id(node_id.clone());
            self.journal
                .append_with_metadata("node_started", metadata.clone(), None)?;

            match executor.execute(started) {
                Ok(outputs) => {
                    let newly_ready = self.scheduler.complete_node(&node_id, outputs)?;
                    self.journal
                        .append_with_metadata("node_completed", metadata, None)?;
                    for node_id in newly_ready {
                        ready.push_back(node_id);
                    }
                }
                Err(error) => {
                    let payload = json!({
                        "code": error.code,
                        "category": format!("{:?}", error.category),
                        "message": error.message,
                    });
                    self.journal.append_with_metadata(
                        "node_failed",
                        metadata.clone(),
                        Some(payload.clone()),
                    )?;
                    self.journal.append_terminal_with_metadata(
                        "run_failed",
                        metadata,
                        Some(payload),
                    )?;
                    return Ok(TestRunResult {
                        status: TestRunStatus::Failed,
                        journal: self.journal.clone(),
                    });
                }
            }
        }

        let unfinished = self
            .scheduler
            .node_states()
            .into_iter()
            .filter(|(_, state)| *state != NodeExecutionState::Completed)
            .collect::<Vec<_>>();
        if unfinished.is_empty() {
            self.journal.append_terminal_with_metadata(
                "run_succeeded",
                JournalMetadata::new(),
                None,
            )?;
            return Ok(TestRunResult {
                status: TestRunStatus::Succeeded,
                journal: self.journal.clone(),
            });
        }

        self.journal.append_terminal_with_metadata(
            "run_failed",
            JournalMetadata::new(),
            Some(json!({
                "unfinished": unfinished
                    .into_iter()
                    .map(|(node_id, state)| json!({
                        "node": node_id,
                        "state": format!("{:?}", state),
                    }))
                    .collect::<Vec<_>>(),
            })),
        )?;
        Ok(TestRunResult {
            status: TestRunStatus::Failed,
            journal: self.journal.clone(),
        })
    }
}
