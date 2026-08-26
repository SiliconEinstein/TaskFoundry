"""安装宿主机提供且摘要固定的 Codex 运行层覆盖包。"""

from __future__ import annotations

import hashlib
from pathlib import Path
import shlex
from typing import override

from harbor.environments.base import BaseEnvironment

from .gateway_codex_agent import GatewayCodex


class PortableCodex(GatewayCodex):
    """上传 Codex 完整运行包，避免题目镜像内置 harness。"""

    _REMOTE_CODEX = "/installed-agent/codex/codex"
    _REMOTE_CODE_MODE_HOST = "/installed-agent/codex/codex-code-mode-host"

    def __init__(
        self,
        logs_dir: Path,
        portable_binary_path: Path | str,
        portable_binary_sha256: str,
        portable_binary_version: str,
        portable_code_mode_host_path: Path | str,
        portable_code_mode_host_sha256: str,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(logs_dir, *args, **kwargs)
        self._portable_binary_path = Path(portable_binary_path)
        self._portable_binary_sha256 = portable_binary_sha256.lower()
        self._portable_binary_version = portable_binary_version
        self._portable_code_mode_host_path = Path(portable_code_mode_host_path)
        self._portable_code_mode_host_sha256 = portable_code_mode_host_sha256.lower()

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        """宿主和 sandbox 各校验一次，再启用完整 Codex 运行包。"""
        artifacts = (
            ("Codex binary", self._portable_binary_path, self._portable_binary_sha256),
            ("Codex code-mode host", self._portable_code_mode_host_path, self._portable_code_mode_host_sha256),
        )
        for label, path, expected in artifacts:
            if not path.is_file():
                raise FileNotFoundError(f"{label} not found: {path}")
            if _sha256_file(path) != expected:
                raise RuntimeError(f"{label} SHA-256 mismatch")

        await environment.upload_file(self._portable_binary_path, self._REMOTE_CODEX)
        await environment.upload_file(self._portable_code_mode_host_path, self._REMOTE_CODE_MODE_HOST)
        remote = shlex.quote(self._REMOTE_CODEX)
        remote_host = shlex.quote(self._REMOTE_CODE_MODE_HOST)
        checksum = shlex.quote(self._portable_binary_sha256)
        host_checksum = shlex.quote(self._portable_code_mode_host_sha256)
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; mkdir -p /installed-agent/codex; "
                f"printf '%s  %s\\n' {checksum} {remote} | sha256sum -c -; "
                f"printf '%s  %s\\n' {host_checksum} {remote_host} | sha256sum -c -; "
                f"chmod 0755 {remote} {remote_host}; ln -sfn {remote} /usr/local/bin/codex"
            ),
        )
        result = await self.exec_as_agent(environment, command="codex --version")
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        actual_version = lines[-1].removeprefix("codex-cli").strip() if lines else ""
        if actual_version != self._portable_binary_version:
            raise RuntimeError(
                f"Codex version mismatch: expected {self._portable_binary_version}, got {actual_version}"
            )


def _sha256_file(path: Path) -> str:
    """以流式方式计算 portable 运行文件摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
