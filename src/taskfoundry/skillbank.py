"""按题型解析并封签每道题、每个阶段使用的 SkillFoundry 知识。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from .model import ContractError


QUESTION_ROOT = Path("/personal/codex-workspace/question-from-questions")
SKILLFOUNDRY_ROOT = Path("/personal/codex-workspace/skillfoundry")
SKILLFOUNDRY_PYTHON = SKILLFOUNDRY_ROOT / ".venv/bin/python"
VALID_STAGES = frozenset({"outline", "author"})
VALID_LEVELS = ("high", "medium", "low")
SKILL_ATTRIBUTIONS = frozenset({"skill_noncompliance", "skill_knowledge_gap"})
EVOLVABLE_FRONTIERS = frozenset({"informative", "boundary"})
STAGE_INSTRUCTIONS = {
    "outline": (
        "在题号根目录中创建或替换且仅保留 QuestionDesignBrief.json 与 "
        "QuestionDesignBrief.md。两份文件必须描述同一份方法选择设计合同，并且只能依据"
        "本次冻结提示生成；背景证据必须区分承重、语境与条件性干扰，并给出公开排除"
        "线索；本阶段不得构建正式题包。"
    ),
    "author": (
        "依据已确认的 QuestionDesignBrief 与本次冻结的 author 知识，构建可运行题目、"
        "公开资源、参考结果生成器、独立交叉复算、grader、来源记录和最小启动合同。"
        "逐项落实 background_evidence，并验证合理干扰不会变成歧义、噪声或标签编码。"
        "失败过程只能保存在 trace/authoring，question-pack 只能保存最终发布题包。"
    ),
}
BACKGROUND_EVIDENCE_REQUIREMENT = {
    "schema_version": 1,
    "question_design_brief": {"background_evidence": "required"},
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ContractError(f"invalid JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise ContractError(f"JSON root must be an object: {path}")
    return value


def _requires_background_evidence(stable_lock: dict[str, Any]) -> bool:
    """返回稳定 Execution Skill 是否启用了背景证据选择合同。"""
    artifacts = stable_lock.get("artifacts")
    if artifacts is None:
        # Legacy/test snapshots predate artifact-bound stable locks and do
        # not opt into revision-specific contracts.
        return False
    if not isinstance(artifacts, list):
        raise ContractError("SkillFoundry stable lock artifacts must be a list")
    selected = next(
        (
            item
            for item in artifacts
            if isinstance(item, dict)
            and item.get("artifact_kind") == "execution_skill"
            and item.get("artifact_id") == "method-selection-teacher"
        ),
        None,
    )
    if selected is None:
        return False
    relative = selected.get("path")
    if not isinstance(relative, str) or not relative:
        raise ContractError("stable method-selection Execution Skill path is invalid")
    marker = QUESTION_ROOT / relative / "CONTRACT_REQUIREMENTS.json"
    if not marker.exists():
        return False
    if not marker.is_file() or marker.is_symlink():
        raise ContractError("Execution Skill contract requirements must be a regular file")
    if _read_object(marker) != BACKGROUND_EVIDENCE_REQUIREMENT:
        raise ContractError("unsupported Execution Skill contract requirements")
    return True


def _validate_required_background_evidence(question: int) -> None:
    """在新 author activation 前执行题型级背景证据硬门。"""
    from .model import QuestionDesignBrief
    from .question_types import MethodSelectionModule

    brief_path = question_root(question) / "QuestionDesignBrief.json"
    brief = QuestionDesignBrief.from_dict(_read_object(brief_path))
    MethodSelectionModule().validate_background_evidence(brief)


def _write_object(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
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


def _skillfoundry(command: list[str]) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(SKILLFOUNDRY_ROOT / "src")
    result = subprocess.run(
        [str(SKILLFOUNDRY_PYTHON), "-m", "skillfoundry.cli", *command],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ContractError(f"skillfoundry {' '.join(command[:1])} failed: {result.stderr.strip()}")
    try:
        value = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise ContractError("skillfoundry returned invalid JSON") from error
    if not isinstance(value, dict):
        raise ContractError("skillfoundry result must be an object")
    return value


def question_root(question: int) -> Path:
    """返回 Q1--Q32 的编号目录。"""
    if not 1 <= question <= 32:
        raise ContractError("question number must be within Q1-Q32")
    return QUESTION_ROOT / str(question)


def ensure_question_layout(question: int) -> Path:
    """建立不会混入题包内容的固定目录骨架。"""
    root = question_root(question)
    root.mkdir(parents=True, exist_ok=True)
    for relative in (
        "trace/authoring/skill-activations",
        "trace/authoring/prompts",
        "trace/final",
        "question-pack",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


def validate_latest_brief(question: int) -> tuple[Path, Path]:
    """要求题号根目录恰有 JSON/Markdown 两份最新版 Brief。"""
    root = question_root(question)
    json_path = root / "QuestionDesignBrief.json"
    markdown_path = root / "QuestionDesignBrief.md"
    extras = sorted(
        path.name
        for path in root.glob("*QuestionDesignBrief*")
        if path.name not in {json_path.name, markdown_path.name}
    )
    if not json_path.is_file() or json_path.is_symlink():
        raise ContractError("numbered question root must contain QuestionDesignBrief.json")
    if not markdown_path.is_file() or markdown_path.is_symlink():
        raise ContractError("numbered question root must contain QuestionDesignBrief.md")
    if extras:
        raise ContractError(f"only the latest two QuestionDesignBrief files are allowed: {extras}")
    return json_path, markdown_path


def _verify_rule_sources(skill_path: Path) -> None:
    manifest = _read_object(skill_path / "RULE_SOURCES.json")
    sources = manifest.get("sources")
    if manifest.get("schema_version") != 1 or not isinstance(sources, list) or not sources:
        raise ContractError("Execution Skill rule-source manifest is incomplete")
    for item in sources:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ContractError("Execution Skill rule-source entry is invalid")
        source = QUESTION_ROOT / str(item["path"])
        if not source.is_file() or source.is_symlink() or _sha256(source) != item["sha256"]:
            raise ContractError(f"Execution Skill source drift: {source}")


def _validate_activation_selector(
    value: dict[str, Any],
    question: int,
    stage: str,
    *,
    snapshot_source: Path | None = None,
) -> None:
    if (
        value.get("schema_version") != 2
        or value.get("role") != "teacher"
        or value.get("task_type") != "method-selection"
        or value.get("stage") != stage
        or value.get("profile") != "multi-question-input"
        or value.get("project_id") != "question-from-questions"
        or value.get("task_id") != f"q{question}"
        or not isinstance(value.get("batch_id"), str)
        or not value["batch_id"]
        or not isinstance(value.get("batch_questions"), list)
        or not value["batch_questions"]
        or any(type(item) is not int or not 3 <= item <= 32 for item in value["batch_questions"])
        or value["batch_questions"] != sorted(set(value["batch_questions"]))
        or question not in value["batch_questions"]
        or not _full_sha256(value.get("input_set_sha256"))
        or type(value.get("stable_generation")) is not int
        or value["stable_generation"] < 1
    ):
        raise ContractError("SkillFoundry activation is not a self-contained Teacher stage")
    snapshot = value.get("stable_lock_snapshot")
    if not isinstance(snapshot, dict) or set(snapshot) != {"path", "sha256", "generation"}:
        raise ContractError("SkillFoundry activation lacks its stable-lock snapshot")
    snapshot_path = Path(str(snapshot["path"]))
    actual_snapshot = snapshot_source or snapshot_path
    if (
        actual_snapshot.is_symlink()
        or not actual_snapshot.is_file()
        or _sha256(actual_snapshot) != snapshot["sha256"]
        or snapshot["generation"] != value["stable_generation"]
        or _read_object(actual_snapshot).get("generation") != value["stable_generation"]
    ):
        raise ContractError("SkillFoundry activation stable generation is not frozen")


def _validate_activation_selection(
    value: dict[str, Any], stage: str
) -> tuple[list[Any], list[Any]]:
    skills = value.get("execution_skills")
    banks = value.get("experience_banks")
    cards = value.get("retrieved_cards")
    if not isinstance(skills, list) or len(skills) != 1 or not isinstance(banks, list) or len(banks) != 1:
        raise ContractError("activation must select one complete method-selection Skill and Bank")
    if skills[0].get("artifact_id") != "method-selection-teacher" or banks[0].get("artifact_id") != "method-selection":
        raise ContractError("activation selected the wrong question-type knowledge")
    if not isinstance(cards, list):
        raise ContractError("activation Experience Cards must be explicit")
    expected_cards = {
        "outline": {"method-selection-contract"},
        "author": {"public-family-fingerprint", "grader-fail-closed"},
    }
    if {item.get("card_id") for item in cards if isinstance(item, dict)} != expected_cards[stage]:
        raise ContractError("activation selected the wrong stage Experience Cards")
    return skills, cards


def _validate_loaded_documents(value: dict[str, Any], card_count: int) -> list[Any]:
    documents = value.get("knowledge_documents")
    if not isinstance(documents, list) or not documents:
        raise ContractError("self-contained activation must include loaded knowledge documents")
    kinds: list[str] = []
    for document in documents:
        if not isinstance(document, dict) or set(document) != {"kind", "path", "sha256", "content"}:
            raise ContractError("loaded knowledge document contract is invalid")
        content = document["content"]
        if not isinstance(content, str) or _sha256_bytes(content.encode("utf-8")) != document["sha256"]:
            raise ContractError("loaded knowledge document digest is invalid")
        kinds.append(str(document["kind"]))
    required_kinds = {
        "execution_skill",
        "rule_source_manifest",
        "rule_source",
        "experience_bank_snapshot",
        "experience_card",
    }
    if not required_kinds <= set(kinds) or kinds.count("experience_card") != card_count:
        raise ContractError("activation does not contain the complete resolved knowledge bundle")
    return documents


def _validate_prompt_bundle(
    value: dict[str, Any],
    documents: list[Any],
    question: int,
    stage: str,
    *,
    prompt_source: Path | None = None,
) -> None:
    prompt_bundle = value.get("prompt_bundle")
    if not isinstance(prompt_bundle, dict):
        raise ContractError("self-contained activation must bind its runtime prompt")
    prompt_path = Path(str(prompt_bundle.get("path", "")))
    expected_prompt = question_root(question) / f"trace/authoring/prompts/{stage}-prompt.txt"
    actual_prompt = prompt_source or prompt_path
    if (
        prompt_path != expected_prompt
        or not actual_prompt.is_file()
        or actual_prompt.is_symlink()
    ):
        raise ContractError("runtime prompt is missing or stored outside the question trace")
    if _sha256(actual_prompt) != prompt_bundle.get("sha256"):
        raise ContractError("runtime prompt digest is invalid")
    task_inputs = prompt_bundle.get("task_inputs")
    if not isinstance(task_inputs, list) or not task_inputs:
        raise ContractError("runtime prompt must include explicit task inputs")
    prompt_text = actual_prompt.read_text(encoding="utf-8")
    for document in (*documents, *task_inputs):
        if not isinstance(document, dict):
            raise ContractError("runtime prompt task input contract is invalid")
        content = document.get("content")
        if (
            not isinstance(content, str)
            or _sha256_bytes(content.encode("utf-8")) != document.get("sha256")
            or content not in prompt_text
        ):
            raise ContractError("runtime prompt omits resolved knowledge or task input content")
    for task_input in task_inputs:
        source = Path(str(task_input.get("path", "")))
        source = source if source.is_absolute() else QUESTION_ROOT / source
        if source.is_symlink() or not source.is_file() or _sha256(source) != task_input.get("sha256"):
            raise ContractError("runtime prompt input does not bind its source bytes")


def validate_activation(path: Path, *, question: int, stage: str) -> dict[str, Any]:
    """验证自包含知识激活及其精确持久化提示。"""
    if stage not in VALID_STAGES:
        raise ContractError(f"unsupported Teacher stage: {stage}")
    value = _read_object(path)
    _validate_activation_value(value, question=question, stage=stage)
    return value


def _validate_activation_value(
    value: dict[str, Any],
    *,
    question: int,
    stage: str,
    prompt_source: Path | None = None,
    snapshot_source: Path | None = None,
) -> None:
    """校验 activation 值；提交前可从暂存文件读取 prompt 与 stable lock。"""
    _validate_activation_selector(
        value,
        question,
        stage,
        snapshot_source=snapshot_source,
    )
    skills, cards = _validate_activation_selection(value, stage)
    skill_path = QUESTION_ROOT / str(skills[0].get("path"))
    if not (skill_path / "SKILL.md").is_file():
        raise ContractError("resolved complete Execution Skill is missing")
    documents = _validate_loaded_documents(value, len(cards))
    _validate_prompt_bundle(
        value,
        documents,
        question,
        stage,
        prompt_source=prompt_source,
    )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def resolve_teacher_activation(
    question: int,
    stage: str,
    attempt_id: str,
    batch_id: str,
    task_inputs: tuple[Path, ...] = (),
    *,
    batch_questions: tuple[int, ...],
) -> Path:
    """Teacher 开工前解析完整知识，并持久化该批次的精确提示。"""
    if stage not in VALID_STAGES:
        raise ContractError(f"unsupported Teacher stage: {stage}")
    _validate_batch_id(batch_id)
    normalized_batch = tuple(sorted(set(batch_questions)))
    if (
        not normalized_batch
        or question not in normalized_batch
        or any(type(item) is not int or not 3 <= item <= 32 for item in normalized_batch)
    ):
        raise ContractError("Teacher Skill resolution requires a valid frozen batch roster")
    root = ensure_question_layout(question)
    output = root / "trace/authoring/skill-activations" / f"{stage}.json"
    prompt = root / "trace/authoring/prompts" / f"{stage}-prompt.txt"
    resolved_inputs = tuple(path.resolve() for path in task_inputs)
    if stage == "author":
        brief_inputs = tuple(path.resolve() for path in validate_latest_brief(question))
        resolved_inputs = tuple(dict.fromkeys((*brief_inputs, *resolved_inputs)))
    if not resolved_inputs:
        raise ContractError(f"{stage} Skill resolution requires explicit task inputs")
    for task_input in resolved_inputs:
        if not task_input.is_file() or task_input.is_symlink():
            raise ContractError(f"invalid Teacher task input: {task_input}")
    stable_lock = QUESTION_ROOT / ".skillbank/stable.lock"
    stable_lock_value = _read_object(stable_lock)
    stable_generation = stable_lock_value.get("generation")
    if type(stable_generation) is not int or stable_generation < 1:
        raise ContractError("SkillFoundry stable lock has no valid generation")
    if stage == "author" and _requires_background_evidence(stable_lock_value):
        _validate_required_background_evidence(question)
    stable_lock_bytes = stable_lock.read_bytes()
    _freeze_batch_roster(
        batch_id,
        normalized_batch,
        stable_generation=stable_generation,
        stable_lock_sha256=_sha256_bytes(stable_lock_bytes),
    )
    input_set_sha256 = _input_set_sha256(resolved_inputs)
    if output.exists():
        existing = _read_object(output)
        if existing.get("schema_version") != 2:
            _archive_activation(output, prompt)
        elif existing.get("batch_id") != batch_id:
            # A new stage batch is allowed to supersede the previous frozen
            # activation even when one of that previous activation's live
            # input files (for example the latest-only Brief) has changed.
            # Validate the new activation after resolving it; requiring the
            # superseded activation to re-bind mutable latest-only inputs here
            # makes legitimate difficulty revisions impossible to start.
            _archive_activation(output, prompt)
        else:
            value = validate_activation(output, question=question, stage=stage)
            if value.get("attempt_id") != attempt_id:
                raise ContractError("running stage already has another frozen Skill activation")
            elif tuple(value.get("batch_questions", ())) != normalized_batch:
                raise ContractError("running stage frozen Skill batch roster changed")
            else:
                recorded_inputs = tuple(
                    (str(item.get("path")), str(item.get("sha256")))
                    for item in value["prompt_bundle"]["task_inputs"]
                )
                actual_inputs = tuple((str(path), _sha256(path)) for path in resolved_inputs)
                if recorded_inputs != actual_inputs:
                    raise ContractError("running stage frozen task input set changed")
                return output
    context = {
        "schema_version": 1,
        "project_id": "question-from-questions",
        "role": "teacher",
        "task_type": "method-selection",
        "stage": stage,
        "profile": "multi-question-input",
        "task_id": f"q{question}",
        "attempt_id": attempt_id,
    }
    lock_snapshot = output.with_name(f"{stage}-stable.lock.json")
    with tempfile.TemporaryDirectory(prefix=f".{stage}-resolve-", dir=output.parent) as staging:
        staging_root = Path(staging)
        context_path = staging_root / "context.json"
        staged_output = staging_root / "activation.json"
        staged_prompt = staging_root / "prompt.txt"
        staged_lock = staging_root / "stable.lock.json"
        _write_object(context_path, context)
        command = [
            "resolve",
            str(context_path),
            "--project-root",
            str(QUESTION_ROOT),
            "--output",
            str(staged_output),
            "--prompt-output",
            str(staged_prompt),
            "--stage-instruction",
            STAGE_INSTRUCTIONS[stage],
        ]
        for task_input in resolved_inputs:
            command.extend(("--task-input", str(task_input)))
        _skillfoundry(command)
        value = _read_object(staged_output)
        if value.get("stable_generation") != stable_generation:
            raise ContractError("SkillFoundry resolve did not use the current stable generation")
        selected_skills = value.get("execution_skills")
        if not isinstance(selected_skills, list) or len(selected_skills) != 1:
            raise ContractError("SkillFoundry resolve selected an invalid Execution Skill set")
        selected_path = selected_skills[0].get("path")
        if not isinstance(selected_path, str) or not selected_path:
            raise ContractError("SkillFoundry resolve selected an invalid Execution Skill path")
        _verify_rule_sources(QUESTION_ROOT / selected_path)
        _atomic_write_bytes(staged_lock, stable_lock_bytes)
        prompt_bundle = value.get("prompt_bundle")
        if not isinstance(prompt_bundle, dict):
            raise ContractError("SkillFoundry resolve omitted its prompt bundle")
        prompt_bundle["path"] = str(prompt)
        prompt_bundle["sha256"] = _sha256(staged_prompt)
        value.update(
            batch_id=batch_id,
            batch_questions=list(normalized_batch),
            input_set_sha256=input_set_sha256,
            stable_lock_snapshot={
                "path": str(lock_snapshot),
                "sha256": _sha256(staged_lock),
                "generation": stable_generation,
            },
        )
        _validate_activation_value(
            value,
            question=question,
            stage=stage,
            prompt_source=staged_prompt,
            snapshot_source=staged_lock,
        )
        _atomic_write_bytes(prompt, staged_prompt.read_bytes())
        _atomic_write_bytes(lock_snapshot, staged_lock.read_bytes())
        _write_object(output, value)
    return output


def _freeze_batch_roster(
    batch_id: str,
    questions: tuple[int, ...],
    *,
    stable_generation: int,
    stable_lock_sha256: str,
) -> Path:
    """首次 resolve 时原子冻结批次成员和本批唯一 Skill 稳定版本。"""
    path = QUESTION_ROOT / ".skillbank/batches" / f"{batch_id}.json"
    value = {
        "schema_version": 1,
        "batch_id": batch_id,
        "questions": list(questions),
        "stable_generation": stable_generation,
        "stable_lock_sha256": stable_lock_sha256,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if _read_object(path) != value:
            raise ContractError("Teacher Skill batch is already frozen with another contract")
        return path
    return path


def _input_set_sha256(paths: tuple[Path, ...]) -> str:
    """绑定有序输入路径和当前字节，防止复用过期 activation。"""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path).encode("utf-8") + b"\0" + _sha256(path).encode("ascii") + b"\0")
    return digest.hexdigest()


def _full_sha256(value: object) -> bool:
    """判断值是否为小写完整 SHA-256。"""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _archive_activation(output: Path, prompt: Path) -> None:
    """把旧 activation、prompt、stable lock 作为一组可复核字节归档。"""
    value = _read_object(output)
    identity = _sha256(output)
    snapshot_path: Path | None = None
    snapshot = value.get("stable_lock_snapshot")
    if isinstance(snapshot, dict):
        candidate = Path(str(snapshot.get("path", "")))
        if candidate.parent == output.parent:
            snapshot_path = candidate
    if value.get("schema_version") == 2 and (
        not prompt.is_file()
        or prompt.is_symlink()
        or snapshot_path is None
        or not snapshot_path.is_file()
        or snapshot_path.is_symlink()
    ):
        raise ContractError("schema-v2 Skill activation archive bundle is incomplete")
    archive = output.parent / "archive" / identity
    archive.mkdir(parents=True, exist_ok=False)
    archived_files: list[Path] = []
    archived_output = archive / output.name
    os.replace(output, archived_output)
    archived_files.append(archived_output)
    if prompt.exists():
        archived_prompt = archive / prompt.name
        os.replace(prompt, archived_prompt)
        archived_files.append(archived_prompt)
    if snapshot_path is not None and snapshot_path.is_file():
        archived_snapshot = archive / snapshot_path.name
        os.replace(snapshot_path, archived_snapshot)
        archived_files.append(archived_snapshot)
    _write_object(
        archive / "ARCHIVE_MANIFEST.json",
        {
            "schema_version": 1,
            "original_activation_sha256": identity,
            "files": [
                {"name": path.name, "sha256": _sha256(path)}
                for path in sorted(archived_files)
            ],
        },
    )


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """先 fsync 暂存文件，再以 rename 提交一份字节产物。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def record_skill_attribution(question: int, evidence_path: Path) -> Path:
    """把一份 Teacher 归因规范化写入题号 trace。"""
    value = _read_object(evidence_path)
    expected = {
        "project_id": "question-from-questions",
        "task_id": f"q{question}",
        "role": "teacher",
        "task_type": "method-selection",
    }
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise ContractError("skill attribution targets another project, question, role, or type")
    if value.get("attribution") not in SKILL_ATTRIBUTIONS:
        raise ContractError("only Skill-responsible failures enter Skill Bank evolution")
    if value.get("frontier") not in EVOLVABLE_FRONTIERS:
        raise ContractError("only informative or boundary failures may evolve the Skill Bank")
    signature = value.get("signature")
    if not isinstance(signature, dict) or signature.get("proposed_target") not in {
        "execution_skill",
        "experience_bank",
    }:
        raise ContractError("skill attribution requires an evolvable diagnostic signature")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    identity = hashlib.sha256(encoded.encode()).hexdigest()
    target = ensure_question_layout(question) / "trace/authoring/skill-attributions" / f"{identity}.json"
    if target.exists():
        if _read_object(target) != value:
            raise ContractError("skill attribution digest collision")
        return target
    _write_object(target, value)
    return target


def reconcile_skill_batch(batch_id: str, questions: tuple[int, ...]) -> dict[str, Any]:
    """聚合同批题目证据，并确定性执行 SkillFoundry reconcile。"""
    _validate_batch_id(batch_id)
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for question in dict.fromkeys(questions):
        root = question_root(question) / "trace/authoring/skill-attributions"
        for path in sorted(root.glob("*.json")):
            value = _read_object(path)
            evidence_id = value.get("evidence_id")
            if not isinstance(evidence_id, str) or not evidence_id:
                raise ContractError("Skill attribution lacks a stable evidence_id")
            if evidence_id in evidence_by_id and evidence_by_id[evidence_id] != value:
                raise ContractError("duplicate evidence_id identifies different attribution bytes")
            evidence_by_id[evidence_id] = value
    evidence = list(evidence_by_id.values())
    if not evidence:
        raise ContractError("Skill Bank reconciliation requires attributed evidence")
    if len({item.get("task_id") for item in evidence}) < 2:
        raise ContractError("Skill Bank reconciliation requires two distinct question examples")
    batch_root = QUESTION_ROOT / ".skillbank/evolution/batches" / batch_id
    manifest = batch_root / "evidence-manifest.json"
    manifest_value = {
        "schema_version": 1,
        "project_id": "question-from-questions",
        "evidence": evidence,
    }
    result_path = batch_root / "reconcile-result.json"
    if manifest.exists() or result_path.exists():
        if not manifest.is_file() or not result_path.is_file():
            raise ContractError("Skill Bank batch has an incomplete reconcile transaction")
        if _read_object(manifest) != manifest_value:
            raise ContractError("Skill Bank batch_id already binds different evidence")
        return _read_object(result_path) | {
            "manifest_path": str(manifest),
            "batch_id": batch_id,
        }
    _write_object(manifest, manifest_value)
    result = _skillfoundry([
        "reconcile",
        str(manifest),
        "--project-root",
        str(QUESTION_ROOT),
        "--min-cluster-size",
        "2",
    ])
    _write_object(result_path, result)
    return result | {"manifest_path": str(manifest), "batch_id": batch_id}


def reconcile_skill_batch_if_ready(batch_id: str, questions: tuple[int, ...]) -> dict[str, Any]:
    """批次终态钩子：无证据或不足两个案例也留下确定性 reconcile 结论。"""
    _validate_batch_id(batch_id)
    evidence: list[dict[str, Any]] = []
    for question in dict.fromkeys(questions):
        root = question_root(question) / "trace/authoring/skill-attributions"
        evidence.extend(_read_object(path) for path in sorted(root.glob("*.json")))
    distinct_tasks = {item.get("task_id") for item in evidence}
    if evidence and len(distinct_tasks) >= 2:
        return reconcile_skill_batch(batch_id, questions)
    batch_root = QUESTION_ROOT / ".skillbank/evolution/batches" / batch_id
    result_path = batch_root / "reconcile-result.json"
    result = {
        "schema_version": 1,
        "batch_id": batch_id,
        "status": "NO_ELIGIBLE_EVIDENCE" if not evidence else "INSUFFICIENT_FAILURE_PATTERN",
        "evidence_count": len(evidence),
        "distinct_task_count": len(distinct_tasks),
    }
    if result_path.exists():
        if _read_object(result_path) != result:
            raise ContractError("completed Skill batch reconcile result drifted")
        return result
    _write_object(result_path, result)
    return result


def _validate_batch_id(batch_id: str) -> None:
    if not batch_id or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for character in batch_id
    ):
        raise ContractError("batch_id must use letters, digits, hyphen, or underscore")


def evaluate_skill_candidate(plan_path: Path, verdict_path: Path) -> dict[str, Any]:
    """仅晋级归因、迁移和回归评测全部通过的候选。"""
    return _skillfoundry([
        "evaluate",
        str(plan_path),
        str(verdict_path),
        "--project-root",
        str(QUESTION_ROOT),
    ])
