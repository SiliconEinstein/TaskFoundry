"""为一份冻结题包收集可复现的 Oracle 和对抗证据。"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def load_checker(path: Path):
    """从冻结题包加载检查器，避免导入出题代码。"""
    sys.path.insert(0, str((path / "tests").resolve()))
    spec = importlib.util.spec_from_file_location("frozen_checker", path / "tests/checker.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load frozen checker")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: object) -> None:
    """创建父目录后写入确定性 JSON 证据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    """执行冻结 Oracle 和代表性对抗提交。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    parser.add_argument("public_data", type=Path)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    oracle = args.evidence / "oracle"
    outputs = oracle / "outputs"
    verifier = oracle / "verifier"
    outputs.mkdir(parents=True)
    environment = dict(os.environ, PUBLIC_DATA_DIR=str(args.public_data.resolve()), OUTPUT_DIR=str(outputs.resolve()))
    subprocess.run(["sh", str(args.package / "solution/solve.sh")], env=environment, check=True)
    subprocess.run(
        [
            "python3", str(args.package / "tests/run_verifier.py"), str(outputs), str(verifier),
            str(args.public_data), str(args.package / "tests"),
        ],
        check=True,
    )
    checker = load_checker(args.package)
    scores = {"empty": checker.verify(args.evidence / "empty", args.public_data, args.package / "tests")["reward"]}

    extra = args.evidence / "extra"
    shutil.copytree(outputs, extra)
    (extra / "extra.txt").write_text("extra\n", encoding="utf-8")
    scores["extra"] = checker.verify(extra, args.public_data, args.package / "tests")["reward"]

    nan = args.evidence / "nan"
    shutil.copytree(outputs, nan)
    prediction = nan / "route_predictions.csv"
    with prediction.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    rows[1][2] = "nan"
    with prediction.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    scores["nan"] = checker.verify(nan, args.public_data, args.package / "tests")["reward"]
    write_json(args.evidence / "adversarial/results.json", scores)
    print(json.dumps(scores, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
