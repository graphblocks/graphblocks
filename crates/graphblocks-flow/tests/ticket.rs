use std::collections::BTreeSet;
use std::sync::{Arc, Barrier, mpsc};
use std::thread;
use std::time::{Duration, SystemTime};

use graphblocks_flow::ticket::{
    AdmissionTicketError, AdmissionTicketQueue, AdmissionTicketQueueConfig, AdmissionTicketState,
};

fn time(seconds: u64) -> SystemTime {
    SystemTime::UNIX_EPOCH + Duration::from_secs(seconds)
}

fn config(
    max_concurrent: usize,
    rate_limit: usize,
    rate_window_seconds: u64,
    max_pending: usize,
    ttl_seconds: u64,
) -> AdmissionTicketQueueConfig {
    AdmissionTicketQueueConfig::new(
        max_concurrent,
        rate_limit,
        Duration::from_secs(rate_window_seconds),
        max_pending,
        Duration::from_secs(ttl_seconds),
    )
    .expect("test queue configuration is valid")
}

#[test]
fn queue_configuration_rejects_invalid_limits() {
    assert_eq!(
        AdmissionTicketQueueConfig::new(0, 1, Duration::from_secs(1), 1, Duration::from_secs(1),),
        Err(AdmissionTicketError::InvalidMaxConcurrent),
    );
    assert_eq!(
        AdmissionTicketQueueConfig::new(1, 0, Duration::from_secs(1), 1, Duration::from_secs(1),),
        Err(AdmissionTicketError::InvalidRateLimit),
    );
    assert_eq!(
        AdmissionTicketQueueConfig::new(1, 1, Duration::ZERO, 1, Duration::from_secs(1),),
        Err(AdmissionTicketError::InvalidRateWindow),
    );
    assert_eq!(
        AdmissionTicketQueueConfig::new(1, 1, Duration::from_secs(1), 0, Duration::ZERO,),
        Err(AdmissionTicketError::InvalidTicketTtl),
    );
}

#[test]
fn idempotent_admission_is_atomic_across_threads() {
    let now = time(10);
    let queue = AdmissionTicketQueue::new_at(config(1, 100, 60, 20, 300), now);
    let worker_count = 16;
    let barrier = Arc::new(Barrier::new(worker_count));
    let mut handles = Vec::new();

    for _ in 0..worker_count {
        let worker_queue = queue.clone();
        let worker_barrier = Arc::clone(&barrier);
        handles.push(thread::spawn(move || {
            worker_barrier.wait();
            let receipt = worker_queue
                .admit_at("request-shared", "tenant-a", now)
                .expect("concurrent admission succeeds");
            (receipt.ticket().ticket_id().to_owned(), receipt.duplicate())
        }));
    }

    let results = handles
        .into_iter()
        .map(|handle| handle.join().expect("admission thread completes"))
        .collect::<Vec<_>>();
    let ticket_ids = results
        .iter()
        .map(|(ticket_id, _)| ticket_id.clone())
        .collect::<BTreeSet<_>>();

    assert_eq!(ticket_ids.len(), 1);
    assert_eq!(
        results.iter().filter(|(_, duplicate)| !duplicate).count(),
        1
    );
    assert_eq!(
        results.iter().filter(|(_, duplicate)| *duplicate).count(),
        15
    );
    assert_eq!(queue.counts_at(now).concurrent(), 1);
}

#[test]
fn queued_admission_returns_without_waiting_for_running_work() {
    let now = time(100);
    let queue = AdmissionTicketQueue::new_at(config(1, 10, 60, 1, 300), now);
    let admitted = queue
        .admit_at("request-running", "tenant-a", now)
        .expect("first request is admitted");
    let claim = queue
        .claim_next_at(now)
        .expect("claim succeeds")
        .expect("admitted work is claimable");
    assert_eq!(claim.ticket_id(), admitted.ticket().ticket_id());

    let worker_queue = queue.clone();
    let (sender, receiver) = mpsc::channel();
    let handle = thread::spawn(move || {
        let receipt = worker_queue
            .admit_at("request-queued", "tenant-a", now)
            .expect("second request is queued");
        sender
            .send(receipt.into_ticket())
            .expect("test receiver remains connected");
    });
    let queued = receiver
        .recv_timeout(Duration::from_secs(2))
        .expect("queued ticket returns before running work completes");

    assert_eq!(queued.state(), AdmissionTicketState::Queued);
    assert_eq!(
        queue
            .ticket_at(admitted.ticket().ticket_id(), now)
            .expect("running ticket remains available")
            .state(),
        AdmissionTicketState::Running,
    );
    handle.join().expect("queued admission thread completes");
}

#[test]
fn pending_capacity_and_fifo_promotion_are_enforced() {
    let started_at = time(1_000);
    let queue = AdmissionTicketQueue::new_at(config(1, 100, 60, 2, 300), started_at);
    let first = queue
        .admit_at("request-a", "tenant-a", started_at)
        .expect("first admission succeeds")
        .into_ticket();
    let second = queue
        .admit_at("request-b", "tenant-a", started_at)
        .expect("second admission queues")
        .into_ticket();
    let third = queue
        .admit_at("request-c", "tenant-a", started_at)
        .expect("third admission queues")
        .into_ticket();

    assert_eq!(first.state(), AdmissionTicketState::Admitted);
    assert_eq!(second.state(), AdmissionTicketState::Queued);
    assert_eq!(third.state(), AdmissionTicketState::Queued);
    assert_eq!(
        queue.admit_at("request-d", "tenant-a", started_at),
        Err(AdmissionTicketError::PendingCapacityExhausted { max_pending: 2 }),
    );

    let first_claim = queue
        .claim_next_at(started_at)
        .expect("first claim succeeds")
        .expect("first ticket is claimable");
    assert_eq!(first_claim.ticket_id(), first.ticket_id());
    queue
        .complete_at(
            first_claim.ticket_id(),
            first_claim.fencing_token(),
            time(1_001),
        )
        .expect("first completion succeeds");

    let second_claim = queue
        .claim_next_at(time(1_001))
        .expect("second claim succeeds")
        .expect("second ticket was promoted");
    assert_eq!(second_claim.ticket_id(), second.ticket_id());
    queue
        .fail_at(
            second_claim.ticket_id(),
            second_claim.fencing_token(),
            time(1_002),
        )
        .expect("second failure releases capacity");

    let third_claim = queue
        .claim_next_at(time(1_002))
        .expect("third claim succeeds")
        .expect("third ticket was promoted");
    assert_eq!(third_claim.ticket_id(), third.ticket_id());
}

#[test]
fn fixed_window_rate_limit_holds_work_even_when_concurrency_is_available() {
    let started_at = time(2_000);
    let queue = AdmissionTicketQueue::new_at(config(2, 1, 10, 2, 300), started_at);
    let first = queue
        .admit_at("request-a", "tenant-a", started_at)
        .expect("first request is admitted")
        .into_ticket();
    let second = queue
        .admit_at("request-b", "tenant-a", started_at)
        .expect("rate-limited request is queued")
        .into_ticket();
    let first_claim = queue
        .claim_next_at(started_at)
        .expect("first claim succeeds")
        .expect("first request is claimable");
    queue
        .complete_at(
            first_claim.ticket_id(),
            first_claim.fencing_token(),
            time(2_001),
        )
        .expect("first request completes");

    assert_eq!(first.state(), AdmissionTicketState::Admitted);
    assert_eq!(second.state(), AdmissionTicketState::Queued);
    assert_eq!(queue.refresh_at(time(2_009)).promoted, 0);
    assert_eq!(
        queue
            .ticket_at(second.ticket_id(), time(2_009))
            .expect("queued ticket exists")
            .state(),
        AdmissionTicketState::Queued,
    );
    assert_eq!(queue.refresh_at(time(2_010)).promoted, 1);
    assert_eq!(
        queue
            .claim_next_at(time(2_010))
            .expect("second claim succeeds")
            .expect("rate window promoted second request")
            .ticket_id(),
        second.ticket_id(),
    );
}

#[test]
fn concurrent_slots_include_admitted_and_running_tickets() {
    let now = time(3_000);
    let queue = AdmissionTicketQueue::new_at(config(2, 100, 60, 2, 300), now);
    let first = queue
        .admit_at("request-a", "tenant-a", now)
        .expect("first request admitted")
        .into_ticket();
    let second = queue
        .admit_at("request-b", "tenant-a", now)
        .expect("second request admitted")
        .into_ticket();
    let third = queue
        .admit_at("request-c", "tenant-a", now)
        .expect("third request queued")
        .into_ticket();
    let first_claim = queue
        .claim_next_at(now)
        .expect("first claim succeeds")
        .expect("first request is claimable");
    let second_claim = queue
        .claim_next_at(now)
        .expect("second claim succeeds")
        .expect("second request is claimable");

    assert_eq!(first_claim.ticket_id(), first.ticket_id());
    assert_eq!(second_claim.ticket_id(), second.ticket_id());
    assert!(second_claim.fencing_token() > first_claim.fencing_token());
    assert_eq!(queue.claim_next_at(now), Ok(None));
    assert_eq!(queue.counts_at(now).concurrent(), 2);

    queue
        .complete_at(
            second_claim.ticket_id(),
            second_claim.fencing_token(),
            time(3_001),
        )
        .expect("second request completes");
    assert_eq!(queue.counts_at(time(3_001)).concurrent(), 2);
    assert_eq!(
        queue
            .claim_next_at(time(3_001))
            .expect("third claim succeeds")
            .expect("third ticket was promoted")
            .ticket_id(),
        third.ticket_id(),
    );
}

#[test]
fn stale_fencing_token_cannot_complete_claimed_work() {
    let now = time(4_000);
    let queue = AdmissionTicketQueue::new_at(config(1, 100, 60, 1, 300), now);
    let ticket = queue
        .admit_at("request-a", "tenant-a", now)
        .expect("request admitted")
        .into_ticket();
    let claim = queue
        .claim_next_at(now)
        .expect("claim succeeds")
        .expect("request is claimable");
    let stale_token = claim.fencing_token() + 1;

    assert_eq!(
        queue.complete_at(ticket.ticket_id(), stale_token, time(4_001)),
        Err(AdmissionTicketError::StaleFencingToken {
            ticket_id: ticket.ticket_id().to_owned(),
            expected: claim.fencing_token(),
            actual: stale_token,
        }),
    );
    assert_eq!(
        queue
            .ticket_at(ticket.ticket_id(), time(4_001))
            .expect("ticket still exists")
            .state(),
        AdmissionTicketState::Running,
    );
    assert_eq!(
        queue
            .complete_at(ticket.ticket_id(), claim.fencing_token(), time(4_002))
            .expect("current claim completes")
            .state(),
        AdmissionTicketState::Completed,
    );
}

#[test]
fn ttl_expires_running_and_queued_work_and_releases_capacity() {
    let started_at = time(5_000);
    let queue = AdmissionTicketQueue::new_at(config(1, 100, 60, 2, 10), started_at);
    let first = queue
        .admit_at("request-a", "tenant-a", started_at)
        .expect("first request admitted")
        .into_ticket();
    let first_claim = queue
        .claim_next_at(started_at)
        .expect("claim succeeds")
        .expect("first request claimable");
    let second = queue
        .admit_at("request-b", "tenant-a", time(5_001))
        .expect("second request queued")
        .into_ticket();
    let third = queue
        .admit_at("request-c", "tenant-a", time(5_005))
        .expect("third request queued")
        .into_ticket();

    let maintenance = queue.refresh_at(time(5_011));
    assert_eq!(maintenance.expired, 2);
    assert_eq!(maintenance.promoted, 1);
    assert_eq!(
        queue.ticket_at(first.ticket_id(), time(5_011)),
        Err(AdmissionTicketError::UnknownTicket {
            ticket_id: first.ticket_id().to_owned(),
        }),
    );
    assert_eq!(
        queue.ticket_at(second.ticket_id(), time(5_011)),
        Err(AdmissionTicketError::UnknownTicket {
            ticket_id: second.ticket_id().to_owned(),
        }),
    );
    assert_eq!(
        queue
            .claim_next_at(time(5_011))
            .expect("claim succeeds")
            .expect("live queued successor is promoted")
            .ticket_id(),
        third.ticket_id(),
    );
    assert_eq!(
        queue.complete_at(first.ticket_id(), first_claim.fencing_token(), time(5_011)),
        Err(AdmissionTicketError::UnknownTicket {
            ticket_id: first.ticket_id().to_owned(),
        }),
    );
}

#[test]
fn ttl_expires_unclaimed_admission_and_promotes_live_successor() {
    let started_at = time(5_200);
    let queue = AdmissionTicketQueue::new_at(config(1, 100, 60, 1, 10), started_at);
    let abandoned = queue
        .admit_at("request-abandoned", "tenant-a", started_at)
        .expect("first request admitted")
        .into_ticket();
    let successor = queue
        .admit_at("request-successor", "tenant-a", time(5_201))
        .expect("successor queues")
        .into_ticket();

    let maintenance = queue.refresh_at(time(5_210));

    assert_eq!(maintenance.expired, 1);
    assert_eq!(maintenance.promoted, 1);
    assert_eq!(
        queue.ticket_at(abandoned.ticket_id(), time(5_210)),
        Err(AdmissionTicketError::UnknownTicket {
            ticket_id: abandoned.ticket_id().to_owned(),
        })
    );
    assert_eq!(
        queue
            .claim_next_at(time(5_210))
            .expect("claim succeeds")
            .expect("successor was promoted")
            .ticket_id(),
        successor.ticket_id()
    );
}

#[test]
fn running_cancellation_requires_fenced_worker_exit() {
    let now = time(5_500);
    let queue = AdmissionTicketQueue::new_at(config(1, 100, 60, 1, 300), now);
    let running = queue
        .admit_at("request-running", "tenant-a", now)
        .expect("request admitted")
        .into_ticket();
    let claim = queue
        .claim_next_at(now)
        .expect("claim succeeds")
        .expect("ticket is claimable");
    let queued = queue
        .admit_at("request-queued", "tenant-a", now)
        .expect("successor queues")
        .into_ticket();

    assert_eq!(
        queue.cancel_at(running.ticket_id(), time(5_501)),
        Err(AdmissionTicketError::InvalidState {
            ticket_id: running.ticket_id().to_owned(),
            expected: "queued or admitted; use cancel_claimed after the worker exits",
            actual: AdmissionTicketState::Running,
        }),
    );
    assert_eq!(
        queue
            .ticket_at(queued.ticket_id(), time(5_501))
            .expect("successor remains queued")
            .state(),
        AdmissionTicketState::Queued,
    );
    queue
        .cancel_claimed_at(running.ticket_id(), claim.fencing_token(), time(5_502))
        .expect("worker exit cancels with current fence");
    assert_eq!(
        queue
            .claim_next_at(time(5_502))
            .expect("successor claim succeeds")
            .expect("successor promotes after worker exit")
            .ticket_id(),
        queued.ticket_id(),
    );
}

#[test]
fn cancellation_removes_pending_work_and_is_idempotent() {
    let now = time(6_000);
    let queue = AdmissionTicketQueue::new_at(config(1, 100, 60, 2, 300), now);
    let first = queue
        .admit_at("request-a", "tenant-a", now)
        .expect("first request admitted")
        .into_ticket();
    let second = queue
        .admit_at("request-b", "tenant-a", now)
        .expect("second request queued")
        .into_ticket();
    let third = queue
        .admit_at("request-c", "tenant-a", now)
        .expect("third request queued")
        .into_ticket();

    assert_eq!(
        queue
            .cancel_at(second.ticket_id(), time(6_001))
            .expect("queued cancellation succeeds")
            .state(),
        AdmissionTicketState::Cancelled,
    );
    assert_eq!(
        queue
            .cancel_at(first.ticket_id(), time(6_002))
            .expect("admitted cancellation succeeds")
            .state(),
        AdmissionTicketState::Cancelled,
    );
    assert_eq!(
        queue
            .cancel_at(first.ticket_id(), time(6_003))
            .expect("duplicate cancellation succeeds")
            .state(),
        AdmissionTicketState::Cancelled,
    );
    assert_eq!(
        queue
            .claim_next_at(time(6_003))
            .expect("successor claim succeeds")
            .expect("only live successor is promoted")
            .ticket_id(),
        third.ticket_id(),
    );
}

#[test]
fn request_identity_is_scoped_by_owner_and_survives_terminal_state() {
    let now = time(7_000);
    let queue = AdmissionTicketQueue::new_at(config(2, 100, 60, 1, 300), now);
    let tenant_a = queue
        .admit_at("request-shared", "tenant-a", now)
        .expect("tenant a admitted")
        .into_ticket();
    let tenant_b = queue
        .admit_at("request-shared", "tenant-b", now)
        .expect("tenant b admitted")
        .into_ticket();
    assert_ne!(tenant_a.ticket_id(), tenant_b.ticket_id());

    let claim = queue
        .claim_next_at(now)
        .expect("tenant a claim succeeds")
        .expect("tenant a is first");
    queue
        .complete_at(claim.ticket_id(), claim.fencing_token(), time(7_001))
        .expect("tenant a completes");
    let duplicate = queue
        .admit_at("request-shared", "tenant-a", time(7_002))
        .expect("terminal request remains idempotent");

    assert!(duplicate.duplicate());
    assert_eq!(duplicate.ticket().ticket_id(), tenant_a.ticket_id());
    assert_eq!(duplicate.ticket().state(), AdmissionTicketState::Completed);

    let retried = queue
        .admit_at("request-shared", "tenant-a", time(7_300))
        .expect("request identity may be reused after its idempotency TTL");
    assert!(!retried.duplicate());
    assert_ne!(retried.ticket().ticket_id(), tenant_a.ticket_id());
}

#[test]
fn zero_pending_capacity_rejects_instead_of_waiting() {
    let now = time(8_000);
    let queue = AdmissionTicketQueue::new_at(config(1, 100, 60, 0, 300), now);
    queue
        .admit_at("request-a", "tenant-a", now)
        .expect("first request admitted");

    assert_eq!(
        queue.admit_at("request-b", "tenant-a", now),
        Err(AdmissionTicketError::PendingCapacityExhausted { max_pending: 0 }),
    );
}

#[test]
fn malformed_or_unknown_identities_are_rejected() {
    let now = time(9_000);
    let queue = AdmissionTicketQueue::new_at(config(1, 100, 60, 1, 300), now);

    assert_eq!(
        queue.admit_at(" ", "tenant-a", now),
        Err(AdmissionTicketError::InvalidIdentity {
            field: "request_id"
        }),
    );
    assert_eq!(
        queue.admit_at("request-a", " tenant-a", now),
        Err(AdmissionTicketError::InvalidIdentity { field: "owner" }),
    );
    assert_eq!(
        queue.ticket_for_at("missing", "tenant-a", now),
        Err(AdmissionTicketError::UnknownRequest {
            request_id: "missing".to_owned(),
            owner: "tenant-a".to_owned(),
        }),
    );
}
