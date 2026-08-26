# 最终题目发布目录清理迁移（2026-08-26）

## 决策

题族发布合同已修正为 `final-question-family-v1`：Q3–Q32 每题发布一个题族，必须
包含 `hard` 和 `medium`，可选 `guided`。`medium` 与 `guided` 只能由独立审核的
非答案 hint 派生，并与 `hard` 保持同一个科学合同。Q1 与 Q2 已由所有者确认为历史
完成题，本次迁移不重新解释它们的旧 evidence。

`new-question` 只用于正式发布；失败、待定、已替代或 `TOO_EASY` 候选不得留在
该目录。

## 只读审计

三组独立审计分别覆盖 Q1–Q11、Q12–Q22 和 Q23–Q32，得到一致结论：

- 当时没有任何 `new-question` 条目符合新的题族目录结构；
- 当时没有任何题包具备新政策要求的完整门禁证据链；
- 多个历史完成标记把接近或等于 `1.0` 的 fresh blind 视为成功，但新方法选择政策
  规定任一 fresh blind 达到 `0.85` 即为 `TOO_EASY`；
- Q16 已具备三次低于阈值的 blind 和一次高于 `0.85` 的 reviewed hint 结果，但缺少
  formal seal、trace 绑定的最终 runtime evidence 和 post-validation seal，因此仍不
  能发布。

在已经废止的 hard-plus 解释下，迁移前可发布数量为零。该历史审计不得用于撤销
Q1/Q2 已由所有者确认的完成状态。

## 迁移动作

批次 `20260826T140000Z-clean-publication-v1` 把 32 个发布目录中的 320 个顶层
条目原子移动到 Git 忽略、append-only 的
`TaskFoundry/archive/question-authoring/qNN/`。

- 常规文件总字节数：`580227636`；
- 破坏性删除：无；
- 操作方式：同文件系统 rename；
- 迁移前 manifest：第一次移动前写入；
- journal：每次顶层移动后执行 fsync；
- 迁移后验证：所有归档树 hash 均匹配；
- 32 个 `new-question` 目录：全部清空。

迁移 evidence SHA-256：

- 迁移前 manifest：`c7272a33c92791c0e431c9bc8ef21a13fab9644bd8478c550eb641e855acd04a`；
- move journal：`b286d8029fb3c226f8698cca9bb35e242a131d446a76edde785427e8bb8e8a5c`；
- 最终迁移 manifest：`013370e138e899beed70792270c5a3a53b61534b68c6e6eaa5e6889c67f27f9f`。

详细 manifest 有意保持 Git 忽略，因为归档树可能包含 private reference、grader、
solution 和执行 trace。

## 强制门禁

`scripts/check_new_question_publication.py` 与 promoter 共同调用
`scripts/question_family_validation.py`。标准 Q3–Q32 题族会拒绝：

- 发布根下出现非题族文件或目录；
- 缺少 `hard` 或 `medium`；
- 任一层没有独立 `PASS_FINAL_PROGRESSION` 证据；
- fresh blind 分数达到或超过 `0.85`；
- blind 数量不是恰好三次；
- reviewed hint 后的分数低于 `0.85`；
- 派生层与 hard 的科学合同或 reviewed hint 不一致；
- 未满 32 个题族却声明最终完成。

Q1/Q2 可使用显式、窄范围的 `grandfathered-v1` 分支：只保留一个按历史摘要精确
绑定、节点类型安全的 `legacy/` 成功题包，并绑定所有者确认的 canonical completion
evidence。当前 lint 不追溯重判这两个题包；该例外不会重算旧 blind/hint，也不允许
Q3–Q32 使用。

迁移期间可用 `--allow-empty` 只检查发布目录是否干净，而不宣称全局完成。后续开发
统一在 `TaskFoundry/workbench/qNN/` 进行；只有完整封签的题族才能通过 promoter
原子晋级 `new-question`。
