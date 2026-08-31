# 持久交互式验证会话 v1

## Problem and non-goals

现有线性验证只复用外层 Researcher task；每轮 Harbor 都新建 Agent sandbox，随后把前轮历史重新注入。它不能保留 Researcher 的文件系统与 Codex 会话，也不能让 Teacher 在一次 Harbor 生命周期中逐轮评分、决定继续或提供提示。

本改动把一份题目修订的验证改成单个持久 Researcher sandbox、单个持久 Codex 会话和逐轮全新 verifier sandbox。它不允许 Researcher 读取私有 GT、grader 或 Teacher 决策目录，不把 Researcher 自评当作正式分数，也不复用 verifier sandbox。

## Mode / scope / applicable rule overrides

- 模式：STRICT / L3，INCREMENTAL。
- 临时架构层：Harbor runtime、TaskFoundry orchestration。
- 每个实现切片不超过 300 个有效生产行、6 个生产文件和 2 层。
- 无 waiver。

## Current flow

`TaskFoundry request -> Harbor job -> Agent sandbox -> verifier sandbox -> stop -> import result -> next Harbor job`

后轮只靠历史文件模拟连续会话。Codex 每次 `run()` 都删除 `CODEX_HOME`，因此即使 Harbor MultiStep 保留 Agent sandbox，也不会保留模型会话。

## Proposed flow

`TaskFoundry session -> one Harbor interactive trial -> persistent Agent sandbox/Codex session`

每轮依次执行：

1. Researcher 在同一 sandbox、同一 Codex session 中继续工作；
2. Harbor 把本轮输出复制到不可变轮次目录；
3. Harbor 创建全新 separate verifier sandbox 并评分；
4. Harbor 原子发布只含公开诊断的 round result；
5. Teacher 写入绑定该 result SHA-256 的决策；
6. Harbor 校验状态转换后继续、提示或终止。

第一轮或后续 blind 达到通过线时只允许 `STOP_TOO_EASY`。三轮 blind 都未通过后才允许 Teacher 以 `teacher_declares_non_answer=true` 提供提示；提示通过时允许 `STOP_PASSED`。平台故障不增加科学轮次。

## Files, modules, and architecture layers

Harbor runtime：

- `harbor/trial/interactive_validation.py`：轮次循环、独立 verifier、决策等待。
- `harbor/trial/validation_control.py`：严格 JSON 协议、原子读写、状态转换。
- `harbor/trial/trial.py`：按显式 agent 配置选择交互 trial。
- `harbor/agents/installed/codex.py`：提供可覆写的 session 保留与 resume seam。

TaskFoundry orchestration：

- `taskfoundry/persistent_codex_agent.py`：启用 Codex resume 且消费控制参数。
- `taskfoundry/validation_control.py`：Teacher 读取结果、写入绑定决策。
- `taskfoundry/harbor.py`：生成持久验证 JobConfig。
- `taskfoundry/researcher.py`：schema-3 会话级请求与 provider sandbox 序列。
- `taskfoundry/harbor_evidence.py`：逐轮绑定 trial、manifest、decision 与 sandbox 身份。
- `taskfoundry/workflow.py`：Teacher 轮间决策及终局原子导入。
- `taskfoundry/validation.py`：允许 Agent 身份复用、强制 verifier 身份逐轮更新。

## Public API/schema/event/config changes

JobConfig 的自定义 Agent `kwargs.persistent_validation` 新增：

- `schema_version=1`
- `validation_session_id`
- `controller_dir`（宿主绝对路径）
- `pass_threshold`
- `max_blind_rounds=3`
- `max_hint_rounds`（1 或 2）
- `decision_timeout_sec`

控制目录新增不可变 `round-NN-result.json` 与 Teacher 独占写入的 `round-NN-decision.json`。每个 decision 必须绑定前一 result 的 SHA-256。

## Compatibility and migration

没有 `persistent_validation` 的 Harbor job 保持 SingleStep/MultiStep 原行为。历史 TaskFoundry request/receipt 继续可读，但不满足新持久会话验收。未完成旧会话以新 revision 迁移，已完成记录不重写。

## Transaction/concurrency/security model

- 一个 controller directory 只绑定一个 validation session 和一个 Harbor trial。
- result 使用临时文件、文件与目录 `fsync`、原子 hard-link 发布；已存在且字节不同即失败。
- decision 只接受普通文件、严格 JSON、无重复键、正确 session/round/result digest。
- Agent sandbox 不挂载 controller directory；只有 Harbor 宿主进程读取。
- verifier 每轮新建、评分后销毁；私有 tests/GT 只进入 verifier。
- Researcher 只能看到 Teacher 明确允许的继续消息或提示文本。

## Error taxonomy and recovery boundaries

- Agent/verifier/provider 故障：发布 `PLATFORM_FAILURE` 结果，不计科学轮次，关闭当前 runtime。若故障发生在首个科学结果前，允许用新的 runtime 身份重试同一个 scientific attempt；若已有科学轮次，则只有 provider checkpoint 能恢复原 workspace 与 Codex session，否则进入 `PLATFORM_RECOVERY_REQUIRED`，不得把新 runtime 伪装成原会话。
- controller 超时或非法 decision：`CONTROLLER_FAILURE`，fail closed。
- Agent sandbox 丢失：若 provider 支持 checkpoint，则新 sandbox 恢复 workspace 与 Codex session，并记录 `PLATFORM_RECOVERY`；不支持时关闭 runtime，不伪造同沙盒续跑。
- 科学字节、题面、grader、阈值变化：关闭当前 session，以新 revision 和新 sandbox 开始。

## Consumer/import graph impact

- Harbor `Trial.create` 是新增 trial 类型的唯一选择入口；普通 job 不受影响。
- TaskFoundry `build_job_config` 是持久会话配置的唯一生产者。
- `execute_harbor` 仍启动一个 Harbor subprocess；Teacher 通过控制目录与该长运行进程交互。
- canonical evidence importer 读取轮次结果、Teacher decision、逐轮 artifact manifest 与最终 Harbor result，不接受 Researcher 自报 reward。

## Implementation slices

1. Harbor 协议与交互 trial：不超过 6 个生产文件，仅 Harbor runtime 层。
2. TaskFoundry Codex/Teacher 控制与 JobConfig：不超过 5 个生产文件，TaskFoundry orchestration + adapter 两层。
3. canonical evidence 接入：schema-3 request、provider identity 序列、逐轮 importer 和终局状态迁移。
4. Q4 迁移：复用现有题目中间结果，以新会话合同重新启动验证，不重做 outline/author。

## Unit, branch, integration, and coverage plan

- 协议：重复 key、错误 digest、越序、非法提示、超时、冲突文件。
- Trial：同一 Agent env 启动一次、Agent run 多次、verifier env 每轮不同且逐轮销毁、首轮通过立即停止。
- Codex：首轮使用 `codex exec`，后轮使用 `codex exec resume --last`，持久模式不删除 `CODEX_HOME`。
- TaskFoundry：JobConfig 精确生成，Teacher decision 只能绑定已发布 result。
- 集成：mock provider 完整执行 blind fail -> blind fail -> blind fail -> hint pass。
- 当前环境没有 coverage 插件；用分支到测试映射替代，不伪报覆盖率。Harbor 全量 unit
  收集被未安装的可选 provider 依赖阻断；相关 166 tests 全通过，TaskFoundry 全量 tests
  全通过。

分支映射：

- blind 首轮通过 -> `STOP_TOO_EASY`：Teacher controller、Harbor controller、workflow 三层测试。
- 三轮 blind 失败 -> hint：交互 trial 四轮序列测试。
- 非法 digest、重复 JSON key、非法提示声明：controller 负向测试。
- 同一 Agent/job/trial/session + 新 verifier：schema-3 validation 与 importer 测试。
- 首轮前平台故障不计数并可换新 runtime 重试、失败 verifier 身份可选绑定：persistent importer/workflow 测试。
- Codex 首轮 exec、后轮 resume、保留 home、重建 config：persistent adapter 双调用测试。

## Observability and sensitive-data policy

记录 session、round、phase、result digest、decision action、Harbor job/trial/sandbox/session、公开 reward/diagnostics 和时间。不得记录 credential、proxy value、private GT、grader 内容或 Teacher 未批准的推理内容。

## Rollback and data-recovery boundary

删除 JobConfig 中 `persistent_validation` 即回到旧 Harbor 路径。新协议文件全部 append-only；回滚代码不删除已经产生的 round/result/decision 证据。科学题包保持冻结，不由运行时修改。

## Waivers, approvers, and expiry

无。

## Open decisions that block implementation

无。用户已确认：同一 Researcher sandbox、同一 Researcher 会话、逐轮新 verifier、Teacher 轮间决策、三轮 blind 后才提示、平台恢复不计科学轮次。
