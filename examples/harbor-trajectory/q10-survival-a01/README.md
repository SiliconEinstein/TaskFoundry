# Q10 survival estimator：Harbor 轨迹示例

这个目录展示一次成功的 fresh Harbor blind solve。Researcher 在独立 Agent 沙盒中
完成题目，随后由独立 Verifier 沙盒评分。

公开内容：

- `trajectory.json`：Harbor Agent 的结构化轨迹；
- `verifier/test-stdout.txt`：公开的汇总评分；
- `verifier/reward.txt`：Harbor reward。

本示例没有复制原始 JobConfig、lock、receipt、sandbox 标识、内部绝对路径、代理配置、
私有 reference 或完整日志。发布前对这些文件执行了密钥名、Bearer token、私钥、邮箱、
内部路径和代理字段扫描，未发现命中。

## 结果

- cases：72
- decision accuracy：1.0
- numeric accuracy：1.0
- reward：1.0

## SHA-256

```text
af385ce7869a3541cadd37bb5c45a496ba80a4f9b2ccfcffc8cd4f6c7b9d6d80  trajectory.json
cbae71f665920c32108b49c7251023cd1f719249f9e18a4a746c7a8f2f7aeac7  verifier/test-stdout.txt
bbc73d9f627fa3fab24003c876ddd2f0c3ad55a8869cc0910b89ed5511013bc5  verifier/reward.txt
```
