# TaskFoundry

TaskFoundry 是一套用强 Agent 持续生产高难科学计算 Harbor 题目的出题框架。
它把题目设计、规范沉淀、Teacher 出题、Harbor 线性验证和运行时环境固化
连接成一条可恢复的直接流程。

本项目采用 [Apache License 2.0](LICENSE)。公开仓库只包含框架源码、测试、
文档和经过脱敏的示例，不包含实时 `runs/`、一次性 capability、内部回执或完整
Harbor 运行目录。

## 当前正式流程

1. 每批先用 `skillfoundry resolve` 固定题型对应的 Execution Skill、Bank Snapshot
   和 Experience Cards；批次内不更换版本。
2. `outline` 阶段生成并保存最新版 `QuestionDesignBrief`；`author` 阶段直接生成
   最小可运行题包。一道题应预计在 1800 秒内可解。
3. Teacher 只执行 `tests/test.sh --probe`（10 秒上限）和最小结构解析，然后原子冻结
   题包并创建一个线性验证会话；
   不再设置 formal Reviewer、Stable 环境、Oracle 或对抗测试等前置门。
4. 同一个 Researcher 会话依次执行最多三轮 blind。第二轮能看到第一轮记录，第三轮
   能看到前两轮记录；每一轮仍由 Harbor 创建 fresh sandbox。
5. 任一 blind 得分 `>=0.85` 立即停止，判为 `TOO_EASY`。Teacher 提高真实科学难度，
   创建新 revision 和全新验证会话，再从第一轮开始，不能继续原会话凑轮数。
6. 三轮 blind 均未过线时，才在同一会话中依次增加一至两级非答案提示；提示后过线
   才算验证成功。Agent 硬超时为 3600 秒。
7. 缺少公开能力时保存平台失败 trace，由 Labwright 根据真实 trace 构建类型化 delta；
   同一科学 attempt 在修复后的 fresh sandbox 重试，平台失败不计科学结果。
8. 提示后通过先进入 `VALIDATION_PASSED`，再固化实际使用过的环境；环境绑定成功才进入
   `COMPLETED`。随后把 `high` 及可选的 `medium`、`low` 难度版本发布到题号目录，
   批次结束后再统一 reconcile Skill Bank。

启动当前线性验证会话：

```bash
taskfoundry freeze-and-start-validation <run-dir> <package-dir> \
  --validation-session-id <session-id> \
  --researcher-thread-id <thread-id>
```

正常出题流程不包含 harness 或模型对比实验。历史实验可以作为旁路研究证据，
但不得写入正式验证账本。

## 多题调度

Teacher 调度器支持 1–5 个带 owner、generation、heartbeat 和 expiry 的 writer lease，
当前默认只启用 1 个。
Reviewer、Harbor、Labwright 或人工等待立即释放 authoring 槽位并自动补位，等待结束后重新排队；旧 `ACTIVE` 字段不再代表真实存活：

```bash
taskfoundry scheduler-enqueue /personal/TaskFoundry/scheduler q18 \
  /personal/TaskFoundry/runs/q18 \
  --teacher-thread-id <teacher-task-id> \
  --teacher-prompt /personal/TaskFoundry/runs/q18/teacher-prompt.md
taskfoundry scheduler-work-once /personal/TaskFoundry/scheduler
taskfoundry scheduler-status /personal/TaskFoundry/scheduler
```

调度状态只表示哪道题应继续推进。真正的解题仍必须通过 Researcher capability
进入 Harbor 沙盒，不能由 Teacher 当前会话直接执行。

## Harbor 并发

Harbor 使用独立的全局队列，硬上限为 200 个活动 Job。每个 JobConfig 必须只有
一个 task、一个 trial 和一个 LBG 环境，因此每轮创建独立沙盒；同一道题各轮通过
`prior_round_receipt_sha256s` 串成同一个线性会话。超过上限
的请求保持 `WAITING`，不会丢弃或计作科学失败：

```bash
taskfoundry harbor-queue-submit /personal/TaskFoundry/harbor-queue \
  <request-id> <job-config.json>
taskfoundry harbor-queue-claim /personal/TaskFoundry/harbor-queue \
  --worker-id <researcher-worker> --limit 200
taskfoundry harbor-queue-status /personal/TaskFoundry/harbor-queue
```

正式 `researcher-run` 必须带同一共享队列根目录。没有预先认领时，它会尝试按
FIFO 自动认领自己的请求；200 个槽位已满时仅返回 `QUEUED`，不会兑换一次性
capability 或启动 Harbor。

TaskFoundry 导入一轮结果时只接收 handoff 根目录和 `request_id`，再从 canonical
Harbor job/trial/result/log 字节重新计算分类、reward、双沙箱身份和 harness session。
在 TaskFoundry 应用接口的威胁模型内，Teacher 手写的 reward、receipt 路径或运行 ID
不能推进状态机。系统不抵抗已经拥有 `/personal` 写权限、能够直接篡改 canonical 证据
或运行日志的恶意进程；该边界不应被表述为平台级签名保证。

## Labwright

Labwright 有两条生命周期，运行时增量必须先于稳定镜像：

- 运行时增量：Researcher trace 触发公开能力请求，Labwright 排他认领并重放基础环境，
  返回带证据的恢复回执；修复后重试同一科学 attempt，配置失败不计科学尝试。
- 稳定镜像：首次有效解题结束后，Labwright 汇总已验证增量形成镜像固化计划，构建题目
  完整镜像，经过干净沙盒验证后登记为 Stable。

普通 CPU 题使用固定 Paper2ARM 基础镜像。最终镜像包含题目依赖
和公开资源，不包含凭证、隐藏答案、测试、提示或 Researcher 轨迹。

## 题包位置

运行目录中的 `question-revisions/` 是不可变验证快照，不是最终交付目录。完成题包
必须同步到：

```text
/personal/codex-workspace/question-from-questions/<题号>/question-pack/
  high/
  medium/  # 可选
  low/
```

`medium/low` 必须与 `high` 的科学、资源、grader、solution 和运行合同逐字节一致，
只允许把实际使用且由 Teacher 显式声明非答案的 hint 以固定段落追加到
`instruction.md`，并新增精确绑定的 `DIFFICULTY_VARIANT.json`。发布事务同时把题族摘要、
逐轮 Harbor 原始证据和 runtime closure 封签到同题号的 `trace/final/`。

## 开发检查

```bash
cd /personal/TaskFoundry
/opt/mamba/bin/python -m pytest -q
python3 /home/codex-work/.codex/skills/engineer-large-code/scripts/check_large_code.py \
  src/taskfoundry/*.py
```

新增和修改的代码注释、公开契约说明及项目文档统一使用中文。命令、协议字段、
模型名称和外部工具的专有名词保持原文。

## Harbor 轨迹示例

[`examples/harbor-trajectory/q10-survival-a01/`](examples/harbor-trajectory/q10-survival-a01/)
收录了一次成功的 fresh Harbor blind solve 轨迹及 verifier 摘要。示例从运行目录
单独提取并复核，不包含凭据、内部代理地址、私有 reference、receipt 或完整日志。

## 架构决定

- [ADR 0001](docs/decisions/0001-taskfoundry-v1.md)：TaskFoundry V1 编排模型
- [ADR 0002](docs/decisions/0002-v1-control-plane-boundary.md)：Codex 控制面边界
- [ADR 0003](docs/decisions/0003-codex-gpt-formal-flow.md)：Codex + GPT 正式流程
- [ADR 0004](docs/decisions/0004-dynamic-teacher-and-harbor-concurrency.md)：动态 Teacher 与 Harbor 200 并发边界
- [ADR 0008](docs/decisions/0008-direct-linear-harbor-validation.md)：直接线性 Harbor 验证流程
