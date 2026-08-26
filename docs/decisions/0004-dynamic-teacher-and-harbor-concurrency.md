# ADR 0004：动态 Teacher 与 Harbor 200 并发边界

状态：2026-08-24 已确认并实现。

## 问题与非目标

旧调度器把 Teacher 活跃题数固定为 3，既不能满足五题并行，也把出题并发和
Harbor 解题并发混成了一个概念。Harbor 还缺少跨进程共享的全局准入门，多个
Researcher 可以绕过统一上限直接启动 Job。

本决定不自动创建 Codex Teacher 任务，不改变单题三轮盲解协议，也不把一个
Harbor Job 内的 trial 数提升到 200。

## 模式与范围

- Engineer Large Code：STRICT / L3，INCREMENTAL。
- 涉及领域契约、持久调度、命令行和 Researcher 启动边界。
- Teacher 与 Harbor 使用不同状态文件、锁和并发计数。

## 决定

1. Teacher 默认并发为 5，可在运行时动态调整到 1–200。
2. 调低 Teacher 上限时不终止活动题；调度器停止补位，直到活动数自然低于上限。
3. Harbor 全局硬上限固定为 200 个活动 Job，不提供运行时放大接口。
4. 每个队列条目绑定唯一 request、唯一 JobConfig、一个 task、一个 trial 和 LBG
   环境；不同条目由 Harbor 创建独立沙盒。
5. 超过 200 的 Job 保持 FIFO `WAITING`。排队不会兑换 Researcher capability，
   不会启动 Harbor，也不会计入科学尝试。
6. 正式 `researcher-run` 必须取得与 request 和 JobConfig 精确匹配的活动 claim。
7. 科学完成和一小时科学超时释放槽位；环境、沙盒、harness、模型或平台失败以
   `PLATFORM_FAILED` 释放槽位，但不写入科学尝试账本。
8. 未取得完整终态回执的中断 Job 保持 `ACTIVE`，转为人工
   `RECOVERY_REQUIRED` 后才能释放；不得仅根据本地 PID 消失自动重跑。

## 公共接口与兼容

- `QuestionScheduler.configure(max_active=...)` 动态修改 Teacher 上限。
- CLI 新增 `scheduler-set-limit`。
- 新增 `HarborJobQueue` 及 `harbor-queue-status/submit/claim/complete`。
- `researcher-run` 新增必填 `--queue-root`，并可接受外部 worker 提供的
  `--claim-id`；未提供 claim 时仅尝试认领自己的 FIFO 请求。
- 旧 scheduler 快照中显式保存的 `max_active=3` 仍可读取；管理员应通过新命令
  明确迁移，不静默改写持久决定。

## 并发、安全和恢复

两个调度器均使用进程级文件锁、序列号、临时文件、`fsync` 和原子替换。
Harbor 上限对同一个规范队列根目录全局生效；正式部署统一使用
`/personal/TaskFoundry/harbor-queue`，不得按 worker 拆分队列根目录规避上限。

claim 同时绑定 request、worker、JobConfig 绝对路径和随机 claim ID。Researcher
在兑换 capability 前后各校验一次 claim，避免排队状态消耗一次性执行权或请求
字节漂移。凭证仍只通过 env 文件传递，队列不读取或保存凭证值。

## 错误与回滚

无槽位返回 `QUEUED`；配置不是单 task、单 trial 或 LBG 时拒绝提交；陈旧 claim
拒绝完成。代码回滚可以忽略新队列，但不能删除队列状态、Researcher receipt 或
远端 Job。回滚前必须先阻止新认领，并核对所有 `ACTIVE` 条目和远端真实状态。

## 验证

- 单元测试提交 201 个独立 Job，只允许 200 个进入 `ACTIVE`。
- 释放一个槽位后，第 201 个 Job 才能被认领。
- 非单 task 或非单 trial 的 JobConfig 必须被拒绝。
- 集成测试覆盖提交、认领、`researcher-run`、receipt 和槽位完成。
- 真实 LBG 采用 5、20 的阶梯验证；除非另有明确执行指令，不一次性启动 200 个
  付费 Job。代码硬上限仍为 200。

## 豁免与剩余工作

没有豁免。自动创建五个独立 Codex Teacher 任务不属于本决定；当前 Teacher
调度要求调用者提供已经存在的任务 ID，是否采用 Codex SDK 另行决定。
