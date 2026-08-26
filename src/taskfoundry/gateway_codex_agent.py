"""支持带命名空间网关模型标识的 Harbor Codex 适配器。"""

from __future__ import annotations

from typing import override

from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


class _GatewayModelName(str):
    """在 Harbor 旧版 basename 分割中保留完整网关服务标识。"""

    @override
    def split(self, sep: str | None = None, maxsplit: int = -1) -> list[str]:
        """Harbor 按斜杠取 basename 时保留完整网关模型名。"""
        if sep == "/" and maxsplit == -1:
            return [str(self)]
        return super().split(sep, maxsplit)


class GatewayCodex(Codex):
    """保留带命名空间模型服务标识并运行内置 Codex。"""

    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        """仅在父类执行期间包装模型名，并在任何退出路径恢复原值。"""
        original = self.model_name
        self.model_name = _GatewayModelName(original)
        try:
            await super().run(instruction, environment, context)
        finally:
            self.model_name = original
