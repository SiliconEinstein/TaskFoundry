---
name: taskfoundry-question-author
description: 按 TaskFoundry 当前代码把一道人选题从大纲推进到可发布题包，并完成 Harbor 线性验证、难度迭代、资源登记和可恢复收尾。
---

# TaskFoundry 单题出题执行 Skill

本 Skill 是一份可执行 runbook。读取后，Agent 必须按下面的顺序操作；不得把“写出题包”“本地测试通过”或“Reviewer 通过”误报为题目完成。所有状态以 TaskFoundry 的运行快照、事件和 Harbor 原始回执为准。

## 0. 固定边界

- 项目代码：`/personal/TaskFoundry`。
- 题目根目录：`/personal/codex-workspace/question-from-questions/<N>`。
- 每道题根目录只保留最新版 `QuestionDesignBrief.json` 与 `QuestionDesignBrief.md` 两份 Brief。
- 失败尝试、平台故障、提示、轨迹和归因写入题目根目录的 `trace/`；不得把失败副本写入 `question-pack/`。
- 最终交付只放在 `question-pack/high`、可选 `question-pack/medium`、可选 `question-pack/low`。等级名称只能是 `high`、`medium`、`low`。
- 每个最终等级包必须有 `resources.json`。它只登记关联的 `paper`、`tool`、`dataset`、`model`；镜像、Python、shell 等运行时不写入该文件，自制数据集不登记为公开 dataset。
- 题型规则的权威来源是 `/personal/codex-workspace/question-from-questions/QUESTION_DESIGN_RULES.md` 及本题型的 SkillFoundry Execution Skill；不要新造并行 policy 文件。
- Harbor 失败、环境缺失、Harness 失败、网络/凭据失败和 verifier 失败都先归因并保留证据，不得伪造科学结果，也不得用平台失败修改题目科学内容。

## 1. 会话启动与知识冻结

先确认题号、题型、Profile、当前 run 和是否已有恢复点。每一批开始前只执行一次 SkillFoundry resolve；outline 与 author 必须分别 resolve，并把返回的自包含 Skill Bundle、stable-lock 快照和任务输入保存到：

```text
/personal/codex-workspace/question-from-questions/<N>/trace/authoring/skill-activations/
/personal/codex-workspace/question-from-questions/<N>/trace/authoring/prompts/
```

实际入口：

```bash
cd /personal/TaskFoundry
taskfoundry resolve-teacher-skill <N> outline <attempt-id> \
  --batch-id <batch-id> --batch-question <N> [--task-input <input>]
taskfoundry resolve-teacher-skill <N> author <attempt-id> \
  --batch-id <batch-id> --batch-question <N> [--task-input <input>]
```

若 resolve 失败，状态是阻塞，不得绕过 bundle 哈希、手改 stable lock 或继续写题。

## 2. Outline 阶段

每条命令前先执行 `taskfoundry status <run-dir>`，按当前状态恢复，而不是从头重放。状态已经是目标阶段时跳过该阶段的进入命令；只有状态机允许的前置状态才调用迁移命令。特别是 `AUTHORING` 状态不要再次调用 `begin-authoring`，`BLIND_VALIDATION`/`HINT_VALIDATION` 状态不要重新 freeze；重复调用会被代码拒绝，这是幂等保护而非题目故障。

按 outline activation 生成题号根目录中的两份最新版 Brief：

```text
/personal/codex-workspace/question-from-questions/<N>/QuestionDesignBrief.json
/personal/codex-workspace/question-from-questions/<N>/QuestionDesignBrief.md
```

Brief 必须明确：研究目标、真正不同的方法族、背景信息中的承重信息/语境信息/干扰信息、输入与输出、未见工况迁移、预期难点、解题路径、GT/评分轴、成本约束、可解性条件、失败归因和 high/medium/low 难度计划。方法选择题的干扰信息必须要求 Agent 证据化排除，不能把 regime、ID、token、行序、元数据或 case 后缀直接编码成答案。

只允许保留这两份 Brief。先执行：

```bash
taskfoundry validate-question-brief <N>
taskfoundry bind-teacher-skill <run-dir> <N> outline <activation.json>
taskfoundry attach-question-brief <run-dir> <N>/QuestionDesignBrief.json
```

完成条件：JSON/Markdown 相互指认、题型校验通过、背景证据完整、outline activation 的 stable generation 和文件摘要已绑定运行快照。

## 3. Author 阶段与题包构建

先绑定 author activation，再开始 authoring：

```bash
taskfoundry bind-teacher-skill <run-dir> <N> author <activation.json>
taskfoundry begin-authoring <run-dir>
```

若 `status` 已显示 `AUTHORING`，直接继续生成/修订候选，不重复调用上述迁移；若显示 `DESIGNING` 才调用它。若显示 `BLOCKED`、`WAITING_EXTERNAL` 或 `ABANDONED`，先读取最近的恢复证据并按归因规则处理，不能强行推进。

在 `trace/authoring/` 下工作，生成最小正式候选。正式包至少应有：

```text
instruction.md
task.toml
environment/resources.yaml
solution/solve.sh
tests/test.sh
resources.json
```

按题型需要加入公开数据、独立 GT producer、独立参考复算、grader、private reference 和运行说明。解题 Agent 只能看题面、公开资源和环境；隐藏答案、private reference、grader、producer 和历史轨迹必须留在私有侧。

在题号根目录维护 `resources.json`，每项只能包含项目约定的资源字段，并逐项写清来源、版本/DOI、用途和访问方式。不要把环境镜像、解释器或通用依赖伪装成资源。

## 4. Author 自检与冻结

正式候选必须先完成本地闭环：

```bash
cd /personal/TaskFoundry
taskfoundry lint-package <candidate-package>
# Do not let unrelated site-installed pytest plugins or a missing optional
# Harbor checkout abort collection.  The project itself is imported from src.
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src /opt/mamba/bin/python -m pytest -q
```

If the full suite reports `ModuleNotFoundError: harbor`, that is a dependency
checkout/runtime problem, not evidence that the question is scientifically
bad. Record it as a platform/environment failure and run the package-local
checks that do not import Harbor; do not install arbitrary packages into the
candidate or change the scientific contract to make collection pass. The
canonical CLI is available without an installed console script as:
`PYTHONPATH=src /opt/mamba/bin/python -m taskfoundry.cli <command>`.

还必须实际执行题包自己的 `tests/test.sh`，覆盖正确答案、缺失/重复/额外 ID、重复 JSON key、布尔数值、NaN/Inf、额外文件、symlink/特殊节点和错误答案；GT 评分量要有独立复算证据。把命令、版本、摘要和失败归因写入 `trace/authoring/`。

通过自检后，必须把候选复制到不可变运行快照并启动线性验证会话：

```bash
taskfoundry freeze-and-start-validation <run-dir> <candidate-package> \
  --validation-session-id <session-id> \
  --researcher-thread-id <real-researcher-thread-id>
```

`--researcher-thread-id` 使用真正新建且未复用的 Researcher 会话；没有真实线程时先记录平台阻塞，不使用合成 ID 冒充。冻结后不得修改候选包；所有修订都必须新建 sibling revision。

## 5. Harbor 线性验证

同一题的验证是一个线性会话，不是互相独立的三题：Researcher 会话保持同一身份，第二轮可见第一轮回执/记录，第三轮可见前两轮；每一轮由 Harbor 建立新的 sandbox。Teacher 负责每轮评分和决策，Researcher 不得自评。

### 5.1 持久会话优先

使用包含 `persistent_validation` 的 JobConfig，让 Supervisor 持有进程并在同一会话中等待 Teacher 决策：

```bash
taskfoundry issue-researcher <run-dir> <request-id> <job-config.json>
taskfoundry researcher-run <handoff.json> <runtime.json> \
  --env-file /personal/TaskFoundry/.env \
  --receipt <receipt.json> --queue-root <queue-root>
```

若请求暂时排队，继续轮询 queue，不兑换新 capability、不计科学尝试。命令退出或平台错误时，先保存 stderr、request、job、trial、sandbox、session 和队列状态，标记 `PLATFORM_FAILURE`/`ENVIRONMENT_FAILURE`/`HARNESS_FAILURE`，再用 `supervisor-resume` 或同一 request 的受控恢复；不要重跑科学轮次。

### 5.2 每轮决策

每轮完成后先导入 canonical Harbor 结果：

```bash
taskfoundry record-persistent-validation-session <run-dir> <request-id>
```

若使用非持久会话，则按轮导入：

```bash
taskfoundry record-validation-round <run-dir> <request-id>
```

Teacher 必须阅读完整 trajectory 和 verifier breakdown，再执行唯一决策：

```bash
taskfoundry decide-persistent-validation-round <run-dir> <request-id> <round> CONTINUE_BLIND
taskfoundry decide-persistent-validation-round <run-dir> <request-id> <round> CONTINUE_HINT \
  --hint <non-answer-hint> --teacher-declares-non-answer
taskfoundry decide-persistent-validation-round <run-dir> <request-id> <round> STOP_TOO_EASY
taskfoundry decide-persistent-validation-round <run-dir> <request-id> <round> STOP_PASSED
taskfoundry decide-persistent-validation-round <run-dir> <request-id> <round> STOP_BLOCKED
```

规则：

1. 首轮 reward 达到题目通过线，立即停止为 `TOO_EASY`，不能为了“凑三轮”继续盲解。
2. 首轮未达线时，只有 trace 证明仍在可解方向前进，才继续 blind；平台失败不计科学轮次。
3. 三轮 blind 后，Teacher 根据 trace 的真实瓶颈增加一至两级非答案提示。提示不能泄露答案、private reference、代码或固定标签。
4. 提示是否有效必须结合轨迹、错误类型、证据链和分数变化判断，不能仅以“分数升/降”自动修改 Skill。
5. 每次需要提高难度，关闭当前会话并创建新 revision；不能在同一会话继续验证新题。

6. 提交输出前必须执行严格白名单核对：`/app/outputs` 只能包含题面列出的文件，禁止额外的汇总、调试、缓存或临时文件。即使额外文件内容正确，verifier 也应按合同拒绝；Researcher 必须在 trace 中完成目录清单核对后再提交。

## 6. 难度、发布与 resources

验证成功不是“盲解一轮满分”，而是完成 difficulty progression：

- high：目标难度的初始题包。
- medium：只追加实际使用、由 Teacher 声明为非答案的固定提示。
- low：在 medium 基础上追加下一条非答案提示。

只有 `TOO_EASY` 才创建难度 revision；每个 revision 都记录原因、原始 trace、提示和新 digest。题目完成后，把成功题包和验证证据发布到：

```text
/personal/codex-workspace/question-from-questions/<N>/question-pack/high/
/personal/codex-workspace/question-from-questions/<N>/question-pack/medium/
/personal/codex-workspace/question-from-questions/<N>/question-pack/low/
```

最多三个等级，允许只有 high/low。发布前检查目录只能有允许等级，并且每个等级都有 `resources.json`、不可变文件、正确权限和完整哈希。

## 7. 环境固化与完成条件

平台只提供冻结的基础镜像。题目是否完成由真实 Harbor 盲解、Teacher 评分与发布证据决定；Labwright 镜像构建是可选的复用/加速步骤，不是 Harbor 或发布前置门。环境失败必须记录并归因为平台/环境，不得改写科学结论。

典型收尾顺序：

```bash
taskfoundry begin-runtime-finalization <run-dir>
taskfoundry complete-without-runtime-environment <run-dir> <optional-status.json>
taskfoundry accept-runtime-closure <run-dir> <closure.json>
taskfoundry bind-runtime-environment <run-dir> <environment-receipt.json>
```

完成条件必须同时满足：题包 digest 冻结；至少三轮有效、独立、无提示 Harbor blind 或按难度合同完成提示 progression；每轮有 canonical request/job/trial/sandbox/session/result/trajectory；GT、grader、资源和 `resources.json` 闭合；最终题包已放入 question-pack；Skill activation、Bank Snapshot、Experience Cards、Teacher/Researcher Profile 与归因已封签。若没有 Labwright closure，保存 `OPTIONAL_UNAVAILABLE`/`OPTIONAL_DEFERRED` 证据即可；若有 closure，则必须校验其绑定，但两者都不改变科学完成判定。

## 8. Skill Bank 归因与迭代

每次失败先写归因，不直接改 stable Skill：

- 平台/环境/Harness/资源/verifier：只记证据和恢复结果。
- Teacher skill 不合规或知识缺口，且失败属于 informative/boundary 难度：记录 Experience Card 候选。
- 科学结果、题目太简单、答案错误或单个偶发失败：进入本题 trace，不自动升级 Skill。

批次结束后统一执行：

```bash
taskfoundry record-skill-attribution <N> <evidence.json>
taskfoundry record-revision-attribution <N> <evidence.json>
taskfoundry reconcile-skill-bank <batch-id> --question <N>
taskfoundry close-skill-batch <batch-id> --question <N>
```

只有至少两个同机制案例形成稳定 Failure Pattern，且候选同时通过“修复旧失败、迁移到新任务、冻结回归集不退化”三类评测，才能：

```bash
taskfoundry evaluate-skill-candidate <plan.json> <verdict.json>
```

评测通过后由 SkillFoundry 更新 stable.lock；运行中的 batch 不得换版本，下一批才重新 resolve。Skill Bundle 必须自包含，不能通过外部 RULE_SOURCES 或固定项目路径补齐规范。

## 9. 中断恢复硬规则

任何命令、线程、Harbor job 或 Agent 中断都必须留下一个可读的 JSON 证据，至少包含：阶段、run、revision、package digest、request/job/trial/session、最后事件、错误层级、是否计科学轮次、下一步和幂等键。恢复时先读取该证据和 `taskfoundry status <run-dir>`，再从最后一个已提交事件继续。

禁止：重用已兑换 capability；把 `ACTIVE` scheduler 字段当作进程存活；把平台错误计为科学失败；修改冻结包后继续同一验证；把旧题包、失败副本、缓存写进最终 `question-pack`；用 Teacher 自己运行的结果冒充 Harbor fresh blind。

## 10. 单题最终清单

在报告“题目完成”前逐项核对并保存清单：

```text
[ ] outline/author Skill Bundle activation 与 stable-lock 摘要已保存
[ ] QuestionDesignBrief.json + .md 恰好两份且为最新版
[ ] 题型是真正方法选择，存在异构方法族和未见工况迁移
[ ] 公开干扰信息不能查表恢复标签
[ ] candidate 的 GT、grader、资源、权限和哈希闭合
[ ] resources.json 只含 paper/tool/dataset/model
[ ] question-pack 只有 high/medium/low 允许等级
[ ] Harbor 线性会话、每轮 trajectory 和 Teacher 决策齐全
[ ] platform/environment/harness/verifier 失败已正确归因
[ ] 难度 progression 按 trace 决定，未因满分继续盲解
[ ] 运行时固化状态已记录；若不可用，明确标记为可选未提供
[ ] Skill Bank 已 reconcile；候选 Skill 未通过评测则未进入 stable
[ ] 最终完成记录写入 trace/final，且 question-pack 未混入失败产物
```

若任一项不能证明，状态只能是 `AUTHORING`、`BLIND_VALIDATION`、`BLOCKED` 或 `WAITING_EXTERNAL`，不能写 `COMPLETE`。
