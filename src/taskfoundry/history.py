"""把历史出题证据整理到新的编号题目目录。"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from taskfoundry.history_transaction import (
    assert_no_symlink_components,
    commit_replacements,
    exclusive_history_lock,
    staging_directory,
)

# LC-INDEX: trace-selection
# LC-INDEX: state-evidence
# LC-INDEX: question-ledger
# LC-INDEX: transactional-apply

# LC-SECTION: trace-selection | 选择、扫描并复制安全的历史 trace
TRACE_DIRECTORY_NAMES = frozenset(
    {
        "attempt",
        "attempts",
        "audit",
        "audits",
        "checkpoint",
        "checkpoints",
        "design",
        "evidence",
        "harbor",
        "progression",
        "receipt",
        "receipts",
        "report",
        "reports",
        "request",
        "requests",
        "researcher",
        "review",
        "reviewer",
        "result",
        "results",
        "seal",
        "seals",
        "trace",
        "traces",
        "trajectory",
        "trajectories",
        "validation",
    }
)
TRACE_NAME_FRAGMENTS = (
    "abandon",
    "attempt",
    "audit",
    "checkpoint",
    "complete",
    "decision",
    "evidence",
    "final",
    "job",
    "pass",
    "receipt",
    "reject",
    "report",
    "request",
    "result",
    "reward",
    "seal",
    "trace",
    "trajectory",
)
TRACE_DIRECTORY_FRAGMENTS = ("agent", "blind", "harbor", "hint", "job", "platform", "runtime", "verifier")
TRACE_EXTENSIONS = frozenset(
    {".csv", ".json", ".jsonl", ".log", ".md", ".sha256", ".sum", ".toml", ".tsv", ".txt", ".yaml", ".yml"}
)
ALWAYS_TRACE_FILES = frozenset({"events.jsonl", "run.json", "SHA256SUMS", "state.json"})
EXCLUDED_PATH_FRAGMENTS = (
    "answer",
    "capability",
    "credential",
    "ground-truth",
    "ground_truth",
    "hidden_oracle",
    "honest",
    "oracle",
    "private_reference",
    "private-reference",
    "secret",
    "solution",
    "token",
)
EXCLUDED_DIRECTORY_NAMES = frozenset({"ground_truth", "package", "private", "question-revisions", "tests"})
TERMINAL_NEGATIVE = ("INVALIDAT", "ABANDON", "REJECT", "REDESIGN", "SUPERSEDE")
TERMINAL_COMPLETE = ("PASS_FINAL", "THREE_BLIND_PASS", "COMPLETE")
SENSITIVE_CONTENT_PATTERNS = (
    ("github_classic_token", re.compile(rb"ghp_[A-Za-z0-9]{20,}")),
    ("github_fine_grained_token", re.compile(rb"github_pat_[A-Za-z0-9_]{20,}")),
    ("api_secret", re.compile(rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(rb"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----")),
    ("bearer_token", re.compile(rb"(?i:authorization)\s*:\s*(?i:bearer)\s+[A-Za-z0-9._-]{12,}")),
)


def sha256_file(path: Path) -> str:
    """计算一个普通文件的 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    """通过同目录原子替换写入 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_text(path: Path, value: str) -> None:
    """通过同目录原子替换写入 UTF-8 文本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def question_number_from_run_name(name: str) -> int | None:
    """解析 ``q3-*`` 与 ``q03-*``，同时避免把 Q30 误识别为 Q3。"""
    match = re.match(r"^q0*([1-9]|[12][0-9]|3[0-2])(?:-|$)", name, re.IGNORECASE)
    return int(match.group(1)) if match else None


def is_trace_path(run_root: Path, path: Path) -> bool:
    """按路径选择 trace 候选，先排除题包、答案和私有材料。"""
    if not path.is_file() or path.is_symlink():
        return False
    relative = path.relative_to(run_root)
    components = [part.lower() for part in relative.parts]
    parent_names = set(components[:-1])
    if parent_names & EXCLUDED_DIRECTORY_NAMES:
        return False
    if any(fragment in component for component in components for fragment in EXCLUDED_PATH_FRAGMENTS):
        return False
    name = path.name.lower()
    selected = bool(parent_names & TRACE_DIRECTORY_NAMES)
    selected = selected or any(fragment in parent for parent in parent_names for fragment in TRACE_DIRECTORY_FRAGMENTS)
    selected = selected or path.name in ALWAYS_TRACE_FILES
    selected = selected or any(fragment in name for fragment in TRACE_NAME_FRAGMENTS)
    return selected and (path.suffix.lower() in TRACE_EXTENSIONS or path.name in ALWAYS_TRACE_FILES)


def sensitive_content_reason(path: Path) -> str | None:
    """流式扫描常见凭据格式；只返回类型，不回显秘密。"""
    overlap = b""
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            payload = overlap + chunk
            for reason, pattern in SENSITIVE_CONTENT_PATTERNS:
                if pattern.search(payload):
                    return reason
            overlap = payload[-256:]
    return None


def is_trace_file(run_root: Path, path: Path) -> bool:
    """只选择不含已知凭据格式的安全 trace。"""
    return is_trace_path(run_root, path) and sensitive_content_reason(path) is None


def selected_trace_files(run_root: Path) -> list[Path]:
    """为一个 TaskFoundry run 返回确定性的 trace 文件集合。"""
    return sorted(path for path in run_root.rglob("*") if is_trace_file(run_root, path))


def sensitive_trace_exclusions(run_root: Path) -> list[dict[str, Any]]:
    """记录因内容级凭据扫描被排除的 trace 元数据，不复制其内容。"""
    excluded: list[dict[str, Any]] = []
    for path in sorted(run_root.rglob("*")):
        if not is_trace_path(run_root, path):
            continue
        reason = sensitive_content_reason(path)
        if reason:
            excluded.append(
                {
                    "path": path.relative_to(run_root).as_posix(),
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                    "reason": reason,
                }
            )
    return excluded


def snapshot_trace_file(
    source: Path,
    destination: Path,
    *,
    snapshot_root: Path | None = None,
    source_root: Path | None = None,
) -> str:
    """复制一个 trace；拒绝源、目标及父目录中的符号链接。"""
    if source_root is not None:
        assert_no_symlink_components(source_root, source.parent)
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"trace 源文件必须是普通文件: {source}")
    if snapshot_root is not None:
        assert_no_symlink_components(snapshot_root, destination.parent)
    source_digest = sha256_file(source)
    if destination.exists():
        if destination.is_symlink() or not destination.is_file() or sha256_file(destination) != source_digest:
            raise RuntimeError(f"trace snapshot conflict: {destination}")
        return source_digest
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        shutil.copy2(source, temporary)
        if sha256_file(temporary) != source_digest:
            raise RuntimeError(f"trace snapshot digest mismatch: {source}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return source_digest


# LC-SECTION: state-evidence | 解析 run 状态与独立历史证据
def load_json_object(path: Path) -> dict[str, Any] | None:
    """读取 JSON 对象；损坏的历史文件仍可由路径被索引。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _status_from_text(value: str) -> str | None:
    """把显式终态文本归一化；INCOMPLETE 不得命中 COMPLETE。"""
    upper = value.upper()
    if "INCOMPLETE" in upper:
        return None
    if any(token in upper for token in TERMINAL_NEGATIVE):
        return "INVALIDATED_OR_ABANDONED"
    if "TOO_EASY" in upper:
        return "TOO_EASY"
    if any(token in upper for token in TERMINAL_COMPLETE):
        return "HISTORICAL_COMPLETE_CLAIM"
    return None


def _terminal_file_signal(path: Path) -> str | None:
    """只从终态标记文件名提取 run 状态，避免组件 PASS 冒充整题完成。"""
    return _status_from_text(path.name)


def _metadata_signal(path: Path, metadata: dict[str, Any] | None) -> tuple[int, str, bool] | None:
    """读取 state/run 根元数据；其时间与终态标记统一比较。"""
    if not metadata or path.is_symlink():
        return None
    explicit = metadata.get("state") or metadata.get("stage") or metadata.get("status")
    if not isinstance(explicit, str) or explicit.upper() == "ACTIVE":
        return None
    modified = path.stat().st_mtime_ns if path.is_file() else 0
    normalized = _status_from_text(explicit)
    return modified, normalized or explicit.upper(), normalized is not None


def run_status(run_root: Path, state: dict[str, Any] | None, run_metadata: dict[str, Any] | None = None) -> str:
    """优先采用最新终态证据；陈旧 ACTIVE 不能表示真实运行。"""
    terminal_signals: list[tuple[int, str]] = []
    fallback_signals: list[tuple[int, str]] = []
    for path, metadata in ((run_root / "state.json", state), (run_root / "run.json", run_metadata)):
        signal = _metadata_signal(path, metadata)
        if signal:
            modified, status, terminal = signal
            (terminal_signals if terminal else fallback_signals).append((modified, status))
    for path in selected_trace_files(run_root):
        signal = _terminal_file_signal(path)
        if signal:
            terminal_signals.append((path.stat().st_mtime_ns, signal))
    if terminal_signals:
        return max(terminal_signals, key=lambda item: item[0])[1]
    if fallback_signals:
        return max(fallback_signals, key=lambda item: item[0])[1]
    return "UNRESOLVED_HISTORICAL"


def run_record(run_root: Path, snapshot_root: Path, *, apply: bool) -> dict[str, Any]:
    """构建一个 run 的记录，并可选保存其 trace 子集。"""
    if run_root.is_symlink() or not run_root.is_dir():
        raise RuntimeError(f"run 根目录必须是普通目录: {run_root}")
    state = load_json_object(run_root / "state.json")
    run_metadata = load_json_object(run_root / "run.json")
    metadata = state or run_metadata
    attempts = metadata.get("attempts", []) if metadata else []
    attempt_records = attempts if isinstance(attempts, list) else []
    scientific_scores = [
        attempt.get("score")
        for attempt in attempt_records
        if isinstance(attempt, dict) and attempt.get("classification") == "SCIENTIFIC_RESULT"
    ]
    platform_failures = sum(
        1
        for attempt in attempt_records
        if isinstance(attempt, dict) and attempt.get("classification") == "PLATFORM_FAILURE"
    )
    trace_files = selected_trace_files(run_root)
    sensitive_exclusions = sensitive_trace_exclusions(run_root)
    copied: list[dict[str, Any]] = []
    for source in trace_files:
        relative = source.relative_to(run_root)
        digest = sha256_file(source)
        if apply:
            digest = snapshot_trace_file(
                source,
                snapshot_root / run_root.name / relative,
                snapshot_root=snapshot_root,
                source_root=run_root,
            )
        copied.append({"path": relative.as_posix(), "sha256": digest, "size": source.stat().st_size})
    return {
        "run_name": run_root.name,
        "source_path": str(run_root),
        "historical_status": run_status(run_root, state, run_metadata),
        "sequence": metadata.get("sequence") if metadata else None,
        "declared_state": metadata.get("state") if metadata else None,
        "package_digest": (metadata.get("package_digest") or metadata.get("package_sha256")) if metadata else None,
        "scientific_attempt_count": len(scientific_scores),
        "scientific_scores": scientific_scores,
        "platform_failure_count": platform_failures,
        "trace_snapshot_path": str(snapshot_root / run_root.name),
        "trace_file_count": len(copied),
        "trace_bytes": sum(item["size"] for item in copied),
        "trace_files": copied,
        "sensitive_trace_exclusions": sensitive_exclusions,
    }


# LC-SECTION: question-ledger | 生成逐题与全局的可读总账
def latest_formal_verdict(question_root: Path) -> dict[str, str] | None:
    """读取 authoring trace 中最新的正式审查结论。"""
    review_root = question_root / "trace/authoring/reviews"
    reports = list(review_root.rglob("formal-review-report.json")) if review_root.is_dir() else []
    if reports:
        report = max(reports, key=lambda path: path.stat().st_mtime_ns)
        value = load_json_object(report) or {}
        verdict = value.get("verdict") or value.get("decision") or value.get("outcome")
        return {"verdict": str(verdict or "UNKNOWN").upper(), "path": str(report)}
    markdown = list(review_root.rglob("formal-review-report.md")) if review_root.is_dir() else []
    if not markdown:
        return None
    report = max(markdown, key=lambda path: path.stat().st_mtime_ns)
    head = report.read_text(encoding="utf-8", errors="replace")[:2000].upper()
    verdict = "REJECT" if "REJECT" in head else "PASS" if "PASS" in head else "UNKNOWN"
    return {"verdict": verdict, "path": str(report)}


def independent_legacy_assessment(question_root: Path, number: int) -> dict[str, Any] | None:
    """读取某题由独立代理生成的只读历史盘点。"""
    audit_root = question_root / "authoring-history-audits"
    if not audit_root.is_dir():
        return None
    for audit_path in sorted(audit_root.glob("inventory-*.json")):
        audit = load_json_object(audit_path) or {}
        questions = audit.get("questions")
        if not isinstance(questions, dict):
            continue
        for key in (f"Q{number}", f"Q{number:02d}"):
            assessment = questions.get(key)
            if isinstance(assessment, dict):
                return {
                    "source_path": str(audit_path),
                    "source_sha256": sha256_file(audit_path),
                    "assessment": assessment,
                }
    return None


def legacy_resolution(assessment_record: dict[str, Any] | None) -> str | None:
    """提取盘点原始终态，但不把它转换为新体系发布结论。"""
    if not assessment_record:
        return None
    assessment = assessment_record.get("assessment")
    if not isinstance(assessment, dict):
        return None
    for key in ("authoritative_historical_outcome", "question_status", "terminal_state", "workspace_state"):
        value = assessment.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def campaign_status(*, published: list[Path], verdict: dict[str, str] | None, brief_complete: bool) -> str:
    """只解析新体系状态；历史完成或未校验 manifest 均不能自动晋级。"""
    if published:
        return "PUBLICATION_PRESENT_REQUIRES_CANONICAL_VALIDATION"
    if verdict and verdict["verdict"].startswith("PASS"):
        return "FORMAL_PASS_AWAITING_HARBOR"
    if verdict and "REJECT" in verdict["verdict"]:
        return "FORMAL_REJECTED"
    if brief_complete:
        return "AUTHORING"
    return "LEGACY_EVIDENCE_CATALOGUED_AWAITING_SKILLBANK_RESOLVE"


def central_archive_record(runs_root: Path, number: int) -> dict[str, Any] | None:
    """绑定旧 new-question 中央归档及迁移批次中的逐题哈希记录。"""
    archive_root = runs_root.parent / "archive/question-authoring"
    question_archive = archive_root / f"q{number:02d}"
    batch_root = archive_root / "_migration_batches"
    if not question_archive.is_dir() and not batch_root.is_dir():
        return None
    batches: list[dict[str, Any]] = []
    if batch_root.is_dir():
        for manifest_path in sorted(batch_root.glob("*/PRE_MIGRATION_MANIFEST.json")):
            manifest = load_json_object(manifest_path) or {}
            questions = manifest.get("questions")
            if not isinstance(questions, list):
                continue
            for question in questions:
                if not isinstance(question, dict) or question.get("question") != f"Q{number:02d}":
                    continue
                encoded = json.dumps(question, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                batches.append(
                    {
                        "batch_id": manifest.get("batch_id"),
                        "manifest_path": str(manifest_path),
                        "manifest_sha256": sha256_file(manifest_path),
                        "question_record_sha256": hashlib.sha256(encoded).hexdigest(),
                        "entry_count": question.get("entry_count"),
                        "archive_destinations": [entry.get("destination") for entry in question.get("entries", [])],
                    }
                )
    return {
        "question_archive_root": str(question_archive),
        "question_archive_exists": question_archive.is_dir(),
        "migration_batches": batches,
    }


def question_record(
    number: int,
    *,
    question_root: Path,
    runs_root: Path,
    workbench_root: Path,
) -> dict[str, Any]:
    """整理一个编号题，但不把任何历史证据晋级为发布结果。"""
    root = question_root / str(number)
    authoring = root / "trace/authoring"
    snapshot_root = authoring / "imported-taskfoundry-runs"
    matching_runs = sorted(
        run for run in runs_root.iterdir() if run.is_dir() and question_number_from_run_name(run.name) == number
    )
    records = [run_record(run, snapshot_root, apply=False) for run in matching_runs]
    legacy_root = authoring / "pre-skillbank/legacy"
    legacy_sections = sorted(path.name for path in legacy_root.iterdir() if path.is_dir()) if legacy_root.is_dir() else []
    workbench = workbench_root / f"q{number:02d}"
    workbench_candidates = sorted(path.name for path in workbench.iterdir() if path.is_dir()) if workbench.is_dir() else []
    verdict = latest_formal_verdict(root)
    question_pack = root / "question-pack"
    published = list(question_pack.rglob("FAMILY_MANIFEST.json")) if question_pack.is_dir() else []
    brief_files = [root / "QuestionDesignBrief.json", root / "QuestionDesignBrief.md"]
    current_campaign_status = campaign_status(
        published=published,
        verdict=verdict,
        brief_complete=all(path.is_file() for path in brief_files),
    )
    legacy_assessment = independent_legacy_assessment(question_root, number)
    record = {
        "schema_version": 1,
        "question": f"Q{number:02d}",
        "campaign_status": current_campaign_status,
        "owner_confirmed_final": False,
        "brief": {
            "json": str(brief_files[0]) if brief_files[0].is_file() else None,
            "markdown": str(brief_files[1]) if brief_files[1].is_file() else None,
        },
        "latest_formal_review": verdict,
        "legacy_resolution": legacy_resolution(legacy_assessment),
        "independent_legacy_assessment": legacy_assessment,
        "legacy_sections": legacy_sections,
        "central_archive": central_archive_record(runs_root, number),
        "taskfoundry_runs": records,
        "workbench_candidates": workbench_candidates,
        "publication": {
            "root": str(question_pack),
            "family_manifests": [str(path) for path in sorted(published)],
            "clean": not any(question_pack.iterdir()) if question_pack.is_dir() else True,
        },
        "reuse_policy": "历史证据可以作为后续出题输入，但不等于通过新 Skill Bank 流程的最终题目。",
    }
    return record


def render_question_markdown(record: dict[str, Any]) -> str:
    """渲染便于人工阅读的逐题历史索引。"""
    lines = [
        f"# {record['question']} 出题历史",
        "",
        f"- 新体系状态：`{record['campaign_status']}`",
        f"- owner 确认最终完成：`{str(record['owner_confirmed_final']).lower()}`",
        f"- 历史 TaskFoundry run 数：`{len(record['taskfoundry_runs'])}`",
        f"- 独立旧进度结论：`{record['legacy_resolution'] or 'NOT_AUDITED'}`",
        f"- workbench 候选数：`{len(record['workbench_candidates'])}`",
        f"- 最终题族 manifest 数：`{len(record['publication']['family_manifests'])}`",
        "",
        "历史 PASS/COMPLETE 标签只作为证据，不会在新 Skill Bank 流程中自动发布题目。",
        "",
        "## TaskFoundry runs",
        "",
    ]
    if not record["taskfoundry_runs"]:
        lines.append("- 未发现历史 run。")
    for run in record["taskfoundry_runs"]:
        lines.append(
            f"- `{run['run_name']}` — `{run['historical_status']}`; "
            f"科学尝试 `{run['scientific_attempt_count']}` 次；trace 文件 `{run['trace_file_count']}` 个。"
        )
    lines.extend(["", "## 复用边界", "", str(record["reuse_policy"]), ""])
    return "\n".join(lines)


def render_global_markdown(ledger: dict[str, Any]) -> str:
    """渲染 Q3–Q32 的全局进度表。"""
    lines = [
        "# Q3–Q32 出题进度",
        "",
        "Q1、Q2 已由 owner 确认完成，本轮整理明确排除；两题目录曾在更早的清理批次中迁移。",
        "",
    ]
    lines.extend(f"- `{status}`: `{count}`" for status, count in sorted(ledger["status_counts"].items()))
    lines.extend(
        [
            "",
            "| 题号 | 新体系状态 | 独立旧进度结论 | 历史 run 数 |",
            "|---|---|---|---:|",
        ]
    )
    lines.extend(
        f"| {record['question']} | `{record['campaign_status']}` | "
        f"`{record['legacy_resolution'] or 'NOT_AUDITED'}` | {len(record['taskfoundry_runs'])} |"
        for record in ledger["questions"]
    )
    lines.append("")
    return "\n".join(lines)


# LC-SECTION: transactional-apply | 锁定、暂存并整批提交 Q3–Q32
def validate_prior_sources(question_root: Path, records: list[dict[str, Any]]) -> None:
    """旧索引一旦存在，就要求其源字节保持不变，防止静默重写历史。"""
    by_question = {record["question"]: record for record in records}
    for number in range(3, 33):
        index_path = question_root / str(number) / "trace/authoring/HISTORY_INDEX.json"
        previous = load_json_object(index_path)
        if not previous:
            continue
        question = by_question.get(previous.get("question"))
        if question is None:
            continue
        for run in previous.get("taskfoundry_runs", []):
            if not isinstance(run, dict):
                continue
            source_root = Path(str(run.get("source_path", "")))
            for item in run.get("trace_files", []):
                if not isinstance(item, dict):
                    continue
                source = source_root / str(item.get("path", ""))
                if not source.is_file() or source.is_symlink() or sha256_file(source) != item.get("sha256"):
                    raise RuntimeError(f"历史源 trace 已漂移: {source}")


def stage_consolidation(question_root: Path, records: list[dict[str, Any]], ledger: dict[str, Any], stage: Path) -> list[tuple[Path, Path]]:
    """在同文件系统完整构建 snapshot 与 ledger，再返回事务替换计划。"""
    replacements: list[tuple[Path, Path]] = []
    for record in records:
        number = int(str(record["question"])[1:])
        staged_snapshot = stage / f"q{number:02d}/imported-taskfoundry-runs"
        staged_snapshot.mkdir(parents=True)
        for run in record["taskfoundry_runs"]:
            source_root = Path(run["source_path"])
            for item in run["trace_files"]:
                source = source_root / item["path"]
                destination = staged_snapshot / run["run_name"] / item["path"]
                digest = snapshot_trace_file(
                    source,
                    destination,
                    snapshot_root=staged_snapshot,
                    source_root=source_root,
                )
                if digest != item["sha256"]:
                    raise RuntimeError(f"staging trace 摘要不匹配: {source}")
        staged_index = stage / f"q{number:02d}/HISTORY_INDEX.json"
        staged_markdown = stage / f"q{number:02d}/HISTORY_INDEX.md"
        atomic_json(staged_index, record)
        atomic_text(staged_markdown, render_question_markdown(record))
        authoring = question_root / str(number) / "trace/authoring"
        replacements.extend(
            [
                (staged_snapshot, authoring / "imported-taskfoundry-runs"),
                (staged_index, authoring / "HISTORY_INDEX.json"),
                (staged_markdown, authoring / "HISTORY_INDEX.md"),
            ]
        )
    staged_global_json = stage / "global/AUTHORING_PROGRESS.json"
    staged_global_md = stage / "global/AUTHORING_PROGRESS.md"
    atomic_json(staged_global_json, ledger)
    atomic_text(staged_global_md, render_global_markdown(ledger))
    replacements.extend(
        [
            (staged_global_json, question_root / "AUTHORING_PROGRESS.json"),
            (staged_global_md, question_root / "AUTHORING_PROGRESS.md"),
        ]
    )
    return replacements


def consolidate(
    numbers: Iterable[int],
    *,
    question_root: Path,
    runs_root: Path,
    workbench_root: Path,
    apply: bool,
) -> dict[str, Any]:
    """整理指定题号，并可选写入全局进度总账。"""
    records = [
        question_record(
            number,
            question_root=question_root,
            runs_root=runs_root,
            workbench_root=workbench_root,
        )
        for number in numbers
    ]
    status_counts: dict[str, int] = {}
    for record in records:
        status = record["campaign_status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    ledger = {
        "schema_version": 1,
        "scope": [record["question"] for record in records],
        "q1_q2_owner_confirmed": True,
        "q1_q2_excluded_from_this_consolidation": True,
        "q1_q2_historical_layout_was_previously_migrated": True,
        "status_counts": status_counts,
        "questions": records,
    }
    if apply:
        with exclusive_history_lock(question_root / ".history-consolidation.lock"):
            validate_prior_sources(question_root, records)
            with staging_directory(question_root) as stage:
                replacements = stage_consolidation(question_root, records, ledger, stage)
                commit_replacements(
                    replacements,
                    transaction_root=question_root,
                    backup_root=question_root / f".history-backup-{os.getpid()}",
                )
    return ledger
