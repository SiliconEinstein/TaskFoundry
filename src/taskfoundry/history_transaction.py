"""为历史证据归档提供排他锁、路径边界和批量回滚。"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shutil
import tempfile


def assert_no_symlink_components(root: Path, path: Path) -> None:
    """拒绝根目录以下任何已有符号链接，防止读写逃逸。"""
    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise RuntimeError(f"路径逃出事务根目录: {path}") from error
    current = root
    if current.is_symlink():
        raise RuntimeError(f"事务根目录不能是符号链接: {current}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"事务路径不能包含符号链接: {current}")


def secure_mkdir_parents(root: Path, path: Path) -> None:
    """在事务根内逐级创建目录，且绝不先跟随符号链接。"""
    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise RuntimeError(f"路径逃出事务根目录: {path}") from error
    current = root
    if current.is_symlink() or not current.is_dir():
        raise RuntimeError(f"事务根目录必须是普通目录: {current}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"事务路径不能包含符号链接: {current}")
        if current.exists():
            if not current.is_dir():
                raise RuntimeError(f"事务父路径必须是目录: {current}")
            continue
        current.mkdir()


@contextmanager
def exclusive_history_lock(lock_path: Path) -> Iterator[None]:
    """用同一问题根下的文件锁串行化所有 history apply。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise RuntimeError(f"锁文件不能是符号链接: {lock_path}")
    with lock_path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def staging_directory(parent: Path) -> Iterator[Path]:
    """创建同文件系统 staging 目录，并在成功或失败后清理。"""
    staging = Path(tempfile.mkdtemp(prefix=".history-stage-", dir=parent))
    try:
        yield staging
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _remove_node(path: Path) -> None:
    """删除仅由本事务创建的节点，供失败回滚使用。"""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _rollback_committed(committed: Sequence[tuple[Path, Path | None]]) -> list[str]:
    """尽力恢复所有已提交目标，并收集不能恢复的备份位置。"""
    failures: list[str] = []
    for target, backup in reversed(committed):
        try:
            _remove_node(target)
            if backup is not None and backup.exists():
                os.replace(backup, target)
        except Exception as error:
            failures.append(f"{target}: {error}; backup={backup}")
    return failures


def commit_replacements(
    replacements: Sequence[tuple[Path, Path]], *, transaction_root: Path, backup_root: Path
) -> None:
    """提交整批同文件系统替换；任一失败时逆序恢复全部已提交目标。"""
    backup_root.mkdir(parents=True, exist_ok=False)
    committed: list[tuple[Path, Path | None]] = []
    preserve_backups = False
    try:
        for index, (staged, target) in enumerate(replacements):
            if not staged.exists() or staged.is_symlink():
                raise RuntimeError(f"staging 节点无效: {staged}")
            secure_mkdir_parents(transaction_root, target.parent)
            if target.is_symlink():
                raise RuntimeError(f"目标节点不能是符号链接: {target}")
            backup = backup_root / str(index) if target.exists() else None
            if backup is not None:
                os.replace(target, backup)
            committed.append((target, backup))
            os.replace(staged, target)
    except Exception as error:
        rollback_failures = _rollback_committed(committed)
        if rollback_failures:
            preserve_backups = True
            details = " | ".join(rollback_failures)
            raise RuntimeError(f"历史整理回滚不完整；备份保留在 {backup_root}: {details}") from error
        raise
    finally:
        if not preserve_backups:
            shutil.rmtree(backup_root, ignore_errors=True)
