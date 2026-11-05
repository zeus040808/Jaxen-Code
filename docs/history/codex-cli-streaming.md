# HH Agent Pro 多模型接入与流式终端说明

## 架构

```text
用户输入 / `/model` 切换
  ↓
HH ContextManager 组装 prompt
  ↓
当前 model client
  ├─ CodexCliModelClient → codex exec JSONL
  ├─ DeepSeek client → Chat Completions SSE
  ├─ Ollama client
  ├─ OpenAI-compatible client
  └─ Anthropic-compatible client
  ↓ provider events / 完整模型文本
  ↓
HH parser
  ├─ <tool> → HH ToolExecutor + HH approval
  ├─ <needs_user_input> → choice> 等待用户
  ├─ <blocked> → 显示阻塞
  └─ <final> → 最终答案
```

关键边界是：Codex CLI 不是第二套执行层。适配器在提示词中禁止 Codex 内部调用工具，并使用只读 sandbox；即便模型要求修改文件，也必须先返回 HH 的 `<tool>`，再经过 HH 参数验证、风险审批与 workspace boundary。

运行中的 `/model <provider> [model]` 只原子替换 `agent.model_client`，状态仍归
`CodingAgent` 持有。因此切换模型不会重建 Agent，也不会清空 Session、history 或记忆。
替换 client 会先完整构造并验证基本配置；构造失败时继续使用旧 client。

## 为什么选择 JSONL

本机 `codex-cli 0.147.0` 的 `codex exec --json` 已验证会发送：

- `thread.started`；
- `turn.started`；
- `error`，例如 reconnect；
- `item.completed`，包括 reasoning 和 agent_message；
- `turn.completed` 与 token usage。

适配器逐行读取 stdout，同时消费 stderr，避免管道写满造成死锁。prompt 从 stdin 发送，随后显式关闭 stdin；否则 Codex 完成一轮后可能继续等待 additional input。

JSONL 当前保证的是事件流，不保证 `agent_message` 一定逐 token 增量发送。因此终端的实时性主要来自状态、reasoning summary 和 HH 工具事件；最终回答在 `agent_message` 完成时打印。

## 关于“打印思考过程”

系统不会输出隐藏 chain-of-thought。终端只展示 Codex 事件中明确提供的 reasoning summary，以及 HH 自己可以审计的事实：

- 正在进行第几次模型决策；
- 准备调用什么工具；
- 参数是什么；
- 工具是否成功；
- 裁剪后的结果；
- 为什么等待用户或被阻塞。

这种输出足以复盘 Agent 行为，同时不会把私有推理过程伪装成稳定接口。

## 运行

```powershell
codex --version
codex login status
.venv\Scripts\python.exe -m hh_agent_pro --approval ask
```

如果 `codex` 不在 PATH：

```powershell
.venv\Scripts\python.exe -m hh_agent_pro --codex-path C:\path\to\codex.exe
```

默认使用 Codex 本地配置中的模型；`--model` 只用于临时覆盖。

启动时选择其他 provider：

```powershell
.venv\Scripts\python.exe -m hh_agent_pro --provider deepseek
.venv\Scripts\python.exe -m hh_agent_pro --provider ollama --model qwen3.5:4b
```

运行中切换：

```text
hh> /model
hh> /model deepseek
hh> /model codex
```

可用 provider 为 `codex`、`deepseek`、`ollama`、`openai`、`anthropic`。

## 用户输入与审批

- 信息或选择不足：模型应返回 `<needs_user_input>`；终端显示问题和 `choice>`。
- 危险工具：终端先显示工具和参数，再显示审批提示，输入 `y` 才执行。
- 非交互 one-shot：如果模型要求补充输入，进程以退出码 2 停止，而不是猜测答案。

## 测试

`tests/test_codex_cli.py` 使用假 Codex 子进程覆盖：

- Codex 是默认 provider，CLI 不再暴露 DeepSeek；
- JSONL reasoning/status/answer/usage 解析；
- stdin 关闭；
- ephemeral + read-only 命令边界；
- 错误信息与登录诊断；
- Claude Code 风格终端事件；
- 结构化 event sink。

Codex/多模型与交互修复专项结果已并入项目全量回归：`161 passed`（另有一个工作区原有的 `.pytest_cache` 权限警告）。

真实 smoke call 还验证了本机 CLI 的实际事件形状。网络曾触发 WebSocket timeout 和 HTTPS fallback，但最终完整运行 HH Agent Pro 并得到“Codex CLI 接入成功。”；适配器会实时显示 reconnect，而不是沉默等待。
