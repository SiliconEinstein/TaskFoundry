# TaskFoundry

## Language

**Resource Catalog**:
最终题包中的 `resources.json`，只列出题目实际关联的外部科学资源。
_Avoid_: Environment manifest, dependency list

**Paper**:
与题目设计或科学方法直接相关且可追溯的论文；资源类型写作 `paper`。
_Avoid_: Literature

**Tool**:
题目实际关联的公开科学软件。
_Avoid_: Runtime, Python, shell, image

**Dataset**:
具有公开外部身份、可公开取得且被题目实际使用的数据集。
_Avoid_: Generated data, calibration fixture, private reference

**Model**:
题目实际关联且具有可追溯外部身份的模型。
_Avoid_: Runtime component

## Campaign execution

**Scientific Round**:
一次确实建立 Researcher 科学执行并产生权威 verifier 结果的验证轮；平台、Harness、环境和证据故障不属于科学轮。
_Avoid_: Harbor retry, sandbox attempt

**Runtime Attempt**:
为完成同一个 Scientific Round 而启动的一次 Harbor 运行；失败后可以换用全新执行身份重试，但不能增加科学轮计数。
_Avoid_: Scientific attempt

**External Wait**:
执行所依赖的外部能力暂不可用，且已经记录恢复条件和下次探测时间的非终态；它不占 Teacher 槽。
_Avoid_: Running, blocked without recovery condition

**Topic Abandonment**:
Teacher 根据科学证据确认当前题材不能满足出题合同后形成的题材终态。
_Avoid_: Platform failure, timeout

**Skill Attribution**:
Teacher 对一个 revision 终局原因的结构化声明；只有 `skill_noncompliance` 和 `skill_knowledge_gap` 可以进入 Skill Bank 演化。
_Avoid_: Raw failure log, platform diagnosis
