"""把完成的线性验证 run 发布为 high/medium/low 题族。"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Any

from .labwright import ArtifactIdentity, EnvironmentReceipt
from .model import ContractError, RunSnapshot, RunState
from .package import lint_package, package_sha256
from .policy import file_sha256
from .researcher import ApprovedHint
from .resources import ResourceCatalogError, validate_resource_catalog
from .validation import AttemptEvidence, HealthEvidence, decide_validation


class PublicationError(ContractError):
    """完成 run 与待发布难度题族不一致。"""


QUESTION_ROOT = Path("/personal/codex-workspace/question-from-questions")
HINT_SECTION_HEADING = "## Teacher Hint"


def published_family_path(question: int) -> Path:
    """从题号派生唯一允许发布的 question-pack 路径。"""
    return QUESTION_ROOT / str(question) / "question-pack"


def validate_published_family(question: int, family: Path, run: RunSnapshot) -> None:
    """只接受由当前 COMPLETED 线性会话及其 Teacher hints 派生的题族。"""
    if not 3 <= question <= 32:
        raise PublicationError("Q1/Q2 不由当前发布合同管理")
    expected_family = published_family_path(question).resolve()
    if family.resolve() != expected_family:
        raise PublicationError(f"question-pack 必须位于题号目录: {expected_family}")
    if run.state is not RunState.COMPLETED or not run.package_digest or not run.package_path:
        raise PublicationError("发布题族必须绑定一个 COMPLETED run")
    _plain_tree(family)
    levels = {path.name for path in family.iterdir() if path.is_dir()}
    non_directories = [path.name for path in family.iterdir() if not path.is_dir()]
    if (
        non_directories not in ([], ["FAMILY_MANIFEST.json"])
        or levels not in ({"high", "medium"}, {"high", "medium", "low"})
    ):
        raise PublicationError("question-pack 只能包含 high/medium 或 high/medium/low 目录")
    high = family / "high"
    _validate_package(high)
    if package_sha256(high) != run.package_digest:
        raise PublicationError("high 题包不是 run 中已验证的冻结题包")

    attempts = tuple(AttemptEvidence(**value) for value in run.attempts)
    decision = decide_validation(
        attempts,
        HealthEvidence(True, True, True, True, True, True),
        final_health_bound=True,
    )
    if decision.action != "VALIDATION_PASSED":
        raise PublicationError("run 没有满足三轮 blind 失败后 hint 通过的线性验证合同")
    scientific = [item for item in attempts if item.classification == "SCIENTIFIC_RESULT"]
    blind = [item for item in scientific if item.mode == "blind"]
    hinted = [item for item in scientific if item.mode == "hint"]
    successful_hints = [
        item for item in hinted if item.score is not None and item.score >= 0.85
    ]
    if len(blind) != 3 or len(hinted) not in {1, 2} or not successful_hints:
        raise PublicationError("发布需要三轮 blind 和至少一轮通过的 Teacher hint")
    expected_levels = (
        ("medium",)
        if len(successful_hints) == 1
        else ("medium", "low")
    )
    if levels != {"high", *expected_levels}:
        raise PublicationError("难度目录数量必须与成功 hint 数量一致")
    for level, attempt in zip(expected_levels, successful_hints, strict=True):
        hints = _bound_hint_chain(run, attempt)
        hint_sha256 = hints[-1][1]
        package = family / level
        _validate_package(package)
        _validate_variant(
            package,
            level,
            run.package_digest,
            hint_sha256,
            tuple(item[1] for item in hints),
        )
        _validate_hint_only_variant(high, package, tuple(item[0] for item in hints))
    _validate_runtime_binding(run)


def finalize_published_family(question: int, family: Path, run: RunSnapshot) -> Path:
    """校验题族，并把最终 canonical Harbor 证据原子封签到题号 trace。"""
    validate_published_family(question, family, run)
    assert run.package_digest is not None
    final_root = QUESTION_ROOT / str(question) / "trace/final"
    final_root.mkdir(parents=True, exist_ok=True)
    target = final_root / f"package-{run.package_digest}"
    staging = Path(tempfile.mkdtemp(prefix=".trace-", dir=final_root))
    try:
        artifacts = _seal_round_artifacts(staging, run)
        artifacts.extend(_seal_runtime_artifacts(staging, run))
        manifest = {
            "schema_version": 1,
            "question": question,
            "run_id": run.run_id,
            "question_revision": run.question_revision,
            "package_sha256": run.package_digest,
            "published_family_sha256": {
                level: package_sha256(family / level)
                for level in ("high", "medium", "low")
                if (family / level).is_dir()
            },
            "environment_key": run.environment_key,
            "ordered_request_ids": [
                str(item["request_id"])
                for item in run.attempts
                if item.get("classification") == "SCIENTIFIC_RESULT"
            ],
            "validation_decision": run.evidence.get("validation_decision"),
            "artifacts": artifacts,
        }
        _write_manifest(staging / "TRACE_MANIFEST.json", manifest)
        _fsync_tree(staging)
        if target.exists():
            _validate_existing_trace(target, manifest)
            return target
        os.rename(staging, target)
        _fsync_directory(final_root)
        staging = target
        return target
    finally:
        if staging.exists() and staging != target:
            shutil.rmtree(staging)


def _seal_round_artifacts(staging: Path, run: RunSnapshot) -> list[dict[str, str]]:
    rounds = run.evidence.get("harbor_rounds")
    if isinstance(rounds, dict):
        return _seal_legacy_round_artifacts(staging, run, rounds)
    return _seal_persistent_round_artifacts(staging, run)


def _seal_legacy_round_artifacts(
    staging: Path,
    run: RunSnapshot,
    rounds: dict[str, Any],
) -> list[dict[str, str]]:
    """封签旧式一轮一请求 Harbor ledger。"""
    artifacts: list[dict[str, str]] = []
    labels = (
        ("request", "request.json"),
        ("capability", "capability.json"),
        ("job_config", "job-config.json"),
        ("job_result", "job-result.json"),
        ("trial_result", "trial-result.json"),
        ("job_log", "job.log"),
        ("provider_identity", "provider-identities.json"),
        ("round_history", "round-history.json"),
    )
    scientific_attempts = [
        attempt
        for attempt in run.attempts
        if attempt.get("classification") == "SCIENTIFIC_RESULT"
    ]
    for position, attempt in enumerate(scientific_attempts, start=1):
        request_id = attempt.get("request_id")
        value = rounds.get(request_id) if isinstance(request_id, str) else None
        if not isinstance(value, dict):
            raise PublicationError(f"科学轮次缺少 canonical 证据: {request_id}")
        destination = Path("rounds") / f"{position:02d}"
        for field, filename in labels:
            source_value = value.get(f"{field}_path")
            digest_value = value.get(f"{field}_sha256")
            artifacts.append(
                _copy_bound_artifact(
                    staging,
                    destination / filename,
                    source_value,
                    digest_value,
                )
            )
        if attempt.get("mode") == "hint":
            request = value.get("request")
            if not isinstance(request, dict):
                raise PublicationError("hint 轮次缺少 request 内的提示绑定")
            artifacts.append(
                _copy_bound_artifact(
                    staging,
                    destination / "approved-hint.json",
                    request.get("approved_hint_path"),
                    request.get("approved_hint_sha256"),
                )
            )
    return artifacts


def _seal_persistent_round_artifacts(
    staging: Path,
    run: RunSnapshot,
) -> list[dict[str, str]]:
    """封签一次请求承载完整线性 progression 的 canonical 证据。"""
    sessions = run.evidence.get("persistent_harbor_sessions")
    if not isinstance(sessions, dict):
        raise PublicationError("COMPLETED run 缺少 canonical Harbor round ledger")
    artifacts: list[dict[str, str]] = []
    common_labels = (
        ("request", "request.json"),
        ("capability", "capability.json"),
        ("job_config", "job-config.json"),
        ("job_result", "job-result.json"),
        ("trial_result", "trial-result.json"),
        ("job_log", "job.log"),
        ("provider_identity", "provider-identities.json"),
    )
    scientific_attempts = [
        attempt
        for attempt in run.attempts
        if attempt.get("classification") == "SCIENTIFIC_RESULT"
    ]
    for position, raw_attempt in enumerate(scientific_attempts, start=1):
        attempt = AttemptEvidence(**raw_attempt)
        session, round_value = _persistent_round_binding(run, attempt)
        destination = Path("rounds") / f"{position:02d}"
        for field, filename in common_labels:
            artifacts.append(
                _copy_bound_artifact(
                    staging,
                    destination / filename,
                    session.get(f"{field}_path"),
                    session.get(f"{field}_sha256"),
                )
            )
        for field, filename in (
            ("result", "round-result.json"),
            ("decision", "round-decision.json"),
            ("artifact_manifest", "artifact-manifest.json"),
        ):
            artifacts.append(
                _copy_bound_artifact(
                    staging,
                    destination / filename,
                    round_value.get(f"{field}_path"),
                    round_value.get(f"{field}_sha256"),
                )
            )
        if attempt.mode == "hint":
            hint, hint_sha256 = _bound_hint(run, attempt)
            round_index = int(round_value["round_index"])
            parent = attempt.request_id.rsplit(".round-", 1)[0]
            previous_attempt = _attempt_by_request_id(
                run,
                parent + f".round-{round_index - 1:02d}",
            )
            _previous_session, previous_round = _persistent_round_binding(
                run,
                previous_attempt,
            )
            relative = destination / "approved-hint.json"
            _write_derived_hint(staging / relative, hint, hint_sha256)
            artifacts.append(
                {
                    "path": relative.as_posix(),
                    "sha256": hint_sha256,
                    "source_path": str(Path(str(previous_round["decision_path"])).resolve()),
                }
            )
    return artifacts


def _seal_runtime_artifacts(staging: Path, run: RunSnapshot) -> list[dict[str, str]]:
    closure = run.evidence.get("runtime_closure")
    environment = run.evidence.get("environment")
    if not isinstance(closure, dict) or not isinstance(environment, dict):
        raise PublicationError("final trace 缺少 runtime closure 或 environment manifest")
    artifacts = [
        _copy_bound_artifact(
            staging,
            Path("runtime/runtime-closure.json"),
            closure.get("path"),
            closure.get("sha256"),
        ),
        _copy_bound_artifact(
            staging,
            Path("runtime/environment-manifest.json"),
            environment.get("manifest_path"),
            environment.get("manifest_sha256"),
        ),
    ]
    manifest_path = Path(str(environment.get("manifest_path", "")))
    closure_path = Path(str(closure.get("path", "")))
    suffix = manifest_path.stem.removeprefix("environment-manifest")
    optional_sources = [
        (
            "environment-receipt.json",
            manifest_path.with_name(f"environment-receipt{suffix}.json"),
        ),
        (
            "clean-sandbox-evidence.json",
            manifest_path.with_name(f"clean-sandbox-evidence{suffix}.json"),
        ),
        ("baseline-runtime.json", manifest_path.with_name("baseline-runtime.json")),
    ]
    closure_value = _strict_object(closure_path)
    optional_sources.append(
        (
            "scientific-trace.json",
            Path(str(closure_value.get("scientific_trace_path", ""))),
        )
    )
    for filename, source in optional_sources:
        if source.is_file() and not source.is_symlink():
            artifacts.append(
                _copy_bound_artifact(
                    staging,
                    Path("runtime") / filename,
                    str(source.resolve()),
                    file_sha256(source),
                )
            )
    return artifacts


def _copy_bound_artifact(
    staging: Path,
    relative: Path,
    source_value: object,
    digest_value: object,
) -> dict[str, str]:
    if not isinstance(source_value, str) or not isinstance(digest_value, str):
        raise PublicationError(f"final trace 证据绑定不完整: {relative}")
    source = Path(source_value)
    if not source.is_file() or source.is_symlink() or file_sha256(source) != digest_value:
        raise PublicationError(f"final trace canonical 证据漂移: {source}")
    destination = staging / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    os.chmod(destination, 0o444)
    if file_sha256(destination) != digest_value:
        raise PublicationError(f"final trace 复制后摘要不一致: {relative}")
    return {
        "path": relative.as_posix(),
        "sha256": digest_value,
        "source_path": str(source.resolve()),
    }


def _write_manifest(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o444)


def _validate_existing_trace(target: Path, expected: dict[str, Any]) -> None:
    if not target.is_dir() or target.is_symlink():
        raise PublicationError("final trace 目标不是普通目录")
    manifest_path = target / "TRACE_MANIFEST.json"
    if _strict_object(manifest_path) != expected:
        raise PublicationError("final trace 已存在但 manifest 不同")
    expected_paths = {"TRACE_MANIFEST.json"}
    for artifact in expected["artifacts"]:
        relative = str(artifact["path"])
        expected_paths.add(relative)
        path = target / relative
        if not path.is_file() or path.is_symlink() or file_sha256(path) != artifact["sha256"]:
            raise PublicationError(f"final trace 已封签 artifact 漂移: {relative}")
    actual_paths = {
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if path.is_file()
    }
    if actual_paths != expected_paths:
        raise PublicationError("final trace 含未封签文件或缺失文件")


def _fsync_tree(root: Path) -> None:
    for directory in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
        _fsync_directory(directory)
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _plain_tree(root: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise PublicationError("question-pack 必须是普通目录")
    for path in (root, *root.rglob("*")):
        mode = path.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise PublicationError(f"question-pack 含链接或特殊节点: {path}")
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            raise PublicationError(f"question-pack 含运行缓存: {path}")


def _validate_package(package: Path) -> None:
    report = lint_package(package)
    if not report.passed:
        raise PublicationError(f"题包 lint 未通过: {package}")
    try:
        validate_resource_catalog(package / "resources.json")
    except ResourceCatalogError as error:
        raise PublicationError(f"题包 resources.json 不合格: {package}: {error}") from error


def _strict_object(path: Path) -> dict[str, Any]:
    def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"重复键 {key}")
            value[key] = item
        return value

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PublicationError(f"非法难度声明 {path}: {error}") from error
    if not isinstance(value, dict):
        raise PublicationError("难度声明根节点必须是对象")
    return value


def _bound_hint(
    run: RunSnapshot,
    attempt: AttemptEvidence,
) -> tuple[ApprovedHint, str]:
    rounds = run.evidence.get("harbor_rounds")
    value = rounds.get(attempt.request_id) if isinstance(rounds, dict) else None
    request = value.get("request") if isinstance(value, dict) else None
    if isinstance(rounds, dict) and not isinstance(request, dict):
        raise PublicationError("hint 尝试缺少 canonical request 证据")
    if isinstance(request, dict):
        hint_path = Path(str(request.get("approved_hint_path", "")))
        if (
            not hint_path.is_file()
            or hint_path.is_symlink()
            or file_sha256(hint_path) != attempt.hint_sha256
            or request.get("approved_hint_sha256") != attempt.hint_sha256
        ):
            raise PublicationError("Teacher hint 字节未绑定到科学尝试")
        return ApprovedHint.from_path(hint_path), str(attempt.hint_sha256)

    _session, round_value = _persistent_round_binding(run, attempt)
    round_index = int(round_value["round_index"])
    if round_index not in {4, 5}:
        raise PublicationError("persistent hint 轮次索引无效")
    previous_attempt = _attempt_by_request_id(
        run,
        attempt.request_id.rsplit(".round-", 1)[0]
        + f".round-{round_index - 1:02d}",
    )
    _previous_session, previous_round = _persistent_round_binding(
        run,
        previous_attempt,
    )
    decision_path = Path(str(previous_round.get("decision_path", "")))
    if (
        not decision_path.is_file()
        or decision_path.is_symlink()
        or file_sha256(decision_path) != previous_round.get("decision_sha256")
    ):
        raise PublicationError("persistent Teacher hint decision 漂移")
    decision = _strict_object(decision_path)
    if (
        decision.get("action") != "CONTINUE_HINT"
        or decision.get("teacher_declares_non_answer") is not True
        or not isinstance(decision.get("hint"), str)
    ):
        raise PublicationError("persistent Teacher hint 缺少非答案声明")
    hint = ApprovedHint(
        validation_session_id=str(attempt.validation_session_id),
        round_index=round_index - 3,
        content=str(decision["hint"]),
        teacher_declares_non_answer=True,
    )
    hint.validate()
    digest = _derived_hint_sha256(hint)
    if attempt.hint_sha256 != digest:
        raise PublicationError("persistent Teacher hint 摘要未绑定到科学尝试")
    return hint, digest


def _bound_hint_chain(
    run: RunSnapshot,
    attempt: AttemptEvidence,
) -> tuple[tuple[ApprovedHint, str], ...]:
    """返回目标通过轮实际继承的、有序 Teacher 非答案提示链。"""
    hinted = [
        AttemptEvidence(**value)
        for value in run.attempts
        if value.get("classification") == "SCIENTIFIC_RESULT"
        and value.get("mode") == "hint"
        and int(value.get("attempt_index", 0)) <= attempt.attempt_index
    ]
    if not hinted or hinted[-1].request_id != attempt.request_id:
        raise PublicationError("通过提示轮缺少有序前序提示链")
    chain = tuple(_bound_hint(run, item) for item in hinted)
    if len(chain) not in {1, 2}:
        raise PublicationError("发布提示链只能包含一至两级非答案提示")
    first_round = chain[0][0].round_index
    if first_round not in {1, 4}:
        raise PublicationError("Teacher hint round_index 起点无效")
    for offset, (hint, _digest) in enumerate(chain):
        if hint.round_index != first_round + offset:
            raise PublicationError("Teacher hint round_index 不连续")
    return chain


def _attempt_by_request_id(run: RunSnapshot, request_id: str) -> AttemptEvidence:
    value = next(
        (
            item
            for item in run.attempts
            if item.get("request_id") == request_id
        ),
        None,
    )
    if not isinstance(value, dict):
        raise PublicationError(f"科学轮次缺少 progression 前序证据: {request_id}")
    return AttemptEvidence(**value)


def _persistent_round_binding(
    run: RunSnapshot,
    attempt: AttemptEvidence,
) -> tuple[dict[str, Any], dict[str, Any]]:
    marker = ".round-"
    if marker not in attempt.request_id:
        raise PublicationError("persistent 科学轮次 request_id 无效")
    parent, suffix = attempt.request_id.rsplit(marker, 1)
    try:
        round_index = int(suffix)
    except ValueError as error:
        raise PublicationError("persistent 科学轮次索引无效") from error
    sessions = run.evidence.get("persistent_harbor_sessions")
    session = sessions.get(parent) if isinstance(sessions, dict) else None
    values = session.get("rounds") if isinstance(session, dict) else None
    if not isinstance(values, list):
        raise PublicationError(f"科学轮次缺少 canonical 证据: {attempt.request_id}")
    value = next(
        (
            item
            for item in values
            if isinstance(item, dict) and item.get("round_index") == round_index
        ),
        None,
    )
    if not isinstance(value, dict):
        raise PublicationError(f"科学轮次缺少 canonical 证据: {attempt.request_id}")
    return session, value


def _derived_hint_bytes(hint: ApprovedHint) -> bytes:
    return (
        json.dumps(
            hint.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _derived_hint_sha256(hint: ApprovedHint) -> str:
    return hashlib.sha256(_derived_hint_bytes(hint)).hexdigest()


def _write_derived_hint(path: Path, hint: ApprovedHint, expected_sha256: str) -> None:
    payload = _derived_hint_bytes(hint)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise PublicationError("persistent Teacher hint 派生摘要不一致")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o444)


def _validate_variant(
    package: Path,
    level: str,
    parent_sha256: str,
    hint_sha256: str,
    hint_chain_sha256s: tuple[str, ...],
) -> None:
    declaration = _strict_object(package / "DIFFICULTY_VARIANT.json")
    expected = (
        {
            "schema_version": 1,
            "difficulty": level,
            "parent_high_package_sha256": parent_sha256,
            "approved_hint_sha256": hint_sha256,
            "teacher_declares_non_answer": True,
        }
        if len(hint_chain_sha256s) == 1
        else {
            "schema_version": 2,
            "difficulty": level,
            "parent_high_package_sha256": parent_sha256,
            "approved_hint_chain_sha256s": list(hint_chain_sha256s),
            "successful_hint_sha256": hint_sha256,
            "teacher_declares_non_answer": True,
            "derived_only_from_reviewed_hint": True,
        }
    )
    if declaration != expected:
        raise PublicationError(f"{level} 难度声明未精确绑定实际 Teacher hint")
    try:
        instruction = (package / "instruction.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise PublicationError(f"{level} 题面无法读取: {error}") from error
    if not instruction:
        raise PublicationError(f"{level} 题面没有包含其声明绑定的提示条件")


def _validate_hint_only_variant(
    high: Path,
    variant: Path,
    hints: tuple[ApprovedHint, ...],
) -> None:
    """难度变体只允许在完整 high 题面后追加一段精确绑定的提示。"""
    high_nodes = _tree_identities(high, ignored={"instruction.md"})
    variant_nodes = _tree_identities(
        variant,
        ignored={"instruction.md", "DIFFICULTY_VARIANT.json"},
    )
    if high_nodes != variant_nodes:
        raise PublicationError(
            f"{variant.name} 只能修改 instruction.md 并新增 DIFFICULTY_VARIANT.json"
        )
    high_instruction = (high / "instruction.md").read_text(encoding="utf-8")
    expected_instruction = (
        high_instruction
        + ("" if high_instruction.endswith("\n") else "\n")
        + f"\n{HINT_SECTION_HEADING}\n\n"
        + "\n\n".join(hint.content.strip() for hint in hints)
        + "\n"
    )
    try:
        actual_instruction = (variant / "instruction.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise PublicationError(f"{variant.name} 题面无法读取: {error}") from error
    if actual_instruction != expected_instruction:
        raise PublicationError(
            f"{variant.name} instruction.md 必须是 high 题面且没有包含之外内容；只可追加唯一绑定的完整 Teacher hint 链"
        )


def _tree_identities(root: Path, *, ignored: set[str]) -> dict[str, tuple[str, int, str | None]]:
    """返回相对节点的类型、权限和文件摘要。"""
    identities: dict[str, tuple[str, int, str | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative in ignored:
            continue
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_dir():
            identities[relative] = ("directory", mode, None)
        elif path.is_file():
            identities[relative] = ("file", mode, file_sha256(path))
        else:  # _plain_tree 已拒绝，保留 fail-closed 保护。
            raise PublicationError(f"难度题包含非法节点: {path}")
    return identities


def _validate_runtime_binding(run: RunSnapshot) -> None:
    closure = run.evidence.get("runtime_closure")
    environment = run.evidence.get("environment")
    if not isinstance(closure, dict) or not isinstance(environment, dict):
        raise PublicationError("COMPLETED run 缺少 runtime closure 或 Stable environment")
    try:
        converted = dict(environment)
        converted["artifact"] = ArtifactIdentity(**converted["artifact"])
        converted["resource_digests"] = tuple(converted["resource_digests"])
        receipt = EnvironmentReceipt(**converted)
        receipt.validate()
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise PublicationError(f"Stable environment 回执无效: {error}") from error
    closure_path = Path(str(closure.get("path", "")))
    if (
        receipt.schema_version != 2
        or run.environment_key != receipt.environment_key
        or not closure_path.is_file()
        or file_sha256(closure_path) != closure.get("sha256")
        or receipt.runtime_closure_path != str(closure_path.resolve())
        or receipt.runtime_closure_sha256 != closure.get("sha256")
    ):
        raise PublicationError("Stable environment 没有精确绑定当前 runtime closure")
