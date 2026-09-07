from __future__ import annotations

from datetime import UTC, datetime, timedelta
from dataclasses import replace
import json
from pathlib import Path

import pytest

from taskfoundry.model import Actor, ContractError, RunState
from taskfoundry.scheduler import QuestionScheduler, QueueState
from taskfoundry.scheduler_state import (
    DispatchState,
    ExternalWait,
    ScheduledQuestion,
    SchedulerSnapshot,
    TeacherLease,
    parse_timestamp,
    snapshot_from_dict,
)
from taskfoundry.store import RunStore


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _run(root: Path, question_id: str) -> Path:
    path = root / question_id
    RunStore(path).initialize(question_id)
    return path


def _prompt(root: Path, question_id: str) -> Path:
    path = root / f"{question_id}-teacher.md"
    path.write_text("[TASKFOUNDRY ROLE=teacher]\n继续正式出题。\n")
    return path


def _enqueue(scheduler: QuestionScheduler, root: Path, question_id: str) -> None:
    scheduler.enqueue(
        question_id,
        _run(root, question_id),
        teacher_thread_id=f"owner-{question_id}",
        teacher_prompt_path=_prompt(root, question_id),
    )


def _set_state(run_dir: Path, state: RunState, key: str) -> None:
    store = RunStore(run_dir)
    current = store.read_snapshot()
    assert current is not None
    store.commit(
        actor=Actor.SYSTEM,
        event_type="test.state.changed",
        idempotency_key=key,
        payload={},
        snapshot=store.advance(current, state=state),
    )


def test_empty_scheduler_exposes_single_q1_q32_campaign_board(tmp_path: Path) -> None:
    snapshot = QuestionScheduler(tmp_path / "scheduler").snapshot()

    assert snapshot.schema_version == 2
    assert snapshot.max_active == 1
    assert [item.question_id for item in snapshot.questions] == [f"q{i}" for i in range(1, 33)]
    assert all(item.state is QueueState.COMPLETED for item in snapshot.questions[:2])
    assert all(item.completion_basis == "GRANDFATHERED" for item in snapshot.questions[:2])
    assert all(item.state is QueueState.BACKLOG for item in snapshot.questions[2:])


def test_scheduler_uses_one_active_slot_by_default(tmp_path: Path) -> None:
    clock = FakeClock()
    scheduler = QuestionScheduler(tmp_path / "scheduler", clock=clock)
    for index in range(3, 7):
        _enqueue(scheduler, tmp_path, f"q{index}")

    snapshot = scheduler.snapshot()
    leased = [item for item in snapshot.questions if item.state is QueueState.LEASED]

    assert [item.question_id for item in leased] == ["q3"]
    assert len({item.lease.lease_id for item in leased if item.lease}) == 1
    assert len({item.lease.owner_id for item in leased if item.lease}) == 1
    assert next(item for item in snapshot.questions if item.question_id == "q4").state is QueueState.READY


def test_heartbeat_refreshes_lease_and_rejects_stale_fencing(tmp_path: Path) -> None:
    clock = FakeClock()
    scheduler = QuestionScheduler(tmp_path / "scheduler", clock=clock, lease_ttl_sec=60)
    scheduler.configure(max_active=3)
    _enqueue(scheduler, tmp_path, "q3")
    item = next(value for value in scheduler.snapshot().questions if value.question_id == "q3")
    assert item.lease is not None
    old_expiry = item.lease.expires_at

    clock.advance(30)
    refreshed = scheduler.heartbeat(
        "q3",
        owner_id=item.lease.owner_id,
        lease_id=item.lease.lease_id,
        generation=item.generation,
    )
    updated = next(value for value in refreshed.questions if value.question_id == "q3")
    assert updated.lease is not None and updated.lease.expires_at > old_expiry

    with pytest.raises(ContractError, match="lease fencing"):
        scheduler.heartbeat(
            "q3",
            owner_id="another-owner",
            lease_id=item.lease.lease_id,
            generation=item.generation,
        )


def test_expired_lease_requires_recovery_and_automatically_refills(tmp_path: Path) -> None:
    clock = FakeClock()
    scheduler = QuestionScheduler(tmp_path / "scheduler", clock=clock, lease_ttl_sec=60)
    scheduler.configure(max_active=3)
    for index in range(3, 7):
        _enqueue(scheduler, tmp_path, f"q{index}")

    clock.advance(61)
    result = scheduler.tick()
    states = {item.question_id: item.state for item in result.snapshot.questions}

    assert result.released == ("q3", "q4", "q5")
    assert result.activated == ("q6",)
    assert states["q3"] is QueueState.RECOVERY_REQUIRED
    assert states["q6"] is QueueState.LEASED


def test_reclaimed_question_rejects_previous_generation(tmp_path: Path) -> None:
    clock = FakeClock()
    scheduler = QuestionScheduler(tmp_path / "scheduler", clock=clock, lease_ttl_sec=60)
    _enqueue(scheduler, tmp_path, "q3")
    first = next(item for item in scheduler.snapshot().questions if item.question_id == "q3")
    assert first.lease is not None

    clock.advance(61)
    scheduler.tick()
    scheduler.requeue("q3")
    second = next(item for item in scheduler.snapshot().questions if item.question_id == "q3")
    assert second.generation == first.generation + 1

    with pytest.raises(ContractError, match="lease fencing"):
        scheduler.heartbeat(
            "q3",
            owner_id=first.lease.owner_id,
            lease_id=first.lease.lease_id,
            generation=first.generation,
        )


def test_waiting_external_releases_slot_and_resume_reenters_queue(tmp_path: Path) -> None:
    clock = FakeClock()
    scheduler = QuestionScheduler(tmp_path / "scheduler", clock=clock)
    scheduler.configure(max_active=3)
    for index in range(3, 7):
        _enqueue(scheduler, tmp_path, f"q{index}")
    q3 = next(item for item in scheduler.snapshot().questions if item.question_id == "q3")
    assert q3.lease is not None

    waiting = scheduler.set_waiting_external(
        "q3",
        owner_id=q3.lease.owner_id,
        lease_id=q3.lease.lease_id,
        generation=q3.generation,
        kind="HARBOR",
        external_id="job-3",
        phase="FRESH_BLIND",
        recovery_condition="provider health probe succeeds",
        next_probe_at="2026-08-26T12:15:00+00:00",
        evidence_path=tmp_path / "probe.json",
    )
    states = {item.question_id: item.state for item in waiting.snapshot.questions}
    assert waiting.released == ("q3",)
    assert waiting.activated == ("q6",)
    assert states["q3"] is QueueState.WAITING_EXTERNAL
    assert states["q6"] is QueueState.LEASED
    waiting_item = next(
        item for item in waiting.snapshot.questions if item.question_id == "q3"
    )
    assert waiting_item.wait is not None
    assert waiting_item.wait.recovery_condition == "provider health probe succeeds"
    assert waiting_item.wait.evidence_path == str((tmp_path / "probe.json").resolve())

    resumed = scheduler.resume_external("q3", external_id="job-3")
    assert next(item for item in resumed.snapshot.questions if item.question_id == "q3").state is QueueState.READY
    assert resumed.activated == ()


def test_topic_abandonment_releases_slot_and_cannot_be_requeued(tmp_path: Path) -> None:
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    scheduler.configure(max_active=2)
    _enqueue(scheduler, tmp_path, "q3")
    _enqueue(scheduler, tmp_path, "q4")
    _enqueue(scheduler, tmp_path, "q5")
    q3 = next(item for item in scheduler.snapshot().questions if item.question_id == "q3")
    assert q3.lease is not None
    evidence = tmp_path / "abandon.json"
    evidence.write_text('{"reason":"scientific design cannot satisfy contract"}\n')

    result = scheduler.abandon_topic(
        "q3",
        owner_id=q3.lease.owner_id,
        lease_id=q3.lease.lease_id,
        generation=q3.generation,
        evidence_path=evidence,
    )

    abandoned = next(
        item for item in result.snapshot.questions if item.question_id == "q3"
    )
    assert abandoned.state is QueueState.ABANDONED
    assert result.released[0] == "q3"
    with pytest.raises(ContractError, match="only recovery or blocked"):
        scheduler.requeue("q3")


def test_dispatch_claim_is_atomic_and_bound_to_lease(tmp_path: Path) -> None:
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    _enqueue(scheduler, tmp_path, "q3")

    claimed = scheduler.claim_dispatch("q3", worker_id="worker-a")
    assert claimed is not None and claimed.lease is not None
    assert scheduler.claim_dispatch("q3", worker_id="worker-b") is None
    acknowledged = scheduler.record_dispatch(
        "q3",
        "message-3",
        worker_id="worker-a",
        owner_id=claimed.lease.owner_id,
        lease_id=claimed.lease.lease_id,
        generation=claimed.generation,
    )
    item = next(value for value in acknowledged.questions if value.question_id == "q3")
    assert item.lease is not None and item.lease.dispatch_message_id == "message-3"


def test_v1_snapshot_migrates_with_backup_and_without_fake_heartbeat(tmp_path: Path) -> None:
    root = tmp_path / "scheduler"
    root.mkdir()
    legacy = {
        "schema_version": 1,
        "sequence": 11,
        "max_active": 5,
        "questions": [
            {
                "question_id": "q18",
                "run_dir": str(_run(tmp_path, "q18")),
                "state": "ACTIVE",
                "position": 1,
                "updated_at": "2026-08-24T07:49:11+00:00",
                "last_run_state": "ENVIRONMENT_DISCOVERY",
                "teacher_thread_id": "old-owner",
                "teacher_prompt_path": str(_prompt(tmp_path, "q18")),
                "dispatch_message_id": "old-message",
            }
        ],
    }
    encoded = json.dumps(legacy, indent=2) + "\n"
    (root / "scheduler.json").write_text(encoded)
    scheduler = QuestionScheduler(root)

    migrated = scheduler.snapshot()
    q18 = next(item for item in migrated.questions if item.question_id == "q18")
    assert migrated.schema_version == 2
    assert migrated.max_active == 1
    assert q18.state is QueueState.RECOVERY_REQUIRED
    assert q18.lease is None

    scheduler.requeue("q18")
    assert (root / "scheduler.v1.json").read_text() == encoded
    persisted = json.loads((root / "scheduler.json").read_text())
    assert persisted["schema_version"] == 2
    assert len(persisted["questions"]) == 32


def test_completed_run_waits_for_validated_publication_before_releasing_slot(
    tmp_path: Path, monkeypatch
) -> None:
    validated: list[tuple[int, Path]] = []
    family = tmp_path / "questions" / "4" / "question-pack"
    trace = tmp_path / "questions" / "4" / "trace/final/package-digest"
    family.mkdir(parents=True)
    trace.mkdir(parents=True)

    def validate_family(question: int, candidate: Path, run: object) -> Path:
        validated.append((question, candidate))
        return trace

    monkeypatch.setattr("taskfoundry.scheduler.published_family_path", lambda _number: family)
    monkeypatch.setattr("taskfoundry.scheduler._default_finalize_published_family", validate_family)
    monkeypatch.setattr(QuestionScheduler, "_reconcile_batch_at_completion", lambda *args: None)
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    scheduler.configure(max_active=3)
    runs = {}
    for index in range(3, 7):
        question_id = f"q{index}"
        run = _run(tmp_path, question_id)
        runs[question_id] = run
        scheduler.enqueue(
            question_id,
            run,
            teacher_thread_id=f"owner-{question_id}",
            teacher_prompt_path=_prompt(tmp_path, question_id),
        )
    _set_state(runs["q4"], RunState.COMPLETED, "test:q4:completed")

    waiting = scheduler.tick()

    assert waiting.released == ()
    assert waiting.activated == ()
    q4 = next(item for item in waiting.snapshot.questions if item.question_id == "q4")
    assert q4.state is QueueState.PUBLICATION_PENDING
    assert q4.phase == "RUN_COMPLETED_AWAITING_PUBLICATION"
    assert q4.last_run_state == RunState.COMPLETED.value
    assert q4.published_family is None
    assert validated == []

    assert q4.lease is not None
    result = scheduler.bind_published_family(
        "q4",
        owner_id=q4.lease.owner_id,
        lease_id=q4.lease.lease_id,
        generation=q4.generation,
    )

    assert result.released == ("q4",)
    assert result.activated == ("q6",)
    items = {item.question_id: item for item in result.snapshot.questions}
    assert items["q4"].state is QueueState.COMPLETED
    assert items["q4"].published_family == str(family.resolve())
    assert items["q4"].completion_basis == "RUN_COMPLETED_AND_FAMILY_PUBLISHED"
    assert items["q6"].state is QueueState.LEASED
    assert validated == [(4, family.resolve())]


def test_publication_binding_rejects_invalid_family_without_mutating_slot(
    tmp_path: Path, monkeypatch
) -> None:
    family = tmp_path / "questions/3/question-pack"

    def reject_family(question: int, candidate: Path, run: object) -> Path:
        raise ContractError(f"family validation rejected q{question}: {family}")

    monkeypatch.setattr("taskfoundry.scheduler.published_family_path", lambda _number: family)
    monkeypatch.setattr("taskfoundry.scheduler._default_finalize_published_family", reject_family)
    monkeypatch.setattr(QuestionScheduler, "_reconcile_batch_at_completion", lambda *args: None)
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    run = _run(tmp_path, "q3")
    scheduler.enqueue(
        "q3",
        run,
        teacher_thread_id="owner-q3",
        teacher_prompt_path=_prompt(tmp_path, "q3"),
    )
    _set_state(run, RunState.COMPLETED, "test:q3:completed")
    waiting = scheduler.tick()
    q3 = waiting.snapshot.questions[2]
    family.mkdir(parents=True)
    assert q3.lease is not None

    with pytest.raises(ContractError, match="family validation rejected"):
        scheduler.bind_published_family(
            "q3",
            owner_id=q3.lease.owner_id,
            lease_id=q3.lease.lease_id,
            generation=q3.generation,
        )

    unchanged = scheduler.snapshot().questions[2]
    assert unchanged.state is QueueState.PUBLICATION_PENDING
    assert unchanged.published_family is None
    assert unchanged.completion_basis is None


def test_publication_binding_requires_current_lease_and_completed_run(
    tmp_path: Path, monkeypatch
) -> None:
    validated: list[tuple[int, Path]] = []
    family = tmp_path / "questions/3/question-pack"
    trace = tmp_path / "questions/3/trace/final/package-digest"
    family.mkdir(parents=True)
    trace.mkdir(parents=True)

    def finalize(question: int, candidate: Path, run: object) -> Path:
        validated.append((question, candidate))
        return trace

    monkeypatch.setattr("taskfoundry.scheduler.published_family_path", lambda _number: family)
    monkeypatch.setattr("taskfoundry.scheduler._default_finalize_published_family", finalize)
    monkeypatch.setattr(QuestionScheduler, "_reconcile_batch_at_completion", lambda *args: None)
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    _enqueue(scheduler, tmp_path, "q3")
    q3 = scheduler.snapshot().questions[2]
    assert q3.lease is not None

    with pytest.raises(ContractError, match="completed run"):
        scheduler.bind_published_family(
            "q3",
            owner_id=q3.lease.owner_id,
            lease_id=q3.lease.lease_id,
            generation=q3.generation,
        )
    with pytest.raises(ContractError, match="lease fencing"):
        scheduler.bind_published_family(
            "q3",
            owner_id="stale-owner",
            lease_id=q3.lease.lease_id,
            generation=q3.generation,
        )
    with pytest.raises(ContractError, match="grandfathered"):
        scheduler.bind_published_family(
            "q1",
            owner_id=q3.lease.owner_id,
            lease_id=q3.lease.lease_id,
            generation=q3.generation,
        )
    assert validated == []


def test_last_published_question_triggers_automatic_skill_batch_reconcile(
    tmp_path: Path, monkeypatch
) -> None:
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    run_dir = _run(tmp_path, "q3")
    scheduler.enqueue(
        "q3",
        run_dir,
        teacher_thread_id="owner-q3",
        teacher_prompt_path=_prompt(tmp_path, "q3"),
    )
    store = RunStore(run_dir)
    current = store.read_snapshot()
    assert current is not None
    contract = {
        "outline": {
            "batch_id": "batch-1", "batch_questions": [3],
            "stable_generation": 1, "stable_lock_sha256": "a" * 64,
        },
        "author": {
            "batch_id": "batch-1", "batch_questions": [3],
            "stable_generation": 1, "stable_lock_sha256": "a" * 64,
        },
    }
    completed = store.advance(
        current,
        state=RunState.COMPLETED,
        evidence={"teacher_skill_contract": contract},
    )
    store.commit(
        actor=Actor.SYSTEM,
        event_type="test.completed",
        idempotency_key="test:batch:completed",
        payload={},
        snapshot=completed,
    )
    calls: list[tuple[str, tuple[int, ...]]] = []
    monkeypatch.setattr(
        "taskfoundry.scheduler.reconcile_skill_batch_if_ready",
        lambda batch, questions: calls.append((batch, questions)) or {"status": "PASS"},
    )
    item = scheduler.snapshot().questions[2]

    QuestionScheduler._reconcile_batch_at_completion(scheduler.snapshot(), item, completed)

    assert calls == [("batch-1", (3,))]


@pytest.mark.parametrize("limit", [0, 6, 200])
def test_campaign_rejects_teacher_limit_outside_one_to_five(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ContractError, match="between 1 and 5"):
        QuestionScheduler(tmp_path / "scheduler").configure(max_active=limit)


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5])
def test_campaign_accepts_teacher_limit_between_one_and_five(tmp_path: Path, limit: int) -> None:
    result = QuestionScheduler(tmp_path / "scheduler").configure(max_active=limit)
    assert result.snapshot.max_active == limit


def test_scheduler_rejects_invalid_public_inputs(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="TTL"):
        QuestionScheduler(tmp_path / "scheduler", lease_ttl_sec=29)
    with pytest.raises(ContractError, match="q1 and q2"):
        QuestionScheduler(tmp_path / "scheduler").enqueue("q1", tmp_path / "run")
    with pytest.raises(ContractError, match="configured together"):
        QuestionScheduler(tmp_path / "scheduler").enqueue(
            "q3",
            tmp_path / "run",
            teacher_thread_id="owner",
        )
    with pytest.raises(ContractError, match="q1 through q32"):
        QuestionScheduler(tmp_path / "scheduler").requeue("q33")


@pytest.mark.parametrize("payload", ["not json", "[]"])
def test_scheduler_rejects_invalid_state_file(tmp_path: Path, payload: str) -> None:
    root = tmp_path / "scheduler"
    root.mkdir()
    (root / "scheduler.json").write_text(payload)
    with pytest.raises(ContractError, match="scheduler state"):
        QuestionScheduler(root).snapshot()


def test_scheduler_rejects_stale_dispatch_and_external_identities(tmp_path: Path) -> None:
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    _enqueue(scheduler, tmp_path, "q3")
    q3 = next(item for item in scheduler.snapshot().questions if item.question_id == "q3")
    assert q3.lease is not None
    with pytest.raises(ContractError, match="worker identity"):
        scheduler.claim_dispatch("q3", worker_id="")
    with pytest.raises(ContractError, match="message identity"):
        scheduler.record_dispatch(
            "q3",
            "",
            worker_id="worker-a",
            owner_id=q3.lease.owner_id,
            lease_id=q3.lease.lease_id,
            generation=q3.generation,
        )
    claimed = scheduler.claim_dispatch("q3", worker_id="worker-a")
    assert claimed is not None and claimed.lease is not None
    with pytest.raises(ContractError, match="worker claim"):
        scheduler.record_dispatch(
            "q3",
            "message",
            worker_id="worker-b",
            owner_id=claimed.lease.owner_id,
            lease_id=claimed.lease.lease_id,
            generation=claimed.generation,
        )
    with pytest.raises(ContractError, match="not waiting"):
        scheduler.resume_external("q3", external_id="job")
    with pytest.raises(ContractError, match="only recovery"):
        scheduler.requeue("q3")


@pytest.mark.parametrize("terminal", [RunState.DEFERRED_TIMEOUT, RunState.HUMAN_REVIEW, RunState.BLOCKED])
def test_run_waiting_or_blocked_state_releases_slot(tmp_path: Path, terminal: RunState) -> None:
    scheduler = QuestionScheduler(tmp_path / "scheduler")
    run = _run(tmp_path, "q3")
    scheduler.enqueue(
        "q3",
        run,
        teacher_thread_id="owner-q3",
        teacher_prompt_path=_prompt(tmp_path, "q3"),
    )
    _set_state(run, terminal, f"test:q3:{terminal.value}")

    result = scheduler.tick()
    q3 = next(item for item in result.snapshot.questions if item.question_id == "q3")
    expected = QueueState.BLOCKED if terminal is RunState.BLOCKED else QueueState.WAITING_EXTERNAL
    assert q3.state is expected
    assert q3.lease is None


def test_state_contract_rejects_invalid_timestamps_and_lease_shapes() -> None:
    with pytest.raises(ContractError, match="ISO timestamp"):
        parse_timestamp("not-a-time", "time")
    with pytest.raises(ContractError, match="timezone"):
        parse_timestamp("2026-08-26T12:00:00", "time")
    with pytest.raises(ContractError, match="lease identity"):
        TeacherLease("", "owner", "2026-08-26T12:00:00+00:00", "2026-08-26T12:01:00+00:00").validate()
    with pytest.raises(ContractError, match="expiry"):
        TeacherLease(
            "lease",
            "owner",
            "2026-08-26T12:01:00+00:00",
            "2026-08-26T12:00:00+00:00",
        ).validate()
    with pytest.raises(ContractError, match="pending dispatch"):
        TeacherLease(
            "lease",
            "owner",
            "2026-08-26T12:00:00+00:00",
            "2026-08-26T12:01:00+00:00",
            dispatch_worker_id="worker",
        ).validate()
    with pytest.raises(ContractError, match="sending dispatch"):
        TeacherLease(
            "lease",
            "owner",
            "2026-08-26T12:00:00+00:00",
            "2026-08-26T12:01:00+00:00",
            dispatch_state=DispatchState.SENDING,
        ).validate()


def test_state_contract_rejects_invalid_question_and_snapshot_shapes() -> None:
    now = "2026-08-26T12:00:00+00:00"
    with pytest.raises(ContractError, match="external wait"):
        ExternalWait("", "job", now).validate()
    with pytest.raises(ContractError, match="position"):
        ScheduledQuestion("q3", QueueState.BACKLOG, 4, now).validate()
    with pytest.raises(ContractError, match="run_dir"):
        ScheduledQuestion("q3", QueueState.READY, 3, now, run_dir="relative").validate()
    with pytest.raises(ContractError, match="configured together"):
        ScheduledQuestion("q3", QueueState.READY, 3, now, teacher_thread_id="owner").validate()
    base = QuestionScheduler(Path("/tmp/scheduler-contract-only")).snapshot()
    with pytest.raises(ContractError, match="unsupported scheduler"):
        SchedulerSnapshot(sequence=0, questions=base.questions, max_active=0).validate()
    with pytest.raises(ContractError, match="ordered q1-q32"):
        SchedulerSnapshot(sequence=0, questions=base.questions[:-1]).validate()
    with pytest.raises(ContractError, match="schema_version"):
        snapshot_from_dict({"schema_version": 9}, now=datetime.now(UTC), lease_ttl_sec=600)


def test_state_contract_rejects_inconsistent_optional_fields() -> None:
    now = "2026-08-26T12:00:00+00:00"
    later = "2026-08-26T12:10:00+00:00"
    lease = TeacherLease("lease", "owner", now, later)
    wait = ExternalWait("HARBOR", "job", now)
    cases = [
        (ScheduledQuestion("q3", QueueState.READY, 3, now, teacher_thread_id="owner", teacher_prompt_path="relative"), "prompt path"),
        (ScheduledQuestion("q3", QueueState.READY, 3, now, published_family="relative"), "published family"),
        (ScheduledQuestion("q3", QueueState.LEASED, 3, now, teacher_thread_id="owner", teacher_prompt_path="/prompt"), "leased question"),
        (ScheduledQuestion("q3", QueueState.READY, 3, now, lease=lease), "only a leased"),
        (ScheduledQuestion("q3", QueueState.WAITING_EXTERNAL, 3, now), "requires wait"),
        (ScheduledQuestion("q3", QueueState.READY, 3, now, wait=wait), "only external"),
        (ScheduledQuestion("q3", QueueState.COMPLETED, 3, now), "completion basis"),
        (
            ScheduledQuestion(
                "q3",
                QueueState.COMPLETED,
                3,
                now,
                completion_basis="RUN_COMPLETED_AND_FAMILY_PUBLISHED",
                last_run_state="COMPLETED",
            ),
            "published family",
        ),
        (
            ScheduledQuestion(
                "q3",
                QueueState.COMPLETED,
                3,
                now,
                completion_basis="RUN_COMPLETED",
                last_run_state="COMPLETED",
                published_family="/questions/3/question-pack",
            ),
            "completion basis",
        ),
    ]
    for item, message in cases:
        with pytest.raises(ContractError, match=message):
            item.validate()
    with pytest.raises(ContractError, match="sent dispatch"):
        replace(lease, dispatch_state=DispatchState.SENT).validate()


def test_snapshot_rejects_duplicate_owner_and_broken_grandfather(tmp_path: Path) -> None:
    base = QuestionScheduler(tmp_path / "scheduler").snapshot()
    now = "2026-08-26T12:00:00+00:00"
    later = "2026-08-26T12:10:00+00:00"
    questions = list(base.questions)
    for index, lease_id in ((2, "lease-3"), (3, "lease-4")):
        questions[index] = replace(
            questions[index],
            state=QueueState.LEASED,
            teacher_thread_id="same-owner",
            teacher_prompt_path=f"/prompt-{index}",
            lease=TeacherLease(lease_id, "same-owner", now, later),
        )
    with pytest.raises(ContractError, match="one Teacher owner"):
        replace(base, questions=tuple(questions), max_active=2).validate()

    broken = list(base.questions)
    broken[0] = replace(broken[0], state=QueueState.BACKLOG, completion_basis=None)
    with pytest.raises(ContractError, match="grandfathered"):
        replace(base, questions=tuple(broken)).validate()


def test_v1_waiting_migration_and_unknown_state(tmp_path: Path) -> None:
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    value = {
        "schema_version": 1,
        "sequence": 2,
        "questions": [
            {"question_id": "q1", "state": "ACTIVE", "updated_at": now.isoformat()},
            {
                "question_id": "q3",
                "state": "DEFERRED",
                "updated_at": now.isoformat(),
                "run_dir": str(tmp_path / "run"),
            },
        ],
    }
    snapshot = snapshot_from_dict(value, now=now, lease_ttl_sec=600)
    q3 = snapshot.questions[2]
    assert q3.state is QueueState.WAITING_EXTERNAL
    assert q3.wait is not None and q3.wait.kind == "LEGACY_DEFERRED"

    value["questions"][1]["state"] = "UNKNOWN"
    with pytest.raises(ContractError, match="unsupported v1 queue state"):
        snapshot_from_dict(value, now=now, lease_ttl_sec=600)


def test_v1_q3_q32_completion_requires_recovery_while_q1_q2_remain_grandfathered() -> None:
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    value = {
        "schema_version": 1,
        "sequence": 7,
        "questions": [
            {"question_id": "q1", "state": "COMPLETED", "updated_at": now.isoformat()},
            {"question_id": "q2", "state": "COMPLETED", "updated_at": now.isoformat()},
            {
                "question_id": "q3",
                "state": "COMPLETED",
                "updated_at": now.isoformat(),
                "last_run_state": "COMPLETED",
            },
            {
                "question_id": "q32",
                "state": "COMPLETED",
                "updated_at": now.isoformat(),
                "last_run_state": "COMPLETED",
            },
        ],
    }

    snapshot = snapshot_from_dict(value, now=now, lease_ttl_sec=600)

    assert all(item.state is QueueState.COMPLETED for item in snapshot.questions[:2])
    assert all(item.completion_basis == "GRANDFATHERED" for item in snapshot.questions[:2])
    for index in (2, 31):
        assert snapshot.questions[index].state is QueueState.RECOVERY_REQUIRED
        assert snapshot.questions[index].phase == "LEGACY_COMPLETION_REVIEW_REQUIRED"
        assert snapshot.questions[index].completion_basis is None


def test_v2_run_only_completion_is_reopened_for_publication_binding(tmp_path: Path) -> None:
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    value = QuestionScheduler(tmp_path / "scheduler").snapshot().to_dict()
    q3 = value["questions"][2]
    q3.update({
        "state": "COMPLETED",
        "phase": "COMPLETED",
        "last_run_state": "COMPLETED",
        "completion_basis": "RUN_COMPLETED",
    })

    snapshot = snapshot_from_dict(value, now=now, lease_ttl_sec=600)

    reopened = snapshot.questions[2]
    assert reopened.state is QueueState.RECOVERY_REQUIRED
    assert reopened.phase == "PUBLICATION_BINDING_REQUIRED"
    assert reopened.last_run_state == "COMPLETED"
    assert reopened.published_family is None
    assert reopened.completion_basis is None
