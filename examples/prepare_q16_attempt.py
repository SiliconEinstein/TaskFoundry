"""从冻结运行状态准备并签发一次 Q16 Researcher 尝试。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from taskfoundry.harbor import DshRuntime, HarborJobSpec, build_job_config, write_job_config
from taskfoundry.model import Actor
from taskfoundry.package import package_sha256
from taskfoundry.researcher import CapabilityStore, ResearcherRequest, write_json


BREAKER = "01a019f0-368e-7a71-8493-0b61ca7755be"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("revision")
    parser.add_argument("mode", choices=("blind", "hint"))
    parser.add_argument("index", type=int)
    parser.add_argument("--context", type=Path, action="append", default=[])
    args = parser.parse_args()

    label = f"{args.revision}-{args.mode}-a{args.index:02d}"
    attempt_dir = args.run_dir / "validation" / label
    attempt_dir.mkdir(parents=True, exist_ok=False)
    package = args.run_dir / "question-revisions" / args.revision / "task"
    jobs = args.run_dir / "harbor-jobs"
    runtime_path = args.run_dir / "runtime/dsh-v4-pro.json"
    runtime = DshRuntime(**json.loads(runtime_path.read_text(encoding="utf-8")))
    spec = HarborJobSpec(
        job_name=f"q16_verified_{args.revision}_{args.mode}_a{args.index:02d}",
        jobs_dir=str(jobs.resolve()),
        task_path=str(package.resolve()),
        context_paths=tuple(str(path.resolve()) for path in args.context),
        lbg_project_id=2309771,
    )
    write_json(attempt_dir / "spec.json", asdict(spec))
    config = build_job_config(spec, runtime)
    config_path = attempt_dir / "job-config.json"
    write_job_config(config_path, config)

    request_id = f"q16-{args.revision}-{args.mode}-a{args.index:02d}-v4pro"
    request = ResearcherRequest(
        request_id=request_id,
        run_id="q16-first-run",
        question_revision=args.revision,
        attempt_index=args.index,
        mode=args.mode,
        package_path=str(package.resolve()),
        package_sha256=package_sha256(package),
        job_config_path=str(config_path.resolve()),
        job_config_sha256=file_sha256(config_path),
        context_digests=tuple(file_sha256(path) for path in args.context),
        researcher_thread_id=BREAKER,
    )
    write_json(attempt_dir / "request.json", request.to_dict())
    handoff = CapabilityStore(args.run_dir / "handoffs").issue(Actor.TEACHER, request)
    write_json(Path(handoff.request_path).parent / "handoff.json", asdict(handoff))

    prompt = f"""[TASKFOUNDRY ROLE=researcher]

执行 Q16 {args.revision} 的全新 {args.mode}-a{args.index:02d}。不得读取其他尝试的轨迹、
输出、分数或总结，不读取 solution/tests，blind 模式不得使用 hint。只执行一次。

```bash
CODEX_THREAD_ID={BREAKER} PYTHONPATH=src python3 -m taskfoundry.cli researcher-run \\
  {Path(handoff.request_path).parent / 'handoff.json'} \\
  {runtime_path} --env-file /personal/flowforge/.env \\
  --queue-root /personal/TaskFoundry/harbor-queue \\
  --receipt {attempt_dir / 'researcher-receipt.json'}
```

保持 DSH + deepseek-v4-pro 与 3600 秒 Agent 科学超时。完成后只汇报 receipt 字段。
"""
    (attempt_dir / "researcher-prompt.md").write_text(prompt, encoding="utf-8")
    print(json.dumps({"attempt_dir": str(attempt_dir), "request_id": request_id}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
