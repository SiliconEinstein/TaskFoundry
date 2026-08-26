在 `/app/outputs/overlay-smoke.txt` 写入且只写入一行：

```text
portable codex overlay ok
```

不要修改 `/app/public_data`，不要安装软件包。

另外创建 `/logs/artifacts/overlay-bootstrap.txt`，内容同样为
`portable codex overlay ok`。这是 Harbor 0.18 separate-verifier 对默认工件目录的
非空传输要求，不属于题目输出。
