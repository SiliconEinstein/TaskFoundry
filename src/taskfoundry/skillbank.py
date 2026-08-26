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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"invalid JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise ContractError(f"JSON root must be an object: {path}")
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
    for relative in ("trace/authoring/skill-activations", "trace/final", "question-pack"):
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


def validate_activation(path: Path, *, question: int, stage: str) -> dict[str, Any]:
    """验证 resolve 输出、完整 Skill、Bank snapshot 和源规则字节。"""
    if stage not in VALID_STAGES:
        raise ContractError(f"unsupported Teacher stage: {stage}")
    value = _read_object(path)
    if (
        value.get("schema_version") != 1
        or value.get("project_id") != "question-from-questions"
        or value.get("task_id") != f"q{question}"
        or type(value.get("stable_generation")) is not int
        or value["stable_generation"] < 1
    ):
        raise ContractError("SkillFoundry activation identity is invalid")
    skills = value.get("execution_skills")
    banks = value.get("experience_banks")
    cards = value.get("retrieved_cards")
    if not isinstance(skills, list) or len(skills) != 1 or not isinstance(banks, list) or len(banks) != 1:
        raise ContractError("activation must select one complete method-selection Skill and Bank")
    if skills[0].get("artifact_id") != "method-selection-teacher" or banks[0].get("artifact_id") != "method-selection":
        raise ContractError("activation selected the wrong question-type knowledge")
    if not isinstance(cards, list):
        raise ContractError("activation Experience Cards must be explicit")
    skill_path = QUESTION_ROOT / str(skills[0].get("path"))
    if not (skill_path / "SKILL.md").is_file():
        raise ContractError("resolved complete Execution Skill is missing")
    _verify_rule_sources(skill_path)
    return value


def resolve_teacher_activation(question: int, stage: str, attempt_id: str) -> Path:
    """调用 SkillFoundry resolve，并把不可变 activation 存入该题 authoring trace。"""
    if stage not in VALID_STAGES:
        raise ContractError(f"unsupported Teacher stage: {stage}")
    root = ensure_question_layout(question)
    output = root / "trace/authoring/skill-activations" / f"{stage}.json"
    if output.exists():
        value = validate_activation(output, question=question, stage=stage)
        if value.get("attempt_id") != attempt_id:
            raise ContractError("running stage already has another frozen Skill activation")
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
    descriptor, temporary_name = tempfile.mkstemp(prefix=f"q{question}-{stage}-", suffix=".json")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(context, stream, ensure_ascii=False, sort_keys=True)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(SKILLFOUNDRY_ROOT / "src")
        result = subprocess.run(
            [
                str(SKILLFOUNDRY_PYTHON), "-m", "skillfoundry.cli", "resolve",
                temporary_name, "--project-root", str(QUESTION_ROOT), "--output", str(output),
            ],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise ContractError(f"skillfoundry resolve failed: {result.stderr.strip()}")
    finally:
        os.unlink(temporary_name)
    validate_activation(output, question=question, stage=stage)
    return output

