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
