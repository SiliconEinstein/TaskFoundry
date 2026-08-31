# Teacher Skill 自包含运行时 v1

## 问题

旧实现只保存 Skill 与 Experience Card 的元数据路径，Teacher 可能只拿到一张“已解析”回执，却没有真正看到规则内容；`outline` 和 `author` 也没有绑定到准确 prompt，导致设计文档与实际出题输入可能分叉。

## 运行合同

每个题号、每个阶段都用以下 selector 调用 SkillFoundry：

```text
role=teacher
task_type=method-selection
stage=outline | author
profile=multi-question-input
```

SkillFoundry 必须加载并哈希完整稳定版 Execution Skill、`RULE_SOURCES.json` 及每份规则原文、冻结 Bank Snapshot、本阶段 Experience Cards、显式任务输入和阶段指令，并把这些字节渲染成一个确定性 prompt。

TaskFoundry 在题号目录中保存：

- `trace/authoring/skill-activations/{stage}.json`
- `trace/authoring/prompts/{stage}-prompt.txt`
- `{stage}-stable.lock.json`

`outline` 必须显式绑定来源输入；`author` 必须绑定题号根目录中仅有的最新版 `QuestionDesignBrief.json` 和 `QuestionDesignBrief.md`。元数据回执、缺失内容、错误 card、重复 JSON key、prompt 漂移、stable generation 漂移或遗漏任务输入均失败关闭。

## 工作流绑定

Workflow 强制执行以下顺序：

1. 接纳并复验 `outline` activation；
2. 接纳同一最新版 Brief 的 JSON/Markdown 两份字节；
3. 接纳并复验 `author` activation；
4. 进入 `AUTHORING`；
5. 冻结题包前再次复验两阶段 activation、prompt 和 Brief。

旧的 `lock_policies` 旁路已废止，不能跳过 Skill 合同直接 author 或 freeze。

## 批次与归档

`batch_id` 表示一组并发题共享的知识版本，不是“一道题一个阶段”的计数器。每道题的 outline/author activation各自持久化，但必须绑定同一批次稳定版本。运行中的同阶段 activation 不得由另一个 attempt 覆盖。

进入新批次时，旧 activation、prompt 和 stable lock 作为一组移入内容寻址归档，并写入逐文件哈希清单。resolve 先在暂存目录生成全部文件，验证成功后提交 prompt、stable lock，最后以 activation 作为事务提交标志；失败时不留下半份 active activation。

## 线性解题关系

一份冻结 revision 只拥有一个外层 Researcher 会话。首轮通过立即判定 `TOO_EASY`；失败才进入下一 blind。三轮均失败后，才在同一会话加入一至两轮 Teacher 非答案提示。科学内容变化时必须创建新 revision 和新空会话。

## 兼容与安全

schema-v1 activation 只保留为历史证据，不可用于新 Teacher 工作。任务输入必须为普通文件且不可为 symlink；规则来源、prompt、stable lock 和输入哈希在使用边界重新计算。不会把凭据或未显式绑定的私有运行数据加入 prompt。

## 验收

- 完整知识加载、来源漂移、card 选择、prompt/input 哈希均有单元测试。
- 缺少任一 activation 或 Brief 时无法进入 author/freeze。
- 新旧批次归档、resolve 崩溃和 stable generation 漂移均失败关闭。
- TaskFoundry 与 SkillFoundry 全套测试、lint、compile 和结构检查通过。

## 已确认决策

无开放决策。用户已确认按阶段 resolve、整体加载完整 Skill、题号根目录只保留两份最新版 Brief、最终难度名为 `high / medium / low`，并采用首轮通过即停止的线性 blind 验证。
