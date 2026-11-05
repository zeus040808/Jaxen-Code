// Jaxen 图形界面（对外名字 Jaxen，内部包名仍是 hh_agent_pro）：Codex 式布局（左侧项目与对话、中间对话流、底部输入框、右侧改动面板）。
// 部件来自 AI Elements（src/components/ai-elements，Apache-2.0）；数据和事件逻辑在 lib/agent.ts。
import { useCallback, useEffect, useReducer, useRef, useState, type DragEvent } from "react";
import {
  Bug,
  ChevronsUpDown,
  FileCode2,
  FileUp,
  FolderGit2,
  GitBranch,
  Moon,
  PanelRight,
  Paperclip,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  SquarePen,
  Sun,
  TestTube2,
  X,
} from "lucide-react";

import { CodeBlock } from "@/components/ai-elements/code-block";
import {
  Confirmation,
  ConfirmationAccepted,
  ConfirmationAction,
  ConfirmationActions,
  ConfirmationRejected,
  ConfirmationRequest,
  ConfirmationTitle,
} from "@/components/ai-elements/confirmation";
import { Conversation, ConversationContent, ConversationScrollButton } from "@/components/ai-elements/conversation";
import { Message, MessageContent, MessageResponse } from "@/components/ai-elements/message";
import {
  PromptInput,
  PromptInputActionMenu,
  PromptInputActionMenuContent,
  PromptInputActionMenuItem,
  PromptInputActionMenuTrigger,
  PromptInputBody,
  PromptInputFooter,
  PromptInputHeader,
  PromptInputSelect,
  PromptInputSelectContent,
  PromptInputSelectItem,
  PromptInputSelectTrigger,
  PromptInputSelectValue,
  PromptInputSubmit,
  PromptInputTextarea,
  PromptInputTools,
} from "@/components/ai-elements/prompt-input";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { Terminal } from "@/components/ai-elements/terminal";
import { Tool, ToolContent, ToolHeader, ToolInput, ToolOutput } from "@/components/ai-elements/tool";
import { FilePicker } from "@/components/file-picker";
import { Button } from "@/components/ui/button";
import { WorkspacePicker } from "@/components/workspace-picker";
import {
  applyEvent,
  buildPrompt,
  cancelTurn,
  emptyUsage,
  fetchDiff,
  fetchInfo,
  store,
  streamTurn,
  toolSummary,
  totalUsage,
  uploadFile,
  type Attachment,
  type Block,
  type Thread,
  type ToolStatus,
} from "@/lib/agent";
import { cn } from "@/lib/utils";

const TOOL_STATE: Record<ToolStatus, "input-available" | "output-available" | "output-error" | "approval-requested" | "output-denied"> = {
  running: "input-available",
  ok: "output-available",
  failed: "output-error",
  waiting: "approval-requested",
  denied: "output-denied",
};

const SUGGESTIONS = [
  { icon: Sparkles, title: "介绍这个仓库", desc: "结构、模块和入口，三句话说清", prompt: "用三句话介绍这个仓库的结构和主要模块" },
  { icon: TestTube2, title: "跑测试并修复", desc: "找出失败原因，改好再验证", prompt: "运行测试，如果有失败的就找出原因并修复" },
  { icon: Bug, title: "找潜在问题", desc: "列出可疑代码，先不动手", prompt: "找出 TODO 或明显的 bug，列出来但先不要改" },
];

const baseName = (path: string) => path.split("/").filter(Boolean).pop() || path;

export default function App() {
  // 对话数据放在 ref 里就地修改（流式事件很多），再用 rerender 触发重绘
  const threads = useRef<Thread[]>(store.get("hh.threads.v2", []));
  const [, rerender] = useReducer((n: number) => n + 1, 0);
  const [activeId, setActiveId] = useState<string | null>(store.get("hh.active.v2", null));
  const [workspace, setWorkspace] = useState<string>(store.get("hh.workspace", ""));
  const [recent, setRecent] = useState<string[]>(store.get("hh.recent", []));
  const [branch, setBranch] = useState("");
  // 审批模式和模型要记住：刷新后悄悄回到"完全自动"等于放宽了权限
  const [approval, setApproval] = useState(store.get("hh.approval", "auto"));
  const [model, setModel] = useState(store.get("hh.model", "default"));
  const [running, setRunning] = useState<{ turnId: string | null; controller: AbortController } | null>(null);
  const [review, setReview] = useState(false);
  const [diff, setDiff] = useState<{ diff?: string; untracked?: string[]; error?: string }>({});
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [filesOpen, setFilesOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(0);
  const [notice, setNotice] = useState("");
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));
  const uploadInput = useRef<HTMLInputElement>(null);

  const thread = threads.current.find((t) => t.id === activeId);
  const cwd = thread?.cwd || workspace;
  const save = useCallback(() => store.set("hh.threads.v2", threads.current.slice(0, 50)), []);

  useEffect(() => store.set("hh.active.v2", activeId), [activeId]);
  useEffect(() => store.set("hh.approval", approval), [approval]);
  useEffect(() => store.set("hh.model", model), [model]);
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    try { localStorage.setItem("hh.theme", dark ? "dark" : "light"); } catch { /* 隐私模式 */ }
  }, [dark]);

  // 首次打开：用服务启动时 --cwd 指定的目录
  useEffect(() => {
    fetchInfo(cwd).then((info) => {
      if (info.error) return setBranch("");
      if (!workspace) setWorkspace(info.workspace);
      setBranch(info.branch || "");
    });
  }, [cwd]); // eslint-disable-line react-hooks/exhaustive-deps

  const loadDiff = useCallback(async () => setDiff(await fetchDiff(cwd)), [cwd]);
  useEffect(() => {
    if (review) loadDiff();
  }, [review, loadDiff]);

  const flash = (text: string) => {
    setNotice(text);
    setTimeout(() => setNotice(""), 3500);
  };

  function openWorkspace(path: string) {
    setWorkspace(path);
    store.set("hh.workspace", path);
    const next = [path, ...recent.filter((p) => p !== path)].slice(0, 8);
    setRecent(next);
    store.set("hh.recent", next);
    setActiveId(null); // 换项目就开新对话：会话和工作区是绑定的
    setAttachments([]);
  }

  const toggleFile = (path: string) =>
    setAttachments((list) => list.some((a) => a.path === path) ? list.filter((a) => a.path !== path) : [...list, { path, source: "workspace" }]);

  async function upload(files: FileList | File[]) {
    for (const file of Array.from(files)) {
      setUploading((n) => n + 1);
      try {
        const path = await uploadFile(cwd, file);
        setAttachments((list) => [...list, { path, source: "upload" }]);
      } catch (err: any) {
        flash(`${file.name} 上传失败：${err?.message ?? err}`);
      } finally {
        setUploading((n) => n - 1);
      }
    }
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files.length) upload(e.dataTransfer.files);
  }

  async function send(text: string, responses?: { inputId: string; approved: boolean }[]) {
    let t = thread;
    if (!t) {
      t = {
        id: crypto.randomUUID(), title: text.slice(0, 40) || "新对话", cwd, sessionId: null, blocks: [],
        usageBase: emptyUsage(), usage: null, updated: Date.now(),
      };
      threads.current.unshift(t);
      setActiveId(t.id);
    }
    const files = text ? attachments : [];
    if (text) t.blocks.push({ type: "user", text, files: files.length ? files : undefined });
    setAttachments([]);
    t.usageBase = totalUsage(t);
    t.usage = null;
    t.updated = Date.now();
    const current = { turnId: null as string | null, controller: new AbortController() };
    setRunning(current);
    rerender();
    try {
      await streamTurn(
        { text: buildPrompt(text, files), sessionId: t.sessionId, responses, cwd: t.cwd, approval, model: model === "default" ? "" : model },
        (row) => {
          if (row.kind === "ui") current.turnId = row.turnId;
          else applyEvent(t!, row);
          rerender();
        },
        current.controller.signal,
      );
    } catch (err: any) {
      t.blocks.push(err?.name === "AbortError" ? { type: "thinking", text: "已停止" } : { type: "error", text: String(err?.message ?? err) });
    }
    t.updated = Date.now();
    setRunning(null);
    save();
    rerender();
    if (review) loadDiff();
  }

  function stop() {
    if (!running) return;
    if (running.turnId) cancelTurn(running.turnId);
    running.controller.abort();
  }

  function decide(block: Extract<Block, { type: "approval" }>, approve: boolean) {
    if (running || !thread) return;
    block.decision = approve ? "approve" : "deny";
    // 那张"等待审批"的工具卡片：批准后 agent 会重新执行并生成新卡片，旧的去掉；拒绝则保留为"已拒绝"
    const waiting = thread.blocks.findIndex((b) => b.type === "tool" && b.status === "waiting" && b.name === block.name);
    if (waiting >= 0) {
      if (approve) thread.blocks.splice(waiting, 1);
      else (thread.blocks[waiting] as Extract<Block, { type: "tool" }>).status = "denied";
    }
    send("", [{ inputId: block.inputId, approved: approve }]);
  }

  const usage = thread ? totalUsage(thread) : null;

  return (
    <div className="app-backdrop relative h-full overflow-hidden">
      <div className={cn("relative z-10 grid h-full",
        review ? "grid-cols-[17rem_1fr_minmax(22rem,40%)] max-[1100px]:grid-cols-[1fr_minmax(20rem,45%)]" : "grid-cols-[17rem_1fr]",
        "max-md:grid-cols-1")}>

        {/* ───── 侧边栏 ───── */}
        <aside className={cn("flex min-h-0 flex-col border-r bg-sidebar/70 backdrop-blur-xl max-md:hidden", review && "max-[1100px]:hidden")}>
          <div className="flex items-center gap-2.5 px-4 pt-4 pb-3">
            <span className="logo-glow grid size-8 place-items-center rounded-lg bg-gradient-to-br from-violet-500 via-fuchsia-500 to-cyan-400 font-bold text-[15px] text-white">J</span>
            <div className="leading-tight">
              <div className="font-semibold tracking-tight">Jaxen</div>
              <div className="text-[11px] text-muted-foreground">本地 coding agent</div>
            </div>
          </div>

          {/* 当前项目：点开换工作区 */}
          <div className="px-3">
            <button onClick={() => setPickerOpen(true)} disabled={!!running}
              className="group flex w-full items-center gap-2.5 rounded-xl border bg-white/[0.03] px-3 py-2.5 text-left transition hover:border-violet-400/40 hover:bg-white/[0.06]">
              <FolderGit2 className="size-4 shrink-0 text-violet-400" />
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium text-sm">{baseName(cwd) || "选择工作区"}</div>
                <div className="flex items-center gap-1 truncate text-[11px] text-muted-foreground">
                  {branch ? <><GitBranch className="size-3" />{branch}</> : "不是 git 仓库"}
                </div>
              </div>
              <ChevronsUpDown className="size-4 shrink-0 text-muted-foreground transition group-hover:text-foreground" />
            </button>
          </div>

          <div className="px-3 pt-3">
            <Button variant="ghost" className="w-full justify-start gap-2 text-muted-foreground hover:text-foreground"
              disabled={!!running} onClick={() => { setActiveId(null); setAttachments([]); }}>
              <SquarePen className="size-4" /> 新建对话
            </Button>
          </div>

          <div className="px-5 pt-3 pb-1.5 font-medium text-[11px] text-muted-foreground uppercase tracking-wider">对话</div>
          <nav className="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-3 pb-3">
            {threads.current.length === 0 && <p className="px-2 text-muted-foreground text-xs">还没有对话</p>}
            {threads.current.map((t) => (
              <button key={t.id} disabled={!!running} onClick={() => setActiveId(t.id)}
                className={cn("relative w-full rounded-lg px-3 py-2 text-left transition hover:bg-white/[0.05]",
                  t.id === activeId && "bg-white/[0.07] before:absolute before:inset-y-2 before:left-0 before:w-0.5 before:rounded-full before:bg-gradient-to-b before:from-violet-400 before:to-cyan-400")}>
                <div className="truncate text-sm">{t.title}</div>
                <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                  <span className="truncate">{baseName(t.cwd)}</span>·
                  <span className="shrink-0">{new Date(t.updated).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}</span>
                </div>
              </button>
            ))}
          </nav>

          <div className="flex items-center justify-between border-t px-4 py-3 text-muted-foreground text-xs">
            <span>经 CLI 协议驱动 · 与终端共用</span>
            <Button variant="ghost" size="icon" className="size-7" title={dark ? "切到浅色" : "切到深色"} onClick={() => setDark(!dark)}>
              {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
            </Button>
          </div>
        </aside>

        {/* ───── 对话区 ───── */}
        <main className="flex min-h-0 min-w-0 flex-col">
          <header className="flex h-14 shrink-0 items-center gap-3 px-5">
            <span className="min-w-0 truncate font-medium">{thread?.title ?? "新对话"}</span>
            <span className="flex-1" />
            {usage && usage.requests > 0 && (
              <span className={cn("hidden shrink-0 rounded-full border bg-white/[0.03] px-2.5 py-1 font-mono text-[11px] text-muted-foreground sm:inline", review && "max-[1100px]:hidden")}>
                ↑ {(usage.inputTokens / 1000).toFixed(1)}k · ↓ {(usage.outputTokens / 1000).toFixed(1)}k · {usage.requests} req
              </span>
            )}
            <Button variant="outline" size="sm" className={cn("shrink-0 gap-1.5 rounded-full bg-white/[0.03]", review && "border-violet-400/50 text-violet-300")}
              onClick={() => setReview(!review)}>
              <PanelRight className="size-4" /> 改动
            </Button>
          </header>

          <Conversation className="min-h-0 flex-1">
            <ConversationContent className="mx-auto w-full max-w-3xl gap-5 px-5">
              {!thread || thread.blocks.length === 0 ? (
                <div className="mx-auto flex max-w-2xl flex-col items-center pt-[10vh] text-center">
                  <span className="inline-flex items-center gap-1.5 rounded-full border bg-white/[0.04] px-3 py-1 text-muted-foreground text-xs backdrop-blur">
                    <span className="size-1.5 animate-pulse rounded-full bg-emerald-400" /> 本地运行 · {model === "default" ? "deepseek-flash" : model}
                  </span>
                  <h1 className="mt-6 font-semibold text-4xl tracking-tight sm:text-5xl"><span className="text-gradient">今天要构建什么？</span></h1>
                  <button onClick={() => setPickerOpen(true)} title={cwd}
                    className="mt-4 flex max-w-full items-center gap-1.5 rounded-full border bg-white/[0.03] px-3 py-1 text-muted-foreground text-sm transition hover:border-violet-400/40 hover:text-foreground">
                    <FolderGit2 className="size-4 shrink-0 text-violet-400" />
                    <span className="truncate">{cwd ? baseName(cwd) : "选择工作区"}</span>
                    {branch && <span className="shrink-0 text-xs opacity-70">· {branch}</span>}
                  </button>
                  <div className="mt-9 grid w-full grid-cols-1 gap-3 sm:grid-cols-3">
                    {SUGGESTIONS.map(({ icon: Icon, title, desc, prompt }) => (
                      <button key={title} disabled={!!running} onClick={() => send(prompt)}
                        className="group glass-panel rounded-2xl p-4 text-left transition hover:-translate-y-0.5 hover:border-violet-400/40">
                        <Icon className="size-5 text-violet-400 transition group-hover:text-cyan-300" />
                        <div className="mt-3 font-medium text-sm">{title}</div>
                        <div className="mt-1 text-muted-foreground text-xs leading-relaxed">{desc}</div>
                      </button>
                    ))}
                  </div>
                </div>
              ) : (
                thread.blocks.map((block, i) => <BlockView key={i} block={block} onDecide={decide} busy={!!running} />)
              )}
              {running && <Shimmer className="text-sm">正在工作…</Shimmer>}
            </ConversationContent>
            <ConversationScrollButton className="glass-panel" />
          </Conversation>

          {/* 输入框：拖文件进来就上传 */}
          <div className="mx-auto w-full max-w-3xl shrink-0 px-5 pb-5">
            {notice && <p className="mb-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-1.5 text-destructive text-xs">{notice}</p>}
            <div className={cn("composer-glow", running && "is-running", dragging && "is-dragging")}
              onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}>
              <PromptInput
                className="[&_[data-slot=input-group]]:rounded-[1.25rem] [&_[data-slot=input-group]]:border-0 [&_[data-slot=input-group]]:bg-card/80 [&_[data-slot=input-group]]:shadow-none [&_[data-slot=input-group]]:backdrop-blur-xl"
                onSubmit={({ text }) => { if (text.trim() && !running && !uploading) send(text.trim()); }}>
                {(attachments.length > 0 || uploading > 0) && (
                  <PromptInputHeader className="flex flex-wrap gap-1.5 px-3 pt-3">
                    {attachments.map((a) => (
                      <span key={a.path} title={a.path}
                        className={cn("inline-flex max-w-64 items-center gap-1.5 rounded-lg border px-2 py-1 text-xs",
                          a.source === "upload" ? "border-cyan-400/30 bg-cyan-400/10 text-cyan-200" : "border-violet-400/30 bg-violet-400/10 text-violet-200")}>
                        {a.source === "upload" ? <FileUp className="size-3.5 shrink-0" /> : <FileCode2 className="size-3.5 shrink-0" />}
                        <span className="truncate">{baseName(a.path)}</span>
                        <button type="button" className="opacity-60 hover:opacity-100" title="移除"
                          onClick={() => setAttachments((list) => list.filter((x) => x.path !== a.path))}><X className="size-3" /></button>
                      </span>
                    ))}
                    {uploading > 0 && <Shimmer className="text-xs">上传中…</Shimmer>}
                  </PromptInputHeader>
                )}
                <PromptInputBody>
                  <PromptInputTextarea placeholder={dragging ? "松开即可上传文件" : "描述任务，或拖入文件…"} />
                </PromptInputBody>
                <PromptInputFooter>
                  <PromptInputTools>
                    <PromptInputActionMenu>
                      <PromptInputActionMenuTrigger tooltip="添加文件"><Paperclip className="size-4" /></PromptInputActionMenuTrigger>
                      <PromptInputActionMenuContent>
                        {/* 等下拉菜单关闭、焦点归还之后再开弹窗，否则 Radix 会把刚打开的弹窗立刻关掉 */}
                        <PromptInputActionMenuItem onSelect={() => setTimeout(() => setFilesOpen(true), 0)}>
                          <FileCode2 className="size-4" /> 从工作区选择文件
                        </PromptInputActionMenuItem>
                        <PromptInputActionMenuItem onSelect={() => setTimeout(() => uploadInput.current?.click(), 0)}>
                          <FileUp className="size-4" /> 从电脑上传
                        </PromptInputActionMenuItem>
                      </PromptInputActionMenuContent>
                    </PromptInputActionMenu>
                    <PromptInputSelect value={approval} onValueChange={setApproval}>
                      <PromptInputSelectTrigger><PromptInputSelectValue /></PromptInputSelectTrigger>
                      <PromptInputSelectContent>
                        <PromptInputSelectItem value="auto">完全自动</PromptInputSelectItem>
                        <PromptInputSelectItem value="ask">风险操作先问我</PromptInputSelectItem>
                        <PromptInputSelectItem value="never">禁止风险操作</PromptInputSelectItem>
                      </PromptInputSelectContent>
                    </PromptInputSelect>
                    <PromptInputSelect value={model} onValueChange={setModel}>
                      <PromptInputSelectTrigger><PromptInputSelectValue /></PromptInputSelectTrigger>
                      <PromptInputSelectContent>
                        <PromptInputSelectItem value="default">deepseek-flash</PromptInputSelectItem>
                        <PromptInputSelectItem value="deepseek-v4-pro">deepseek-v4-pro</PromptInputSelectItem>
                      </PromptInputSelectContent>
                    </PromptInputSelect>
                  </PromptInputTools>
                  <PromptInputSubmit status={running ? "streaming" : undefined} onStop={stop}
                    className="rounded-full bg-gradient-to-br from-violet-500 to-cyan-500 text-white hover:opacity-90" />
                </PromptInputFooter>
              </PromptInput>
            </div>
            <input ref={uploadInput} type="file" multiple className="hidden"
              onChange={(e) => { if (e.target.files) upload(e.target.files); e.target.value = ""; }} />
            <p className="mt-2.5 text-center text-[11px] text-muted-foreground">
              Enter 发送 · Shift+Enter 换行 · 选中的文件会作为本轮的修改范围 · 上传的文件存到 <code className="font-mono">.hh/uploads/</code>
            </p>
          </div>
        </main>

        {/* ───── 改动面板 ───── */}
        {review && (
          <section className="flex min-h-0 min-w-0 flex-col border-l bg-sidebar/60 backdrop-blur-xl max-md:hidden">
            <header className="flex h-14 shrink-0 items-center gap-2 border-b px-4">
              <span className="font-medium">工作区改动</span>
              <span className="truncate text-muted-foreground text-xs">{baseName(cwd)}</span>
              <span className="flex-1" />
              {/* /review：只读审查子 agent 按清单审查当前未提交改动，只出报告、不改代码 */}
              <Button variant="outline" size="sm" className="gap-1.5 rounded-full bg-white/[0.03]"
                disabled={!!running || (!diff.diff && !diff.untracked?.length)} onClick={() => send("/review")}
                title="让只读的审查子 agent 检查这些改动（只给意见，不改代码）">
                <ShieldCheck className="size-4 text-violet-400" /> AI 审查
              </Button>
              <Button variant="ghost" size="icon" onClick={loadDiff} title="刷新"><RefreshCw className="size-4" /></Button>
            </header>
            <div className="min-h-0 flex-1 space-y-3 overflow-auto p-3">
              {diff.error && <p className="text-destructive text-sm">{diff.error}</p>}
              {!diff.error && !diff.diff && !diff.untracked?.length && (
                <div className="flex flex-col items-center gap-2 p-10 text-center text-muted-foreground text-sm">
                  <GitBranch className="size-6 opacity-50" /> 工作区没有未提交的改动
                </div>
              )}
              {diff.diff && <CodeBlock code={diff.diff} language="diff" className="glass-panel" />}
              {!!diff.untracked?.length && (
                <div className="glass-panel rounded-xl p-3 text-sm">
                  <div className="mb-1.5 font-medium">未跟踪的新文件</div>
                  {diff.untracked.map((f) => <div key={f} className="font-mono text-emerald-400 text-xs">+ {f}</div>)}
                </div>
              )}
            </div>
          </section>
        )}
      </div>

      <WorkspacePicker open={pickerOpen} onOpenChange={setPickerOpen} current={cwd} recent={recent} onPick={openWorkspace} />
      <FilePicker open={filesOpen} onOpenChange={setFilesOpen} cwd={cwd}
        selected={attachments.filter((a) => a.source === "workspace").map((a) => a.path)} onToggle={toggleFile} />
    </div>
  );
}

function BlockView({ block, onDecide, busy }: {
  block: Block;
  onDecide: (block: Extract<Block, { type: "approval" }>, approve: boolean) => void;
  busy: boolean;
}) {
  switch (block.type) {
    case "user":
      return (
        <Message from="user">
          <MessageContent className="whitespace-pre-wrap rounded-2xl border border-violet-400/20 bg-gradient-to-br from-violet-500/20 to-cyan-500/10 text-foreground">
            {block.text}
            {block.files && (
              <div className="mt-2 flex flex-wrap gap-1">
                {block.files.map((f) => (
                  <span key={f.path} className="inline-flex items-center gap-1 rounded-md bg-white/10 px-1.5 py-0.5 font-mono text-[11px]">
                    {f.source === "upload" ? <FileUp className="size-3" /> : <FileCode2 className="size-3" />}{f.path}
                  </span>
                ))}
              </div>
            )}
          </MessageContent>
        </Message>
      );
    case "assistant":
      return (
        <Message from="assistant">
          <MessageContent><MessageResponse>{block.text}</MessageResponse></MessageContent>
        </Message>
      );
    case "thinking":
      return <p className="flex items-center gap-2 text-muted-foreground text-sm"><Sparkles className="size-3.5 text-violet-400" />{block.text}</p>;
    case "error":
      return <p className="whitespace-pre-wrap rounded-xl border border-destructive/40 bg-destructive/10 p-3 text-destructive text-sm">{block.text}</p>;
    case "tool": {
      const title = `${block.name}  ${toolSummary(block.input)}`;
      return (
        <Tool className="glass-panel mb-0 rounded-xl">
          <ToolHeader type="dynamic-tool" toolName={block.name} state={TOOL_STATE[block.status]} title={title}
            className="[&>div]:min-w-0 [&_svg]:shrink-0 [&>div>span:nth-child(2)]:truncate [&>div>span:nth-child(2)]:font-mono [&>div>span:nth-child(2)]:font-normal [&>div>span:nth-child(2)]:text-[13px]" />
          <ToolContent>
            {block.name === "run_shell" ? (
              <Terminal output={block.output ?? ""} isStreaming={block.status === "running"} className="max-h-80" />
            ) : (
              <>
                <ToolInput input={block.input} />
                {block.output && (
                  <ToolOutput errorText={block.status === "failed" ? "工具返回错误" : undefined}
                    output={<pre className="max-h-80 overflow-auto whitespace-pre-wrap p-3 font-mono text-xs">{block.output}</pre>} />
                )}
              </>
            )}
          </ToolContent>
        </Tool>
      );
    }
    case "approval": {
      const approved = block.decision === undefined ? undefined : block.decision === "approve";
      return (
        <Confirmation className="glass-panel rounded-xl border-amber-400/30"
          approval={approved === undefined ? { id: block.inputId } : { id: block.inputId, approved }}
          state={block.decision ? "approval-responded" : "approval-requested"}>
          <ConfirmationTitle>
            <span className="font-medium text-amber-600 dark:text-amber-200">{block.question}</span>
            <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-lg bg-black/5 p-2.5 font-mono text-xs dark:bg-black/30">
              {block.name} {JSON.stringify(block.args, null, 2)}
            </pre>
          </ConfirmationTitle>
          <ConfirmationRequest>
            <ConfirmationActions>
              <ConfirmationAction variant="outline" disabled={busy} onClick={() => onDecide(block, false)}>拒绝</ConfirmationAction>
              <ConfirmationAction disabled={busy} className="bg-gradient-to-r from-violet-500 to-cyan-500 text-white hover:opacity-90"
                onClick={() => onDecide(block, true)}>批准</ConfirmationAction>
            </ConfirmationActions>
          </ConfirmationRequest>
          <ConfirmationAccepted><span className="text-emerald-500 text-sm dark:text-emerald-400">✓ 已批准</span></ConfirmationAccepted>
          <ConfirmationRejected><span className="text-muted-foreground text-sm">已拒绝</span></ConfirmationRejected>
        </Confirmation>
      );
    }
  }
}
