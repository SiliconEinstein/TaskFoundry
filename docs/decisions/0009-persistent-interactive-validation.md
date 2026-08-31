# ADR 0009：持久 Researcher 沙盒与 Teacher 轮间控制

- 状态：已采纳
- 日期：2026-08-28
- 范围：题目验证会话
- 取代：ADR 0008 中“每轮新建 Agent sandbox、手工注入历史”的部分

## 决定

同一题目 revision 的一次验证会话只创建一个 Researcher sandbox，并在其中保留同一个 Researcher 模型会话。每轮答案由全新的 separate verifier sandbox 评分；Researcher 不参与正式评分，也不能看到 private GT、grader 或 verifier 私有诊断。

Teacher 在每轮评分后读取 Harbor 证据并作出唯一下一步决定：继续 blind、提供一条声明不含答案的提示、因过易终止、提示后通过，或阻断终止。三轮 blind 是上限；任何 blind 首次达到通过线都立即判定当前 revision `TOO_EASY`，不得机械执行剩余轮次。只有三轮 blind 全部未通过才允许提示。

Harbor 以宿主控制目录在轮次之间暂停。round result 与 Teacher decision 均使用不可变 JSON；decision 必须绑定上一 result 的 SHA-256。Agent sandbox 不挂载控制目录。平台故障不计科学轮次；首个科学结果前可用全新 runtime 重试同一 scientific attempt。已有科学轮次后则优先使用 provider checkpoint，恢复后必须标记 `PLATFORM_RECOVERY`；没有 checkpoint 时进入 `PLATFORM_RECOVERY_REQUIRED`，不把新 sandbox 冒充原 sandbox。

环境仍采用 runtime-first：首次真实解题不等待 Stable Labwright。真实 trace 暴露的环境缺口在验证后固化，但不能以缺失环境制造题目难度。
