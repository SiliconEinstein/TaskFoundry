# 最终题族发布规范 v1

`question-from-questions/<Q>/new-question` 是发布边界，只能包含已经完整验收的
题族。创作候选、被拒题包、运行时实验、复审请求和验证 trace 必须保存在该目录
之外。

## 标准题族

Q3–Q32 必须使用 `publication_mode: family-v1`，目录结构如下：

```text
<family-slug>/
├── FAMILY_MANIFEST.json
├── hard/
└── medium/
```

`guided/` 可选。每一层都必须是可独立执行、可独立 lint 的 TaskFoundry 题包。
`medium` 和 `guided` 必须保持与 `hard` 相同的科学目标、Ground Truth、grader、
容差、权重和交付物，只能增加经独立审核的公开先验。增加行数、收紧格式、缩小
解析容差、暴露隐藏字段或改变科学目标，都不能构成有效的难度梯度。

一个标准题族必须绑定以下 `hard` 证据链：

1. 独立 formal Reviewer `PASS`，并明确覆盖 grader、安全、来源、provenance 和
   同源科学合同；
2. 独立 Oracle 与 public-only honest solver 均得到 `1.0`；
3. 三次相互独立、无泄漏的 fresh Harbor blind 均为 `SCIENTIFIC_RESULT`，且分数
   严格低于 `0.85`；
4. 一个经独立审核、不得包含答案的 hint；使用该 hint 后的 fresh Harbor 分数不低于
   `0.85`；
5. 基于真实 trace 的 runtime closure `PASS`；
6. 独立 post-validation Reviewer 给出 `PASS_FINAL_PROGRESSION`。

成功 hint 物化为 `medium`；如果存在更晚、不同且同样通过审核的 hint，可以物化为
`guided`。每个派生层都必须独立满足：formal Reviewer `PASS`、Oracle `1.0`、
honest solver `1.0`，以及至少一次 fresh、无泄漏、分数不低于 `0.85` 的 Harbor
科学结果。派生层 formal evidence 必须声明 `derived_only_from_reviewed_hint=true`。

`FAMILY_MANIFEST.json` 通过 `scientific_contract_sha256` 绑定唯一科学合同。每一层
及其 evidence 都必须重复绑定准确的 question ID、level、package digest 和科学合同
digest。Oracle 与 honest solver 必须是两份独立 evidence，不能用同一路径或单一
“验证通过”字段替代。

每个题包根必须包含 canonical JSON `SCIENTIFIC_CONTRACT.json`，且字段恰好为：
`schema_version`、`ground_truth`、`grader`、`tolerance`、`weights` 和
`output_contract`。前三类文件集合都用题包内相对路径和 SHA-256 绑定；validator
会重新读取每个普通文件、复算摘要，再把实际 GT、grader、容差、权重和输出合同编码
为 canonical JSON 计算 `scientific_contract_sha256`。manifest 或 Reviewer 自报的
“same contract”布尔值不构成证据。`hard`、`medium` 和可选 `guided` 的实算摘要必须
完全相同。

整个题族内的 Harbor 执行身份必须全局唯一。`job_id`、`trial_id`、`sandbox_id` 和
`session_id` 四个字段分别执行唯一性检查，不能通过只替换四元组中的另一个字段绕过。
每条科学结果还必须绑定原始 Researcher request 和状态为 `CONSUMED` 的 capability；
capability 必须绑定 request 的实际文件 SHA 和指定 Researcher thread。blind request
的 `context_digests` 必须严格为空，三个 blind 的 `attempt_index` 必须依次为 1、2、3。

Hint evidence 只使用正式 workflow 可生成的 `mode=hint`，不定义 publication-only 的
`mode=derived`。第一条 hint 的 `hint_index=1`、`parent_hint_sha256=null`；若发布
`guided`，其 hint 必须为 `hint_index=2`，且 parent 精确等于第一条 hint 的 SHA。
review seal、hint result、Researcher request 和派生层 Harbor 结果必须对上述索引、父链
和 context digest 给出一致绑定。`medium` 对第一条成功 hint 的引用不是新执行，不得
重复计数。

### Runtime closure 的实质复验

`hard.runtime_closure` 不是一个 `trace_backed=true` 声明。它必须链接并封签以下真实
文件，发布 validator 会逐项重新解析和验证：

1. schema-v2 `ImageSealPlan`，其 scientific trace、冻结 package、基线镜像和所有
   delta receipt 均通过 TaskFoundry runtime contract；
2. schema-v2 `EnvironmentReceipt`，生命周期为 `STABLE`，且精确绑定同一 closure 和
   environment manifest 字节；
3. `ENVIRONMENT_READY` manifest，其中 image 必须 `immutable=true` 并带完整
   `sha256:<64 hex>` digest，镜像 provider、record、URL 和 digest 与 receipt 一致；
4. manifest 精确绑定的 dependency lock；
5. 至少两个不同 clean sandbox 的验证记录。每个 sandbox 都要通过正向探针和五类
   harness-neutral 负向扫描，且不得在验收时执行安装或上传公开资产。

closure、receipt、manifest、lock 或 clean-sandbox evidence 任一字节漂移，或者仅提供
旧 schema-v1 Stable receipt，都不得发布。

任一 fresh blind 分数达到或超过 `0.85`，该 revision 即为 `TOO_EASY`。Teacher
只能在科学题材仍有效的前提下重新设计难度并重跑整组三盲；若来源、Ground Truth、
科学目标或根方法选择合同失效，则必须放弃整个题材。

## Q1/Q2 窄范围历史兼容

Q1 和 Q2 是唯一允许使用 `publication_mode: grandfathered-v1` 的题号。这个例外
只解决发布结构迁移，不重新解释历史 blind、hint 或 progression evidence，也不能
推广到 Q3–Q32。

历史兼容题族必须且只能包含：

```text
<family-slug>/
├── FAMILY_MANIFEST.json
└── legacy/
```

`legacy/` 须保持所有者确认时的历史字节，digest 精确绑定且无 symlink 或特殊节点。
当前 TaskFoundry lint 不得作为追溯门重新判定 Q1/Q2；新 lint 只约束 Q3–Q32 和
祖父条款之后产生的新题包。
manifest 必须绑定 canonical JSON 的 `grandfathered-completion` evidence；该 evidence
必须包含 `PASS_GRANDFATHERED`、`owner_attested=true`、非空
`historical_policy_version`，以及至少一个合法的历史 evidence SHA-256。这个分支不
接受 `hard`、`medium`、`guided` 或额外目录，也不允许 Q3–Q32 使用。

## Evidence 与晋级

所有绑定 evidence 都必须是 canonical JSON：UTF-8、对象键排序、紧凑分隔符、无
重复键、无 NaN/Infinity，且文件末尾恰好一个换行。标准 evidence 必须声明
`evidence_type`、准确的 `package_sha256`、question、level 和科学合同；历史兼容
evidence 使用上述窄身份字段。只有路径和文件 hash 相符、但缺少语义 verdict 的
文件，不能通过门禁。

promoter 与 publication checker 必须调用同一个共享语义 validator；JSON Schema
只是该实现的声明式镜像，不能形成第二套更宽松的判断逻辑。

开发和发布路径为：

```text
workbench -> minimal preflight -> Harbor 难度验证
          -> reviewed hint progression -> trace-backed runtime closure
          -> formal/final review -> 原子晋级 new-question
```

未完成或失败材料必须 append-only 保存在
`TaskFoundry/archive/question-authoring/qNN/`；整个 `archive/` 目录必须保持 Git
忽略，因为其中可能包含 private reference、grader、solution 和执行 trace。

发布字节不可原地修改。发现缺陷时，应撤销该发布层并归档，禁止在已发布目录内修补。
