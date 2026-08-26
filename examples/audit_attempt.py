"""审计一份 Researcher 回执的身份、冻结字节和隐藏信息泄漏。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from taskfoundry.model import Actor
from taskfoundry.researcher import write_json
from taskfoundry.store import RunStore
from taskfoundry.workflow import RunWorkflow


FORBIDDEN = ("/tests/", "/solution/", "hint.md", "ground_truth", "checker.py")


def sha256(path: Path) -> str:
    """返回一个文件的小写 SHA-256。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    """扫描 Agent 可见日志，并用审计证据推进流程。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("revision")
    parser.add_argument("mode", choices=("blind", "hint"))
    parser.add_argument("index", type=int)
    parser.add_argument("--question-label", required=True)
    parser.add_argument("--variant", default="", help="named execution variant used during preparation")
    parser.add_argument(
        "--platform-retry",
        type=int,
        default=0,
        help="audit a fresh execution issued after a non-scientific platform failure",
    )
    args = parser.parse_args()
    if args.platform_retry < 0:
        parser.error("--platform-retry must be non-negative")
    if args.variant and not args.variant.replace("-", "").isalnum():
        parser.error("--variant may contain only letters, digits, and hyphens")
    retry_suffix = f"-retry{args.platform_retry:02d}" if args.platform_retry else ""
    variant_suffix = f"-{args.variant}" if args.variant else ""
    label = f"{args.revision}-{args.mode}-a{args.index:02d}{retry_suffix}{variant_suffix}"
    attempt = args.run_dir / "validation" / label
    request = json.loads((attempt / "request.json").read_text(encoding="utf-8"))
    job = json.loads((attempt / "job-config.json").read_text(encoding="utf-8"))
    job_dir = Path(job["jobs_dir"]) / job["job_name"]
    agent_files = [path for path in job_dir.glob("*/agent/**/*") if path.is_file()]
    matches = []
    for path in agent_files:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        matches.extend(term for term in FORBIDDEN if term in text)
    if matches:
        raise SystemExit(f"forbidden leakage markers found: {sorted(set(matches))}")
    receipt = attempt / "researcher-receipt.json"
    leakage = attempt / "leakage-audit.json"
    write_json(leakage, {
        "schema_version": 1,
        "audit_status": "PASSED",
        "audited_request_id": request["request_id"],
        "forbidden_path_matches": 0,
        "reviewed_for": list(FORBIDDEN),
        "prompt_sha256": sha256(attempt / "researcher-prompt.md"),
        "job_config_sha256": sha256(attempt / "job-config.json"),
        "receipt_sha256": sha256(receipt),
        "reviewer": "teacher-evidence-audit",
    })
    handoff_dir = args.run_dir / "handoffs" / request["request_id"]
    snapshot = RunWorkflow(RunStore(args.run_dir)).audit_researcher_receipt(
        Actor.REVIEWER,
        request_path=handoff_dir / "request.json",
        capability_path=handoff_dir / "capability.json",
        receipt_path=receipt,
        leakage_evidence_path=leakage,
        idempotency_key=f"audit:{args.question_label}:{label}",
    )
    print(json.dumps(snapshot.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
