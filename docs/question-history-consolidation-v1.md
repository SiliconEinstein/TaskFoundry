# 题目历史整理 v1

## 问题与非目标

Q3–Q32 在旧目录中积累了 Brief、候选证据、Reviewer 报告、Harbor attempt、
平台失败和完成声明。整理任务要让这些工作可检索、可复用，同时不能把旧状态
自动当成新的 Skill Bank 发布结论。本操作不重新评分、不晋级旧题包，也不把
Q1、Q2 纳入本轮整理。Q1、Q2 已由 owner 确认完成，但其目录在更早的清理批次
中确实发生过迁移；总账必须保留这一事实，不能写成“从未触碰”。

## 模式与范围

采用 `STRICT / INCREMENTAL`。改动新增持久化历史索引、trace 选择规则和跨目录
事务。无 waiver，也没有仓库规则覆盖。

## 现状与目标流程

旧流程需要分别检查编号题目录、TaskFoundry run 和中央 archive。新流程先对
全部输入做只读盘点，再按 allowlist 选择出题、审查、Harbor 与生命周期证据，
整批写入 staging，最后在排他锁下提交。逐题与全局索引同时更新。

## 文件、层次与公开契约

- `taskfoundry.history`：选择、哈希、状态归一、中央 archive 绑定和索引渲染。
- `taskfoundry.history_transaction`：符号链接边界、排他锁、staging 和失败回滚。
- `scripts/consolidate_question_history.py`：操作入口。
- `HISTORY_INDEX.json`、`AUTHORING_PROGRESS.json`：带 schema version 的持久证据。

临时层次模型为：`run/archive 证据 → 整理服务 → 编号题 trace → 人工/全局视图`。

## 兼容与迁移

原始 run 和中央 archive 保持不动。已有 `trace/authoring/pre-skillbank` 不修改。
相同输入重复执行必须得到相同索引字节；旧索引引用的源文件一旦漂移就 fail closed。
新 run 可以在下一次整理中追加。Q1、Q2 明确排除于 Q3–Q32 操作范围。

## 事务、安全与恢复

源 run、源文件、目标路径及其父目录均拒绝符号链接。题包、solution、tests、
private reference、hidden oracle、capability、credential、secret 和 token 路径不复制。
真正的 Agent trajectory、Harbor 日志、verifier reward 和终态标记允许进入内部
`trace/authoring`，但不会进入公开题包。

完整 snapshot 和 ledger 先在同文件系统 staging 中生成并复核哈希。提交时先备份
旧目标，任一替换失败便逆序恢复已经提交的目标。全程持有文件锁，避免两个 apply
并发覆盖。原 run 是恢复源；中央 archive 由批次 manifest 和逐题记录摘要绑定。

## 消费者与影响

当前只有操作脚本直接导入整理服务。Teacher 和进度工具读取 JSON/Markdown；
最终发布检查器仍独立验证 `question-pack/`，历史整理不能直接产生发布状态。

## 实现切片

1. trace allowlist/denylist、状态和 archive 绑定；
2. 事务锁、staging、回滚；
3. 逐题/全局索引与真实 Q3–Q32 迁移；
4. 单元、分支、全仓和独立抽查。

每个切片不超过两个层次和六个生产文件。

## 验证与可观测性

- 单元测试覆盖解析、筛选、状态优先级、幂等、符号链接、冲突和回滚；
- 完整 TaskFoundry tests、Ruff、large-code checker；
- 真实集成检查 30 个题根、所有 source/index/snapshot SHA；
- publication checker 确认 Q3–Q32 未被污染；
- 独立 Standards、Spec 和抽样证据复审。

索引只记录路径、尺寸、摘要和状态证据，不记录凭据。内部科学 trace 与公开题包
严格分离。

## 回滚边界

回滚仅删除生成的 `HISTORY_INDEX.*`、`AUTHORING_PROGRESS.*` 与 imported snapshot。
源 run、中央 archive、Brief 和正式题包不重写。Q3–Q5 已拒绝候选可按各自 archive
记录通过同文件系统 rename 恢复。

## Waiver 与未决项

无 waiver。目录、Q1/Q2 边界、难度命名和 Skill Bank 路由均已由用户确认，当前
没有阻断执行的开放决策。
