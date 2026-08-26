# ADR 0001：TaskFoundry V1 编排模型

状态：2026-08-21 接受实现；正式运行配置已由 ADR 0003 更新，镜像内置 harness
的旧条款已由 ADR 0005 取代。

## 问题

TaskFoundry 需要让科学题目出题过程可复现，同时保留强 Agent 的自主工作能力。
它协调 Codex Teacher、独立 Researcher、可调用 Labwright、Harbor 和版本化规范。
V1 不替代 Harbor，不调用普通 LLM API，也不允许 Teacher 直接执行 Researcher
拥有的 Harbor 任务。

## 分层与范围

- 工程模式：STRICT / L3，INCREMENTAL。
- 临时层次：领域契约、应用编排、适配器、命令行。
- 单个实现切片不超过 300 行有效生产代码、6 个生产文件和2个架构层。

## 流程

1. 题型模块生成版本化 `QuestionDesignBrief`，与 Harbor 和最终题包解耦。
2. 规范解析器锁定固定题包规范和题型规范。
3. Teacher 依据只追加运行日志工作。Agent 输出只是候选证据，校验通过后才能推进。
4. Labwright 解析或构建科学环境。最终环境必须形成 Candidate、完成干净验证并
   发布 Stable 身份。
5. Teacher 冻结 Paper2Task 题包，先执行 Oracle、诚实解、对抗和隔离检查。
6. Teacher 签发一次性 Researcher 请求；只有 Researcher 可以兑换并启动 Harbor。
7. 三轮全新盲解必须低于通过阈值，之后才能加入经过审核的提示。盲解通过意味着
   题目过易；带提示通过且健康门完整后，题目完成。

正式 harness、模型和时间预算由 ADR 0003 规定。

## 公开契约

- `QuestionDesignBrief`。
- `RunSnapshot` 和只追加 `RunEvent`。
- `EnvironmentDeltaRequest`、`EnvironmentReceipt` 和稳定镜像身份。
- `ResearcherRequest`、原始 `ResearcherReceipt` 和一次性 capability。
- Paper2Task 题包检查结果和内容摘要。

版本1内只允许兼容性新增字段。破坏性变化必须提升 schema 版本并提供迁移。

## 角色与安全

- Teacher 写设计、规范、题包修订、提示和审计。
- Labwright 只写环境状态与镜像证据，不读取隐藏测试、答案、提示和 Researcher 输出。
- Researcher 拥有 Harbor 执行权，不能修改冻结题包、隐藏评价器和 Stable 镜像。
- capability 一次有效，并绑定题包摘要、执行任务和 Researcher 身份。
- 配置只保存凭证变量名，日志不得持久化凭证值。

## 错误和恢复

事件先持久化并同步，再原子替换快照。命令使用幂等键；恢复时重放日志并拒绝序列
缺口。环境、镜像、沙盒、harness、模型连接、科学执行、Verifier 和平台失败分别
记录。只有权威科学结果进入难度统计。

## Labwright 生命周期

运行时沙盒和最终镜像是两个逻辑对象。普通 CPU 题从摘要固定的 Paper2ARM 镜像
开始；Labwright 只添加题目依赖和公开资源。最终镜像必须使用
不可变标识，经过至少两个干净沙盒验证，且不含凭证、隐藏答案和 Researcher 痕迹。

Researcher 解题时需要额外公开能力，可以通过 Labwright 运行时增量模块配置同一
沙盒。配置时间暂停科学计时；解题结束后将验证过的增量固化到最终镜像。

## 验证与回滚

单元测试覆盖状态转换、CAS、capability 防重放、时间预算、题包可见性、规范修订和
分数门。集成测试覆盖从设计大纲到 Researcher 请求的主要路径。

代码回滚不删除运行证据、Harbor 任务或外部镜像。错误运行由新运行或新题目修订
取代，不允许破坏性回滚证据。
