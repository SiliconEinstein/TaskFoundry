# 可信线性验证修复 v1

## 问题与非目标

当前直接验证流程把 Harbor 证据导入与运行状态推进混在同一个方法中，调用者可以
提供任意 request、capability 和 receipt 路径，导致科学结果、沙箱身份和提示上下文
不能由权威运行产物独立证明。Skill activation 也只校验内嵌内容的自带哈希，旧
schema 会阻断已有题号重新 resolve。

本次修复不恢复首次 Harbor 前的 Formal Reviewer、Labwright、环境稳定、捷径或
科学质量门；不改写既有运行证据；不删除 `runs/`；不修改题目的科学内容。

## 模式、范围与规则

- Engineer Large Code：STRICT / L3，INCREMENTAL。
- 临时架构层：领域契约、证据 Adapter、工作流事务、SkillFoundry Adapter、CLI。
- 无 waiver。
- 每个实现切片不超过 300 个有效生产代码变更行、6 个生产文件和 2 个架构层。

## 当前流程

`record_validation_round()` 同时解析调用者给出的三个 JSON、判断它们是否可信、
构造尝试证据并推进状态。沙箱身份是本地合成哈希；hint 可携带任意额外上下文；
成功的 hint 直接把运行推进到 `COMPLETED`，后续无法绑定真实运行环境。

## 目标流程

```text
AUTHORING
  -> freeze_and_start_validation_session
  -> HarborEvidenceImporter.import_round
  -> BLIND_VALIDATION
  -> TOO_EASY -> 新 revision / 新 thread / 新 session
  或
  -> HINT_VALIDATION
  -> VALIDATION_PASSED
  -> RUNTIME_FINALIZATION
  -> COMPLETED
```

首次 Harbor 前只执行最小 launch probe。科学质量、难度、泄漏、资源充分性和环境
需求继续从真实解题 trace 中得出。

## Module、Interface 与 Seam

### HarborEvidenceImporter

Interface：`HarborEvidenceImporter(handoff_root).import_round(request_id) -> VerifiedRoundEvidence`。

Implementation 从固定 handoff 目录和 JobConfig 定位唯一 Harbor job/trial，严格解析
JSON，重新计算 classification/reward，并读取真实 provider agent/verifier sandbox ID、
trial ID 和 harness session ID。调用者不能提供 reward、classification 或身份字段。

### ValidationSessionRegistry

Interface：`reserve(...)` 与 `close(...)`。Implementation 在文件锁内维护追加式 registry，
拒绝重复 revision、validation session 和 Researcher thread，并绑定不可变题包快照。

### LaunchChecker

Interface：`check_launchability(package) -> LaunchReport`。Implementation 只检查必需普通
文件、配置解析与 `tests/test.sh --probe` 在 10 秒内成功；发布 lint 保持独立。

### SkillActivationStore

现有 `resolve_teacher_activation()` 作为外部 Interface。Implementation 用 `batch_id`、
`input_set_sha256` 和精确 stable generation 判断是否复用，将 schema 1 或旧批次资料按
原哈希归档，再原子提交当前 activation 与 prompt。

## 公开契约变更

- `classification` 改为严格枚举，未知值失败。
- 新增 `VerifiedRoundEvidence`，Workflow 不再接收自由 receipt 字段。
- schema-v2 hint context 必须是全部历史科学 receipt 加恰好一份 Teacher 声明的
  `ApprovedHint`。
- hint 通过进入 `VALIDATION_PASSED`，环境固化后才进入 `COMPLETED`。
- Teacher 并发上限为 1–5，当前部署值为 1。
- Skill resolve 增加 `batch_id` 和输入集合身份。

## 兼容与迁移

- 已完成历史运行保持原样并标注其验证模型。
- 未完成旧 direct session 关闭为 `CLOSED_MIGRATED`，使用新 revision、thread 和 session
  重开。
- schema 1 activation 只作为历史证据归档，不能直接当作 schema 2 使用。
- 当前题包目录先复制为只含普通文件的 revision snapshot，再参与 Harbor。

## 事务、并发与安全

- CapabilityStore 的 request、redemption 和 canonical result 必须在同一 request 目录闭合。
- 证据导入在状态事务前完成；状态事务只接收不可变领域对象。
- registry 和 run journal 分别使用文件锁；先 reserve registry，再提交 run event，失败时
  写入可重放 reservation，重复调用保持幂等。
- 本轮不抵抗拥有 `/personal` 写权限的恶意进程，也不要求平台签名；保证应用层不信任
  Teacher 手写 reward/receipt。
- 不记录 token、credential、私有 GT 或 grader 内容。

## 错误分类与恢复

- 原始 Harbor 产物不完整、身份缺失、JSON 非法：`EVIDENCE_INCOMPLETE`，不计科学结果。
- 环境、Harness、平台失败：同一科学 round 使用 fresh sandbox 重试。
- 未知 classification、重复身份、伪造上下文：契约失败并停止自动推进。
- 科学字节变化：关闭旧 session，开始新 revision。
- 环境固化失败：停在 `RUNTIME_FINALIZATION`，不重跑已经通过的科学验证。

## 调用者与依赖影响

- TaskFoundry CLI、`RunWorkflow`、`CapabilityStore`、Harbor/LBG Adapter、调度器、
  SkillFoundry resolve 调用和对应测试均需更新。
- `/personal/harbor-lbg` 需要把真实远端 sandbox identity 写入正式 trial result；若当前
  结果已有该字段，只新增读取 Adapter，不改外部仓库。

## 实现切片

1. 严格结果枚举、canonical evidence importer 与负向测试。
2. session registry、不可变题包 snapshot、revision/thread/session 唯一性。
3. ApprovedHint、精确上下文链和线性状态迁移。
4. launch probe 与 runtime finalization。
5. Skill activation 批次迁移、源字节绑定和 reconcile 去重。
6. 1–5 路调度、中文契约、完整回归和独立双轴复审。

## 测试、覆盖率与集成验证

- 每个切片按一个公开 Seam 做 red→green。
- 负向测试覆盖伪造 capability/receipt、结果漂移、未知分类、缺失/复用真实 sandbox、
  重复 revision/thread/session、上下文乱序、多 hint、旧 activation、源字节漂移和重复
  reconcile。
- 集成路径覆盖 AUTHORING 到三轮 blind、hint、VALIDATION_PASSED、环境固化和 COMPLETED。
- 变更行 statement coverage 至少 90%，branch coverage 至少 80%，总覆盖率下降不超过
  0.5 个百分点。

## 可观测性与敏感数据

事件只记录 digest、公开身份、状态决策和失败分类。完整 trace 保留在题号目录规定的
`trace/` 下；事件日志不复制私有结果字节。

## 回滚与数据恢复

代码可按切片回滚。新 schema 和 registry 只追加，不覆盖旧 events、runs 或历史
activation。若 Adapter 无法从现有 Harbor 产物取得真实身份，只回滚该 Adapter 切片，
不得恢复合成身份。

## 已确认决策

- 不要求平台签名或抵抗拥有 `/personal` 写权限的恶意进程。
- 后续轮次使用同一外层 Researcher 会话，并携带完整 Agent 可见历史记录包。
- Teacher 自行声明 hint 非答案；系统验证身份、顺序、数量和字节，不做自然语言判断。
- 未完成旧 session 关闭后按新契约重开。
- launch contract 使用 `tests/test.sh --probe`。
- 验证成功后必须固化真实环境才进入 `COMPLETED`。
- 并发上限为 1–5，当前设置为 1。

## 阻断实施的开放决策

无。
