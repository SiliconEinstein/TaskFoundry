# Paper2Task Harbor 题包固定规范 v1

本规范适用于所有题型。

## 必需文件

最终题包必须包含 `instruction.md`、`task.toml`、`environment/`、
`environment/resources.yaml`、`solution/solve.sh` 和 `tests/test.sh`。

`instruction.md` 必须说明目标、输入、输出、约束和可验证验收标准，并指向
`resources.yaml`。它不得泄漏隐藏数据、阈值、参考值或标准答案实现。

`task.toml` 必须声明唯一题目名称、可拉取的不可变预构建镜像、真实绝对工作目录、
资源上限以及 Agent/Verifier 超时。Harbor-LBG 使用 `schema_version = "1.3"`。
新题目标解题时间不超过 1800 秒，Agent 硬超时固定为 3600 秒。

`environment/` 是 Agent 可见的初始工作目录。它不得包含 Dockerfile、Compose、
标准答案、检查器、隐藏参考、提示或凭证。`resources.yaml` 必须完整声明捆绑、
外部和预安装资源。凭证只能引用环境变量名称。

`solution/solve.sh` 必须确定性、非交互，并且只使用公开资源证明题目可解。
`tests/test.sh` 必须在成功和失败路径都输出有限 reward，并评价科学结果而非某个
固定实现。

## 发布门

题包必须通过结构检查和可见性检查；Oracle 必须满分；空提交和代表性错误提交
必须低分；至少一次真实 Researcher 尝试必须通过 Harbor 沙盒运行。最终完成条件
由锁定的验证规范决定，不能只看 Harbor 进程是否成功退出。
