# ADR 0007：runtime-first 首次解题（已被 ADR 0008 取代）

- 状态：Superseded
- 取代者：[ADR 0008](0008-direct-linear-harbor-validation.md)

ADR 0007 曾移除“Stable 环境必须先于首次 Harbor”的门，但仍保留 Formal Reviewer、健康证据和 post-validation 等中间流程。用户随后明确要求：题包完成最小启动检查后立即进入真实解题；质量、难度、泄漏、资源和环境需求都由线性解题 trace 验证。

因此 ADR 0007 不再是可执行规范。当前唯一有效流程、状态迁移和完成条件均以 ADR 0008 及其对应测试为准；代码不得继续提供 ADR 0007 的旁路入口。
