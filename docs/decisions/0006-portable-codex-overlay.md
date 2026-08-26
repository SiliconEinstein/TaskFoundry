# ADR 0006：Portable Codex + GPT 运行层覆盖包

状态：2026-08-25 已确认，进入实现。

## 事实与根因

Q17 的 portable Codex 尝试把单个 `codex` 文件上传到 Researcher sandbox。启动后：

- Codex 因同目录缺少 `codex-code-mode-host` 而关闭 Code Mode；
- 适配器没有保留网关命名空间，`matmaster/gpt-5.6-sol` 被 Harbor 旧逻辑截成
  `gpt-5.6-sol`；
- 旧 job config 没有 strong gateway 环境映射，最终错误连接公共 API 地址。

这些是 harness bootstrap 失败，不是科学尝试。

## 决策

1. Portable runtime 是不可拆分的二进制包：`codex` 与同一发行版的
   `codex-code-mode-host` 必须同时提供路径和 SHA-256，版本由 Codex CLI 输出锁定。
2. 两个文件都由宿主先验摘要校验、上传到 `/installed-agent/codex/`，再在 sandbox
   内二次校验并设为可执行。任何缺失或漂移均在 Agent execution 前失败。
3. portable 适配器复用 namespaced gateway 适配器，完整保留
   `matmaster/gpt-5.6-sol`。
4. 正式 runtime 构造器必须显式接收覆盖包；不再允许“正式运行但默认依赖镜像内置
   Codex”。strong gateway 只引用环境变量名，不持久化值。
5. bootstrap 证据属于运行层，不写入环境 manifest，也不 snapshot 回题目镜像。

## 验收

- 单元测试证明两个文件均上传并经过双摘要检查，模型命名空间完整保留。
- 缺任一 bundle 字段、文件不存在或摘要漂移时失败关闭。
- JobConfig 同时包含完整模型名、strong gateway 变量引用和两个 portable 文件字段。
- 使用真实 0.149.1 bundle 生成并校验一次 synthetic JobConfig；随后才允许启动 LBG
  synthetic Harbor smoke。

## 真实 LBG 验证记录

2026-08-25 使用固定的 0.149.1 覆盖包进行了两次平台前置 smoke：

- Researcher sandbox 中 `codex` 与 `codex-code-mode-host` 均完成宿主和沙盒内双重
  SHA-256 校验，Code Mode 正常启用；
- 模型名完整保留为 `matmaster/gpt-5.6-sol`，网关调用成功，Agent 生成了预期结果；
- JobConfig 明确设置 `mount_user_storage=false`；
- 第一次在空隐式 artifact 传输处失败，第二次完成 Researcher 销毁并创建 fresh
  verifier sandbox，证明 portable overlay 本身不是失败点；
- 第二次暴露 Harbor-LBG 的 separate verifier 缺陷：非共享挂载后端没有把任务
  `tests/` 显式上传到 `/tests`。该缺陷在 Harbor-LBG 中按能力修复：仅当 verifier
  声明 `mounted=true` 时才跳过 tests 上传。相关定向回归 28 项全部通过。
- 修复后第三次 smoke 完整通过：Researcher 与 verifier 使用两个不同 LBG sandbox；
  Researcher 删除后才创建 verifier，`tests/` 被显式上传到 `/tests`，最终 reward=1.0、
  exceptions=0、总墙钟 7 分 39 秒。权威结果位于
  `smoke/harbor-jobs/portable_codex_overlay_01491_smoke03/result.json`。

这两次都属于平台前置 smoke，不计入任何题目的科学盲解次数。正式题目仍须取得完整
Researcher receipt 与权威 reward 后才记为有效尝试。
