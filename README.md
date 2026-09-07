# TaskFoundry

TaskFoundry 是一套面向科研问题的题目生产与验证框架。它把研究材料、题型规范、题目设计、参考答案、评分器和真实盲解串成一个可追踪、可恢复的闭环。

项目的目标不是生成一段看起来像题目的文字，而是稳定地产出满足以下条件的科学任务：解题者只凭公开题面和公开资源即可诚实完成；任务要求真实的科学判断，而不是查表、猜标签或利用文件命名；参考结果和评分器可以独立重算、复核和回放；难度来自方法选择、证据筛选、泛化或推理，而不是缺包、环境故障或题面歧义；每次出题、解题、提示、失败归因和版本变更都有可审计记录。

本项目采用 [Apache License 2.0](LICENSE)。公开仓库只保存框架源码、测试、规范说明和经过脱敏的示例，不保存凭据、隐藏答案、私有 reference、实时运行目录或完整 Harbor 日志。

## 一、整体流程

### 1. 输入整理与题纲生成

输入可以是多道已有题目，也可以是研究课题、论文集合、知识图谱、软件工具链或公开数据集。Teacher 首先确定研究目标和可验证的结果，再生成 `QuestionDesignBrief`，明确科学目标、候选方法族、输入输出、训练证据、未见工况留出、承重与干扰证据、Ground Truth、评分里程碑、提示策略和运行预算。

每道题的最新版题纲保存在题号根目录：

```text
/personal/codex-workspace/question-from-questions/<题号>/
  QuestionDesignBrief.md
  QuestionDesignBrief.json
```

根目录只保留这两份最新版文件；旧版本进入该题的 `trace/authoring/`。

### 2. 加载题型 Skill

题纲确定后，Teacher 根据角色、题型、阶段和输入 profile 解析 Skill Bank。方法选择题使用 `role=teacher`、`task_type=method-selection`、`stage=outline|author` 和 `profile=multi-question-input`。

当前 Skill Bank 使用自包含 Skill Bundle。一个 Bundle 内直接包含完整 Execution Skill、经验卡和 manifest，不依赖项目外的规则文件。`.skillbank/stable.lock` 固定当前版本，运行中的批次不更换版本；下一批任务重新 resolve 时才使用新稳定版本。

Skill 负责通用出题原则、质量底线和安全合同。领域知识、特定数据源和局部失败经验作为 Experience Card 进入 Bundle。每道题都记录实际使用的 Skill Bundle、稳定锁快照和输入哈希。

### 3. 正式编题

Author 依据已确认题纲构建最小可运行题包，通常包括 `instruction.md`、`environment/`、`resources.json`、`solution/`、`tests/` 和 `task.toml`。

`resources.json` 只记录题目实际关联的外部科学资源：论文使用 `paper`，软件使用 `tool`，公开数据集使用 `dataset`，具有外部身份的模型使用 `model`。镜像、Python、shell、运行时依赖和题目自制数据不作为关联资源列入。

正式题包完成后先做最小结构检查和 `tests/test.sh --probe`，确认题面、资源入口、输出目录和 verifier 能启动，然后进入 Harbor 验证。环境是运行能力记录，不替代题目科学验证。

### 4. Harbor 盲解与线性会话

同一冻结 revision 使用一个 Researcher 会话，最多线性运行三轮 blind：第二轮能看到第一轮记录，第三轮能看到前两轮记录；每轮仍由 Harbor 创建新的 job、trial 和 sandbox。

任一轮达到 `0.85` 就停止该 revision，判为 `TOO_EASY`。需要增加难度时，关闭旧会话，创建新的兄弟 revision 和新的空 Researcher 会话，从 blind-a01 重启。

三轮均未达到通过线时，才进入提示验证。提示由 Teacher 提供，不能直接泄露答案，并按里程碑顺序解除真正瓶颈。判断题目是否可解时，必须结合 trace，不能只看分数升降。

平台、Harness、环境、资源传输和 verifier 启动失败属于非科学失败：保存证据、修复运行层并重试同一科学轮，不增加科学盲解计数，也不能把平台失败当成题目难度证据。

### 5. 评分与难度迭代

评分主要落在隐藏科学结果，同时保留可独立核验的中间里程碑，包括证据解释、方法排除、参数辨识、数值预测、物理或统计约束以及最终决策。

评分应形成能力梯度，避免低分长期不变后突然跳到满分。若 trace 显示跳变来自评分断崖、单字段或答案级提示，应在新 revision 中修复。最终题目最多保存 `high`、`medium`、`low` 三个版本：

```text
question-pack/
  high/
  medium/   # 可选
  low/      # 可选
```

难度版本保持同一科学数据、Ground Truth、grader、容差和权重，只增加不泄露答案的提示。

### 6. 发布与持续记录

发布记录至少包括最终题包 SHA-256、`resources.json`、Skill Bundle 和 Experience Card 版本、Teacher/Researcher Profile、逐轮 Harbor trace、verifier 结果、失败归因以及最终难度版本。

题号目录中的 `question-pack/` 只保存成功发布的版本；失败候选、实验输出和私有答案只能保存在 `trace/authoring/` 或受控运行目录。

## 二、可跨领域复用的部分

QuestionDesignBrief 结构、Skill resolve、stable lock、公开/私有隔离、题包和资源清单、Ground Truth ledger、独立交叉复算、grader 的 fail-closed 校验、Harbor 盲解与提示、失败归因、版本冻结、难度变体和 Skill Bank 迁移回归都属于通用骨架。

## 三、必须按领域定制的部分

领域定制集中在候选方法族、数据表示、单位和边界、Ground Truth 生成、误差与成本指标、物理或统计约束、条件性干扰证据、提示顺序、计算资源和精度要求。定制不能把方法标签写进公开元数据，也不能把一次性经验当成通用规则。

## 四、不同输入如何适配

研究课题先拆成研究目标、候选路线和可测结果；知识图谱先识别实体、关系和可验证查询；论文、软件和数据集组合则建立来源角色图，明确每个来源在模型、数据、评价或设计变量中的作用。

新输入类型可以先用临时 Experience Card 试运行；只有在多个任务中出现稳定、可迁移规律并通过回归评测后，才升级为稳定 Skill。

## 五、Skill Bank 的演化

每次失败先归因为平台、环境、Harness、资源、verifier、科学结果或 Skill 问题。只有 `skill_noncompliance` 或 `skill_knowledge_gap` 才可能进入演化；至少两个同机制案例形成 Failure Pattern 后生成 Candidate Lesson。候选必须通过原失败修复、未见同类任务迁移和冻结回归三类评测，全部通过后才更新 stable lock，失败候选只保留在 Evolution Memory。

运行中的批次固定 Skill 版本，动态迭代发生在批次之间，以保证每次实验可复现。

## 六、Q8 示例：Airfoil 鲁棒交叉验证路线选择

Q8 以翼型自噪声和局部工况迁移为科学背景，要求在同一研究目标下比较不同回归路线，并在未见物理族上进行预测和策略选择。题面包含整族留出、局部支持、条件数、残差稳定性和跨工况迁移等证据，同时保留具有科学合理性的条件性干扰路径。

Q8 展示了公开资源与私有评分数据分离、候选方法真实比较、完整物理族留出、独立 grader、资源说明、环境入口以及分层 trace 的组织方式。最终状态以对应 revision 的独立验证证据为准，不能以一次本地 prototype 代替 Harbor 结论。

## 七、项目目录

```text
TaskFoundry/
  src/taskfoundry/       # 编排、状态机、Harbor、发布与恢复
  tests/                 # 单元、合同和集成测试
  docs/                  # 项目说明、设计记录和 ADR
  policies/              # 发布与任务合同
  runtime/               # 可复用运行时描述
  smoke/                 # 最小启动和能力探针
  examples/              # 脱敏 Harbor trace 示例
  skill.md               # Agent 执行层入口说明
```

题号目录位于 `/personal/codex-workspace/question-from-questions/<题号>/`；运行目录、失败历史、私有答案和完整 trace 受控保存在 `/personal/TaskFoundry/runs/` 或题号的 `trace/` 下。

## 八、运行与检查

```bash
cd /personal/TaskFoundry
/opt/mamba/bin/python -m pytest -q
python3 /home/codex-work/.codex/skills/engineer-large-code/scripts/check_large_code.py \
  src/taskfoundry/*.py
taskfoundry freeze-and-start-validation <run-dir> <package-dir> \
  --validation-session-id <session-id> \
  --researcher-thread-id <thread-id>
```

调度器按 owner、generation、heartbeat 和 expiry 管理 Teacher writer lease；Harbor 队列限制活动 Job 数量，并为每一轮绑定唯一请求和题包摘要。调度状态不等于科学完成状态，只有 canonical Harbor 结果和发布证据才能推进最终状态机。

## 九、已知边界

TaskFoundry 能严格约束应用层题包、状态机、输入校验和证据链，但不能把本地可写目录自动变成平台级不可伪造存储。平台网络、模型代理、LBG worker、镜像拉取和第三方资源可用性属于外部能力，必须与科学失败分开记录。

## 十、相关文档

- [项目流程说明](docs/project-overview.md)
- [TaskFoundry Agent Skill](skill.md)
- [方法选择发布合同](policies/question-types/method-selection/README.md)
- [最终题族发布规范](policies/publishing/final-question-family-v1.md)
- [Artifact Contract](policies/artifact-contract-v1.md)
- [架构决策记录](docs/decisions/)
- [Harbor 轨迹示例](examples/harbor-trajectory/q10-survival-a01/)
