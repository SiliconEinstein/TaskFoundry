from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import taskfoundry.scheduler as scheduler_module

from taskfoundry.codex_agent import DispatchReceipt
from taskfoundry.cli import main
from taskfoundry.package import package_sha256
from taskfoundry.researcher import ResearcherReceipt
from taskfoundry.labwright import ArtifactIdentity, EnvironmentDeltaRequest, FileLabwrightRegistry
from taskfoundry.labwright_runtime import artifact_identity_sha256
from taskfoundry.health import BoundHealthEvidence, HealthGate
from taskfoundry.model import Actor, RunState
from taskfoundry.store import RunStore
from taskfoundry.validation import HealthEvidence
from taskfoundry.workflow import RunWorkflow


def make_package(root):
    root.mkdir()
    (root / "instruction.md").write_text("See resources.yaml.\n")
    (root / "task.toml").write_text(
        'schema_version="1.3"\n[task]\nname="org/task"\n[environment]\n'
        'docker_image="registry/task:fixed"\nworkdir="/app"\n[agent]\ntimeout_sec=3600\n'
    )
    (root / "environment").mkdir()
    (root / "environment/resources.yaml").write_text("resources: []\n")
    (root / "solution").mkdir()
    (root / "solution/solve.sh").write_text("#!/bin/sh\ntrue\n")
    (root / "tests").mkdir()
    (root / "tests/test.sh").write_text("#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n")
    return root


def design_links(tmp_path: Path) -> dict[str, dict[str, str]]:
    """为只测试 CLI 幂等性的裸状态生成稳定设计证据引用。"""
    result: dict[str, dict[str, str]] = {}
    for name in ("source_role_map", "ground_truth_ledger"):
        path = tmp_path / f"{name}.json"
        path.write_text("{}")
        result[name] = {
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return result


def write_design_evidence(brief: Path, tmp_path: Path, revision: str = "r1") -> tuple[Path, Path]:
    """生成可由 attach-design-evidence 命令接纳的两份证据。"""
    value = json.loads(brief.read_text())
    digest = hashlib.sha256(brief.read_bytes()).hexdigest()
    source_ids = [item["source_id"] for item in value["source_questions"]]
    role_map = tmp_path / "source-role-map.json"
    role_map.write_text(json.dumps({
        "schema_version": 1,
        "evidence_type": "source-role-map",
        "question_revision": revision,
        "brief_sha256": digest,
        "source_ids": source_ids,
        "roles": value["evidence_roles"],
        "immutable_source_sha256s": {
            source_id: hashlib.sha256(source_id.encode()).hexdigest()
            for source_id in source_ids
        },
        "license_review_pass": True,
    }))
    ledger = tmp_path / "ground-truth-ledger.json"
    ledger.write_text(json.dumps({
        "schema_version": 1,
        "evidence_type": "ground-truth-ledger",
        "question_revision": revision,
        "brief_sha256": digest,
        "scored_quantities": ["prediction", "method_selection"],
        "producer_sha256": "1" * 64,
        "independent_crosscheck_sha256": "2" * 64,
        "scoring_contract_sha256": "3" * 64,
        "derived_reference": True,
        "crosscheck_pass": True,
    }))
    return role_map, ledger


def test_status_prints_snapshot(tmp_path, capsys) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(
        json.dumps({"run_id": "r", "state": "DESIGNING", "sequence": 0})
    )
    assert main(["status", str(run)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "DESIGNING"


def test_cli_can_prepare_first_blind_without_stable_environment(tmp_path, capsys) -> None:
    run = tmp_path / "run"
    store = RunStore(run)
    store.initialize("run-1")
    flow = RunWorkflow(store)
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps({
        "brief_id": "brief-1",
        "question_type": "method-selection",
        "title": "方法选择",
        "scientific_goal": "选择可迁移方法",
        "research_object": "隐藏工况",
        "source_questions": [
            {"source_id": f"q{i}", "paper_id": f"p{i}", "research_goal": "目标", "method": f"m{i}", "transferred_role": "候选"}
            for i in range(1, 4)
        ],
        "method_space": ["m1", "m2", "m3"],
        "evidence_roles": {"q1": "候选", "q2": "候选", "q3": "候选"},
        "public_inputs": ["input.csv"],
        "required_outputs": ["output.csv"],
        "hidden_evaluation_axes": ["迁移"],
        "environment_capabilities": ["python"],
        "difficulty_hypothesis": "需要比较方法",
        "solvability_argument": "公开数据充分",
    }))
    policies = tmp_path / "policies.json"
    policies.write_text("{}")
    flow.attach_brief(Actor.TEACHER, brief, "brief")
    flow.lock_policies(Actor.TEACHER, policies, "policies")
    task = make_package(tmp_path / "task")
    health = tmp_path / "preflight.json"
    health.write_text(json.dumps({
        "oracle_full_score": True,
        "independent_honest_executed": True,
        "adversarial_low_score": True,
        "environment_stable": False,
        "package_compliant": True,
        "no_hidden_leakage": True,
    }))

    assert main(["begin-authoring", str(run)]) == 0
    capsys.readouterr()
    role_map, ledger = write_design_evidence(brief, tmp_path)
    assert main([
        "attach-design-evidence", str(run), str(role_map), str(ledger),
    ]) == 0
    capsys.readouterr()
    assert main(["freeze-package", str(run), str(task)]) == 0
    capsys.readouterr()
    assert main([
        "accept-health", str(run), str(health),
        "--evidence-ref", "reviewer/preflight.json",
    ]) == 0
    capsys.readouterr()
    assert main(["start-blind", str(run)]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "BLIND_VALIDATION"
    assert result["environment_key"] is None


def test_freeze_package_idempotency_is_scoped_to_question_revision(tmp_path, capsys) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(json.dumps({
        "run_id": "run-1",
        "state": "AUTHORING",
        "sequence": 0,
        "question_revision": "r1",
        "evidence": {"design_evidence": design_links(tmp_path)},
    }))
    task = make_package(tmp_path / "task")

    assert main(["freeze-package", str(run), str(task)]) == 0
    r1 = json.loads(capsys.readouterr().out)
    assert r1["state"] == "PACKAGE_FROZEN"
    assert r1["question_revision"] == "r1"

    reason = tmp_path / "environment-revision.json"
    reason.write_text('{"reason":"runtime environment changed"}')
    flow = RunWorkflow(RunStore(run))
    flow.begin_environment_revision(
        Actor.TEACHER,
        "r2",
        reason,
        "environment-revision:r2",
    )
    revised_state = json.loads((run / "state.json").read_text())
    revised_state.setdefault("evidence", {})["design_evidence"] = design_links(tmp_path)
    (run / "state.json").write_text(json.dumps(revised_state))

    assert main(["freeze-package", str(run), str(task)]) == 0
    r2 = json.loads(capsys.readouterr().out)
    assert r2["state"] == "PACKAGE_FROZEN"
    assert r2["question_revision"] == "r2"
    sequence = r2["sequence"]
    events = RunStore(run).events()
    event_count = len(events)
    digest = package_sha256(task)
    freeze_keys = [event.idempotency_key for event in events if event.event_type == "package.frozen"]
    assert freeze_keys == [
        f"package:freeze:r1:{digest}",
        f"package:freeze:r2:{digest}",
    ]

    assert main(["freeze-package", str(run), str(task)]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["sequence"] == sequence
    assert len(RunStore(run).events()) == event_count


def test_accept_health_idempotency_is_scoped_to_question_revision(tmp_path, capsys) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(json.dumps({
        "run_id": "run-1",
        "state": "AUTHORING",
        "sequence": 0,
        "question_revision": "r1",
        "evidence": {"design_evidence": design_links(tmp_path)},
    }))
    task = make_package(tmp_path / "task")
    health_path = tmp_path / "preflight.json"
    health_path.write_text(json.dumps({
        "oracle_full_score": True,
        "independent_honest_executed": True,
        "adversarial_low_score": True,
        "environment_stable": False,
        "package_compliant": True,
        "no_hidden_leakage": True,
    }))
    health_command = [
        "accept-health", str(run), str(health_path),
        "--evidence-ref", "reviewer/preflight.json",
    ]

    assert main(["freeze-package", str(run), str(task)]) == 0
    capsys.readouterr()
    assert main(health_command) == 0
    r1 = json.loads(capsys.readouterr().out)
    assert r1["state"] == "PREFLIGHT_PASSED"

    flow = RunWorkflow(RunStore(run))
    flow.start_blind_validation(Actor.TEACHER, "workflow:blind:r1")
    reason = tmp_path / "environment-revision.json"
    reason.write_text('{"reason":"runtime environment changed"}')
    flow.begin_environment_revision(
        Actor.TEACHER,
        "r2",
        reason,
        "environment-revision:r2",
    )
    revised_state = json.loads((run / "state.json").read_text())
    revised_state.setdefault("evidence", {})["design_evidence"] = design_links(tmp_path)
    (run / "state.json").write_text(json.dumps(revised_state))
    assert main(["freeze-package", str(run), str(task)]) == 0
    capsys.readouterr()

    assert main(health_command) == 0
    r2 = json.loads(capsys.readouterr().out)
    assert r2["state"] == "PREFLIGHT_PASSED"
    assert r2["question_revision"] == "r2"
    sequence = r2["sequence"]
    events = RunStore(run).events()
    event_count = len(events)
    health_keys = [event.idempotency_key for event in events if event.event_type == "health.preflight.accepted"]
    assert health_keys == ["health:r1:preflight.json", "health:r2:preflight.json"]

    assert main(health_command) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["sequence"] == sequence
    assert len(RunStore(run).events()) == event_count


def test_start_blind_idempotency_is_scoped_to_question_revision(tmp_path, capsys) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(json.dumps({
        "run_id": "run-1",
        "state": "AUTHORING",
        "sequence": 0,
        "question_revision": "r1",
        "evidence": {"design_evidence": design_links(tmp_path)},
    }))
    task = make_package(tmp_path / "task")
    health_path = tmp_path / "preflight.json"
    health_path.write_text(json.dumps({
        "oracle_full_score": True,
        "independent_honest_executed": True,
        "adversarial_low_score": True,
        "environment_stable": False,
        "package_compliant": True,
        "no_hidden_leakage": True,
    }))
    health_command = [
        "accept-health", str(run), str(health_path),
        "--evidence-ref", "reviewer/preflight.json",
    ]

    assert main(["freeze-package", str(run), str(task)]) == 0
    capsys.readouterr()
    assert main(health_command) == 0
    capsys.readouterr()
    assert main(["start-blind", str(run)]) == 0
    r1 = json.loads(capsys.readouterr().out)
    assert r1["state"] == "BLIND_VALIDATION"

    r1_sequence = r1["sequence"]
    r1_event_count = len(RunStore(run).events())
    assert main(["start-blind", str(run)]) == 0
    r1_replay = json.loads(capsys.readouterr().out)
    assert r1_replay["sequence"] == r1_sequence
    assert len(RunStore(run).events()) == r1_event_count

    reason = tmp_path / "environment-revision.json"
    reason.write_text('{"reason":"runtime environment changed"}')
    RunWorkflow(RunStore(run)).begin_environment_revision(
        Actor.TEACHER,
        "r2",
        reason,
        "environment-revision:r2",
    )
    revised_state = json.loads((run / "state.json").read_text())
    revised_state.setdefault("evidence", {})["design_evidence"] = design_links(tmp_path)
    (run / "state.json").write_text(json.dumps(revised_state))
    assert main(["freeze-package", str(run), str(task)]) == 0
    capsys.readouterr()
    assert main(health_command) == 0
    capsys.readouterr()

    assert main(["start-blind", str(run)]) == 0
    r2 = json.loads(capsys.readouterr().out)
    assert r2["state"] == "BLIND_VALIDATION"
    assert r2["question_revision"] == "r2"
    events = RunStore(run).events()
    blind_keys = [event.idempotency_key for event in events if event.event_type == "validation.blind.started"]
    assert blind_keys == [
        "validation:blind:r1:start",
        "validation:blind:r2:start",
    ]

    sequence = r2["sequence"]
    event_count = len(events)
    assert main(["start-blind", str(run)]) == 0
    r2_replay = json.loads(capsys.readouterr().out)
    assert r2_replay["sequence"] == sequence
    assert len(RunStore(run).events()) == event_count


def test_import_environment_command(tmp_path, capsys) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "status": "ENVIRONMENT_READY",
                "image": {
                    "provider": "lbg",
                    "record_id": 1,
                    "url": "registry/task:fixed",
                    "immutable": True,
                    "reproducibility_digest": "a" * 64,
                    "digest": None,
                },
                "resources": [],
            }
        )
    )
    clean = tmp_path / "clean.json"
    exclusions = [
        {"id": check_id, "passed": True, "exit_code": 0, "observed_count": 0}
        for check_id in (
            "exclusion:app-allowlist",
            "exclusion:task-privileged-material",
            "exclusion:harness-executables",
            "exclusion:credential-material",
            "exclusion:researcher-residue",
        )
    ]
    validation = [{"id": "tool:python", "passed": True, "exit_code": 0}]
    clean.write_text(json.dumps({
        "image_record_id": 1,
        "image_url": "registry/task:fixed",
        "clean_sandboxes_verified": 2,
        "install_commands_executed": 0,
        "public_asset_uploads_executed": 0,
        "runs": [
            {"sandbox_id": "one", "validation": validation, "exclusions": exclusions},
            {"sandbox_id": "two", "validation": validation, "exclusions": exclusions},
        ],
    }))
    result = main(
        [
            "import-environment",
            str(tmp_path / "state"),
            str(manifest),
            "--clean-evidence",
            str(clean),
            "--fencing-token",
            "test-fence",
            "--endpoint",
            "lbg://prod",
            "--project-id",
            "42",
        ]
    )
    assert result == 0
    assert json.loads(capsys.readouterr().out)["lifecycle"] == "STABLE"


def test_lint_and_migrate_commands(tmp_path, capsys) -> None:
    task = make_package(tmp_path / "task")
    assert main(["lint-package", str(task)]) == 0
    assert json.loads(capsys.readouterr().out)["passed"]

    legacy = make_package(tmp_path / "legacy")
    (legacy / "environment/resources.yaml").unlink()
    (legacy / "public_data").mkdir()
    (legacy / "public_data/input.csv").write_text("x\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"status": "ENVIRONMENT_READY", "image": {"immutable": True}}))
    target = tmp_path / "target"
    assert main(["migrate-task", str(legacy), str(target), "--manifest", str(manifest)]) == 0
    assert json.loads(capsys.readouterr().out)["passed"]


def test_build_config_and_issue_researcher_commands(tmp_path, capsys) -> None:
    task = make_package(tmp_path / "task")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    artifact, node, adapter = tmp_path / "dsh.tgz", tmp_path / "node", tmp_path / "adapter"
    artifact.write_bytes(b"dsh")
    node.write_bytes(b"node")
    adapter.mkdir()
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "agent_import": "adapter:Agent",
                "adapter_pythonpath": str(adapter),
                "development_artifact_path": str(artifact),
                "development_artifact_sha256": hashlib.sha256(b"dsh").hexdigest(),
                "harness_version": "1",
                "node_runtime_path": str(node),
                "node_runtime_sha256": hashlib.sha256(b"node").hexdigest(),
                "node_runtime_version": "v22",
            }
        )
    )
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "job_name": "job-1",
                "jobs_dir": str(jobs),
                "task_path": str(task),
                "context_paths": [],
                "lbg_project_id": 42,
            }
        )
    )
    config = tmp_path / "job.json"
    main(["build-harbor-config", str(spec), str(runtime), str(config)])
    capsys.readouterr()
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "request_id": "request-1",
                "run_id": "run-1",
                "question_revision": "r1",
                "attempt_index": 1,
                "mode": "blind",
                "package_path": str(task),
                "package_sha256": package_sha256(task),
                "job_config_path": str(config),
                "job_config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
                "context_digests": [],
                "researcher_thread_id": "breaker",
            }
        )
    )
    assert main(["issue-researcher", str(request), str(tmp_path / "handoffs")]) == 0
    assert Path(json.loads(capsys.readouterr().out)["handoff_path"]).is_file()


def test_dispatch_and_researcher_run_handlers(tmp_path, capsys, monkeypatch) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("[TASKFOUNDRY ROLE=researcher]\nrun\n")
    monkeypatch.setattr(
        "taskfoundry.cli.CodexDispatcher.queue",
        lambda self, **kwargs: DispatchReceipt("message", kwargs["thread_id"], kwargs["role"]),
    )
    main(["dispatch", "--role", "researcher", "--thread-id", "breaker", "--prompt", str(prompt)])
    assert json.loads(capsys.readouterr().out)["role"] == "researcher"

    task = tmp_path / "task"
    jobs = tmp_path / "jobs"
    task.mkdir()
    jobs.mkdir()
    job_config = tmp_path / "job.json"
    job_config.write_text(json.dumps({
        "job_name": "job-1",
        "jobs_dir": str(jobs),
        "n_concurrent_trials": 1,
        "environment": {"type": "lbg", "kwargs": {"project_id": 42}},
        "tasks": [{"path": str(task)}],
    }))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"request_id": "r", "job_config_path": str(job_config)}))
    handoff = tmp_path / "handoff.json"
    handoff.write_text(json.dumps({
        "request_path": str(request),
        "capability_path": "c/x",
        "token_path": "t",
    }))
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "agent_import": "a:A",
                "adapter_pythonpath": "/tmp",
                "development_artifact_path": "/tmp/a",
                "development_artifact_sha256": "a" * 64,
                "harness_version": "1",
                "node_runtime_path": "/tmp/n",
                "node_runtime_sha256": "b" * 64,
                "node_runtime_version": "v",
            }
        )
    )
    env_file = tmp_path / ".env"
    env_file.write_text("X=1\n")
    monkeypatch.setenv("CODEX_THREAD_ID", "breaker")
    monkeypatch.setattr(
        "taskfoundry.cli.CapabilityStore.redeem",
        lambda *args: type("Request", (), {
            "request_id": "r",
            "job_config_path": str(job_config),
        })(),
    )
    monkeypatch.setattr(
        "taskfoundry.cli.execute_harbor",
        lambda **kwargs: ResearcherReceipt("r", "SCIENTIFIC_RESULT", 0.4, "result", "s", "f", 0),
    )
    receipt = tmp_path / "receipt.json"
    queue_root = tmp_path / "harbor-queue"
    main(
        [
            "researcher-run",
            str(handoff),
            str(runtime),
            "--env-file",
            str(env_file),
            "--receipt",
            str(receipt),
            "--queue-root",
            str(queue_root),
        ]
    )
    assert receipt.is_file()
    assert json.loads((queue_root / "harbor-queue.json").read_text())["jobs"][0]["state"] == "COMPLETED"


def test_scheduler_commands_activate_and_report_question(tmp_path, capsys) -> None:
    run = tmp_path / "run"
    RunStore(run).initialize("q3")
    prompt = tmp_path / "teacher.md"
    prompt.write_text("[TASKFOUNDRY ROLE=teacher]\n继续正式出题。\n")
    root = tmp_path / "scheduler"

    assert main([
        "scheduler-enqueue",
        str(root),
        "q3",
        str(run),
        "--teacher-thread-id",
        "teacher-thread",
        "--teacher-prompt",
        str(prompt),
    ]) == 0
    enqueued = json.loads(capsys.readouterr().out)
    assert enqueued["activated"] == ["q3"]
    q3 = next(item for item in enqueued["snapshot"]["questions"] if item["question_id"] == "q3")

    assert main([
        "scheduler-heartbeat",
        str(root),
        "q3",
        "--owner-id",
        q3["lease"]["owner_id"],
        "--lease-id",
        q3["lease"]["lease_id"],
        "--generation",
        str(q3["generation"]),
    ]) == 0
    heartbeat = json.loads(capsys.readouterr().out)
    assert next(item for item in heartbeat["questions"] if item["question_id"] == "q3")["state"] == "LEASED"
    assert main(["scheduler-status", str(root)]) == 0
    assert len(json.loads(capsys.readouterr().out)["questions"]) == 32


def test_scheduler_limit_command_reports_fixed_three_slots(tmp_path, capsys) -> None:
    root = tmp_path / "scheduler"

    assert main(["scheduler-set-limit", str(root), "3"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["snapshot"]["max_active"] == 3
    assert main(["scheduler-status", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["max_active"] == 3


def test_scheduler_wait_and_resume_commands_release_slot(tmp_path, capsys) -> None:
    run = tmp_path / "run"
    RunStore(run).initialize("q3")
    prompt = tmp_path / "teacher.md"
    prompt.write_text("[TASKFOUNDRY ROLE=teacher]\n继续正式出题。\n")
    root = tmp_path / "scheduler"
    main([
        "scheduler-enqueue",
        str(root),
        "q3",
        str(run),
        "--teacher-thread-id",
        "teacher-thread",
        "--teacher-prompt",
        str(prompt),
    ])
    q3 = next(
        item
        for item in json.loads(capsys.readouterr().out)["snapshot"]["questions"]
        if item["question_id"] == "q3"
    )

    assert main([
        "scheduler-wait-external",
        str(root),
        "q3",
        "--owner-id",
        q3["lease"]["owner_id"],
        "--lease-id",
        q3["lease"]["lease_id"],
        "--generation",
        str(q3["generation"]),
        "--kind",
        "HARBOR",
        "--external-id",
        "job-3",
        "--phase",
        "FRESH_BLIND",
    ]) == 0
    waited = json.loads(capsys.readouterr().out)
    assert waited["released"] == ["q3"]

    assert main([
        "scheduler-resume-external",
        str(root),
        "q3",
        "--external-id",
        "job-3",
    ]) == 0
    resumed = json.loads(capsys.readouterr().out)
    item = next(value for value in resumed["snapshot"]["questions"] if value["question_id"] == "q3")
    assert item["state"] == "LEASED"


def test_scheduler_cli_binds_validated_family_before_marking_complete(
    tmp_path, capsys, monkeypatch
) -> None:
    validated: list[tuple[int, Path]] = []

    def validate_family(question: int, family: Path) -> None:
        validated.append((question, family))

    monkeypatch.setattr(scheduler_module, "_default_validate_published_family", validate_family)
    run = tmp_path / "run"
    RunStore(run).initialize("q3")
    prompt = tmp_path / "teacher.md"
    prompt.write_text("[TASKFOUNDRY ROLE=teacher]\n继续正式出题。\n")
    family = tmp_path / "questions" / "3" / "new-question" / "family"
    family.mkdir(parents=True)
    root = tmp_path / "scheduler"
    assert main([
        "scheduler-enqueue",
        str(root),
        "q3",
        str(run),
        "--teacher-thread-id",
        "teacher-thread",
        "--teacher-prompt",
        str(prompt),
    ]) == 0
    enqueued = json.loads(capsys.readouterr().out)
    q3 = next(item for item in enqueued["snapshot"]["questions"] if item["question_id"] == "q3")
    store = RunStore(run)
    current = store.read_snapshot()
    assert current is not None
    store.commit(
        actor=Actor.SYSTEM,
        event_type="test.state.changed",
        idempotency_key="test:q3:completed",
        payload={},
        snapshot=store.advance(current, state=RunState.COMPLETED),
    )

    assert main(["scheduler-tick", str(root)]) == 0
    waiting = json.loads(capsys.readouterr().out)
    q3 = next(item for item in waiting["snapshot"]["questions"] if item["question_id"] == "q3")
    assert q3["state"] == "LEASED"
    assert q3["phase"] == "RUN_COMPLETED_AWAITING_PUBLICATION"

    assert main([
        "scheduler-bind-published-family",
        str(root),
        "q3",
        str(family),
        "--owner-id",
        q3["lease"]["owner_id"],
        "--lease-id",
        q3["lease"]["lease_id"],
        "--generation",
        str(q3["generation"]),
    ]) == 0
    completed = json.loads(capsys.readouterr().out)
    q3 = next(item for item in completed["snapshot"]["questions"] if item["question_id"] == "q3")
    assert q3["state"] == "COMPLETED"
    assert q3["published_family"] == str(family.resolve())
    assert validated == [(3, family.resolve())]


def test_harbor_queue_commands_submit_and_claim_independent_job(tmp_path, capsys) -> None:
    task = tmp_path / "task"
    jobs = tmp_path / "jobs"
    task.mkdir()
    jobs.mkdir()
    config = tmp_path / "job.json"
    config.write_text(json.dumps({
        "job_name": "job-1",
        "jobs_dir": str(jobs),
        "n_concurrent_trials": 1,
        "environment": {"type": "lbg", "kwargs": {"project_id": 42}},
        "tasks": [{"path": str(task)}],
    }))
    root = tmp_path / "harbor-queue"

    assert main(["harbor-queue-submit", str(root), "request-1", str(config)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "WAITING"
    assert main(["harbor-queue-claim", str(root), "--worker-id", "worker-1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["max_active"] == 200
    assert result["claimed"][0]["request_id"] == "request-1"


def test_labwright_delta_cli_round_trip(tmp_path, capsys) -> None:
    root = tmp_path / "labwright"
    FileLabwrightRegistry(root).request_delta(
        Actor.RESEARCHER,
        EnvironmentDeltaRequest(
            request_id="delta-1",
            run_id="run-1",
            question_revision="r1",
            kind="package",
            name="scipy",
            version_constraint="==1.14.0",
            reason="需要求解器",
            researcher_request_id="researcher-1",
            sandbox_id="sandbox-1",
        ),
    )
    assert main([
        "labwright-claim-delta",
        str(root),
        "delta-1",
        "--worker-id",
        "worker-1",
        "--fencing-token",
        "fence-1",
    ]) == 0
    claim = json.loads(capsys.readouterr().out)
    assert claim["request_id"] == "delta-1"

    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({
        "request_id": "builder-runtime-1",
        "sandbox_id": "builder-sandbox-1",
        "artifact": {
            "provider": "lbg",
            "endpoint_identity": "lbg://production",
            "project_id": "42",
            "record_id": "builder-1",
            "image_url": "registry/labwright:fixed",
            "digest": "sha256:" + "c" * 64,
        },
        "started_at": "2026-08-24T00:00:00+00:00",
        "status": "READY",
    }))
    inventory_before = tmp_path / "inventory-before.json"
    inventory_before.write_text('{"packages":[]}')
    inventory_after = tmp_path / "inventory-after.json"
    inventory_after.write_text('{"packages":["scipy==1.14.0"]}')
    probes = tmp_path / "probes.json"
    probes.write_text('{"import_scipy":true}')
    builder_artifact = json.loads(runtime.read_text())["artifact"]
    baseline_artifact = {
        "provider": "lbg",
        "endpoint_identity": "lbg://production",
        "project_id": "42",
        "record_id": "1",
        "image_url": "registry/task:fixed",
        "digest": "sha256:" + "a" * 64,
    }
    source_trace = tmp_path / "source-failure-trace.json"
    source_trace.write_text(json.dumps({
        "schema_version": 1,
        "classification": "ENVIRONMENT_FAILURE",
        "run_id": "run-1",
        "question_revision": "r1",
        "package_sha256": "b" * 64,
        "researcher_request_id": "researcher-1",
        "sandbox_id": "sandbox-1",
        "baseline_identity_sha256": artifact_identity_sha256(
            ArtifactIdentity(**baseline_artifact)
        ),
    }))
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({
        "schema_version": 2,
        "run_id": "run-1",
        "question_revision": "r1",
        "package_sha256": "b" * 64,
        "researcher_request_id": "researcher-1",
        "sandbox_id": "sandbox-1",
        "request_sha256": claim["request_sha256"],
        "baseline_artifact": baseline_artifact,
        "baseline_identity_sha256": artifact_identity_sha256(
            ArtifactIdentity(**baseline_artifact)
        ),
        "builder_runtime_request_id": "builder-runtime-1",
        "builder_sandbox_id": "builder-sandbox-1",
        "builder_identity_sha256": artifact_identity_sha256(
            ArtifactIdentity(**builder_artifact)
        ),
        "capability_name": "scipy",
        "capability_version": "1.14.0",
        "source_trace": {
            "path": str(source_trace),
            "sha256": hashlib.sha256(source_trace.read_bytes()).hexdigest(),
        },
        "inventory_before": {
            "path": str(inventory_before),
            "sha256": hashlib.sha256(inventory_before.read_bytes()).hexdigest(),
        },
        "inventory_after": {
            "path": str(inventory_after),
            "sha256": hashlib.sha256(inventory_after.read_bytes()).hexdigest(),
        },
        "probes": {
            "path": str(probes),
            "sha256": hashlib.sha256(probes.read_bytes()).hexdigest(),
        },
    }))
    assert main([
        "labwright-complete-delta",
        str(root),
        "delta-1",
        str(runtime),
        str(evidence),
        "--capability-version",
        "1.14.0",
        "--fencing-token",
        "fence-1",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "READY"

    baseline_runtime = tmp_path / "baseline-runtime.json"
    baseline_runtime.write_text(json.dumps({"artifact": baseline_artifact}))
    scientific_trace = tmp_path / "scientific-trace.json"
    scientific_trace.write_text(json.dumps({
        "schema_version": 1,
        "classification": "SCIENTIFIC_RESULT",
        "run_id": "run-1",
        "question_revision": "r1",
        "package_sha256": "b" * 64,
        "researcher_request_id": "fresh-retry-request-1",
        "sandbox_id": "fresh-retry-sandbox-1",
        "baseline_identity_sha256": artifact_identity_sha256(
            ArtifactIdentity(**baseline_artifact)
        ),
        "runtime_delta_request_ids": ["delta-1"],
    }))
    plan = tmp_path / "seal-plan.json"
    assert main([
        "labwright-seal-plan",
        str(root),
        "run-1",
        "r1",
        "b" * 64,
        str(baseline_runtime),
        str(scientific_trace),
        str(plan),
        "--request-id",
        "delta-1",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["question_revision"] == "r1"
    assert plan.is_file()


def test_accept_bound_health_command_rejects_legacy_environment(tmp_path, capsys) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(json.dumps({
        "run_id": "run-1",
        "state": "PACKAGE_FROZEN",
        "sequence": 0,
        "package_digest": "a" * 64,
        "environment_key": "b" * 64,
    }))
    evidence = []
    for gate in (
        HealthGate.PACKAGE,
        HealthGate.ENVIRONMENT,
        HealthGate.ORACLE,
        HealthGate.HONEST,
        HealthGate.ADVERSARIAL,
        HealthGate.LEAKAGE,
    ):
        path = tmp_path / f"{gate.value}.json"
        path.write_text(f'{{"gate":"{gate.value}"}}')
        evidence.append((gate, path))
    bundle = BoundHealthEvidence.create(
        question_revision="r1",
        package_sha256="a" * 64,
        environment_key="b" * 64,
        health=HealthEvidence(True, True, True, True, True, True),
        evidence=evidence,
    )
    bundle_path = tmp_path / "bound-health.json"
    bundle_path.write_text(json.dumps(bundle.to_dict()))

    with pytest.raises(RuntimeError, match="runtime closure"):
        main(["accept-bound-health", str(run), str(bundle_path)])


def test_accept_bound_health_command_requires_closure_even_after_trace(tmp_path, capsys) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(json.dumps({
        "run_id": "run-1",
        "state": "BLIND_VALIDATION",
        "sequence": 0,
        "question_revision": "r2",
        "package_digest": "a" * 64,
        "environment_key": "b" * 64,
        "attempts": [{"classification": "SCIENTIFIC_RESULT"}],
    }))
    evidence = []
    for gate in (
        HealthGate.PACKAGE,
        HealthGate.ENVIRONMENT,
        HealthGate.ORACLE,
        HealthGate.HONEST,
        HealthGate.ADVERSARIAL,
        HealthGate.LEAKAGE,
    ):
        path = tmp_path / f"runtime-{gate.value}.json"
        path.write_text(f'{{"gate":"{gate.value}"}}')
        evidence.append((gate, path))
    bundle = BoundHealthEvidence.create(
        question_revision="r2",
        package_sha256="a" * 64,
        environment_key="b" * 64,
        health=HealthEvidence(True, True, True, True, True, True),
        evidence=evidence,
    )
    bundle_path = tmp_path / "runtime-bound-health.json"
    bundle_path.write_text(json.dumps(bundle.to_dict()))

    with pytest.raises(RuntimeError, match="runtime closure"):
        main(["accept-bound-health", str(run), str(bundle_path)])


def test_legacy_health_and_blind_commands_remain_compatible(tmp_path, capsys) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(json.dumps({
        "run_id": "run-1",
        "state": "PACKAGE_FROZEN",
        "sequence": 0,
    }))
    health = tmp_path / "health.json"
    health.write_text(json.dumps({
        "oracle_full_score": True,
        "independent_honest_executed": True,
        "adversarial_low_score": True,
        "environment_stable": True,
        "package_compliant": True,
        "no_hidden_leakage": True,
    }))

    assert main([
        "accept-health",
        str(run),
        str(health),
        "--evidence-ref",
        "legacy-evidence.json",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "ORACLE_PASSED"

    assert main(["start-blind", str(run)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "BLIND_VALIDATION"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ('{"oracle_full_score":true,"oracle_full_score":true}', "duplicate key"),
        ('{"oracle_full_score":NaN}', "non-finite number"),
    ],
)
def test_cli_json_inputs_fail_closed_on_ambiguous_numbers_and_keys(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    """所有 CLI JSON 入口必须拒绝重复键和非有限常量。"""
    run = tmp_path / "run"
    run.mkdir()
    health = tmp_path / "health.json"
    health.write_text(payload)

    with pytest.raises(SystemExit, match=message):
        main(["accept-health", str(run), str(health), "--evidence-ref", "evidence.json"])
