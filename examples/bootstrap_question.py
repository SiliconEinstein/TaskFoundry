"""从一份权威题包创建 TaskFoundry 验证运行。"""

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
    """校验输入，并创建绑定冻结环境的出题运行。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--brief", type=Path, required=True)
    parser.add_argument("--source-task", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--clean-evidence", type=Path, required=True)
    parser.add_argument("--fencing-token", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--endpoint", default="lbg://dptech-production")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    if args.run_dir.exists():
        raise SystemExit(f"run directory already exists: {args.run_dir}")
    for path in (args.brief, args.manifest, args.clean_evidence):
        if not path.is_file():
            raise SystemExit(f"required input is missing: {path}")

    brief_value = json.loads(args.brief.read_text(encoding="utf-8"))
    QuestionDesignBrief.from_dict(brief_value)
    design_dir = args.run_dir / "design"
    policy_dir = args.run_dir / "policy"
    task = args.run_dir / "question-revisions/r1/task"
    design_dir.mkdir(parents=True)
    shutil.copy2(args.brief, design_dir / "brief.json")

    store = RunStore(args.run_dir)
    store.initialize(args.run_id)
    workflow = RunWorkflow(store)
    workflow.attach_brief(Actor.TEACHER, design_dir / "brief.json", f"{args.run_id}:brief")
    policies = PolicyRepository(args.project_root / "policies")
    lock = policies.lock(brief_value["question_type"])
    policies.write_lock(lock, policy_dir / "lock.json")
    workflow.lock_policies(Actor.TEACHER, policy_dir / "lock.json", f"{args.run_id}:policy")
    workflow.request_environment(Actor.TEACHER, f"{args.run_id}:environment-request")

    registry = FileLabwrightRegistry(args.run_dir / "labwright-state")
    receipt = registry.import_stable(
        args.manifest,
        clean_evidence_path=args.clean_evidence,
        fencing_token=args.fencing_token,
        endpoint_identity=args.endpoint,
        project_id=args.project_id,
        workdir="/app",
    )
    workflow.environment_ready(Actor.LABWRIGHT, receipt, f"{args.run_id}:environment-ready")
    workflow.begin_authoring(Actor.TEACHER, f"{args.run_id}:authoring")

    report = migrate_legacy_task(args.source_task, task, args.manifest)
    source_digest = scientific_contract_sha256(args.source_task, args.manifest)
    migrated_digest = scientific_contract_sha256(task, args.manifest)
    if source_digest != migrated_digest:
        raise SystemExit("layout migration changed the scientific contract")
    equivalence = {
        "schema_version": 1,
        "source_package": str(args.source_task.resolve()),
        "migrated_package": str(task.resolve()),
        "source_package_sha256": source_digest,
        "migrated_package_sha256": migrated_digest,
        "scientifically_equivalent": True,
    }
    (args.run_dir / "migration-equivalence.json").write_text(
        json.dumps(equivalence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    workflow.freeze_package(Actor.TEACHER, task, f"{args.run_id}:package-freeze")
    print(json.dumps({"run_dir": str(args.run_dir), "package": report.to_dict()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
