# 错误恢复：故障注入评测

测 harness 的错误恢复机制在整条链路上对不对：该救回的救回，救不回的正确停止，正常行为不误触发，
**绝不把失败报成成功**。用假模型，零成本，结果可复现；进 pytest，每次提交都跑。

```bash
.venv/bin/python scripts/eval/run_fault_injection.py   # 结果写入 artifacts/deterministic/fault-injection-v1.json
```

代码：`jaxen/evaluation/fault_injection.py`（场景表、假服务器、运行器、判分），门禁测试 `tests/test_fault_injection.py`。

## 做法

| 部分 | 真的还是 mock | 说明 |
|---|---|---|
| 模型回复 | mock | 假服务器替换 urllib，按脚本返回 DeepSeek 格式的流式回复（原生 tool_calls、思考内容） |
| 故障 | mock | 在脚本指定位置注入：HTTP 错误、超时、断流、空响应、截断、坏 JSON、DSML 泄露 |
| provider | **真的** | 真实的 `OpenAIChatClient`，重试、空响应重发、截断标记都是真代码在跑 |
| agent 循环、工具、门禁、trace | **真的** | 被测对象。工具真的读写文件、真的跑单元测试 |

每个场景：在新的临时目录里放一个小仓库（`add()` 写成减法 + 一个单元测试）→ 新建 agent → 跑任务 → 判分 → 删目录。

**隔离**：临时目录（`.jaxen` 状态也在里面）；每个场景新建 agent；urllib 补丁用完即恢复；运行期间清掉 `*_API_KEY`、
`*_TOKEN` 和代理变量，假服务器之外不可能联网；CLI 前后比对项目仓库的 `git status`，评测改了真实代码就报错。
假服务器还会拒绝复用的 `Request` 对象，模拟代理把第二次请求变成 `CONNECT host:80`（fix8 的场景）。

**判分**：隐藏检查（在工作区跑单元测试）+ 工作区比对（只允许改 `calc.py`）+ 停止原因 + trace/transcript 里的标记。

| 结果 | 含义 |
|---|---|
| recovered | 报告成功，任务确实做对 |
| stopped | 没有报告成功（停止原因如 `model_error`、`incomplete_delivery`） |
| silent_failure | 报告成功但任务没做对：最坏，必须为 0 |

场景通过 = 结果、停止原因、trace 标记都和预期一致；不一致记为 misfire（误触发或停错）。
`known_issue` 场景是已知问题的复现，不通过不挡门禁，修好后去掉标记。

## 结果（2026-11-05）

23 个场景：首跑 21 通过，修复新发现的问题后 **22 通过，静默失败 0**；剩下 1 个是已知问题。连跑 3 次结果一致，全套约 10 秒。

| 层 | 场景 | 通过 | 内容 |
|---|---|---|---|
| 网络 | 8 | 8 | 503、429 带 Retry-After、超时、断流、代理下连续 503 都救回；503 超过 6 次、401 正确停止（401 不重试） |
| 模型输出 | 9 | 9 | 空响应、有正文截断、正文为空截断（fix7）、文本和原生工具调用 JSON 坏、DSML 泄露一次都救回；持续空响应、两次截断、两次 DSML 正确停止 |
| 工具 | 3 | 3 | 同一失败测试无进展重跑 → 停止；改完代码后重跑（fix6）、只读搜索出错后继续（fix5）都不误触发 |
| 交付 | 3 | 2 | 测试失败仍交付 → `incomplete_delivery`；改了代码没测按设计只提醒不拦 |

### 发现的问题

| 场景 | 现象 | 状态 |
|---|---|---|
| `bad_native_tool_args` | 原生 tool_calls 的参数 JSON 坏一次，provider 直接抛 `RuntimeError`，整个运行以 `model_error` 结束；文本格式的坏 JSON 却会让模型重来。真实运行里还没碰到过，但 DeepSeek 偶发半截参数时会整题失败 | **已修**：provider 交回一个解析不了的文本工具调用，走"格式错了、让模型重来"的路径，并清空原生协议状态（不留下没有工具结果的 tool_call id）；回归测试 `tests/test_model_retry.py::test_malformed_native_tool_arguments_become_a_retry_not_a_model_error` |
| `test_runner_missing_then_script_check` | 测试命令因环境问题失败（模拟没装 pytest）后，用脚本验证通过，门禁仍把它当未解决失败拦下 | 已知（SWE-bench astropy-8707、django-16662），待定 |

### 搭评测时踩的坑

- 脚本一秒内就改完再测，修复前后文件一样大时 Python 沿用旧 `.pyc`，改对了测试照样失败。shell 只继承白名单
  环境变量，`PYTHONDONTWRITEBYTECODE` 传不进去，所以让修复后的文件大小不同。真实模型每轮要几秒，基本碰不到。
- 场景标记不能用 `"retry"`：工具元数据里有 `retryable` 字段会误命中，改用 provider 重试提示里的 `request failed (`。
- 先前以为"改了代码没跑测试会被拦"，实际设计是只提醒（`tests/test_core_capacity.py::test_unchecked_code_edit_is_delivered_with_a_verification_notice`），
  README 已更正。

## 局限

- 脚本只覆盖想得到的故障；真实模型的新花样靠 SWE-bench trace 统计发现，发现后补成场景。
- 只测"机制对不对"，不测恢复对通过率的贡献。
- 场景串行执行（urllib 补丁是进程级的）。
