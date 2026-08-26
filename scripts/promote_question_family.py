#!/usr/bin/env python3
"""校验并原子发布一个已完成题族。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Iterator

from question_family_validation import (
    FamilyValidationError,
    QUESTION_ROOT,
    file_sha256,
    validate_family,
)


PROMOTION_EVIDENCE_ROOT = Path("/personal/TaskFoundry/runs/publication-promotions")


@dataclass(frozen=True)
class TreeSnapshot:
    """稳定的源根身份，以及内容和权限模式摘要。"""

    root_device: int
    root_inode: int
    digest: str


def snapshot_tree(root: Path) -> TreeSnapshot:
    """不跟随链接，也不接受特殊节点，计算一棵常规树的摘要。"""
    root_stat = root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode):
        raise FamilyValidationError(f"题族不是常规目录：{root}")
    digest = hashlib.sha256()
    paths = [root, *sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())]
    for path in paths:
        path_stat = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        if stat.S_ISDIR(path_stat.st_mode):
            kind = b"directory"
            content_digest = b""
        elif stat.S_ISREG(path_stat.st_mode):
            kind = b"file"
            content_digest = file_sha256(path).encode()
        else:
            raise FamilyValidationError(f"发布树包含软链接或特殊节点：{path}")
        for item in (
            relative.encode(),
            kind,
            f"{stat.S_IMODE(path_stat.st_mode):04o}".encode(),
            str(path_stat.st_size if kind == b"file" else 0).encode(),
            content_digest,
        ):
            digest.update(len(item).to_bytes(8, "big"))
            digest.update(item)
    return TreeSnapshot(
        root_device=root_stat.st_dev,
        root_inode=root_stat.st_ino,
        digest=digest.hexdigest(),
    )


def copy_family(source: Path, destination: Path) -> None:
    """复制候选题族，复制期间遇到的链接一律不跟随。"""
    shutil.copytree(
        source,
        destination,
        copy_function=shutil.copy2,
        symlinks=True,
    )


@contextmanager
def promotion_lock(lock_path: Path) -> Iterator[None]:
    """使用 fail-closed 进程锁串行化同一题号的发布者。"""
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    locked = False
    try:
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise FamilyValidationError(f"晋级锁不是单链接常规文件：{lock_path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    """在发布题族之外原子写入晋级账本。"""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def parse_args() -> argparse.Namespace:
    """解析题号、暂存题族路径和可选执行标志。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("question", type=int, choices=range(1, 33))
    parser.add_argument("family", type=Path)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def require_empty_publication(publication: Path) -> None:
    """在创建隐藏 staging 前拒绝发布目录中的任何可见或隐藏条目。"""
    existing = list(publication.iterdir())
    if existing:
        raise FamilyValidationError(f"发布目录非空：{existing}")


def validate_hidden_stage(
    *,
    question: int,
    source: Path,
    staged_family: Path,
    manifest: dict[str, object],
    source_snapshot: TreeSnapshot,
) -> None:
    """发布前重新校验源身份和已复制候选。"""
    if snapshot_tree(source) != source_snapshot:
        raise FamilyValidationError("源题族在晋级期间发生变化")
    if snapshot_tree(staged_family).digest != source_snapshot.digest:
        raise FamilyValidationError("隐藏 staging 与源题族摘要不一致")
    staged_manifest = validate_family(question, staged_family, staging_required=False)
    final_source_manifest = validate_family(question, source)
    if staged_manifest != manifest or final_source_manifest != manifest:
        raise FamilyValidationError("源题族或隐藏 staging 的 manifest 发生变化")
    if snapshot_tree(source) != source_snapshot:
        raise FamilyValidationError("源题族在最终校验期间发生变化")
    if snapshot_tree(staged_family).digest != source_snapshot.digest:
        raise FamilyValidationError("隐藏 staging 在最终校验期间发生变化")


def publish_locked(
    *,
    question: int,
    source: Path,
    publication: Path,
    target: Path,
    manifest: dict[str, object],
    source_snapshot: TreeSnapshot,
) -> None:
    """持锁复制、完整校验，并最终公开一个题族。"""
    require_empty_publication(publication)
    if validate_family(question, source) != manifest or snapshot_tree(source) != source_snapshot:
        raise FamilyValidationError("源题族在晋级前发生变化")
    evidence_root = PROMOTION_EVIDENCE_ROOT / f"q{question:02d}"
    evidence_root.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    pre: dict[str, object] = {
        "question": f"Q{question:02d}",
        "source": str(source),
        "target": str(target),
        "family_manifest_sha256": file_sha256(source / "FAMILY_MANIFEST.json"),
        "source_tree_sha256": source_snapshot.digest,
        "operation": "copy-validate-same-filesystem-atomic-rename",
    }
    atomic_json(evidence_root / f"{timestamp}-pre-promotion.json", pre)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".promotion-{manifest['family_slug']}-", dir=publication)
    )
    staged_family = staging_root / str(manifest["family_slug"])
    try:
        if staging_root.stat().st_dev != publication.stat().st_dev:
            raise FamilyValidationError("隐藏 staging 与发布目录不在同一文件系统")
        copy_family(source, staged_family)
        validate_hidden_stage(
            question=question,
            source=source,
            staged_family=staged_family,
            manifest=manifest,
            source_snapshot=source_snapshot,
        )
        if set(publication.iterdir()) != {staging_root} or target.exists():
            raise FamilyValidationError("发布目录在晋级期间被并发修改")
        os.rename(staged_family, target)
        staging_root.rmdir()
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root)
    final = pre | {
        "status": "PUBLISHED",
        "post_rename_family_manifest_sha256": file_sha256(target / "FAMILY_MANIFEST.json"),
        "published_tree_sha256": source_snapshot.digest,
    }
    atomic_json(evidence_root / f"{timestamp}-promotion-complete.json", final)


def main() -> int:
    """默认只校验；apply 时复制验证后原子公开完整题族。"""
    args = parse_args()
    manifest = validate_family(args.question, args.family)
    source_snapshot = snapshot_tree(args.family)
    publication = QUESTION_ROOT / str(args.question) / "new-question"
    target = publication / manifest["family_slug"]
    print(json.dumps({"validated": True, "target": str(target)}, indent=2))
    if not args.apply:
        require_empty_publication(publication)
        return 0
    lock_path = publication.parent / ".new-question-promotion.lock"
    with promotion_lock(lock_path):
        publish_locked(
            question=args.question,
            source=args.family,
            publication=publication,
            target=target,
            manifest=manifest,
            source_snapshot=source_snapshot,
        )
    print(target)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, FamilyValidationError) as error:
        print(f"拒绝晋级：{error}", file=sys.stderr)
        raise SystemExit(1)
