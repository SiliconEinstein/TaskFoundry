# ADR 0007：首次解题优先与运行时 Labwright

状态：2026-08-26 已实现并纳入主状态机。

## 问题与非目标

旧流程在题包产生前要求 Labwright 发布 Stable 镜像，Teacher 因而必须猜测
Researcher 将使用的依赖。Q22 在没有任何正式解题 trace 时预装 16 个 wheel，
镜像构建长期停在下载步骤，正式 Harbor 验证仍为零。

本决定不取消 Labwright，不允许把私有答案、grader 或凭证写入 Researcher 环境，
也不把环境启动和依赖配置失败计为科学尝试。

## 模式与范围

- Engineer Large Code：STRICT / L3，INCREMENTAL。
- 临时架构层：领域状态机、Researcher 执行、Labwright 运行时适配器、CLI/文档。
- 第一实现切片只改变首次解题的准入顺序；Harbor/LBG 仍负责沙盒生命周期。

## 被替代的旧流程

旧顺序是 `POLICIES_LOCKED → ENVIRONMENT_DISCOVERY → ENVIRONMENT_READY → AUTHORING`，
把 Stable 镜像误设为首次解题前置。schema-v1 旧 run 可以只以显式迁移事件进入新流程；
迁移会清除旧环境绑定，不能把它带入最终完成门。

## 决定

1. Teacher 在规范锁定后直接进入 AUTHORING，并冻结可运行的初版题包。
2. 独立 Reviewer 只做首次解题前必需的题型、明显泄漏、Oracle 与 grader 快速门；
   Stable 环境不属于这一门。
3. 首次 blind 使用固定 Paper2ARM 基础镜像立即启动 Harbor。
4. 环境、沙盒、harness、模型连接和平台失败保持同一 attempt，均不计科学尝试。
5. Researcher 遇到缺失公开能力时提交类型化失败 trace；Labwright 在独立 builder
   runtime 中重放该公开能力缺口、应用 delta 并返回可审计回执。修复后使用 fresh
   Researcher sandbox 重试同一科学 attempt；不要求、也不声称在线修改或恢复原沙盒。
6. 首次有效科学解题结束后，Labwright 根据真实 delta 生成镜像固化计划；没有
   实际 delta 时不得凭 Teacher 猜测构建依赖集合。
7. 最终完成仍要求 Stable 环境、完整 Reviewer、题包冻结、三轮 fresh blind 和
   按需提示验证。提前解题不降低最终验收标准。

## 公开接口与状态迁移

- `RunWorkflow.begin_authoring()` 从 `POLICIES_LOCKED` 进入 `AUTHORING`。
- 新增首次解题的快速健康门，允许 `environment_stable=false`，但其余健康轴必须
  通过。
- 首次 blind 可在没有 `environment_key` 时启动；后续 Stable 回执在已有科学
  trace 后绑定，完整健康证据由 Reviewer 另行提交。
- 快速健康门只能由 Reviewer 接受。运行时 Stable 回执只绑定环境身份，不会把
  快速健康门静默提升为最终健康。
- 首次科学 trace 和运行时 Stable 回执均已落盘后，Reviewer 可补交同时绑定当前
  题包摘要、题目修订、环境身份和证据字节的 `BoundHealthEvidence`；状态保持在
  当前 blind/hint 阶段。
- 三盲与提示验证满足科学门后先进入 `VALIDATION_PASSED`。只有当前 revision 的
  schema-v2 closure、Stable `EnvironmentReceipt`、完整 `BoundHealthEvidence` 和独立
  post-validation 都重新复验通过，才进入 `COMPLETED`。
- `ResearcherRequest` 继续绑定题包与 JobConfig；环境失败仍通过现有分类返回
  `RETRY_SAME_ATTEMPT`。

### 运行时证据闭合

首次 blind 的环境证据分成四段，不能再用一个预先生成的 Stable key 代替：

1. **baseline**：冻结题包、Researcher request、JobConfig 和实际启动的基础镜像
   `ArtifactIdentity`；基础镜像必须有不可变 digest。
2. **source failure trace**：绑定同一 run、question revision、package、source Researcher
   request/sandbox 和 baseline identity 的类型化非科学失败证据。
3. **runtime delta**：可选。每份 `DeltaReceipt` 同时绑定 source Researcher
   request/sandbox 与独立 Labwright builder request/sandbox/image，还必须绑定请求规范摘要、
   题包、baseline、增量前后 inventory、验证 probes 和 source failure trace。请求认领在
   同一 worker/fence 下幂等。恢复起止和耗时是可选的运行可观测字段，不进入科学 wall time，
   也不表示存在 live pause/resume。
4. **Stable closure**：`ImageSealPlan` 直接绑定 baseline artifact 和科学 trace，不再要求
   预先存在的 Stable `environment_key`。科学 trace 来自应用已验证 delta 后启动的 fresh
   Researcher sandbox，并列出实际使用的精确 delta request IDs。若 baseline 已足够，delta
   列表可以为空；若有 delta，closure 内嵌回执会保留 source 与 builder 的两套运行身份。

`ImageSealPlan` 是 Stable 构建输入，不是 Stable 回执。Labwright 完成构建和两个 clean
sandbox 复验后，仍使用 `EnvironmentReceipt` 和 `BoundHealthEvidence` 进入最终门。
旧 schema-v1 `DeltaReceipt` 与依赖 `base_environment_key` 的 seal plan 只保留为历史证据，
不得无证据升级成 schema v2 或作为 runtime-first Stable 构建输入。

## 安全、恢复与可观测性

- runtime delta 只允许公开 capability，不接受 shell、本地宿主路径或无哈希外部
  资源；Researcher 不能冒充 Labwright 完成回执。
- delta evidence 使用严格 JSON 解码，拒绝重复键、非有限数和不完整引用；inventory、
  probes 与原始 evidence 都按路径和 SHA-256 双重绑定。
- Stable 镜像只能从已完成的 delta receipt 生成，禁止从隐藏 reference 或
  Teacher 私有环境反推依赖。无 delta 时必须由有效科学 trace 证明 baseline 足够。
- 同一科学 attempt 表示计数和 progression 身份不变，不表示复用同一 Harbor job、request
  或 sandbox。失败的 source sandbox 不恢复；fresh retry 必须显式声明采用的 delta request
  IDs，closure 据此拒绝遗漏、额外或顺序漂移的回执。
- 旧 run 不原位重写历史事件。未发生科学尝试的 run 可追加迁移事件进入新主路径；
  已有尝试的 run 保留原协议。
- `ENVIRONMENT_DISCOVERY → AUTHORING` 兼容迁移写入
  `authoring.runtime_first.migrated`；只要已有 attempt 就拒绝迁移。
- CLI 的冻结题包、快速健康门和首次 blind 幂等身份均绑定 `question_revision`；同一
  修订重放不追加事件，新修订不会命中旧修订事件。
- 回滚只恢复旧状态转换代码，不删除题包、attempt、delta receipt、远端沙盒或镜像。

## 测试 seam 与验收

公开测试 seam 为 `RunWorkflow` 与 `researcher-run`：

1. 无 Stable environment 时可以 Authoring、冻结题包、通过快速门并开始首次 blind。
2. 环境失败返回同一 attempt 重试，不增加科学 blind 计数。
3. 没有科学 trace 时不能创建 Stable 构建计划；baseline 已足够时允许 zero-delta
   closure，不得为了满足 schema 人为制造增量。
4. 完成状态仍要求 Stable environment 和全部健康门。
5. Q22 使用基础镜像完成一次真实 Harbor blind，环境缺口只以权威 trace/receipt
   记录。

## 豁免与实现状态

没有豁免。文件适配器和 `RunWorkflow` 已闭合 baseline、source failure trace、独立 builder
delta、fresh scientific trace、Stable 构建计划、schema-v2 环境回执、最终健康和
post-validation。difficulty revision 会清除上一 revision 的环境、健康、closure、hint 与
package evidence，禁止跨修订借用。控制面不实现 live same-sandbox pause/resume；source
sandbox 终止后必须用已验证 delta 启动 fresh Researcher sandbox，并把实际 delta IDs 写入
科学 trace。环境缺失不能转成科学失败或另起题目。
