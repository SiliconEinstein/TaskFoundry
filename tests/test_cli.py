from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import taskfoundry.scheduler as scheduler_module

from taskfoundry.codex_agent import DispatchReceipt
from taskfoundry.cli import main
from taskfoundry.researcher import IssuedHandoff, ResearcherReceipt
from taskfoundry.labwright import ArtifactIdentity, EnvironmentDeltaRequest, FileLabwrightRegistry
from taskfoundry.labwright_runtime import artifact_identity_sha256
from taskfoundry.model import Actor, RunState
from taskfoundry.store import RunStore
from taskfoundry.supervisor import AdvanceResult
from taskfoundry.workflow import RunWorkflow, WorkflowError


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
    (root / "tests/test.sh").write_text(
        '#!/bin/sh\n[ "$1" = "--probe" ] && exit 0\necho 1 > /logs/verifier/reward.txt\n'
    )
    return root


def test_status_prints_snapshot(tmp_path, capsys) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(
        json.dumps({"run_id": "r", "state": "DESIGNING", "sequence": 0})
    )
    assert main(["status", str(run)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "DESIGNING"


def test_supervisor_cli_register_once_and_resume(tmp_path, capsys, monkeypatch) -> None:
    run = tmp_path / "runs" / "q12-run"
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "question_id": "q12",
        "run_dir": str(run.resolve()),
        "package_path": str((tmp_path / "package").resolve()),
        "job_config_path": str((tmp_path / "job.json").resolve()),
        "runtime_path": str((tmp_path / "runtime.json").resolve()),
        "env_file": str((tmp_path / ".env").resolve()),
        "queue_root": str((tmp_path / "queue").resolve()),
        "validation_session_id": "q12-r16-persistent-01",
        "pass_threshold": 0.85,
        "overall_timeout_sec": 7200,
        "schema_version": 1,
    }))
    assert main(["supervisor-register", str(plan)]) == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "READY"

    monkeypatch.setattr(
        "taskfoundry.cli.CampaignSupervisor.run_once",
        lambda self: (AdvanceResult("q12", "RUNNING", "WORKER_RUNNING"),),
    )
    assert main(["supervise", str(tmp_path / "runs"), "--once"]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["action"] == "WORKER_RUNNING"

    monkeypatch.setattr(
        "taskfoundry.cli.CampaignSupervisor.resume",
        lambda self, question_id: AdvanceResult(question_id, "READY", "RESUMED"),
    )
    assert main(["supervisor-resume", str(tmp_path / "runs"), "q12"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "RESUMED"


def test_cli_direct_validation_cannot_skip_teacher_skill_contract(tmp_path, capsys) -> None:
    run = tmp_path / "direct-run"
    store = RunStore(run)
    store.initialize("direct-run-1")
    current = store.read_snapshot()
    assert current is not None
    store.commit(
        actor=Actor.TEACHER,
        event_type="test.authoring",
        idempotency_key="test-authoring",
        payload={},
        snapshot=store.advance(current, state=RunState.AUTHORING),
    )
    task = make_package(tmp_path / "direct-task")

    with pytest.raises(WorkflowError, match="Skill contract"):
        main([
            "freeze-and-start-validation",
            str(run),
            str(task),
            "--validation-session-id",
            "validation-session-1",
            "--researcher-thread-id",
            "researcher-thread-1",
        ])


def test_current_workflow_cli_adapters_route_without_legacy_gates(
    tmp_path, capsys, monkeypatch
) -> None:
    RunStore(tmp_path / "run").initialize("q3")
    snapshot = SimpleNamespace(to_dict=lambda: {"state": "OK"})
    for name in (
        "begin_authoring",
        "accept_teacher_skill_activation",
        "attach_brief",
        "record_validation_round",
        "begin_revision",
        "accept_runtime_delta",
        "migrate_incomplete_validation_session",
        "accept_runtime_closure",
        "bind_runtime_environment",
        "begin_runtime_finalization",
    ):
        monkeypatch.setattr(RunWorkflow, name, lambda self, *args, **kwargs: snapshot)
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}")
    activation = tmp_path / "activation.json"
    activation.write_text(json.dumps({"prompt_bundle": {"path": "/prompt"}}))
    brief_json = tmp_path / "QuestionDesignBrief.json"
    brief_markdown = tmp_path / "QuestionDesignBrief.md"
    brief_json.write_text("{}")
    brief_markdown.write_text("# Brief")
    monkeypatch.setattr(
        "taskfoundry.cli.resolve_teacher_activation", lambda *args, **kwargs: activation
    )
    monkeypatch.setattr(
        "taskfoundry.cli.validate_latest_brief",
        lambda question: (brief_json, brief_markdown),
    )
    monkeypatch.setattr("taskfoundry.cli.record_skill_attribution", lambda *args: artifact)
    monkeypatch.setattr("taskfoundry.cli.reconcile_skill_batch", lambda *args: {"ok": True})
    monkeypatch.setattr("taskfoundry.cli.evaluate_skill_candidate", lambda *args: {"ok": True})
    monkeypatch.setattr("taskfoundry.cli._environment_receipt", lambda path: object())

    commands = (
        ["begin-authoring", str(tmp_path / "run")],
        ["bind-teacher-skill", str(tmp_path / "run"), "3", "outline", str(artifact)],
        ["attach-question-brief", str(tmp_path / "run"), str(artifact)],
        [
            "resolve-teacher-skill", "3", "outline", "attempt",
            "--batch-id", "batch", "--batch-question", "3",
            "--task-input", str(artifact),
        ],
        ["validate-question-brief", "3"],
        ["record-skill-attribution", "3", str(artifact)],
        ["reconcile-skill-bank", "batch", "--question", "3", "--question", "4"],
        ["evaluate-skill-candidate", str(artifact), str(artifact)],
        ["record-validation-round", str(tmp_path / "run"), "request-1"],
        ["begin-difficulty-revision", str(tmp_path / "run"), "r2", str(artifact)],
        ["accept-runtime-delta", str(tmp_path / "run"), str(artifact)],
        ["migrate-incomplete-validation", str(tmp_path / "run"), "r2"],
        ["accept-runtime-closure", str(tmp_path / "run"), str(artifact)],
        ["bind-runtime-environment", str(tmp_path / "run"), str(artifact)],
        ["begin-runtime-finalization", str(tmp_path / "run")],
    )
    for command in commands:
        assert main(command) == 0
        assert json.loads(capsys.readouterr().out)


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


def test_lint_package_command(tmp_path, capsys) -> None:
    task = make_package(tmp_path / "task")
    assert main(["lint-package", str(task)]) == 0
    assert json.loads(capsys.readouterr().out)["passed"]


def test_build_config_and_issue_researcher_commands(tmp_path, capsys, monkeypatch) -> None:
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
    handoff_root = tmp_path / "run/researcher-requests/request-1"
    handoff_root.mkdir(parents=True)
    monkeypatch.setattr(
        "taskfoundry.cli.RunWorkflow.issue_researcher_request",
        lambda self, actor, **kwargs: IssuedHandoff(
            str(handoff_root / "request.json"),
            str(handoff_root / "capability.json"),
            str(handoff_root / "token"),
        ),
    )
    assert main(
        ["issue-researcher", str(tmp_path / "run"), "request-1", str(config)]
    ) == 0
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


def test_researcher_run_rejects_invalid_runtime_before_redeeming_or_queueing(
    tmp_path, monkeypatch
) -> None:
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"request_id": "r", "job_config_path": str(tmp_path / "job.json")}))
    handoff = tmp_path / "handoff.json"
    handoff.write_text(json.dumps({
        "request_path": str(request),
        "capability_path": str(tmp_path / "capability.json"),
        "token_path": str(tmp_path / "token"),
    }))
    invalid_runtime = tmp_path / "invalid-runtime.json"
    invalid_runtime.write_text(json.dumps({"agents": []}))
    env_file = tmp_path / ".env"
    env_file.write_text("X=1\n")
    monkeypatch.setenv("CODEX_THREAD_ID", "breaker")
    redeemed = False

    def fail_if_redeemed(*args):
        nonlocal redeemed
        redeemed = True
        raise AssertionError("invalid runtime consumed the capability")

    monkeypatch.setattr("taskfoundry.cli.CapabilityStore.redeem", fail_if_redeemed)
    with pytest.raises(TypeError):
        main([
            "researcher-run",
            str(handoff),
            str(invalid_runtime),
            "--env-file",
            str(env_file),
            "--receipt",
            str(tmp_path / "receipt.json"),
            "--queue-root",
            str(tmp_path / "harbor-queue"),
        ])
    assert redeemed is False
    assert not (tmp_path / "harbor-queue/harbor-queue.json").exists()


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


def test_scheduler_limit_command_updates_slots(tmp_path, capsys) -> None:
    root = tmp_path / "scheduler"

    assert main(["scheduler-set-limit", str(root), "1"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["snapshot"]["max_active"] == 1
    assert main(["scheduler-status", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["max_active"] == 1


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

    trace = tmp_path / "questions" / "3" / "trace/final/package-digest"
    trace.mkdir(parents=True)

    def validate_family(question: int, family: Path, _run: object) -> Path:
        validated.append((question, family))
        return trace

    monkeypatch.setattr(scheduler_module, "_default_finalize_published_family", validate_family)
    run = tmp_path / "run"
    RunStore(run).initialize("q3")
    prompt = tmp_path / "teacher.md"
    prompt.write_text("[TASKFOUNDRY ROLE=teacher]\n继续正式出题。\n")
    family = tmp_path / "questions" / "3" / "question-pack"
    family.mkdir(parents=True)
    monkeypatch.setattr(scheduler_module, "published_family_path", lambda _number: family)
    monkeypatch.setattr(
        scheduler_module.QuestionScheduler,
        "_reconcile_batch_at_completion",
        lambda *args: None,
    )
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
    assert q3["state"] == "PUBLICATION_PENDING"
    assert q3["phase"] == "RUN_COMPLETED_AWAITING_PUBLICATION"

    assert main([
        "scheduler-bind-published-family",
        str(root),
        "q3",
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


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ('{"oracle_full_score":true,"oracle_full_score":true}', "duplicate evidence JSON key"),
        ('{"oracle_full_score":NaN}', "non-finite evidence JSON number"),
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
    (run / "state.json").write_text(json.dumps({
        "run_id": "run-1",
        "state": "BLIND_VALIDATION",
        "sequence": 0,
    }))
    bundle = tmp_path / "runtime-delta.json"
    bundle.write_text(payload)

    with pytest.raises(RuntimeError, match=message):
        main(["accept-runtime-delta", str(run), str(bundle)])
