# TaskFoundry

TaskFoundry 是一套用强 Agent 持续生产高难科学计算 Harbor 题目的出题框架。
它把题目设计、规范沉淀、Teacher 出题、Labwright 环境配置和 Researcher 盲测
拆成独立契约，同时用持久状态和证据门把它们连接起来。

本项目采用 [Apache License 2.0](LICENSE)。公开仓库只包含框架源码、测试、
文档和经过脱敏的示例，不包含实时 `runs/`、一次性 capability、内部回执或完整
Harbor 运行目录。

## 当前正式流程

1. 题型模块生成 `QuestionDesignBrief`、来源角色图和 Ground Truth 账本。一道题应预计在 1800 秒内可解。
2. Teacher 锁定题型规范、最小可运行题包和固定基础镜像合同。
3. Reviewer 检查题型、明显泄漏、Oracle、grader 和
   首次解题安全门，不等待 Stable 环境。
4. Teacher 向独立 Researcher 签发一次性请求；Researcher 立即从固定 Paper2ARM
   基础镜像启动 Harbor。
5. 缺少公开能力时，保存失败 trace 并提交类型化请求；Labwright 重放基础环境与
   delta，同一科学 attempt 使用修复后的 fresh sandbox 重试，环境失败不计科学尝试。
6. 首次有效解题后，Teacher 只依据公开验证结果调整真实科学难度，Labwright 根据真实 delta 精简并
   固化题目镜像，不按作者侧依赖猜测镜像内容。
7. 完整 Reviewer 验证 Oracle、诚实解、对抗样例、最终环境和无泄漏证据。
8. 每个 `high` revision 执行三轮独立、空策略上下文的 fresh blind；任一得分
   `>=0.85` 即 `TOO_EASY`，修订难度后从 blind-a01 重启。三轮均低于通过线后，
   才能运行一至两级经审核的非答案提示；提示后必须达到通过线。Agent 硬超时为
   3600 秒；达到超时后暂存该题并调度下一题。

`freeze-package` 前必须先显式绑定与当前 brief/revision 一致的两份设计证据：

```bash
taskfoundry attach-design-evidence <run-dir> <source-role-map.json> <ground-truth-ledger.json>
taskfoundry freeze-package <run-dir> <package-dir>
```

难度修订会使这两份绑定失效；修订后的题包必须重新绑定同一科学来源和 GT 合同。

正常出题流程不包含 harness 或模型对比实验。历史实验可以作为旁路研究证据，
但不得写入正式验证账本。

## 多题调度

Teacher 调度器保持三个带 owner、generation、heartbeat 和 expiry 的 writer lease。
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
一个 task、一个 trial 和一个 LBG 环境，因此每项正式尝试创建独立沙盒。超过上限
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
/personal/codex-workspace/question-from-questions/<题号>/question-pack/<题族>/
  FAMILY_MANIFEST.json
  high/
  medium/
  low/  # 可选
```

## 历史进度归档

Q3–Q32 的旧题包尝试、Reviewer、Harbor、运行回执和失败归因按 trace 类型复制到
各题的 `trace/authoring/imported-taskfoundry-runs/`，并以 SHA-256 建立逐题索引。
原始 run 保持不动，历史 `PASS` 或 `COMPLETE` 仅作为可复用证据，不会自动写入
`question-pack/`：

```bash
cd /personal/TaskFoundry
PYTHONPATH=src /opt/mamba/bin/python scripts/consolidate_question_history.py
PYTHONPATH=src /opt/mamba/bin/python scripts/consolidate_question_history.py --apply
```

全局总账位于 `question-from-questions/AUTHORING_PROGRESS.json` 和 `.md`；每道题的
明细位于 `trace/authoring/HISTORY_INDEX.json` 和 `.md`。Q1、Q2 不在迁移范围内。

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
