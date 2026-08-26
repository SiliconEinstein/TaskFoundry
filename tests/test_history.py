from __future__ import annotations

import json
from pathlib import Path

import pytest

from taskfoundry import history
from taskfoundry.history import (
    atomic_json,
    atomic_text,
    campaign_status,
    central_archive_record,
    consolidate,
    independent_legacy_assessment,
    latest_formal_verdict,
    legacy_resolution,
    load_json_object,
    question_number_from_run_name,
    render_question_markdown,
    run_record,
    run_status,
    sensitive_trace_exclusions,
    selected_trace_files,
    snapshot_trace_file,
)
from taskfoundry.history_transaction import (
    assert_no_symlink_components,
    commit_replacements,
    exclusive_history_lock,
    staging_directory,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("q3-run", 3), ("q03-run", 3), ("q30-run", 30), ("q3x-run", None), ("publication", None)],
)
def test_question_number_from_run_name(name: str, expected: int | None) -> None:
    assert question_number_from_run_name(name) == expected


def test_consolidate_preserves_trace_and_keeps_publication_clean(tmp_path: Path) -> None:
    questions = tmp_path / "questions"
    runs = tmp_path / "runs"
    workbench = tmp_path / "workbench"
    for number in (3, 4):
        (questions / str(number) / "trace/authoring/pre-skillbank/legacy/new-question").mkdir(parents=True)
        (questions / str(number) / "question-pack").mkdir(parents=True)
    run = runs / "q03-example"
    (run / "reviewer").mkdir(parents=True)
    (run / "package").mkdir()
    (run / "state.json").write_text(
        json.dumps(
            {
                "sequence": 7,
                "state": "TOO_EASY",
                "package_digest": "abc",
                "attempts": [
                    {"classification": "SCIENTIFIC_RESULT", "score": 0.91},
                    {"classification": "PLATFORM_FAILURE", "score": None},
                ],
            }
        ),
        encoding="utf-8",
    )
    (run / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (run / "reviewer/report.md").write_text("PASS evidence\n", encoding="utf-8")
    (run / "package/private.json").write_text("secret\n", encoding="utf-8")
    (workbench / "q03/candidate-a").mkdir(parents=True)
    audit_root = questions / "authoring-history-audits"
    audit_root.mkdir()
    (audit_root / "inventory-q03-q04.json").write_text(
        json.dumps({"questions": {"Q3": {"resolved_status": "HISTORICAL_COMPLETE"}}}),
        encoding="utf-8",
    )

    ledger = consolidate(
        (3, 4),
        question_root=questions,
        runs_root=runs,
        workbench_root=workbench,
        apply=True,
    )

    q3 = ledger["questions"][0]
    assert q3["taskfoundry_runs"][0]["scientific_scores"] == [0.91]
    assert q3["taskfoundry_runs"][0]["platform_failure_count"] == 1
    assert q3["independent_legacy_assessment"]["assessment"]["resolved_status"] == "HISTORICAL_COMPLETE"
    assert q3["legacy_resolution"] is None
    imported = questions / "3/trace/authoring/imported-taskfoundry-runs/q03-example"
    assert (imported / "state.json").is_file()
    assert (imported / "reviewer/report.md").is_file()
    assert not (imported / "package/private.json").exists()
    assert not any((questions / "3/question-pack").iterdir())
    assert (questions / "AUTHORING_PROGRESS.json").is_file()
    assert (questions / "3/trace/authoring/HISTORY_INDEX.json").is_file()
    first_global = (questions / "AUTHORING_PROGRESS.json").read_bytes()
    first_index = (questions / "3/trace/authoring/HISTORY_INDEX.json").read_bytes()

    second = consolidate(
        (3, 4),
        question_root=questions,
        runs_root=runs,
        workbench_root=workbench,
        apply=True,
    )
    assert second["questions"][0]["taskfoundry_runs"][0]["trace_file_count"] == 3
    assert (questions / "AUTHORING_PROGRESS.json").read_bytes() == first_global
    assert (questions / "3/trace/authoring/HISTORY_INDEX.json").read_bytes() == first_index


def test_prior_source_drift_is_rejected(tmp_path: Path) -> None:
    questions = tmp_path / "questions"
    runs = tmp_path / "runs"
    workbench = tmp_path / "workbench"
    (questions / "3/trace/authoring").mkdir(parents=True)
    (questions / "3/question-pack").mkdir(parents=True)
    run = runs / "q3-run"
    run.mkdir(parents=True)
    (run / "state.json").write_text("{}\n", encoding="utf-8")
    index = questions / "3/trace/authoring/HISTORY_INDEX.json"
    index.write_text(
        json.dumps(
            {
                "question": "Q03",
                "taskfoundry_runs": [
                    {
                        "source_path": str(run),
                        "trace_files": [{"path": "state.json", "sha256": "0" * 64}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="历史源 trace 已漂移"):
        consolidate(
            (3,),
            question_root=questions,
            runs_root=runs,
            workbench_root=workbench,
            apply=True,
        )


def test_json_status_and_campaign_edge_branches(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    sequence = tmp_path / "sequence.json"
    sequence.write_text("[]", encoding="utf-8")
    assert load_json_object(malformed) is None
    assert load_json_object(sequence) is None
    assert load_json_object(tmp_path / "missing.json") is None

    run = tmp_path / "run"
    run.mkdir()
    assert run_status(run, {"state": "BLOCKED"}) == "BLOCKED"
    (run / "Q_COMPLETE.json").write_text("{}", encoding="utf-8")
    assert run_status(run, {"status": "ACTIVE"}) == "HISTORICAL_COMPLETE_CLAIM"
    (run / "Q_COMPLETE.json").unlink()
    (run / "ABANDONED.json").write_text("{}", encoding="utf-8")
    assert run_status(run, None) == "INVALIDATED_OR_ABANDONED"
    (run / "state.json").write_text(json.dumps({"state": "BLOCKED"}), encoding="utf-8")
    assert run_status(run, {"state": "BLOCKED"}) == "INVALIDATED_OR_ABANDONED"
    (run / "state.json").unlink()
    (run / "ABANDONED.json").unlink()
    assert run_status(run, None) == "UNRESOLVED_HISTORICAL"
    (run / "Q_INCOMPLETE.json").write_text("{}", encoding="utf-8")
    assert run_status(run, None) == "UNRESOLVED_HISTORICAL"

    internal = run / "agent/trace-validation.json"
    internal.parent.mkdir()
    internal.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    (run / "state.json").write_text(json.dumps({"state": "DEFERRED_TIMEOUT"}), encoding="utf-8")
    assert run_status(run, {"state": "DEFERRED_TIMEOUT"}) == "DEFERRED_TIMEOUT"

    assert (
        campaign_status(published=[tmp_path], verdict=None, brief_complete=False)
        == "PUBLICATION_PRESENT_REQUIRES_CANONICAL_VALIDATION"
    )
    assert campaign_status(published=[], verdict={"verdict": "PASS"}, brief_complete=False).startswith("FORMAL_PASS")
    assert campaign_status(published=[], verdict={"verdict": "REJECT"}, brief_complete=True) == "FORMAL_REJECTED"
    assert campaign_status(published=[], verdict=None, brief_complete=True) == "AUTHORING"


def test_review_assessment_and_markdown_branches(tmp_path: Path) -> None:
    question = tmp_path / "3"
    assert latest_formal_verdict(question) is None
    review = question / "trace/authoring/reviews/v1"
    review.mkdir(parents=True)
    report = review / "formal-review-report.md"
    report.write_text("Verdict: PASS\n", encoding="utf-8")
    assert latest_formal_verdict(question)["verdict"] == "PASS"
    report.write_text("No decision yet\n", encoding="utf-8")
    assert latest_formal_verdict(question)["verdict"] == "UNKNOWN"
    json_report = review / "formal-review-report.json"
    json_report.write_text(json.dumps({"decision": "reject"}), encoding="utf-8")
    assert latest_formal_verdict(question)["verdict"] == "REJECT"

    assert independent_legacy_assessment(tmp_path, 3) is None
    audits = tmp_path / "authoring-history-audits"
    audits.mkdir()
    (audits / "inventory-empty.json").write_text(json.dumps({"questions": []}), encoding="utf-8")
    (audits / "inventory-q03.json").write_text(
        json.dumps({"questions": {"Q03": {"terminal_state": "COMPLETE"}}}), encoding="utf-8"
    )
    assessment = independent_legacy_assessment(tmp_path, 3)
    assert assessment is not None
    assert legacy_resolution(assessment) == "COMPLETE"
    assert legacy_resolution(None) is None
    assert legacy_resolution({"assessment": []}) is None
    assert legacy_resolution({"assessment": {"terminal_state": ""}}) is None

    markdown = render_question_markdown(
        {
            "question": "Q03",
            "campaign_status": "AUTHORING",
            "owner_confirmed_final": False,
            "taskfoundry_runs": [],
            "legacy_resolution": None,
            "workbench_candidates": [],
            "publication": {"family_manifests": []},
            "reuse_policy": "reuse",
        }
    )
    assert "未发现历史 run" in markdown


def test_dry_run_and_snapshot_digest_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = tmp_path / "q3-run"
    run.mkdir()
    (run / "events.jsonl").write_text("{}\n", encoding="utf-8")
    record = run_record(run, tmp_path / "snapshots", apply=False)
    assert record["trace_file_count"] == 1
    assert not (tmp_path / "snapshots").exists()

    source = run / "events.jsonl"
    destination = tmp_path / "snapshot/events.jsonl"
    real_digest = history.sha256_file
    calls = 0

    def mismatching_digest(path: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            return "0" * 64
        return real_digest(path)

    monkeypatch.setattr(history, "sha256_file", mismatching_digest)
    with pytest.raises(RuntimeError, match="digest mismatch"):
        snapshot_trace_file(source, destination)
    assert not destination.exists()


def test_trace_selection_excludes_private_package_and_keeps_terminal_and_harbor(tmp_path: Path) -> None:
    run = tmp_path / "q3-run"
    private = run / "reviewer/scope/package/tests"
    private.mkdir(parents=True)
    (private / "hidden_oracle.json").write_text("{}", encoding="utf-8")
    ground_truth = run / "reviewer/ground-truth"
    ground_truth.mkdir()
    (ground_truth / "answer.json").write_text("{}", encoding="utf-8")
    (run / "ABANDONED-formal.json").write_text("{}", encoding="utf-8")
    harbor = run / "harbor-jobs/job-1/verifier"
    harbor.mkdir(parents=True)
    (harbor / "reward.json").write_text("{}", encoding="utf-8")
    packaged_harbor = run / "harbor-jobs/job-2/package__temporary/verifier"
    packaged_harbor.mkdir(parents=True)
    (packaged_harbor / "reward.json").write_text("{}", encoding="utf-8")
    (run / "run.json").write_text("{}", encoding="utf-8")
    (run / "agent").mkdir()
    (run / "agent/safe-task.log").write_text("task-selection-method-selection", encoding="utf-8")
    secret_trace = run / "agent/runtime.log"
    secret_trace.write_text("sk-" + "a" * 24, encoding="utf-8")

    selected = {path.relative_to(run).as_posix() for path in selected_trace_files(run)}
    assert "reviewer/scope/package/tests/hidden_oracle.json" not in selected
    assert "reviewer/ground-truth/answer.json" not in selected
    assert "ABANDONED-formal.json" in selected
    assert "harbor-jobs/job-1/verifier/reward.json" in selected
    assert "harbor-jobs/job-2/package__temporary/verifier/reward.json" in selected
    assert "run.json" in selected
    assert "agent/safe-task.log" in selected
    assert "agent/runtime.log" not in selected
    exclusions = sensitive_trace_exclusions(run)
    assert [(item["path"], item["reason"]) for item in exclusions] == [
        ("agent/runtime.log", "api_secret")
    ]


def test_symlink_run_and_destination_are_rejected(tmp_path: Path) -> None:
    real_run = tmp_path / "real-run"
    real_run.mkdir()
    (real_run / "state.json").write_text("{}", encoding="utf-8")
    linked_run = tmp_path / "q3-linked"
    linked_run.symlink_to(real_run, target_is_directory=True)
    with pytest.raises(RuntimeError, match="run 根目录必须是普通目录"):
        run_record(linked_run, tmp_path / "snapshot", apply=False)

    source = real_run / "state.json"
    destination = tmp_path / "snapshot/state.json"
    destination.parent.mkdir()
    destination.symlink_to(source)
    with pytest.raises(RuntimeError, match="trace snapshot conflict"):
        snapshot_trace_file(source, destination, snapshot_root=tmp_path / "snapshot")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "trace.json").write_text("{}", encoding="utf-8")
    linked_parent = real_run / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="事务路径不能包含符号链接"):
        snapshot_trace_file(
            linked_parent / "trace.json",
            tmp_path / "safe/trace.json",
            source_root=real_run,
        )


def test_transaction_rolls_back_prior_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target_a = root / "a"
    target_b = root / "b"
    target_a.write_text("old-a", encoding="utf-8")
    target_b.write_text("old-b", encoding="utf-8")
    staged_a = root / "stage-a"
    staged_b = root / "stage-b"
    staged_a.write_text("new-a", encoding="utf-8")
    staged_b.write_text("new-b", encoding="utf-8")
    real_replace = history.os.replace

    def fail_on_staged_b(source: Path, destination: Path) -> None:
        if Path(source) == staged_b:
            raise OSError("commit failure")
        real_replace(source, destination)

    monkeypatch.setattr("taskfoundry.history_transaction.os.replace", fail_on_staged_b)
    with pytest.raises(OSError, match="commit failure"):
        commit_replacements(
            [(staged_a, target_a), (staged_b, target_b)],
            transaction_root=root,
            backup_root=root / "backup",
        )
    assert target_a.read_text(encoding="utf-8") == "old-a"
    assert target_b.read_text(encoding="utf-8") == "old-b"


def test_transaction_does_not_follow_parent_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    staged = root / "staged"
    staged.write_text("new", encoding="utf-8")
    with pytest.raises(RuntimeError, match="事务路径不能包含符号链接"):
        commit_replacements(
            [(staged, root / "linked/created/target")],
            transaction_root=root,
            backup_root=root / "backup",
        )
    assert not (outside / "created").exists()


def test_transaction_preserves_backup_when_rollback_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    targets = [root / "a", root / "b"]
    staged = [root / "stage-a", root / "stage-b"]
    for index, target in enumerate(targets):
        target.write_text(f"old-{index}", encoding="utf-8")
        staged[index].write_text(f"new-{index}", encoding="utf-8")
    backup_root = root / "backup"
    real_replace = history.os.replace

    def fail_install_and_restore(source: Path, destination: Path) -> None:
        if Path(source) == staged[1] or (Path(source) == backup_root / "0" and Path(destination) == targets[0]):
            raise OSError("injected replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr("taskfoundry.history_transaction.os.replace", fail_install_and_restore)
    with pytest.raises(RuntimeError, match="备份保留"):
        commit_replacements(
            list(zip(staged, targets, strict=True)),
            transaction_root=root,
            backup_root=backup_root,
        )
    assert (backup_root / "0").read_text(encoding="utf-8") == "old-0"


def test_archive_binding_existing_snapshot_and_transaction_guards(tmp_path: Path) -> None:
    runs = tmp_path / "TaskFoundry/runs"
    runs.mkdir(parents=True)
    batch = tmp_path / "TaskFoundry/archive/question-authoring/_migration_batches/b1"
    batch.mkdir(parents=True)
    manifest = batch / "PRE_MIGRATION_MANIFEST.json"
    manifest.write_text(
        json.dumps(
            {
                "batch_id": "b1",
                "questions": [
                    {
                        "question": "Q03",
                        "entry_count": 1,
                        "entries": [{"destination": "/archive/q03/item"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    record = central_archive_record(runs, 3)
    assert record is not None
    assert record["migration_batches"][0]["batch_id"] == "b1"
    assert central_archive_record(runs, 4) is not None

    source = tmp_path / "source.json"
    source.write_text("{}\n", encoding="utf-8")
    destination = tmp_path / "snapshots/source.json"
    digest = snapshot_trace_file(source, destination, snapshot_root=tmp_path / "snapshots")
    assert snapshot_trace_file(source, destination, snapshot_root=tmp_path / "snapshots") == digest

    outside = tmp_path.parent / "outside"
    with pytest.raises(RuntimeError, match="路径逃出事务根目录"):
        assert_no_symlink_components(tmp_path, outside)
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path / "missing")
    with pytest.raises(RuntimeError, match="事务路径不能包含符号链接"):
        assert_no_symlink_components(tmp_path, linked / "child")

    lock = tmp_path / "history.lock"
    with exclusive_history_lock(lock):
        assert lock.is_file()
    with staging_directory(tmp_path) as staging:
        assert staging.is_dir()
    assert not staging.exists()


@pytest.mark.parametrize(("writer", "value"), [(atomic_json, {"a": 1}), (atomic_text, "text\n")])
def test_atomic_writer_cleans_temporary_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: object, value: object
) -> None:
    def fail_replace(source: str, destination: Path) -> None:
        raise OSError(f"cannot replace {destination} from {source}")

    monkeypatch.setattr(history.os, "replace", fail_replace)
    with pytest.raises(OSError, match="cannot replace"):
        writer(tmp_path / "output", value)  # type: ignore[operator]
    assert not list(tmp_path.glob(".output.*"))
