# 换强模型：Opus 4.8 跑 5 道持续失败题（2026-11-02）

## 结论

deepseek-flash 跑了 3 次都失败的 5 道题，换成 Claude Opus 4.8（公司 LLM 网关），其他条件不变：

| 实例 | flash（3 次） | Opus 4.8 | 判断 |
|---|---|---|---|
| django-16631 | ❌❌❌ | ✅ | **模型能力**：Opus 想到了"验证通过后把会话 hash 更新成新密钥的值" |
| astropy-14369 | ❌❌❌ | ✅ | **模型能力**：Opus 只把除法改成左结合，改动最小，和官方思路一致 |
| django-12325 | ❌❌❌ | ❌（blocked，空补丁）→ 修复后复测 ❌ | 首轮是 **harness 问题**：已找到和官方一致的修法，被防死循环规则禁用了工具；规则修复后复测，补丁已应用但保留了与新原则矛盾的旧报错（模型设计判断） |
| django-13212 | ❌❌❌ | ❌ | 两个模型都漏了 `forms/fields.py`；隐藏测试检查的行为在 issue 里没有提到 |
| pylint-4551 | ❌❌❌ | ❌ | 两个模型都只改了 4 个文件中的 2 个；功能型需求，范围大 |

所以答案是"两者都有"：2 道是模型能力，1 道最初被 harness 机制挡住（已修复，复测后变成模型设计判断问题），2 道两个模型都做不到。

| 项目 | 数值 |
|---|---|
| 通过 | 2/5 |
| token | 327 万（输入 324 万、输出 3.8 万），在 800 万上限以内 |
| 请求 | 116 次 |
| 配置 | 和 flash 第二版规则那轮相同：同样的提示规则、隔离环境、50 步、单题 30 分钟；只换模型（`--provider anthropic --model Claude-Opus-4.8-joybuilder`） |

## django-12325：被防死循环规则挡住

过程（trace）：
1. Opus 在 `base.py`、`related.py` 之间尝试了多种改法，期间撤回改动又重改（共 13 次 `patch_file`）。
2. 第 43 步 `git diff` 时工作区恰好是空的（刚撤回）。
3. 之后再读 `base.py` 的同一段，触发 `repeated_cached_read`：同一段**未变化**的内容第三次被读到，
   harness 强制进入 final-only 模式，工具全部禁用。
4. 这时 Opus 已经得出正确修法（只把 `parent_link=True` 的一对一字段记为父链接，和官方补丁核心一致），
   但只能在回答里写出来，交付空补丁，`stop_reason=blocked`。

规则在 `agent_loop.py`：`cache_hit_count >= 2` 就进入 final-only。问题有两点：
- 不区分"连续反复读同一段"（真死循环）和"长任务里改过、撤回后再看一眼"（正常工作）。文件内容恢复原样后，之前的读取次数仍然累计。
- 一旦触发就禁用所有工具，连已经想清楚的修改都不能做。

建议改法：重复读取时先返回提示（"这段内容和之前一样，没有变化"），只有**连续**多次重复、中间没有任何其他进展时才进入 final-only；
或者进入 final-only 前留一次修改的机会。这是 harness 的问题，flash 跑得少、探索少，所以之前没暴露出来。

### 修复后复测（同一题再跑一次 Opus）

防死循环规则修复后（见 `docs/architecture/harness-upgrades-20261030.md` 第四轮修复三），用 Opus 重跑 django-12325：

| | 修复前 | 修复后 |
|---|---|---|
| 重复读取命中缓存 | 3 次，触发 `repeated_cached_read` | **0 次** |
| 进入 final-only 的原因 | 防死循环误判（第 45 步） | 50 步工具预算用完（第 54 步） |
| 补丁 | 空 | 改了 `base.py`、`options.py`：核心一行和官方一致（只把 `parent_link=True` 的一对一字段记为父链接） |
| 结果 | ❌ | ❌：把旧的"Add parent_link=True"报错从 `options.py` 挪到 `base.py` 保留下来；官方是彻底删掉，隐藏测试检查"没加标记的一对一字段不应报错" |
| token | 171 万 | 226 万 |

结论：防死循环修复在真实任务里有效（不再误判），这道题现在卡在模型的设计判断上（是否保留与新原则矛盾的旧报错）和步数预算上，
不再是 harness 机制问题。Opus 两次合计用了 554 万 token（含 5 题首轮 327 万），在 800 万上限以内。

## 其他发现

- **两个模型都做不到的题**（13212、pylint-4551）：隐藏测试要求的改动超出了 issue 字面描述（13212 要求表单字段在调用校验器之前
  自己抛错时也带上值；pylint 要求同时改图表、写出器、工具函数），这类题单靠更强的模型或更多规则都不够，
  可能需要"独立审查"一类的机制，也可能本身就超出了只看 issue 能推断的范围。
- **Opus 没有出现找答案的行为**（同样的禁止规则）。
- **接入方式**：新增 Anthropic Messages 兼容 provider（`hh_agent_pro/providers/anthropic_messages.py`），评测出口代理支持
  转发白名单内的普通 HTTP（网关是 `http://`），代码与测试见 `tests/test_anthropic_provider.py`、`tests/test_eval_isolation.py`。

## 注意事项

- 每题只跑 1 次，Opus 的 2/5 也有随机性；flash 那边是 3 次都失败，对比相对可靠。
- Opus 没有开启提示缓存（`cache_control`），缓存命中为 0；用量按 token 计不受影响，但延迟和网关侧成本更高。

明细：`artifacts/swebench/hh-opus-persistent5.{results,predictions,agent-runs}.jsonl`、`traces/<实例>/runs/` 最新一次。
