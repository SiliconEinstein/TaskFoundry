"""通过公开契约把 Q16 迁移为正式 TaskFoundry 运行。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from taskfoundry.labwright import FileLabwrightRegistry
from taskfoundry.model import Actor, QuestionDesignBrief
from taskfoundry.package import migrate_legacy_task, scientific_contract_sha256
from taskfoundry.policy import PolicyRepository
from taskfoundry.store import RunStore
from taskfoundry.workflow import RunWorkflow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--clean-evidence", type=Path)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path("/personal/codex-workspace/question-from-questions/16"),
    )
    args = parser.parse_args()
    if args.run_dir.exists():
        raise SystemExit(f"run directory already exists: {args.run_dir}")

    source_task = args.source_root / "new-question/pde-stability-route-transfer"
    manifest = args.source_root / "labwright/published-v4/environment-manifest.json"
    clean_evidence = args.clean_evidence or args.run_dir / "labwright-verification/clean-sandbox-evidence.json"
    if not clean_evidence.is_file():
        raise SystemExit(f"two-sandbox Labwright evidence is required: {clean_evidence}")
    brief_source = args.project_root / "examples/q16-brief.json"
    brief_value = json.loads(brief_source.read_text(encoding="utf-8"))
    QuestionDesignBrief.from_dict(brief_value)

    design_dir = args.run_dir / "design"
    policy_dir = args.run_dir / "policy"
    task = args.run_dir / "question-revisions/r1/task"
    design_dir.mkdir(parents=True)
    shutil.copy2(brief_source, design_dir / "brief.json")

    store = RunStore(args.run_dir)
    store.initialize("q16-first-run")
    workflow = RunWorkflow(store)
    workflow.attach_brief(Actor.TEACHER, design_dir / "brief.json", "q16-brief")

    policies = PolicyRepository(args.project_root / "policies")
    lock = policies.lock("method-selection")
    policies.write_lock(lock, policy_dir / "lock.json")
    workflow.lock_policies(Actor.TEACHER, policy_dir / "lock.json", "q16-policy-lock")
    workflow.request_environment(Actor.TEACHER, "q16-environment-request")

    registry = FileLabwrightRegistry(args.run_dir / "labwright-state")
    receipt = registry.import_stable(
        manifest,
        clean_evidence_path=clean_evidence,
        fencing_token="q16-r4-double-clean-v1",
        endpoint_identity="lbg://dptech-production",
        project_id="2309771",
        workdir="/app",
    )
    workflow.environment_ready(Actor.LABWRIGHT, receipt, "q16-environment-ready")
    workflow.begin_authoring(Actor.TEACHER, "q16-authoring")

    report = migrate_legacy_task(source_task, task, manifest)
    old_digest = scientific_contract_sha256(source_task, manifest)
    new_digest = scientific_contract_sha256(task, manifest)
    if old_digest != new_digest:
        raise SystemExit("Q16 layout migration changed the scientific contract")
    equivalence = {
        "schema_version": 1,
        "source_package": str(source_task),
        "migrated_package": str(task),
        "source_package_sha256": old_digest,
        "migrated_package_sha256": new_digest,
        "scientifically_equivalent": True,
    }
    (args.run_dir / "migration-equivalence.json").write_text(
        json.dumps(equivalence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    workflow.freeze_package(Actor.TEACHER, task, "q16-package-freeze")
    print(json.dumps({"run_dir": str(args.run_dir), "package": report.to_dict()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
