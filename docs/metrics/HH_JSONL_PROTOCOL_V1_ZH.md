# HH Agent JSONL Protocol v1

该接口是 NiceEval、CI 和其他机器调用方使用的非交互入口；普通终端 CLI 不变。

```powershell
python -m hh_agent_pro --jsonl --cwd <workspace> --provider deepseek
```

stdin 每行是一个请求：

```json
{"protocolVersion":1,"requestId":"req_x","text":"用户请求","sessionId":null,"responses":[]}
```

stdout 只写 JSONL。前面是有序 event，最后一行是 Turn：

```json
{"kind":"turn","status":"completed|failed|waiting","sessionId":"...","runId":"...","finalAnswer":"...","usage":{},"artifactPaths":{}}
```

标准事件包括 `message`、`operation.started`、`operation.finished`、
`input.requested`、`thinking`、`compaction`、`usage` 和 `error`。delegate 会标记为
`operationType=subagent`。所有事件和 Turn 在写出前再次经过运行时密钥脱敏。

## HITL 恢复

澄清回复：

```json
{"inputId":"input_text_x","value":"30"}
```

审批回复：

```json
{"inputId":"input_approval_x","decision":"approve"}
```

或：

```json
{"inputId":"input_approval_x","approved":false}
```

审批请求会先把精确工具名、参数、SHA-256 指纹和 execution state 持久化到 Session。
批准前不会执行 workspace 动作；批准只能消费该指纹一次；拒绝会清除 pending action，
返回 completed 的安全答复，并且不登记为 tool failure。崩溃时如果 action 已进入
`executing`，系统不会把它当成新的 pending action 自动重放。

## 超时与基础设施错误

`--episode-timeout-ms` 限制整个 episode，而非单次 provider 请求。Provider 402、连接或
响应解析失败的 Turn 带 `failureKind=infra_error`；episode 预算耗尽带
`failureKind=functional_failed`。NiceEval adapter 应将前者抛为 experiment `errored`，
不能交给能力 grader 判成通过。
