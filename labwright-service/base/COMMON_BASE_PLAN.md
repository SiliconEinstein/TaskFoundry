# Q18–Q22 公共基础层计划

状态：`AUDITED_NOT_BUILT`。本文是可恢复构建输入，不是镜像 manifest 或 Stable 回执。

## 固定身份

- 镜像：`dp-harbor-registry.cn-zhangjiakou.cr.aliyuncs.com/public/paper2arm-env:v1.0-20260708`
- digest：`sha256:e56af8bcfc37be6ee859a7867ad2a3a941f9b576445a56378c6a4a5a017b57d2`
- 平台：`linux/amd64`
- 工作目录：`/app`
- 基础镜像既有 harness：Codex、Claude；只验证、不得重装或回写。
- DSH：不安装、不要求、不作为环境验收项。

## 公共职责

公共层只负责固定基础镜像身份、创建 `/app/public_data` 与 `/app/outputs` 的目录合同、
保留基础镜像既有 harness，并提供 POSIX shell、Python 和摘要探针。科学 Python 包与公开
资产全部留在题目 delta 中，避免 Q18 的旧版依赖组污染 Q21/Q22 的新版依赖组。

最终镜像必须完整包含科学依赖和公开数据。运行时不得安装科学包、上传公开数据或把
临时 sandbox snapshot 回写成环境。配置时间不计科学一小时。

## 负向边界

负向扫描中的“harness-neutral”统一解释为：不得加入 DSH、凭证、harness 配置、轨迹或
历史 session residue；不得删除或把基础镜像既有 Codex/Claude 误报为泄漏。任何 checker、
隐藏 Ground Truth、private reference、producer、solution、hint 与 API 凭证均不得进入
构建上下文。

## 发布门

1. 使用真实构建后端解析上述 tag 到同一 digest；tag/digest 不一致立即阻断。
2. 各题 delta 先闭合精确依赖、公开资源路径/大小/SHA-256 与 smoke checks。
3. builder 验收后，从不可变候选镜像启动至少两个不同 clean sandbox；不得执行科学装包
   或公开数据上传。
4. 只有真实 provider record、不可变 image URL、manifest 和双 clean-sandbox evidence
   全部存在时，才调用 TaskFoundry `import-environment`，使用发布围栏生成真实回执。
5. 本检查点不创建 runtime delta request；当前没有 Researcher request/sandbox，因此不得
   调用 `labwright-claim-delta` 或伪造同沙盒 delta receipt。

## 当前基础设施阻断

当前 Desktop 沙盒没有 `docker`、`podman`、`buildah`、`skopeo`、`crane` 或 LBG 构建
客户端，也没有可见的 registry/LBG 构建凭据名称。因此可以完成审计与依赖解析，不能在
本端构建、推送或取得 provider record。阻断层级为外部镜像构建能力/凭据，不是科学题目。
