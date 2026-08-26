# ADR 0005：Harness-neutral 环境合同

状态：2026-08-25 已确认，进入实现。

## 问题

TaskFoundry 旧实现要求题目镜像内同时存在 Codex 和 Claude，并要求
`EnvironmentSpec` 提供根 schema 不允许的 `base_image_digest` 与
`required_harnesses` 字段。当前权威协议要求相反：题目镜像只包含科学环境，
DSH、Codex、Claude 等 harness 必须由 Harbor 在运行层临时注入。

这导致符合根协议的 Q15 环境无法导入 TaskFoundry，进而阻断所有新题进入
Labwright→Harbor 验证。

## 决策

1. `EnvironmentSpec` 只消费根 schema 已定义的 `base_image` 与 `platform`。
   镜像的不可变身份由发布后的 manifest、lock 和 provider record 绑定，不在 spec
   中私造字段。
2. 基础镜像必须是非空、非 `latest` 的 `linux/amd64` 镜像。专用基础镜像直接通过
   schema 内的 `base_image` 表达，不再使用 schema 外的 override 对象。
3. Stable 导入要求至少两个不同 clean sandbox。每个 sandbox 都必须包含并通过五项
   负向扫描：应用目录 allowlist、特权题目材料、harness 可执行文件、凭证材料和
   Researcher 残留。
4. 旧的 `harness-codex`、`harness-claude` 正向检查不能替代负向扫描。旧镜像和旧证据
   保持可读，但必须重新构建/复验后才能按新合同登记 Stable。
5. Harness bootstrap 继续属于 Harbor 运行层，不进入 EnvironmentReceipt，也不得回写
   或 snapshot 到题目镜像。

## 范围与风险

- 工程模式：STRICT / L3，增量实施。
- 修改环境策略、Stable 导入门和相关测试；不修改外部镜像、已有运行记录或 Harbor
  provider。
- 这是破坏性的合同修正：依赖旧正向 harness 证据的调用方会失败关闭。回滚代码不会
  删除任何证据，但会重新引入根协议冲突，因此不作为正式恢复路径。

## 验收

- Q15 的 harness-neutral clean evidence 能被 TaskFoundry 导入并解析。
- 缺少任一必需负向扫描、重复 sandbox、失败 probe、证据指向不同镜像时均拒绝。
- 根 schema 合法的 spec 能通过基础策略；`latest`、空镜像和非 amd64 平台被拒绝。
- 相关单元测试、CLI 集成测试、全仓测试和结构检查通过。

## 实施验证记录

- 环境、CLI 和边界测试 38 项通过；全仓 176 项通过。
- 目标模块 statement/branch 综合覆盖率 90%。
- large-code 结构检查 `errors=0`。`cli.parser` 的 102 行为既有参数声明聚合，未被
  本切片修改，人工审查为 `PASS`，不作无关拆分。
- Q15 record `146536` 的真实双 clean-sandbox 证据通过根 schema bindings，并成功
  导入临时 TaskFoundry registry；旧正向 harness 证据和非零禁用材料计数均有拒绝测试。
