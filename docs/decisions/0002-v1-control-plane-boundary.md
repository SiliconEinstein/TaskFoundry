# ADR 0002：V1 Codex 控制面边界

状态：接受；首题验证保留窄范围运维豁免。

本地文件系统代码无法证明同一 Unix 用户下的 Codex 任务身份，因此 V1 把 Codex
任务控制面视为可信角色调度者。正式 Researcher 任务是唯一允许兑换 Researcher
请求并执行 Harbor 的操作者；Teacher 不执行 Harbor，也不读取凭证值。

本地纵深防御仍使用一次性加锁 capability，并绑定冻结题包、JobConfig、harness
运行材料、模型、LBG 项目、上下文摘要、结果字节和执行身份。Agent 科学执行上限
固定为 3600 秒；环境准备、harness 启动、Labwright 增量配置和 Verifier 时间分别
记账。

这项豁免不代表同主机角色已经实现密码学隔离。正式生产部署仍需独立运行身份、
凭证隔离、最小目录挂载、提供方签名回执和单调围栏。完成这些条件前，不得声称
本地 capability 能抵抗同 Unix 用户下的恶意任务。
