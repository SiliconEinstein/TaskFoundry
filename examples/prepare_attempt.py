"""从冻结运行中准备一次受 capability 约束的正式 Researcher 尝试。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

from taskfoundry.harbor import CodexRuntime, HarborJobSpec, build_job_config, runtime_from_dict, write_job_config
from taskfoundry.model import Actor
from taskfoundry.package import package_sha256
from taskfoundry.researcher import CapabilityStore, ResearcherRequest, write_json
from taskfoundry.store import RunStore


def file_sha256(path: Path) -> str:
    """返回一个不可变输入文件的小写 SHA-256。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    """签发一次性 Researcher capability 并生成沙盒任务提示。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("revision")
    parser.add_argument("mode", choices=("blind", "hint"))
    parser.add_argument("index", type=int)
    parser.add_argument("--question-label", required=True)
    parser.add_argument("--researcher-thread-id", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--lbg-project-id", type=int, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--context", type=Path, action="append", default=[])
    parser.add_argument("--harness", choices=("dsh", "codex"), default="codex")
    parser.add_argument("--variant", default="", help="全新执行的可选名称，例如 codex01")
    parser.add_argument(
        "--platform-retry",
        type=int,
        default=0,
        help="非科学平台重试使用的全新执行身份",
    )
    args = parser.parse_args()
    if args.platform_retry < 0:
        parser.error("--platform-retry must be non-negative")
    if args.variant and not args.variant.replace("-", "").isalnum():
        parser.error("--variant may contain only letters, digits, and hyphens")
    snapshot = RunStore(args.run_dir).read_snapshot()
    if snapshot is None:
        raise SystemExit("run is not initialized")

    retry_suffix = f"-retry{args.platform_retry:02d}" if args.platform_retry else ""
    variant_suffix = f"-{args.variant}" if args.variant else ""
    identity_suffix = retry_suffix + variant_suffix
    label = f"{args.revision}-{args.mode}-a{args.index:02d}{identity_suffix}"
    attempt_dir = args.run_dir / "validation" / label
    attempt_dir.mkdir(parents=True, exist_ok=False)
    package = args.run_dir / "question-revisions" / args.revision / "task"
    runtime = runtime_from_dict(json.loads(args.runtime.read_text(encoding="utf-8")))
    if args.harness == "codex" and not isinstance(runtime, CodexRuntime):
        raise SystemExit("--harness codex requires a Codex runtime descriptor")
    if args.harness == "dsh" and isinstance(runtime, CodexRuntime):
        raise SystemExit("--harness dsh requires a DSH runtime descriptor")
    jobs_dir = args.run_dir / "harbor-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    spec = HarborJobSpec(
        job_name=(
            f"{args.question_label}_{args.revision}_{args.mode}_a{args.index:02d}"
            f"{identity_suffix.replace('-', '_')}"
        ),
        jobs_dir=str(jobs_dir.resolve()),
        task_path=str(package.resolve()),
        context_paths=tuple(str(path.resolve()) for path in args.context),
        lbg_project_id=args.lbg_project_id,
        model_name=runtime.model_name if isinstance(runtime, CodexRuntime) else "deepseek/deepseek-v4-pro",
    )
    write_json(attempt_dir / "spec.json", asdict(spec))
    config_path = attempt_dir / "job-config.json"
    write_harbor = build_job_config(spec, runtime)
    write_job_config(config_path, write_harbor)

    request_id = (
        f"{args.question_label}-{args.revision}-{args.mode}-a{args.index:02d}"
        f"{identity_suffix}-v4pro"
    )
    request = ResearcherRequest(
        request_id=request_id,
        run_id=snapshot.run_id,
        question_revision=args.revision,
        attempt_index=args.index,
        mode=args.mode,
        package_path=str(package.resolve()),
        package_sha256=package_sha256(package),
        job_config_path=str(config_path.resolve()),
        job_config_sha256=file_sha256(config_path),
        context_digests=tuple(file_sha256(path) for path in args.context),
        researcher_thread_id=args.researcher_thread_id,
        harness=args.harness,
        model=runtime.model_name if isinstance(runtime, CodexRuntime) else "deepseek-v4-pro",
    )
    write_json(attempt_dir / "request.json", request.to_dict())
    handoff = CapabilityStore(args.run_dir / "handoffs").issue(Actor.TEACHER, request)
    handoff_path = Path(handoff.request_path).parent / "handoff.json"
    write_json(handoff_path, asdict(handoff))

    prompt = f"""[TASKFOUNDRY ROLE=researcher]

执行 {args.question_label} {args.revision} 的全新 {args.mode}-a{args.index:02d}{identity_suffix}。不得读取其他尝试的轨迹、
输出、分数或总结，不读取 solution/tests，blind 模式不得使用 hint。只执行一次。

```bash
CODEX_THREAD_ID={args.researcher_thread_id} PYTHONPATH=src {sys.executable} -m taskfoundry.cli researcher-run \\
  {handoff_path} \\
  {args.runtime} --env-file {args.env_file} \\
  --queue-root /personal/TaskFoundry/harbor-queue \\
  --receipt {attempt_dir / 'researcher-receipt.json'}
```

保持 {args.harness} + {request.model} 与 3600 秒 Agent 科学超时。完成后只汇报 receipt 字段。
"""
    (attempt_dir / "researcher-prompt.md").write_text(prompt, encoding="utf-8")
    print(json.dumps({"attempt_dir": str(attempt_dir), "request_id": request_id}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
