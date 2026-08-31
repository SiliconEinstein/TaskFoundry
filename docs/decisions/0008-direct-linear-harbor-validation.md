# ADR 0008：直接线性 Harbor 验证会话

- 状态：已采纳
- 日期：2026-08-27
- 范围：题目 authoring 到验证交接

## 背景

旧流程在可运行题包与首次真实解题之间加入题包健康检查、独立 formal review、
shortcut/honest-solver 门和环境固化。这些前置门延迟 Harbor，也重复产生本应由
真实解题阶段直接给出的证据。

旧实现还把三轮 blind 当成三个互不相关的空上下文 Researcher 会话，不符合线性渐进
验证要求。

## 决定

Teacher 生成可运行题包后，系统只执行最小启动检查，把普通文件复制到 run 内不可变
修订快照，创建 validation session，然后启动 Harbor。题目质量、难度、泄漏、shortcut、
环境、Labwright、健康证据或独立 Reviewer 都不能阻断首次解题。

最小启动检查只确认必需普通文件存在、配置可解析且 verifier 的 `--probe` 入口能启动，
它不是科学质量门。

一个冻结题目修订对应一个 Researcher validation session：

1. 第 1 轮不带前序解题记录；
2. 第 2 轮沿用同一 Researcher 会话，并能看到第 1 轮；
3. 第 3 轮沿用同一会话，并能看到前两轮。

三轮是上限，不是必须凑满的配额。每一轮科学 blind 结束后，状态机立即判断难度；
达到阈值就立刻进入 `TOO_EASY`，本修订不得再启动下一轮。只有前一轮未通过时才允许
继续后一轮。

每轮使用新的 Harbor sandbox，避免文件系统残留造成虚假通过；会话历史由外层
Researcher session 保留。

题目、公开数据、grader、提示、难度条件或其他有科学意义的字节一旦改变，当前 session
必须关闭。修改后的修订使用全新空 Researcher 会话，从第 1 轮重新验证。

正确性、可解性、资源充分性、难度、泄漏、shortcut、verifier 稳定性和提示需求均从
解题 trace 归因。环境需求也从 trace 中学习，只在验证成功后的 runtime finalization
阶段固化；环境绑定成功后才进入 `COMPLETED`。

## 状态机影响

旧的首次解题前序列：

`attach evidence -> freeze -> formal review -> accept health -> start blind`

由原子操作 `freeze_and_start_validation_session` 取代。来源、哈希和事件仍作为可审计
元数据，但不再构成独立审批门。Reviewer 可以审计已完成的解题 trace，但不是首次解题
前置条件。

验证成功后必须按顺序执行：

`VALIDATION_PASSED -> RUNTIME_FINALIZATION -> runtime closure -> Stable binding -> COMPLETED`

旧 `accept-post-validation` 完成旁路已删除，验证阶段也不能提前绑定 Stable 环境。

## 兼容迁移

历史的独立 fresh-session Harbor 运行仍可作为复现证据，但不满足本 ADR 的三轮线性会话
要求。历史完成记录不得静默改写；未完成旧 session 必须追加 `CLOSED_MIGRATED` 事件，
使用新 revision、新 Researcher thread 和新 validation session 重开。
