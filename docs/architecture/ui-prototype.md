# 图形界面（hh_agent_ui，2026-11-02）

## 结论

在不改 `hh_agent_pro` 任何代码的前提下，加了一个本地网页界面，布局参照 Codex：左侧对话列表与工作区，
中间对话流（工具调用卡片、审批卡片），底部输入框（审批模式、模型），右侧工作区改动（git diff）。
CLI 照常使用；界面、CLI、评测三者共用同一个 agent 核心。目前是样例，不是完整产品。

```bash
python -m hh_agent_ui --cwd <要操作的仓库>   # 打开 http://127.0.0.1:8765
```

## 架构

```
浏览器（static/index.html）
   │  POST /api/turn  ← 逐行返回 NDJSON
   ▼
hh_agent_ui/server.py（标准库 http.server，只监听 127.0.0.1）
   │  每轮启动一次：python -m hh_agent_pro --jsonl --cwd <仓库> --approval <模式>
   ▼
hh_agent_pro 的 JSONL 机器协议（评测一直在用的接口）
```

| 设计点 | 做法 | 取舍 |
|---|---|---|
| 和 CLI 兼容 | 只调用 CLI 的 `--jsonl` 模式，不 import agent 内部 | agent 改动不用同步改 UI；代价是每轮多一次进程启动（约 1 秒） |
| 多轮对话 | 协议里的 `sessionId` 续接会话，会话由 agent 自己存在 `.hh/` | 界面只在浏览器里存显示用的记录 |
| 审批 | "风险操作先问我"模式下 agent 暂停并发出 `input.requested`，界面显示批准/拒绝，下一轮用 `responses` 回传 | 每次审批是一轮新的 CLI 调用 |
| 实时显示 | 服务把 CLI 的输出逐行转发，前端用 fetch 流式读取 | 不用 WebSocket，代码更少 |
| 密钥 | 服务启动时把本项目 `.env` 读进环境变量，子进程继承；不打印、不写进页面 | 被操作的仓库里不需要 `.env` |
| 依赖 | 后端只用 Python 标准库，前端是单个 HTML 文件，原生 JS | 不需要 npm 构建；以后要做成桌面软件可以用 Tauri/Electron 套这个页面 |

## 样例实测（示例仓库：`average()` 多加了 1）

| 场景 | 结果 |
|---|---|
| "完全自动"：跑测试并修复失败 | agent 列文件 → 读代码 → 跑测试 → 定位 `+ 1` → 修复 → 复测，8 次请求；改动面板显示一行 diff |
| "风险操作先问我"：加一个 median 函数 | `patch_file` 和 `run_shell` 前都弹出审批卡片，批准后继续执行，最终只改了 `calc.py` |

调试中发现并修掉的问题：

1. 对话变长后整页滚动，侧边栏被滚出屏幕（纵向弹性布局缺 `min-height: 0`）。
2. 每次模型请求都插入一行"DeepSeek turn completed"，刷屏。现在隐藏 `provider_status` 类状态。
3. 窄窗口打开改动面板时顶栏按钮被挤成竖排。现在宽度不足 1100px 时隐藏侧边栏。
4. 等待审批的工具卡片显示为失败（红点）。现在改为"等待中"。
5. 每次审批都会新开一轮，token 统计只显示当前这一轮。现在按整个对话累计。
6. **刷新页面后审批模式回到"完全自动"**，等于悄悄放宽了权限。现在审批模式和模型都记在本地。

回归测试：`tests/test_ui_server.py`（命令拼装、请求格式符合协议、diff 接口隐藏 `.hh/`、用假的 agent 进程测流式转发、拒绝非法审批模式）。

## 第二版：AI Elements 组件（当前默认）

界面部件改用 [Vercel AI Elements](https://elements.ai-sdk.dev)（Apache-2.0，已核对仓库 LICENSE）。它不是 npm 依赖，
而是用 `npx ai-elements add <组件>` 把 `.tsx` 源码复制进 `hh_agent_ui/web/src/components/ai-elements/`，可以直接读、直接改。
后端 `server.py` 和 CLI 协议都没变：有构建产物（`static/dist/`）就用新界面，没有就退回第一版单文件页面。

```bash
cd hh_agent_ui/web && npm install && npm run build   # 产物输出到 hh_agent_ui/static/dist
python -m hh_agent_ui --cwd <要操作的仓库>
# 开发：先起 server.py，再在 web/ 里 npm run dev（/api 会转给 8765 端口）
```

| 界面部件 | AI Elements 组件 |
|---|---|
| 对话流、自动滚到底部、"回到底部"按钮 | `Conversation` / `ConversationContent` / `ConversationScrollButton` |
| 用户消息、agent 回答（Markdown、表格、代码高亮、复制按钮） | `Message` / `MessageResponse`（Streamdown + Shiki） |
| 工具调用卡片（状态徽标、可折叠、参数与结果） | `Tool` / `ToolHeader` / `ToolInput` / `ToolOutput` |
| `run_shell` 的输出 | `Terminal` |
| 审批（批准 / 拒绝 / 已批准） | `Confirmation` 系列 |
| 输入框、审批模式和模型下拉框、发送和停止 | `PromptInput` 系列 |
| 空白页的建议按钮、运行中提示 | `Suggestion`、`Shimmer` |
| 改动面板的 diff 高亮 | `CodeBlock`（language="diff"） |

代码组织：`src/lib/agent.ts`（176 行）负责请求、读流、把 CLI 事件转成界面块，不依赖 React；
`src/App.tsx`（385 行）只负责布局和把块交给组件渲染。第三方组件源码约 4900 行，大部分不用改。

实测中发现并修掉的问题：

1. **展开 `run_shell` 卡片后整页白屏**（React #130）。`ansi-to-react` 是 CommonJS 包，组件在 `exports.default` 里，
   Vite 构建后默认导入拿到的是外层对象。已在 `terminal.tsx` 里兼容两种形态（文件里标了"HH 修改"）。
2. 等待审批的调用显示为 "Error"。原因是 CLI 先发出 `input.requested`，后发出 `operation.finished`；
   现在工具结束时如果后面已有同名、未处理的审批，就标为等待审批。批准后 agent 会重新执行，旧的等待卡片随之去掉。
3. "Thinking - decision 1/150 | tools 0/50" 这类内部计数和"请求完成"状态刷屏。现在只显示模型的思考摘要。
4. 建议按钮横向滚动被截断；长标题把卡片图标挤小。

测试：`cd hh_agent_ui/web && npm test`（`src/lib/agent.test.ts`，7 个，用 Node 自带的测试运行器，覆盖审批事件的先后顺序等）；
`tests/test_ui_server.py` 增加了"提供构建产物、阻止 `../` 越出 dist"的测试。

体积：`web/node_modules` 383MB（不入库，根目录 `.gitignore` 已忽略）；`static/dist` 14MB，大部分是按需加载的代码高亮语言包和 Mermaid。
dist 是否入库待定：入库则别人拿到仓库就能直接用，不入库则需要先在本地构建一次。

## 第三版：视觉重做 + 选工作区 + 附加文件（待确认）

视觉：默认深色（侧边栏可切浅色并记住），紫青渐变点缀，背景两团光晕加渐隐网格，面板半透明毛玻璃，
输入框常态柔光、运行时边框流动（`prefers-reduced-motion` 时不动画），Inter / JetBrains Mono 字体。全部是 `src/index.css`
里的几个样式类（`app-backdrop`、`glass-panel`、`text-gradient`、`composer-glow`），组件本身没改。

| 功能 | 做法 | 接口 |
|---|---|---|
| 打开工作区 | 侧边栏项目卡片或空白页的胶囊 → 弹窗逐层浏览文件夹（git 仓库带标记）、记住最近 8 个；换工作区自动开新对话 | `GET /api/fs?path=`：只列子目录名，不读文件内容 |
| 从工作区选文件 | 📎 → 搜索框，服务端按关键字过滤（遵守 `.gitignore`，最多返回 200 个），可多选，显示为输入框上方的紫色标签 | `GET /api/files?cwd=&q=` |
| 从电脑上传 / 拖拽 | 📎 → 选择文件，或直接拖到输入框；存到 `<工作区>/.hh/uploads/`，显示为青色标签 | `POST /api/upload`：只取文件名（防 `../`）、同名不覆盖、单个 ≤ 10MB |
| 发送 | `buildPrompt` 把选定文件写成"[选定文件] 只修改下面这些文件"，上传文件写成"[用户上传的参考文件] 先阅读，不要修改" | — |

**范围是软约束**：只在指令里告诉 agent，agent 没有"只许改这些文件"的硬拦截。实测（选定 `calc.py` 修 bug）它只改了
`calc.py`，回答里也写明"修改内容（仅 calc.py）"。要做成硬约束，需要给 CLI 加一个允许修改的路径列表参数，属于 agent 本身的改动，这次没做。

截图核对时修掉的问题：

1. 从下拉菜单里打开"选择文件"弹窗时，弹窗一闪就关（Radix 下拉菜单关闭时归还焦点，把刚打开的弹窗关掉）。改为等菜单关闭后再打开。
2. 文件夹弹窗的面包屑路径太长，撑出粗滚动条。现在只显示最后 3 级，前面折成"…"。
3. 空白页标题下的完整路径换行。改成"项目名 · 分支"的胶囊，悬停显示完整路径。

测试：`tests/test_ui_server.py` 新增 3 个（只列子目录、文件列表遵守 `.gitignore` 并支持查询、上传不越界且同名不覆盖）；
`agent.test.ts` 新增 `buildPrompt` 的测试。

顺带观察到一个 agent 本身的行为（不是界面问题）：第一次用 `python` 跑测试失败，之后改用 `python3 … || …` 成功，
但交付时仍报 `unresolved failure run_shell:tool_failed`。原因是 agent 认为带 `||` 的命令可能掩盖失败，不算修复了那次失败。

## 对外改名为 Jaxen（2026-11-02）

只改用户看得到的地方：网页标题、侧边栏名字与标志（渐变 "J"）、备用单文件页面、服务启动提示；命令行的 ASCII 欢迎大字、
`JAXEN` 标题、`jaxen>` 提示符、`--help` 里的程序名和描述。包名、目录、环境变量、`.hh/` 状态目录、系统提示词都没动
（系统提示词会影响 agent 行为和评测可比性）。改后全量 pytest、确定性回归、前端测试均通过。

## 还没做（样例范围之外）

- 打开已有的 CLI 会话（目前界面只列自己创建的对话）。
- 选定文件的硬约束（CLI 参数级别的允许修改路径）；`@文件名` 行内提及。
- 中途插话或修改指令（Codex 的 steer）；目前只能停止后重新发。
- diff 面板里逐块接受或撤销改动。
- 打包成桌面应用。
