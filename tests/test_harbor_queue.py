from __future__ import annotations

import json
from pathlib import Path

import pytest

from taskfoundry.harbor_queue import (
    HarborJobQueue,
    HarborQueueEntry,
    HarborQueueSnapshot,
    HarborQueueState,
)
from taskfoundry.model import ContractError


def _config(tmp_path: Path, index: int, *, trials: int = 1, task_count: int = 1) -> Path:
    tasks = []
    for task_index in range(task_count):
        task = tmp_path / f"task-{index}-{task_index}"
        task.mkdir()
        tasks.append({"path": str(task)})
    jobs = tmp_path / f"jobs-{index}"
    jobs.mkdir()
    path = tmp_path / f"job-{index}.json"
    path.write_text(json.dumps({
        "job_name": f"job-{index}",
        "jobs_dir": str(jobs),
        "n_concurrent_trials": trials,
        "environment": {"type": "lbg", "kwargs": {"project_id": 42}},
        "tasks": tasks,
    }))
    return path


def test_harbor_queue_claims_two_hundred_independent_jobs(tmp_path: Path) -> None:
    queue = HarborJobQueue(tmp_path / "queue")
    for index in range(201):
        queue.submit(f"request-{index}", _config(tmp_path, index))

    claimed = queue.claim(worker_id="worker-1", limit=200)
    snapshot = queue.snapshot()

    assert len(claimed) == 200
    assert sum(item.state is HarborQueueState.ACTIVE for item in snapshot.jobs) == 200
    assert [item.request_id for item in snapshot.jobs if item.state is HarborQueueState.WAITING] == [
        "request-200"
    ]


def test_harbor_queue_releases_slot_and_claims_next_job(tmp_path: Path) -> None:
    queue = HarborJobQueue(tmp_path / "queue")
    for index in range(201):
        queue.submit(f"request-{index}", _config(tmp_path, index))
    claimed = queue.claim(worker_id="worker-1", limit=200)

    queue.complete(
        "request-0",
        claim_id=claimed[0].claim_id,
        terminal_state=HarborQueueState.COMPLETED,
    )
    next_claim = queue.claim(worker_id="worker-2", limit=200)

    assert [item.request_id for item in next_claim] == ["request-200"]


def test_single_researcher_claim_respects_full_global_queue(tmp_path: Path) -> None:
    queue = HarborJobQueue(tmp_path / "queue")
    for index in range(201):
        queue.submit(f"request-{index}", _config(tmp_path, index))
    queue.claim(worker_id="worker-1", limit=200)

    assert queue.claim_request("request-200", worker_id="worker-2") is None


@pytest.mark.parametrize(
    ("trials", "task_count", "message"),
    [
        (2, 1, "single trial"),
        (1, 2, "exactly one task"),
    ],
)
def test_harbor_queue_rejects_shared_job_shapes(
    tmp_path: Path,
    trials: int,
    task_count: int,
    message: str,
) -> None:
    queue = HarborJobQueue(tmp_path / "queue")

    with pytest.raises(ContractError, match=message):
        queue.submit("request-1", _config(tmp_path, 1, trials=trials, task_count=task_count))


def test_harbor_queue_submit_is_idempotent_but_rejects_request_drift(tmp_path: Path) -> None:
    queue = HarborJobQueue(tmp_path / "queue")
    first = _config(tmp_path, 1)
    queue.submit("request-1", first)

    assert queue.submit("request-1", first).request_id == "request-1"
    with pytest.raises(ContractError, match="another Harbor job"):
        queue.submit("request-1", _config(tmp_path, 2))


def test_harbor_queue_rejects_invalid_public_operations(tmp_path: Path) -> None:
    queue = HarborJobQueue(tmp_path / "queue")
    config = _config(tmp_path, 1)
    with pytest.raises(ContractError, match="request identity"):
        queue.submit("", config)
    with pytest.raises(ContractError, match="must exist"):
        queue.submit("request-missing", tmp_path / "missing.json")
    with pytest.raises(ContractError, match="worker identity"):
        queue.claim(worker_id="", limit=1)
    with pytest.raises(ContractError, match="between 1 and 200"):
        queue.claim(worker_id="worker", limit=201)
    with pytest.raises(ContractError, match="terminal"):
        queue.complete("request-1", claim_id="claim", terminal_state=HarborQueueState.WAITING)
    with pytest.raises(ContractError, match="not queued"):
        queue.complete("request-1", claim_id="claim", terminal_state=HarborQueueState.COMPLETED)


def test_harbor_queue_rejects_stale_claim_and_allows_exact_authorization(tmp_path: Path) -> None:
    queue = HarborJobQueue(tmp_path / "queue")
    config = _config(tmp_path, 1)
    queue.submit("request-1", config)
    claimed = queue.claim_request("request-1", worker_id="worker")
    assert claimed is not None and claimed.claim_id is not None

    assert queue.authorize(
        "request-1",
        claim_id=claimed.claim_id,
        job_config_path=config,
    ).request_id == "request-1"
    with pytest.raises(ContractError, match="matching claim"):
        queue.authorize("request-1", claim_id="stale", job_config_path=config)
    with pytest.raises(ContractError, match="stale"):
        queue.complete(
            "request-1",
            claim_id="stale",
            terminal_state=HarborQueueState.RECOVERY_REQUIRED,
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"job_name": ""}, "job name"),
        ({"environment": {"type": "docker"}}, "LBG"),
        ({"tasks": [{"path": "relative"}]}, "absolute"),
    ],
)
def test_harbor_queue_rejects_invalid_independent_job_contract(
    tmp_path: Path,
    change: dict,
    message: str,
) -> None:
    config = _config(tmp_path, 1)
    value = json.loads(config.read_text()) | change
    config.write_text(json.dumps(value))

    with pytest.raises(ContractError, match=message):
        HarborJobQueue(tmp_path / "queue").submit("request-1", config)


def test_harbor_queue_snapshot_rejects_duplicate_and_over_capacity_entries(tmp_path: Path) -> None:
    config = _config(tmp_path, 1)
    entry = HarborQueueEntry(
        request_id="request-1",
        job_name="job-1",
        job_config_path=str(config.resolve()),
        state=HarborQueueState.WAITING,
        position=1,
        updated_at="2026-08-24T00:00:00+00:00",
    )
    with pytest.raises(ContractError, match="unique"):
        HarborQueueSnapshot(sequence=1, jobs=(entry, entry)).validate()

    active = tuple(
        HarborQueueEntry(
            request_id=f"request-{index}",
            job_name=f"job-{index}",
            job_config_path=str(config.resolve()),
            state=HarborQueueState.ACTIVE,
            position=index + 1,
            updated_at="2026-08-24T00:00:00+00:00",
            claim_id=f"claim-{index}",
            worker_id="worker",
        )
        for index in range(201)
    )
    with pytest.raises(ContractError, match="exceeds 200"):
        HarborQueueSnapshot(sequence=1, jobs=active).validate()
