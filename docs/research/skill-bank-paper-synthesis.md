# Skill Bank：从失败中学习，但不让失败直接改写能力

> 研究底稿。本文只使用论文、论文作者仓库和当前实现代码等一手来源。论文实验结论只适用于各自的实验域，不能直接视为 SkillFoundry 的效果证明。

## 结论先行

Skill Bank 的新意不在于“让 Agent 自动写 Skill”。Agent 根据执行反馈修改 Skill 已是常见做法。我们要解决的是后续治理问题：一次失败是否真的暴露了 Skill 缺陷，局部经验应写成一张可检索卡片还是改造完整 Skill，候选版本是否能迁移到新任务，以及运行时如何准确复现当时使用的能力版本。

两篇参考论文正好补上了两个不同的环节：

- **SESA 负责产生高信息密度的练习和轻量经验**：让任务靠近当前能力边界，将可利用的失败蒸馏成可检索的 Skill Card，再让更新后的记忆影响下一轮任务与求解。
- **VeriSkill 负责控制什么可以进入稳定能力**：先归因、再按失败模式抽象，最后以原失败修复、同模式迁移和独立验证决定候选 Skill 是否准入。

因此，我们的框架不是把两套代码拼起来，而是将其组合成一条更完整的链路：**任务前沿提供学习信号，失败归因过滤噪声，双层知识承载不同粒度的经验，可执行验证控制晋级，Git 快照保证复现和回退。**

## 1. 从 VeriSkill 吸收的思想：失败不能直接成为修改指令

VeriSkill 面向程序验证提出三阶段演化流程。其价值不在特定验证工具，而在于它把“看到失败就改 Skill”改造成一套有门槛的准入过程。[论文 §4](https://arxiv.org/html/2607.27733v1#S4)

### 1.1 先判断责任是否属于 Skill

VeriSkill 将失败区分为任务不可满足、证明器能力限制、Agent 未遵循已有 Skill、Skill 缺少必要知识。只有后两类进入演化。这一划分指出了一个容易被忽略的问题：任务有错、环境不完整、工具能力不足和 Agent 偶发失误，都可能表现为“任务失败”，但修改 Skill 未必能修复它们。[论文 §4.1](https://arxiv.org/html/2607.27733v1#S4.SS1)

SkillFoundry 将这一思想泛化为 `Run Evidence` 和 `Failure Attribution`。目前只有 `skill_noncompliance` 与 `skill_knowledge_gap` 会进入候选经验生成；TaskFoundry 示例适配器会把环境、Harness、平台及超时类失败留在忽略证据中，不让它们生成候选 lesson。[SkillFoundry contracts](https://github.com/starbilibili/skillfoundry/blob/e03f591768bf60efe1b7b771652ce40ba7c361ea/src/skillfoundry/contracts.py) [TaskFoundry evidence adapter](https://github.com/starbilibili/skillfoundry/blob/e03f591768bf60efe1b7b771652ce40ba7c361ea/examples/taskfoundry/export_evidence.py)

### 1.2 从失败模式抽象经验，不从单次轨迹复制答案

VeriSkill 为失败提取 diagnostic signature，包括失败发生在哪个环节、缺少什么可复用责任，以及 Skill 文本通过何种机制导致失败。它先将同构失败聚类，再生成带有适用条件、具体步骤和不适用情形的候选 lesson；任务 ID、文件名、变量名和常量等实例细节必须移除。[论文 §4.2](https://arxiv.org/html/2607.27733v1#S4.SS2)

SkillFoundry 当前默认要求至少两个具有相同诊断签名的、可演化失败形成 `Failure Pattern`，再生成 `Candidate Lesson`。这是一个保守的 MVP：它降低了把单题偶然现象升级成通用规则的概率，但目前仍是确定性字段聚类，失败归因由项目侧提供，还没有内置语义聚类或归因模型。[SkillFoundry reconcile](https://github.com/starbilibili/skillfoundry/blob/e03f591768bf60efe1b7b771652ce40ba7c361ea/src/skillfoundry/reconcile.py)

### 1.3 候选版本必须修复、迁移且不回退

VeriSkill 不把 lesson 机械追加到原 Skill，而是将其受控整合为完整候选版本。候选先在导致本次修改的 attribution cases 上验证，再在未参与经验抽象的同模式 transfer cases 上验证；通过后才进入冻结验证集。论文要求候选提高验证成功率，同时保持程序语义。[论文 §4.3](https://arxiv.org/html/2607.27733v1#S4.SS3)

我们把“程序语义保持”泛化为项目自己的回归与不可破坏约束。SkillFoundry 的 `Evaluation Plan` 固定当前基线、候选摘要、归因案例、迁移案例和回归集；只有三个门全部通过，`stable.lock` 才原子前移。过期基线、内容摘要漂移和任一门失败都会拒绝晋级。[SkillFoundry evaluation](https://github.com/starbilibili/skillfoundry/blob/e03f591768bf60efe1b7b771652ce40ba7c361ea/src/skillfoundry/evaluate.py)

论文消融实验支持这三道门在程序验证域的重要性：以 Dafny、Claude Code 和 Opus 4.8 的设置为例，完整方法 PASS 率为 57.3%，移除归因、经验抽象和可执行验证后分别下降 13.0、8.3 和 21.3 个百分点。这个结果说明论文机制值得借鉴，但不构成我们在出题或其他项目上的性能承诺。[论文 §6.2](https://arxiv.org/html/2607.27733v1#S6.SS2)

## 2. 从 SESA 吸收的思想：让任务前沿与外部记忆共同演化

SESA 原本是搜索 Agent 的自博弈训练框架。Challenger 出题，Solver 解题；只有 Solver 可以检索 Skill。Solver 的失败被蒸馏进 Bank，更新后的 Bank 改变 Solver 的能力，又迫使 Challenger 产生新的、更难的问题。[论文 Method](https://arxiv.org/html/2607.29468v1#S4)

### 2.1 选择“困难但可利用”的失败

SESA 不奖励越难越好的问题。它惩罚成功率为 0 的不可解问题和成功率为 1 的简单问题，把训练分布推向中间成功率附近。这样产生的失败更靠近 Solver 的当前能力边界，也更可能被一条新策略修复。[论文 Frontier Shaping](https://arxiv.org/html/2607.29468v1#S4.SS5)

在我们的出难题场景中，这一思想应转化为：优先学习“难度足够、确实可解、当前 Agent 仍暴露稳定缺口”的题，而不是从所有失败中平均取样。这里不照搬 SESA 的奖励函数；任务是否可解、是否过易以及是否位于能力边界，需要由 Harbor/LBG rollout、独立评测和题目契约共同判断。

### 2.2 将局部策略做成轻量 Experience Card

SESA 的卡片包含适用模式、常见混淆、关键区分信号、触发词、查询模板以及检索后的 helpful/hurt 统计。执行前通过 dense top-k 检索，失败后将问题、目标、预测、检索证据和已用 Skill 一起写入待处理队列；候选经过相似度去重，低效的非 seed 卡片可被淘汰。[论文 Failure Distillation](https://arxiv.org/html/2607.29468v1#S4.SS6) [SESA `skill_bank.py`](https://github.com/Zenghuang-Fu/SESA-Self-Evolving-Search-Agents/blob/master/quarl/utils/skill_bank.py)

这启发我们将知识分成两层：

- **Complete Execution Skill** 是完整的角色或题型执行规范，运行时整体加载；
- **Experience Card** 是只在特定情形下有用的局部程序性经验，从冻结的 Bank snapshot 中按上下文检索。

两层不能混为一谈。把每条局部经验都追加进完整 Skill，会让规范不断膨胀；反过来，只保存碎片卡片，又无法承载必须整体遵循的流程、契约和阶段关系。SkillFoundry 因而把两者建模为不同 artifact，并将失败模式明确指向 `execution_skill` 或 `experience_bank`。[SkillFoundry ADR](https://github.com/starbilibili/skillfoundry/blob/e03f591768bf60efe1b7b771652ce40ba7c361ea/docs/adr/0001-git-native-dual-layer.md)

需要区分论文方法与仓库原型的质量边界。SESA 代码中的 `SkillBank` 会将调用方提交的失败直接追加到队列；核心类本身不验证该失败是否真的处在能力前沿。更新时，它用“曾检索 Skill、同一 UID 重复、材料较长”等启发式排序，再由 LLM judge 为每条失败生成一张卡片。代码提供 embedding 去重、helpful/hurt 计数和淘汰，但没有 VeriSkill 式的责任归因、同模式 held-out 迁移集或冻结回归门。论文写的是每次 consolidation 最多选 30 条，代码默认 `gen_per_update=50`，实际数量还可由配置改变；因此文档不应把这些数值当作通用协议。[SESA `skill_bank.py` raw source](https://raw.githubusercontent.com/Zenghuang-Fu/SESA-Self-Evolving-Search-Agents/master/quarl/utils/skill_bank.py)

仓库的持久化也是可变的 `skills.jsonl`、step snapshot 和 `meta.json`，并非 Git-native 的候选—稳定版治理。SESA 证明了轻量经验在自博弈训练中的作用，但不能直接提供我们所需的版本准入与回退语义；这部分由 SkillFoundry 的 artifact revision、evaluation decision 和 `stable.lock` 补足。

### 2.3 从 Solver-only memory 泛化出角色隔离

SESA 只允许 Solver 看见求解记忆，Challenger 不直接观察解题策略。这不只是训练技巧，也是一条防止信息泄漏的职责边界。[论文 Asymmetric Self-Play](https://arxiv.org/html/2607.29468v1#S4.SS3)

用于出题系统时，出题者只能加载出题规范和已批准的出题经验；解题者使用解题 Bank；评测者依据独立 rubric 和 verifier 作判断。解题策略、参考答案或评测隐私不能以 Experience Card 的形式反向泄漏给出题角色。

SESA 在七个搜索问答基准上相对 SSP 的平均提升为 1.2～3.2 分，消融也显示 memory priming、frontier shaping 和 failure distillation 都有贡献；但个别数据集并非单调提升，检索也可能造成干扰。[论文 Main Results](https://arxiv.org/html/2607.29468v1#S6.SS2) [论文 Component Ablations](https://arxiv.org/html/2607.29468v1#S6.SS4) 因此，我们需要保留 no-skill 基线、可拒绝检索和候选 A/B 验证，不能假定“加载更多经验一定更好”。

## 3. 两篇论文如何合成我们的框架

SESA 回答“系统应该从哪些任务和失败中学习，以及经验如何重新影响下一轮”；VeriSkill 回答“哪些失败有资格触发修改，以及候选修改如何被证明有效”。SkillFoundry 将二者组织成以下闭环：

1. **任务接入与能力路由**：项目用 `role`、`task_type`、`stage`、`profile` 描述运行上下文，选择完整 Skill 与 Experience Bank。
2. **冻结本次激活**：解析稳定版本并校验内容 SHA-256，生成 `Runtime Activation`，记录本次任务实际加载的完整 Skill、Bank snapshot 和卡片。
3. **执行并收集证据**：保留任务、产物、验证结果、所用知识快照及指标引用，不把原始轨迹和凭证写入 Bank。
4. **失败归因与前沿过滤**：排除任务、环境和工具问题；优先处理信息性或边界失败。
5. **模式级经验抽象**：将重复的 Skill 缺口聚合为 `Failure Pattern` 与 `Candidate Lesson`，同时写明适用范围和反例边界。
6. **选择演化目标**：局部经验生成新的 Experience Bank snapshot；执行规范的结构性缺口生成完整 Skill revision。
7. **Agent 编写候选**：SkillFoundry 不替代 Agent 的内容判断，只负责将候选绑定到来源证据、当前基线和不可变摘要。
8. **三门验证**：验证是否修复归因案例、迁移到同模式新案例、通过冻结回归和项目不变量。
9. **准入与发布**：通过后原子更新 `stable.lock`；接受和拒绝的评测 decision 持久化。完整框架还应像 VeriSkill 一样记录被跳过的候选，但这些演化记录都不进入运行提示。
10. **返回下一轮**：新稳定版本改变后续出题、解题与评测表现，新的能力边界又产生下一批证据。

运行时解析与演化过程被刻意分开：设计上，Evolution Memory 用于指导候选生成和避免重走失败路线；运行时只加载已经准入的知识。这样既保留学习历史，也不会把诊断记录、失败样本和被拒绝方案一并塞进 prompt。[SkillFoundry resolve](https://github.com/starbilibili/skillfoundry/blob/e03f591768bf60efe1b7b771652ce40ba7c361ea/src/skillfoundry/resolve.py)

## 4. 在出难题流程中的当前落点

TaskFoundry 已接入 SkillFoundry 的稳定解析路径。当前方法选择题在 `outline` 和 `author` 两个阶段，根据 `teacher / method-selection / multi-question-input` 上下文同时加载一套完整的 `method-selection-teacher` Skill 和一个 `method-selection` Experience Bank。每道题、每个阶段的 activation 都写入 authoring trace；完整 Skill 的规则来源也会再次校验摘要，防止规则源漂移。[TaskFoundry integration](https://github.com/SiliconEinstein/TaskFoundry/blob/042e3df/src/taskfoundry/skillbank.py)

这证明的是**运行时封签与可复现加载已接通**。目前还不能声称完整闭环已经自动运行：TaskFoundry 的 evidence exporter 仍是示例适配器；语义向量检索、helpful/hurt 在线统计、去重与淘汰尚未进入 SkillFoundry MVP；跨项目自动迁移、在线队列和模型权重训练也明确不在当前范围。[SkillFoundry implementation plan](https://github.com/starbilibili/skillfoundry/blob/e03f591768bf60efe1b7b771652ce40ba7c361ea/docs/implementation-plan.md)

当前检索采用显式上下文匹配而不是 SESA 的 embedding top-k。这是有意选择的第一阶段：题型、角色和阶段是强约束，确定性路由更容易审计。后续若增加语义检索，应先用结构化条件缩小候选集合，再在集合内排序，并允许“没有合适卡片”的空结果。

## 5. 框架展现出的优势

### 学习信号更干净

系统只从可归因、接近能力边界、具有重复模式的失败中提炼经验，避免把环境故障、坏题或偶发执行错误固化成能力规则。

### 局部经验与完整规范各归其位

Experience Card 处理窄场景策略，Complete Execution Skill 承载必须整体遵循的流程。二者可以独立演化、评测和回退，不需要把所有知识堆进一个不断增长的 `SKILL.md`。

### 自动迭代不再等于直接覆盖

Agent 仍然负责写和改，但修改先成为候选。系统保留稳定版、候选版、来源证据、验证计划和准入决策，只有通过迁移与回归验证才晋级。

### 每次任务可复现

Git 保存普通 JSON、Markdown 和 Skill 目录；`stable.lock` 固定稳定渠道，SHA-256 校验实际内容，`Runtime Activation` 封签任务所用版本。出现退化时，可以沿 Git 历史和 lock generation 定位并回退。

### 管理机制可以跨项目复用，判断权仍属于项目

SkillFoundry 提供统一的证据、候选、评测、准入和解析协议；项目自行定义任务分类、verifier、回归集、角色、题型和不变量。这样可以复用治理机制，而不假设不同领域共享同一种“好 Skill”标准。

## 6. 证据边界与后续验证

两篇论文分别在程序验证和搜索自博弈中验证了自己的方法，尚未验证我们的组合框架。尤其需要保留以下边界：

- SESA 的模型参数内化收益依赖 on-policy RL；当前 Codex/DSH 工作流不训练权重，不能宣称同样的 parametric carryover。
- SESA 的余弦相似度只用于去重，不能证明卡片正确、有效或可迁移；质量准入应由 VeriSkill 式可执行验证承担。
- 单次 LLM judge 生成的卡片不能直接进入稳定 Bank；归因、迁移和回归门不能省略。
- Skill 不能替代底层模型、工具和环境能力。VeriSkill 的跨模型实验也显示，Skill 可迁移并不意味着较弱模型会达到相同的最终成功率。[论文 §6.3](https://arxiv.org/html/2607.27733v1#S6.SS3)
- 语义检索可能引入无关经验，必须记录检索命中与实际效果，并与 no-skill / stable baseline 对照。

下一阶段应优先补齐三件事：把 TaskFoundry 的真实执行证据稳定导出为 `Evidence Manifest`；为 Experience Card 增加项目可验证的 helpful/hurt 归因；用出题、解题和独立评测构成的冻结实验，比较稳定版、候选版与 no-skill 基线。完成这些验证后，才能对 Skill Bank 在“出难题”项目中的收益作量化结论。

## 参考文献与实现来源

1. Jia, C. et al. [VeriSkill: A Self-Evolution Framework for Program Verification Skills](https://arxiv.org/html/2607.27733v1), arXiv:2607.27733v1, 2026.
2. Fu, Z. et al. [Self-Play Meets Skill Evolution: Self-Evolving Search Agents that Pose, Solve, and Remember](https://arxiv.org/html/2607.29468v1), arXiv:2607.29468v1, 2026.
3. Fu, Z. et al. [SESA-Self-Evolving-Search-Agents](https://github.com/Zenghuang-Fu/SESA-Self-Evolving-Search-Agents), source repository, 2026.
4. [SkillFoundry](https://github.com/starbilibili/skillfoundry/tree/e03f591768bf60efe1b7b771652ce40ba7c361ea), current Git-native MVP implementation.
5. [TaskFoundry Skill Bank integration](https://github.com/SiliconEinstein/TaskFoundry/commit/042e3df), runtime integration commit.
