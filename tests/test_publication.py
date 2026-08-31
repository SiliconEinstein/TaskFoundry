"""最终 high/medium/low 题族只能来自已完成的线性 Harbor 会话。"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskfoundry.labwright import ArtifactIdentity, EnvironmentReceipt
from taskfoundry.model import RunSnapshot, RunState
from taskfoundry.policy import file_sha256
from taskfoundry.publication import (
    HINT_SECTION_HEADING,
    PublicationError,
    finalize_published_family,
    validate_published_family,
)
from taskfoundry.researcher import ApprovedHint
from taskfoundry.validation import AttemptEvidence


def _attempt(index: int, mode: str, *, hint_sha256: str | None = None) -> AttemptEvidence:
    return AttemptEvidence(
        request_id=f"round-{index}",
        attempt_index=index,
        mode=mode,
        classification="SCIENTIFIC_RESULT",
        score=0.2 if mode == "blind" else 0.9,
        frozen_contract_digest="a" * 64,
        job_id=f"job-{index}",
        trial_id=f"trial-{index}",
        sandbox_id=f"agent-{index}",
        verifier_sandbox_id=f"verifier-{index}",
        session_id=f"harness-{index}",
        wall_time_sec=10.0,
        hint_sha256=hint_sha256,
        validation_session_id="validation-session",
        researcher_thread_id="researcher-thread",
        prior_round_receipt_sha256s=tuple(str(position) * 64 for position in range(1, index)),
        prior_round_history_paths=tuple(f"/history/{position}" for position in range(1, index)),
        schema_version=2,
    )


def _completed_run(tmp_path: Path, hint_path: Path) -> RunSnapshot:
    closure = tmp_path / "runtime-closure.json"
    closure.write_text('{"closed":true}\n', encoding="utf-8")
    closure_sha = file_sha256(closure)
    receipt = EnvironmentReceipt(
        environment_key="e" * 64,
        lifecycle="STABLE",
        artifact=ArtifactIdentity(
            provider="lbg",
            endpoint_identity="lbg://provider",
            project_id="project",
            record_id="record",
            image_url="registry/task:fixed",
            digest="sha256:" + "d" * 64,
        ),
        workdir="/app",
        manifest_path=str(tmp_path / "manifest.json"),
        manifest_sha256="b" * 64,
        resource_digests=(),
        runtime_closure_path=str(closure.resolve()),
        runtime_closure_sha256=closure_sha,
        schema_version=2,
    )
    attempts = (
        _attempt(1, "blind"),
        _attempt(2, "blind"),
        _attempt(3, "blind"),
        _attempt(4, "hint", hint_sha256=file_sha256(hint_path)),
    )
    return RunSnapshot(
        run_id="run",
        state=RunState.COMPLETED,
        sequence=20,
        package_path=str(tmp_path / "frozen-high"),
        package_digest="a" * 64,
        environment_key=receipt.environment_key,
        attempts=tuple(asdict(item) for item in attempts),
        evidence={
            "harbor_rounds": {
                "round-4": {
                    "request": {
                        "approved_hint_path": str(hint_path),
                        "approved_hint_sha256": file_sha256(hint_path),
                    }
                }
            },
            "runtime_closure": {"path": str(closure), "sha256": closure_sha},
            "environment": receipt.to_dict(),
        },
    )


def _family(tmp_path: Path, question: int = 3) -> tuple[Path, Path]:
    root = tmp_path / "questions" / str(question) / "question-pack"
    high = root / "high"
    low = root / "medium"
    high.mkdir(parents=True)
    low.mkdir()
    (high / "instruction.md").write_text("hard question\n", encoding="utf-8")
    resources = json.dumps(
        [
            {
                "id": "r1",
                "name": "Scientific source paper",
                "type": "paper",
                "access": {"paper_id": "paper-1"},
                "notes": "Load-bearing scientific source.",
            }
        ],
        ensure_ascii=False,
    )
    (high / "resources.json").write_text(resources, encoding="utf-8")
    (low / "resources.json").write_text(resources, encoding="utf-8")
    hint = ApprovedHint("validation-session", 1, "先检查边界条件。", True)
    hint_path = tmp_path / "hint.json"
    hint_path.write_text(
        json.dumps(hint.to_dict(), ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    (low / "instruction.md").write_text(
        f"hard question\n\n{HINT_SECTION_HEADING}\n\n{hint.content}\n", encoding="utf-8"
    )
    (low / "DIFFICULTY_VARIANT.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "difficulty": "medium",
                "parent_high_package_sha256": "a" * 64,
                "approved_hint_sha256": file_sha256(hint_path),
                "teacher_declares_non_answer": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return root, hint_path


def test_publication_requires_resource_catalog(tmp_path: Path, monkeypatch) -> None:
    family, hint = _family(tmp_path)
    run = _completed_run(tmp_path, hint)
    monkeypatch.setattr("taskfoundry.publication.QUESTION_ROOT", tmp_path / "questions")
    monkeypatch.setattr("taskfoundry.publication.lint_package", lambda _path: SimpleNamespace(passed=True))
    monkeypatch.setattr("taskfoundry.publication.package_sha256", lambda _path: "a" * 64)
    (family / "high/resources.json").unlink()

    with pytest.raises(PublicationError, match="resources.json"):
        validate_published_family(3, family, run)


def test_publication_accepts_only_bound_linear_family(tmp_path: Path, monkeypatch) -> None:
    family, hint = _family(tmp_path)
    run = _completed_run(tmp_path, hint)
    monkeypatch.setattr("taskfoundry.publication.QUESTION_ROOT", tmp_path / "questions")
    monkeypatch.setattr("taskfoundry.publication.lint_package", lambda _path: SimpleNamespace(passed=True))
    monkeypatch.setattr("taskfoundry.publication.package_sha256", lambda _path: "a" * 64)

    validate_published_family(3, family, run)


def test_publication_accepts_persistent_session_hint_binding(
    tmp_path: Path, monkeypatch
) -> None:
    family, hint_path = _family(tmp_path)
    run = _completed_run(tmp_path, hint_path)
    attempts = []
    rounds = []
    for index, value in enumerate(run.attempts, start=1):
        attempt = dict(value)
        attempt["request_id"] = f"persistent-request.round-{index:02d}"
        attempt["hint_sha256"] = None
        attempts.append(attempt)
        result = tmp_path / f"persistent-round-{index:02d}-result.json"
        result.write_text(json.dumps({"round": index}) + "\n", encoding="utf-8")
        decision = tmp_path / f"persistent-round-{index:02d}-decision.json"
        payload = {
            "schema_version": 1,
            "validation_session_id": "validation-session",
            "round_index": index,
            "result_sha256": file_sha256(result),
            "action": (
                "CONTINUE_HINT"
                if index == 3
                else "STOP_PASSED"
                if index == 4
                else "CONTINUE_BLIND"
            ),
            "hint": "先检查边界条件。" if index == 3 else None,
            "teacher_declares_non_answer": True if index == 3 else None,
        }
        decision.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rounds.append(
            {
                "round_index": index,
                "result_path": str(result),
                "result_sha256": file_sha256(result),
                "decision_path": str(decision),
                "decision_sha256": file_sha256(decision),
            }
        )
    hint = ApprovedHint("validation-session", 1, "先检查边界条件。", True)
    canonical_hint = tmp_path / "persistent-approved-hint.json"
    canonical_hint.write_text(
        json.dumps(
            hint.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )
    attempts[3]["hint_sha256"] = file_sha256(canonical_hint)
    declaration = json.loads(
        (family / "medium/DIFFICULTY_VARIANT.json").read_text(encoding="utf-8")
    )
    declaration["approved_hint_sha256"] = file_sha256(canonical_hint)
    (family / "medium/DIFFICULTY_VARIANT.json").write_text(
        json.dumps(declaration), encoding="utf-8"
    )
    evidence = dict(run.evidence)
    evidence.pop("harbor_rounds")
    evidence["persistent_harbor_sessions"] = {
        "persistent-request": {"rounds": rounds}
    }
    run = RunSnapshot(
        **(run.__dict__ | {"attempts": tuple(attempts), "evidence": evidence})
    )
    monkeypatch.setattr("taskfoundry.publication.QUESTION_ROOT", tmp_path / "questions")
    monkeypatch.setattr(
        "taskfoundry.publication.lint_package",
        lambda _path: SimpleNamespace(passed=True),
    )
    monkeypatch.setattr(
        "taskfoundry.publication.package_sha256", lambda _path: "a" * 64
    )

    validate_published_family(3, family, run)

    rounds[2]["decision_sha256"] = "0" * 64
    with pytest.raises(PublicationError, match="decision 漂移"):
        validate_published_family(3, family, run)


def test_publication_rejects_pollution_and_unbound_variant(tmp_path: Path, monkeypatch) -> None:
    family, hint = _family(tmp_path)
    run = _completed_run(tmp_path, hint)
    monkeypatch.setattr("taskfoundry.publication.QUESTION_ROOT", tmp_path / "questions")
    monkeypatch.setattr("taskfoundry.publication.lint_package", lambda _path: SimpleNamespace(passed=True))
    monkeypatch.setattr("taskfoundry.publication.package_sha256", lambda _path: "a" * 64)
    (family / "old-candidate.txt").write_text("pollution", encoding="utf-8")

    with pytest.raises(PublicationError, match="只能包含"):
        validate_published_family(3, family, run)

    (family / "old-candidate.txt").unlink()
    declaration = json.loads((family / "medium/DIFFICULTY_VARIANT.json").read_text())
    declaration["teacher_declares_non_answer"] = False
    (family / "medium/DIFFICULTY_VARIANT.json").write_text(json.dumps(declaration))
    with pytest.raises(PublicationError, match="未精确绑定"):
        validate_published_family(3, family, run)


def test_publication_rejects_replaced_variant_instruction_even_when_hint_remains(
    tmp_path: Path, monkeypatch
) -> None:
    family, hint = _family(tmp_path)
    run = _completed_run(tmp_path, hint)
    monkeypatch.setattr("taskfoundry.publication.QUESTION_ROOT", tmp_path / "questions")
    monkeypatch.setattr("taskfoundry.publication.lint_package", lambda _path: SimpleNamespace(passed=True))
    monkeypatch.setattr("taskfoundry.publication.package_sha256", lambda _path: "a" * 64)
    hint_value = ApprovedHint.from_path(hint)
    (family / "medium/instruction.md").write_text(
        f"another question with an answer\n\n{HINT_SECTION_HEADING}\n\n{hint_value.content}\n",
        encoding="utf-8",
    )

    with pytest.raises(PublicationError, match="必须是 high 题面"):
        validate_published_family(3, family, run)


def test_publication_accepts_exact_hint_append_when_high_has_no_final_newline(
    tmp_path: Path, monkeypatch
) -> None:
    family, hint = _family(tmp_path)
    run = _completed_run(tmp_path, hint)
    monkeypatch.setattr("taskfoundry.publication.QUESTION_ROOT", tmp_path / "questions")
    monkeypatch.setattr("taskfoundry.publication.lint_package", lambda _path: SimpleNamespace(passed=True))
    monkeypatch.setattr("taskfoundry.publication.package_sha256", lambda _path: "a" * 64)
    value = ApprovedHint.from_path(hint)
    (family / "high/instruction.md").write_text("hard question", encoding="utf-8")
    (family / "medium/instruction.md").write_text(
        f"hard question\n\n{HINT_SECTION_HEADING}\n\n{value.content}\n",
        encoding="utf-8",
    )

    validate_published_family(3, family, run)


def test_publication_rejects_non_utf8_variant_instruction(tmp_path: Path, monkeypatch) -> None:
    family, hint = _family(tmp_path)
    run = _completed_run(tmp_path, hint)
    monkeypatch.setattr("taskfoundry.publication.QUESTION_ROOT", tmp_path / "questions")
    monkeypatch.setattr("taskfoundry.publication.lint_package", lambda _path: SimpleNamespace(passed=True))
    monkeypatch.setattr("taskfoundry.publication.package_sha256", lambda _path: "a" * 64)
    (family / "medium/instruction.md").write_bytes(b"\xff")

    with pytest.raises(PublicationError, match="题面无法读取"):
        validate_published_family(3, family, run)


def test_publication_rejects_wrong_scope_state_digest_and_progression(
    tmp_path: Path, monkeypatch
) -> None:
    family, hint = _family(tmp_path)
    run = _completed_run(tmp_path, hint)
    monkeypatch.setattr("taskfoundry.publication.QUESTION_ROOT", tmp_path / "questions")
    monkeypatch.setattr(
        "taskfoundry.publication.lint_package", lambda _path: SimpleNamespace(passed=True)
    )
    monkeypatch.setattr("taskfoundry.publication.package_sha256", lambda _path: "a" * 64)
    with pytest.raises(PublicationError, match="Q1/Q2"):
        validate_published_family(2, family, run)
    with pytest.raises(PublicationError, match="COMPLETED"):
        validate_published_family(
            3,
            family,
            RunSnapshot(**(run.__dict__ | {"state": RunState.AUTHORING})),
        )
    monkeypatch.setattr("taskfoundry.publication.package_sha256", lambda _path: "f" * 64)
    with pytest.raises(PublicationError, match="冻结题包"):
        validate_published_family(3, family, run)
    monkeypatch.setattr("taskfoundry.publication.package_sha256", lambda _path: "a" * 64)
    too_easy_attempts = list(run.attempts)
    too_easy_attempts[0] = too_easy_attempts[0] | {"score": 1.0}
    with pytest.raises(PublicationError, match="线性验证合同"):
        validate_published_family(
            3,
            family,
            RunSnapshot(**(run.__dict__ | {"attempts": tuple(too_easy_attempts)})),
        )


def test_publication_rejects_noncanonical_question_pack_path(tmp_path: Path, monkeypatch) -> None:
    family, hint = _family(tmp_path)
    run = _completed_run(tmp_path, hint)
    monkeypatch.setattr("taskfoundry.publication.QUESTION_ROOT", tmp_path / "questions")
    monkeypatch.setattr("taskfoundry.publication.lint_package", lambda _path: SimpleNamespace(passed=True))
    monkeypatch.setattr("taskfoundry.publication.package_sha256", lambda _path: "a" * 64)
    impostor = tmp_path / "impostor"
    impostor.mkdir()

    with pytest.raises(PublicationError, match="题号目录"):
        validate_published_family(3, impostor, run)


def test_publication_rejects_scientific_drift_between_difficulty_variants(
    tmp_path: Path, monkeypatch
) -> None:
    family, hint = _family(tmp_path)
    run = _completed_run(tmp_path, hint)
    monkeypatch.setattr("taskfoundry.publication.QUESTION_ROOT", tmp_path / "questions")
    monkeypatch.setattr("taskfoundry.publication.lint_package", lambda _path: SimpleNamespace(passed=True))
    monkeypatch.setattr("taskfoundry.publication.package_sha256", lambda _path: "a" * 64)
    (family / "high/tests").mkdir()
    (family / "medium/tests").mkdir()
    (family / "high/tests/test.sh").write_text("same-science\n", encoding="utf-8")
    (family / "medium/tests/test.sh").write_text("changed-science\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="只能修改 instruction.md"):
        validate_published_family(3, family, run)


def test_publication_atomically_seals_canonical_final_trace(tmp_path: Path, monkeypatch) -> None:
    family, hint = _family(tmp_path)
    run = _completed_run(tmp_path, hint)
    monkeypatch.setattr("taskfoundry.publication.QUESTION_ROOT", tmp_path / "questions")
    monkeypatch.setattr("taskfoundry.publication.lint_package", lambda _path: SimpleNamespace(passed=True))
    monkeypatch.setattr("taskfoundry.publication.package_sha256", lambda _path: "a" * 64)
    evidence = dict(run.evidence)
    rounds = dict(evidence["harbor_rounds"])
    for index in range(1, 5):
        source = tmp_path / "canonical" / f"round-{index}"
        source.mkdir(parents=True)
        bindings: dict[str, object] = {}
        for field, name in (
            ("request", "request.json"),
            ("capability", "capability.json"),
            ("job_config", "job-config.json"),
            ("job_result", "job-result.json"),
            ("trial_result", "trial-result.json"),
            ("job_log", "job.log"),
            ("provider_identity", "provider-identities.json"),
            ("round_history", "round-history.json"),
        ):
            path = source / name
            path.write_text(f"round {index} {field}\n", encoding="utf-8")
            bindings[f"{field}_path"] = str(path)
            bindings[f"{field}_sha256"] = file_sha256(path)
        request = dict(rounds.get(f"round-{index}", {}).get("request", {}))
        if index == 4:
            request.update(
                approved_hint_path=str(hint),
                approved_hint_sha256=file_sha256(hint),
            )
        bindings["request"] = request
        rounds[f"round-{index}"] = bindings
    environment_manifest = tmp_path / "environment-manifest.json"
    environment_manifest.write_text('{"stable":true}\n', encoding="utf-8")
    environment = dict(evidence["environment"])
    environment.update(
        manifest_path=str(environment_manifest),
        manifest_sha256=file_sha256(environment_manifest),
    )
    evidence.update(harbor_rounds=rounds, environment=environment)
    run = RunSnapshot(**(run.__dict__ | {"evidence": evidence}))

    trace = finalize_published_family(3, family, run)

    manifest = json.loads((trace / "TRACE_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["published_family_sha256"] == {"high": "a" * 64, "medium": "a" * 64}
    assert trace.parent == tmp_path / "questions/3/trace/final"
    assert manifest["package_sha256"] == "a" * 64
    assert manifest["ordered_request_ids"] == [f"round-{index}" for index in range(1, 5)]
    assert len(manifest["artifacts"]) == 4 * 8 + 3
    assert finalize_published_family(3, family, run) == trace

    extra = trace / "unexpected.txt"
    extra.write_text("pollution", encoding="utf-8")
    with pytest.raises(PublicationError, match="未封签文件"):
        finalize_published_family(3, family, run)
    extra.unlink()
    sealed = trace / manifest["artifacts"][0]["path"]
    original = sealed.read_bytes()
    sealed.chmod(0o644)
    sealed.write_bytes(b"tampered")
    with pytest.raises(PublicationError, match="artifact 漂移"):
        finalize_published_family(3, family, run)
    sealed.write_bytes(original)
    sealed.chmod(0o444)
    source = Path(manifest["artifacts"][0]["source_path"])
    source_original = source.read_bytes()
    source.write_bytes(b"source drift")
    with pytest.raises(PublicationError, match="canonical 证据漂移"):
        finalize_published_family(3, family, run)
    source.write_bytes(source_original)
    manifest_path = trace / "TRACE_MANIFEST.json"
    manifest_original = manifest_path.read_bytes()
    manifest_path.chmod(0o644)
    manifest_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(PublicationError, match="manifest 不同"):
        finalize_published_family(3, family, run)
    manifest_path.write_bytes(manifest_original)
    manifest_path.chmod(0o444)


def test_final_trace_rejects_missing_round_and_runtime_artifacts(tmp_path: Path, monkeypatch) -> None:
    family, hint = _family(tmp_path)
    run = _completed_run(tmp_path, hint)
    monkeypatch.setattr("taskfoundry.publication.QUESTION_ROOT", tmp_path / "questions")
    monkeypatch.setattr("taskfoundry.publication.lint_package", lambda _path: SimpleNamespace(passed=True))
    monkeypatch.setattr("taskfoundry.publication.package_sha256", lambda _path: "a" * 64)
    environment_manifest = tmp_path / "environment-manifest.json"
    environment_manifest.write_text("manifest\n", encoding="utf-8")
    environment = dict(run.evidence["environment"])
    environment.update(
        manifest_path=str(environment_manifest),
        manifest_sha256=file_sha256(environment_manifest),
    )
    run = RunSnapshot(
        **(
            run.__dict__
            | {"evidence": run.evidence | {"environment": environment}}
        )
    )

    with pytest.raises(PublicationError, match="缺少 canonical 证据"):
        finalize_published_family(3, family, run)

    with pytest.raises(PublicationError, match="runtime closure"):
        finalize_published_family(
            3,
            family,
            RunSnapshot(**(run.__dict__ | {"evidence": run.evidence | {"runtime_closure": None}})),
        )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("level", "数量"),
        ("cache", "运行缓存"),
        ("symlink", "链接或特殊节点"),
        ("lint", "lint 未通过"),
        ("hint", "canonical request"),
        ("declaration", "非法难度声明"),
        ("declaration-root", "根节点必须是对象"),
        ("instruction", "没有包含"),
        ("runtime", "没有精确绑定"),
        ("environment", "回执无效"),
    ],
)
def test_publication_contract_fails_closed_at_each_release_boundary(
    tmp_path: Path,
    monkeypatch,
    case: str,
    message: str,
) -> None:
    family, hint = _family(tmp_path)
    run = _completed_run(tmp_path, hint)
    monkeypatch.setattr("taskfoundry.publication.QUESTION_ROOT", tmp_path / "questions")
    monkeypatch.setattr("taskfoundry.publication.package_sha256", lambda _path: "a" * 64)
    monkeypatch.setattr(
        "taskfoundry.publication.lint_package",
        lambda _path: SimpleNamespace(passed=case != "lint"),
    )
    if case == "level":
            (family / "low").mkdir()
    elif case == "cache":
        (family / "high/__pycache__").mkdir()
    elif case == "symlink":
        (family / "high/link").symlink_to(family / "high/instruction.md")
    elif case == "hint":
        run = RunSnapshot(**(run.__dict__ | {"evidence": run.evidence | {"harbor_rounds": {}}}))
    elif case == "declaration":
            (family / "medium/DIFFICULTY_VARIANT.json").write_text("{", encoding="utf-8")
    elif case == "declaration-root":
            (family / "medium/DIFFICULTY_VARIANT.json").write_text("[]", encoding="utf-8")
    elif case == "instruction":
            (family / "medium/instruction.md").write_text("no hint\n", encoding="utf-8")
    elif case == "runtime":
        closure = dict(run.evidence["runtime_closure"])
        closure["sha256"] = "0" * 64
        run = RunSnapshot(
            **(run.__dict__ | {"evidence": run.evidence | {"runtime_closure": closure}})
        )
    elif case == "environment":
        environment = dict(run.evidence["environment"])
        environment.pop("artifact")
        run = RunSnapshot(
            **(run.__dict__ | {"evidence": run.evidence | {"environment": environment}})
        )

    with pytest.raises(PublicationError, match=message):
        validate_published_family(3, family, run)
