# TaskFoundry 仓库瘦身 v1

## 目标

ADR 0008 已取代首次解题前的 evidence、Formal Reviewer、health、post-validation 和预配置环境流程。仓库中残留的公开命令、Workflow 方法、旧迁移器、示例与自测会继续误导执行者，因此本轮直接删除这些表面，不保留兼容备份。

本轮不删除 `runs/`、scheduler 状态、Harbor queue 状态、最终题目 trace、已脱敏 Harbor 示例、当前 Skill Bank、当前发布工具或有效 ADR。

## 唯一维护流程

```text
resolve outline Skill
  -> 最新 QuestionDesignBrief.json/.md
  -> resolve author Skill
  -> author
  -> freeze-and-start-validation
  -> record-validation-round
  -> runtime finalization
  -> publish high/medium/low
  -> batch Skill reconcile
```

已删除的旧接口包括 `migrate-task`、`attach-design-evidence`、`freeze-package`、`accept-health`、`start-blind`、`audit-researcher`、`accept-post-validation` 及其 Workflow、示例、迁移脚本和专属测试。旧 `health.py`、题目历史复制器及其事务层也已删除；历史 run 本身仍可只读检查。

## 安全与恢复

瘦身不修改运行状态。当前冻结/会话原子事务、CapabilityStore、canonical Harbor evidence importer、runtime closure 和 Stable 环境绑定保持不变。源码可通过 Git 恢复，运行证据不依赖被删除的迁移工具。

## 验收

- 全套 pytest：通过。
- statement/branch combined coverage：90%，且 branch coverage 达到 80%。
- Ruff、compileall、`git diff --check`：通过。
- large-code checker：0 error、10 个人工 review 项。逐项复核结论：CLI parser 只是声明式注册；Harbor importer、Skill resolve、session reserve 与 Workflow 长函数各自保持一个事务或一个领域转换，没有混入第二责任。
- 被删除符号和脚本不存在现行调用者。

## 已确认决策

无开放决策。用户明确要求永久删除无用代码和缓存，不保留备份，同时保留 `/personal/TaskFoundry/runs/`。
