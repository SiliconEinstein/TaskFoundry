"""Harbor 独立 Job 的持久排队与全局并发准入。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4

from .model import ContractError


HARBOR_MAX_ACTIVE_JOBS = 200


class HarborQueueState(StrEnum):
    """Harbor Job 在全局调度门中的状态。"""

    WAITING = "WAITING"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    PLATFORM_FAILED = "PLATFORM_FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


@dataclass(frozen=True)
class HarborQueueEntry:
    """一个绑定单任务 JobConfig 的持久队列条目。"""

    request_id: str
    job_name: str
    job_config_path: str
    state: HarborQueueState
    position: int
    updated_at: str
    claim_id: str | None = None
    worker_id: str | None = None

    def validate(self) -> None:
        """校验身份、绝对路径和认领字段。"""
        if not self.request_id.strip() or not self.job_name.strip() or self.position < 1:
            raise ContractError("Harbor queue identity and positive position are required")
        if not Path(self.job_config_path).is_absolute():
            raise ContractError("Harbor queue JobConfig path must be absolute")
        if bool(self.claim_id) != bool(self.worker_id):
            raise ContractError("Harbor claim and worker identities must be recorded together")
        if self.state is HarborQueueState.ACTIVE and not self.claim_id:
            raise ContractError("active Harbor job requires a claim")
        if self.state is HarborQueueState.WAITING and self.claim_id:
            raise ContractError("waiting Harbor job cannot retain a claim")

    def to_dict(self) -> dict[str, Any]:
        """返回稳定 JSON 表示。"""
        self.validate()
        value = asdict(self)
        value["state"] = self.state.value
        return value


@dataclass(frozen=True)
class HarborQueueSnapshot:
    """硬上限为 200 个活动 Job 的持久快照。"""

    sequence: int
    jobs: tuple[HarborQueueEntry, ...]
    max_active: int = HARBOR_MAX_ACTIVE_JOBS
    schema_version: int = 1

    def validate(self) -> None:
        """拒绝上限漂移、重复身份和超额准入。"""
        if self.schema_version != 1 or self.sequence < 0 or self.max_active != HARBOR_MAX_ACTIVE_JOBS:
            raise ContractError("unsupported Harbor queue snapshot")
        requests = [item.request_id for item in self.jobs]
        names = [item.job_name for item in self.jobs]
        positions = [item.position for item in self.jobs]
        if len(requests) != len(set(requests)) or len(names) != len(set(names)):
            raise ContractError("Harbor request and job identities must be unique")
        if len(positions) != len(set(positions)):
            raise ContractError("Harbor queue positions must be unique")
        if sum(item.state is HarborQueueState.ACTIVE for item in self.jobs) > self.max_active:
            raise ContractError("active Harbor job count exceeds 200")
        for item in self.jobs:
            item.validate()

    def to_dict(self) -> dict[str, Any]:
        """返回稳定 JSON 表示。"""
        self.validate()
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "max_active": self.max_active,
            "jobs": [item.to_dict() for item in self.jobs],
        }


class HarborJobQueue:
    """原子提交、认领和结束独立 Harbor Job。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_path = root / "harbor-queue.json"
        self.lock_path = root / "harbor-queue.lock"

    def snapshot(self) -> HarborQueueSnapshot:
        """读取当前队列；未初始化时返回空快照。"""
        if not self.state_path.is_file():
            return HarborQueueSnapshot(sequence=0, jobs=())
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        jobs = tuple(
            HarborQueueEntry(**item | {"state": HarborQueueState(item["state"])})
            for item in value.get("jobs", ())
        )
        snapshot = HarborQueueSnapshot(
            sequence=value["sequence"],
            jobs=jobs,
            max_active=value.get("max_active", HARBOR_MAX_ACTIVE_JOBS),
            schema_version=value.get("schema_version", 1),
        )
        snapshot.validate()
        return snapshot

    def submit(self, request_id: str, job_config_path: Path) -> HarborQueueEntry:
        """幂等提交一个单任务、单 trial、LBG Job。"""
        if not request_id.strip():
            raise ContractError("Harbor request identity is required")
        resolved = job_config_path.resolve()
        job_name = _validate_independent_job(resolved)
        with self._locked():
            current = self.snapshot()
            existing = next((item for item in current.jobs if item.request_id == request_id), None)
            if existing:
                if existing.job_config_path != str(resolved) or existing.job_name != job_name:
                    raise ContractError("request ID already identifies another Harbor job")
                return existing
            if any(item.job_name == job_name for item in current.jobs):
                raise ContractError("Harbor job name is already queued")
            entry = HarborQueueEntry(
                request_id=request_id,
                job_name=job_name,
                job_config_path=str(resolved),
                state=HarborQueueState.WAITING,
                position=max((item.position for item in current.jobs), default=0) + 1,
                updated_at=datetime.now(UTC).isoformat(),
            )
            self._write(replace(current, sequence=current.sequence + 1, jobs=(*current.jobs, entry)))
            return entry

    def claim(self, *, worker_id: str, limit: int = HARBOR_MAX_ACTIVE_JOBS) -> tuple[HarborQueueEntry, ...]:
        """按 FIFO 认领空闲槽；超过 200 的 Job 保持等待。"""
        if not worker_id.strip() or not 1 <= limit <= HARBOR_MAX_ACTIVE_JOBS:
            raise ContractError("worker identity and claim limit between 1 and 200 are required")
        with self._locked():
            current = self.snapshot()
            active = sum(item.state is HarborQueueState.ACTIVE for item in current.jobs)
            available = min(limit, current.max_active - active)
            if available <= 0:
                return ()
            claimed: list[HarborQueueEntry] = []
            jobs = list(current.jobs)
            now = datetime.now(UTC).isoformat()
            for index, item in enumerate(jobs):
                if item.state is not HarborQueueState.WAITING:
                    continue
                updated = replace(
                    item,
                    state=HarborQueueState.ACTIVE,
                    claim_id=str(uuid4()),
                    worker_id=worker_id,
                    updated_at=now,
                )
                jobs[index] = updated
                claimed.append(updated)
                if len(claimed) == available:
                    break
            if not claimed:
                return ()
            self._write(replace(current, sequence=current.sequence + 1, jobs=tuple(jobs)))
            return tuple(claimed)

    def claim_request(self, request_id: str, *, worker_id: str) -> HarborQueueEntry | None:
        """让单个 Researcher 仅在 FIFO 可用范围内认领自己的请求。"""
        if not worker_id.strip():
            raise ContractError("Harbor worker identity is required")
        with self._locked():
            current = self.snapshot()
            active = sum(item.state is HarborQueueState.ACTIVE for item in current.jobs)
            available = current.max_active - active
            if available <= 0:
                return None
            waiting = [item for item in current.jobs if item.state is HarborQueueState.WAITING]
            eligible = {item.request_id for item in waiting[:available]}
            if request_id not in eligible:
                return None
            jobs = list(current.jobs)
            found = next(index for index, item in enumerate(jobs) if item.request_id == request_id)
            claimed = replace(
                jobs[found],
                state=HarborQueueState.ACTIVE,
                claim_id=str(uuid4()),
                worker_id=worker_id,
                updated_at=datetime.now(UTC).isoformat(),
            )
            jobs[found] = claimed
            self._write(replace(current, sequence=current.sequence + 1, jobs=tuple(jobs)))
            return claimed

    def complete(
        self,
        request_id: str,
        *,
        claim_id: str,
        terminal_state: HarborQueueState,
    ) -> HarborQueueEntry:
        """结束一个已认领 Job；平台失败不会写入科学尝试账本。"""
        allowed = {
            HarborQueueState.COMPLETED,
            HarborQueueState.PLATFORM_FAILED,
            HarborQueueState.RECOVERY_REQUIRED,
        }
        if terminal_state not in allowed:
            raise ContractError("Harbor completion requires a terminal queue state")
        with self._locked():
            current = self.snapshot()
            jobs = list(current.jobs)
            found = next((index for index, item in enumerate(jobs) if item.request_id == request_id), None)
            if found is None:
                raise ContractError("Harbor request is not queued")
            item = jobs[found]
            if item.state is not HarborQueueState.ACTIVE or item.claim_id != claim_id:
                raise ContractError("Harbor claim is stale or unavailable")
            completed = replace(item, state=terminal_state, updated_at=datetime.now(UTC).isoformat())
            jobs[found] = completed
            self._write(replace(current, sequence=current.sequence + 1, jobs=tuple(jobs)))
            return completed

    def reclaim_stale(
        self,
        request_id: str,
        *,
        claim_id: str,
        stale_after_sec: int,
        evidence_path: Path,
    ) -> HarborQueueEntry:
        """Return an abandoned ACTIVE claim to WAITING, never to a result state.

        Reclamation is deliberately explicit: callers must preserve a readable
        interruption record and prove the lease age.  A stale claim cannot be
        silently overwritten or counted as a scientific attempt.
        """
        if stale_after_sec < 1 or not evidence_path.is_file():
            raise ContractError("stale reclaim requires a positive age and evidence file")
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("stale reclaim evidence must be valid JSON") from exc
        if not isinstance(evidence, dict) or evidence.get("request_id") != request_id:
            raise ContractError("stale reclaim evidence does not bind the request")
        with self._locked():
            current = self.snapshot()
            index = next((i for i, item in enumerate(current.jobs) if item.request_id == request_id), None)
            if index is None:
                raise ContractError("Harbor request is not queued")
            item = current.jobs[index]
            if item.state is not HarborQueueState.ACTIVE or item.claim_id != claim_id:
                raise ContractError("Harbor claim is stale or unavailable")
            updated_at = datetime.fromisoformat(item.updated_at)
            age = (datetime.now(UTC) - updated_at).total_seconds()
            if age < stale_after_sec:
                raise ContractError("Harbor claim has not exceeded stale lease age")
            reclaimed = replace(
                item,
                state=HarborQueueState.WAITING,
                claim_id=None,
                worker_id=None,
                updated_at=datetime.now(UTC).isoformat(),
            )
            jobs = list(current.jobs)
            jobs[index] = reclaimed
            self._write(replace(current, sequence=current.sequence + 1, jobs=tuple(jobs)))
            return reclaimed

    def authorize(self, request_id: str, *, claim_id: str, job_config_path: Path) -> HarborQueueEntry:
        """确认正式 Researcher 只执行已经取得全局槽位的精确 JobConfig。"""
        resolved = str(job_config_path.resolve())
        with self._locked():
            current = self.snapshot()
            item = next((job for job in current.jobs if job.request_id == request_id), None)
            if (
                item is None
                or item.state is not HarborQueueState.ACTIVE
                or item.claim_id != claim_id
                or item.job_config_path != resolved
            ):
                raise ContractError("Harbor job lacks an active matching claim")
            return item

    def _write(self, snapshot: HarborQueueSnapshot) -> None:
        snapshot.validate()
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="harbor-queue-", suffix=".json", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(snapshot.to_dict(), stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _locked(self) -> _FileLock:
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(mode=0o600, exist_ok=True)
        return _FileLock(self.lock_path)


def _validate_independent_job(path: Path) -> str:
    """确认一个 JobConfig 只会请求一个 LBG 沙盒任务。"""
    if not path.is_file():
        raise ContractError("Harbor JobConfig must exist")
    value = json.loads(path.read_text(encoding="utf-8"))
    job_name = value.get("job_name")
    if not isinstance(job_name, str) or not job_name.strip():
        raise ContractError("Harbor job name is required")
    if value.get("n_concurrent_trials") != 1:
        raise ContractError("independent Harbor job requires a single trial")
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1:
        raise ContractError("independent Harbor job requires exactly one task")
    task_path = tasks[0].get("path") if isinstance(tasks[0], dict) else None
    if not isinstance(task_path, str) or not Path(task_path).is_absolute():
        raise ContractError("Harbor task path must be absolute")
    environment = value.get("environment")
    if not isinstance(environment, dict) or environment.get("type") != "lbg":
        raise ContractError("independent Harbor job requires an LBG environment")
    return job_name


class _FileLock:
    """队列状态使用的进程级排他锁。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream = None

    def __enter__(self) -> None:
        self.stream = self.path.open("r+")
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX)

    def __exit__(self, exc_type, exc, traceback) -> None:
        assert self.stream is not None
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()
