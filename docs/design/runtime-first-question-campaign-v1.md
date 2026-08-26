# Runtime-first 三槽出题 campaign v1

## 问题与非目标

Q3–Q32 需要一条可执行且唯一的出题主流程。历史 run 同时存在互相冲突的规则：
部分线程把三次 fresh 得分 `1.0` 当成完成，另一些线程按正确规则把任一
`>=0.85` 判为 `TOO_EASY`；有些题在首次解题前等待 Stable Labwright 镜像，
另一些题使用 runtime-first。新流程必须同时保留题目大纲、环境管理、并发出题、
Harbor 难度验证、提示递进和干净发布，不能为了简化状态机丢掉其中任何一环。

Q1 与 Q2 是 owner 明确确认的 grandfathered 完成题。本次改造不重写它们的题包，
也不追溯重判旧证据。历史 run 和私有证据同样不删除。

## 模式与范围

- Engineer Large Code：STRICT / L3，按切片渐进实施。
- 临时架构层：政策文档、campaign/scheduler 领域、workflow/validation 领域、
  CLI/脚本和测试。
- 不使用 waiver。

## 现状

1. 根协议同时残留 Stable-first 与 runtime-first 两种环境顺序。
2. `validation.decide_validation()` 能正确识别 `TOO_EASY`，但过去仍有其他路径
   可以在绕过该判断时写入完成标记。
3. 旧 scheduler 默认五个活动 Teacher，没有 writer lease 或 heartbeat，且把可能
   永久不变化的 run 状态当成存活信号。
4. 旧发布政策要求 `hard-plus` 增加科学约束，而破壁协议实际是用经审核的提示从
   同一高难题派生更易版本。

## 目标流程

```text
题目大纲 + 来源角色图 + Ground Truth 账本
  -> 最小可运行题包 + 基础 EnvironmentSpec
  -> 轻量独立 preflight
  -> 三次 fresh Harbor blind
       任一得分 >= 0.85 -> 同题科学难度修订 -> 从 blind-a01 重启
       平台/环境失败 -> trace-backed runtime delta -> 重试同一科学 attempt
       三次均 < 0.85 -> 经审核的提示递进
  -> 带提示 fresh 解题 >= 0.85
  -> 物化 hard/medium/(guided)，保持同一科学合同
  -> trace-backed Stable 环境 + 正式复审 + 独立最终复审
  -> 原子发布
```

campaign 模块是 Q1–Q32 唯一调度状态源，记录题号、科学阶段、owner lease、
heartbeat、当前 run 和发布结果。scheduler 只公开认领、心跳、等待、恢复、tick
和快照接口，把 lease 过期和三槽自动补位隐藏在模块内部。

## 公共合同与兼容性

- Scheduler snapshot 从 schema v1 升级到 v2；首次写 v2 前保留 v1 原始字节备份。
- 默认且 campaign 固定的 Teacher writer lease 上限为 3。
- 发布级别为 `hard`、`medium` 和可选 `guided`。
- 高难版闭环是三次 blind 均 `<0.85`，随后至少一次 reviewed hint fresh 解题
  `>=0.85`。`medium/guided` 从成功提示派生，并与 `hard` 共享科学合同。
- Q1/Q2 以 grandfathered completion 进入总看板，不经过 Q3–Q32 的新晋级合同。

## 事务、并发与安全模型

- 每题最多一个 active writer lease。lease 含 owner、随机 lease ID、generation、
  heartbeat 和 expiry。
- `tick()` 先回收过期 lease，再把 active writer 数补到 3。
- 所有状态更新继续使用文件锁、临时文件、`fsync` 和原子替换。
- private trace、grader、reference 和失败候选只保存在忽略版本控制的 `runs/`、
  `workbench/` 或 `archive/`；公开目录只含最终题包和脱敏 family manifest。
- 环境、harness 与平台失败不增加科学尝试计数。

## 错误分类与恢复

- `TOO_EASY`：修订同一科学题材的真实难度，不能计完成。
- `SCIENTIFIC_FAILURE`：计入三次 blind。
- `ENVIRONMENT_FAILURE`、`HARNESS_FAILURE`、`PLATFORM_FAILURE`：保存 trace，必要时
  生成 runtime delta，重试同一科学 attempt。
- 来源、Ground Truth、方法选择合同或科学目标根本无效：封存整个题材并换题。
- grader、打包、交付布局、证据 schema 或环境能力缺陷：同题 append-only 修复，
  不得滥用“失败即换题”。
- writer lease 过期：转 `RECOVERY_REQUIRED`，保留工作，显式 requeue 后才能再认领。

## 消费者影响

- `SchedulerWorker`、scheduler CLI、scheduler tests 和 `scheduler.json` 读取方需要
  消费 schema v2。
- 发布 checker 与 promoter 必须共享同一个 family manifest 校验实现。
- 根协议与 `AGENTS.md` 必须引用同一 runtime-first、三盲、提示和发布语义。

## 实施切片

1. **政策切片**：统一 breaker、环境、发布和 agent 指引，不改生产代码。
2. **Campaign/scheduler 切片**：实现 lease、heartbeat、三槽、v1 迁移和确定性看板。
3. **发布切片**：统一 schema/checker/promoter 为 hard/medium/guided 和单一高难证据链。
4. **Validation/workflow 切片**：关闭任何绕过 `decide_validation()`、完整健康门或
   post-validation 就能写 `COMPLETED` 的路径。
5. **运行切片**：初始化 Q1/Q2 grandfather，Q3–Q32 排队，并恢复三个隔离 writer lease。

## 验证

- 单元测试覆盖 v1 迁移、heartbeat、lease 过期、三槽补位和唯一 owner。
- validation 测试覆盖 `0.85` 边界、`TOO_EASY` 修订、三次 blind、最多两级 hint、
  非科学失败和最终 post-validation。
- publication 测试覆盖级别名称、同科学合同、证据身份、原子 promotion 和 Q1/Q2
  窄范围 grandfather。
- 集成路径初始化临时 Q1–Q32 campaign；Q1/Q2 grandfather；Q3–Q5 认领；Q3 经历
  `TOO_EASY` 修订与 hint 完成；Q4 lease 过期；证明补位确定且没有重复 writer。
- 最终运行 Ruff、全量 pytest、branch coverage、`check_large_code.py` 和
  `git diff --check`。

### L3 结构复审记录

`check_large_code.py` 对生产改动给出 0 error、6 个需要人工确认的 review；逐项结论如下：

- `cli.parser` 只负责声明命令和参数，没有执行业务逻辑；继续集中声明可避免子命令
  注册顺序和分发映射分裂，判定为 cohesive PASS。
- `labwright_runtime._validate_completion_evidence` 只校验一份 runtime delta 证据并返回
  已绑定引用；长度来自完整 fail-closed schema，判定为 cohesive PASS。
- `researcher.execute_harbor` 只覆盖单次 Harbor 子进程生命周期、超时分类和回执组装；
  资源通过上下文管理器关闭，异常边界清楚，判定为 cohesive PASS。
- `workflow.attach_design_evidence` 只完成来源角色图和 Ground Truth 账本的同一冻结事务；
  两份证据必须原子绑定，拆开会产生半绑定状态，判定为 cohesive PASS。
- `workflow.audit_researcher_receipt` 只执行一次 Researcher 回执的 capability、题包、
  leakage、结果字节和提示授权复核，随后交给 `_record_attempt`，判定为 cohesive PASS。
- `_current_final_health_bound` 的 26 行 `try` 只把外部证据反序列化和一致性异常统一翻译为
  布尔健康门；不吞掉业务写入错误，判定为 boundary PASS。

上述 review 不使用 waiver；后续若任一函数再增加独立责任，应优先拆分而不是继续增长。

2026-08-26 最终集成测量：全量测试通过；总覆盖率 `90.2845%`，相对 `HEAD`
基线 `90.3880%` 下降 `0.1036` 个百分点；变更行覆盖率 `90.0742%`，变更分支
覆盖率 `83.3333%`。Ruff、`git diff --check` 均通过。

## 可观测性与敏感数据

公开进度看板只展示题号、phase、owner、lease 时间、run 路径和发布状态，不嵌入
private reference、hint 内容、凭证或 Researcher trace。

## 回滚与恢复

证据层只 append。源码可用普通 Git revert 回滚。scheduler 首次迁移保存原 v1
字节备份。发布使用原子 rename，历史材料仍可从忽略版本控制的 archive 恢复。

## 未决策项

无。用户已于 2026-08-26 明确确认完整决策前沿。
