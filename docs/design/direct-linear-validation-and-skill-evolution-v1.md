# 直接线性验证与 Skill Bank 演化 v1

## 问题与非目标

旧流程把首次真实解题阻塞在设计证据、Reviewer health 和环境门之后；三轮 blind 又被建模为彼此无关的空上下文会话，同时没有闭合 SkillFoundry 的演化链。本设计改为“先真实解题、再根据 trace 固化运行环境”，并补齐批次级 Skill 演化入口。

历史事件继续可读。本设计不改写已有 run，不删除证据，不自动启动 Harbor，也不修改科学题包。

## 目标流程

```text
outline Skill -> QuestionDesignBrief -> author Skill -> AUTHORING
  -> freeze_and_start_validation_session
  -> 同一 Researcher 会话中的 blind 1
  -> 失败后 blind 2
  -> 失败后 blind 3
  -> 一至两轮 Teacher 非答案提示
  -> RUNTIME_FINALIZATION
  -> COMPLETED
```

冻结与会话创建是一个原子操作，只执行启动所需的题包解析、lint 和最小 probe。一份冻结 revision 绑定一个外层 Researcher thread；每一轮仍使用全新的 Harbor job、trial、agent sandbox、verifier sandbox 和内部 harness session。后续轮次必须携带此前所有 Agent 可见 transcript 的规范历史包。

任一 blind 达到通过线，当前 revision 立即进入 `TOO_EASY`，剩余 blind 不再执行；Teacher 必须提高难度并以新 revision、新空会话重新验证。只有三轮 blind 均未通过，才允许在同一会话中加入最小 Teacher 提示。提示由 Teacher 声明不含答案，系统只验证身份、顺序、数量和字节绑定。

## Skill Bank

每道题在 `outline` 和 `author` 开工前分别执行一次 `skillfoundry resolve`。一次 activation 必须完整包含自包含 Skill Bundle、任务输入和最终 prompt；兼容旧版本时才允许加载历史规则来源。首次 resolve 原子冻结 `batch_questions`、stable generation 和 stable-lock 摘要；同批各题及两个阶段均不得漂移。

每批结束后只聚合明确归因为 `skill_noncompliance` 或 `skill_knowledge_gap` 且位于 informative/boundary 区间的失败。至少两个不同题目的同机制证据才可 reconcile。候选必须同时通过原失败修复、未见任务迁移和冻结回归，才允许由 SkillFoundry 原子更新 `stable.lock`。

## 公开契约

- `freeze-and-start-validation`：冻结不可变题包并建立唯一线性会话。
- schema-v2 `ResearcherRequest` / `AttemptEvidence`：绑定 validation session、Researcher thread、全部前轮记录和历史包。
- `record-validation-round`：只从 canonical Harbor 产物导入，不接受调用者填写 reward 或执行身份。
- `reconcile-skill-bank`：执行批次级归因聚合与候选评测。
- 最终发布只接受 `question-pack/high` 加 `low`，或再加 `medium`；变体除固定追加实际 Teacher hint 与 `DIFFICULTY_VARIANT.json` 外，全部节点、权限和科学字节必须与 high 相同；final trace 同时绑定各难度包摘要。

## 事务、并发与恢复

- session registry 拒绝 revision、session 和 Researcher thread 复用。
- capability 只能兑换一次；request ID 只能使用安全路径字符。
- 每个 Harbor 执行身份必须全新；平台失败不计科学轮次，但重试也不能复用已创建身份。
- run journal 已提交而 registry 尚未关闭时，同一幂等请求负责自动对账。
- 科学字节变化必须关闭旧 session 并开始新 revision。
- 环境、Harness 或平台失败在同一科学轮次内重试；真实依赖由 trace 进入 runtime finalization。

## 安全边界

本设计不要求平台签名，也不抵抗拥有 `/personal` 写权限的恶意进程。应用层不得信任 Teacher 手写的 reward、receipt 或 sandbox ID。事件中不记录 token、credential、私有 GT 或 grader 内容。

## 验收

- 首轮通过立即 `TOO_EASY`，不会机械补跑第二、三轮。
- 三轮 blind 与后续 hint 在线性同一外层会话中完成，且每轮执行环境身份不同。
- 后轮 JobConfig 精确注入全部既有 `round-history.json`，不注入 verifier 私有内容。
- 成功提示解后必须完成 runtime closure 和 schema-v2 Stable environment 绑定，才进入 `COMPLETED`。
- 最终发布题族与 run、hint、环境回执逐字节闭合。
- 完整 pytest、覆盖率、结构检查和 `git diff --check` 通过。

## 已确认决策

无开放决策。用户已确认直接 runtime-first、线性会话、Teacher 自声明提示非答案、不要求平台签名，以及 1–5 路并发能力。
