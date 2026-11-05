// 和 server.py 打交道的全部逻辑：发请求、读 NDJSON 流、把 CLI 的 JSONL 事件变成界面要画的"块"。
// 事件格式见 hh_agent_pro/jsonl_protocol.py（JsonlEventSink.emit 和 _turn_payload）。

export type ToolStatus = "running" | "ok" | "failed" | "waiting" | "denied";

export type Attachment = { path: string; source: "workspace" | "upload" };

export type Block =
  | { type: "user"; text: string; files?: Attachment[] }
  | { type: "assistant"; text: string }
  | { type: "thinking"; text: string }
  | { type: "error"; text: string }
  | { type: "tool"; id: string; name: string; input: Record<string, unknown>; output?: string; status: ToolStatus }
  | { type: "approval"; inputId: string; question: string; name: string; args: Record<string, unknown>; decision?: "approve" | "deny" };

export type Usage = { inputTokens: number; outputTokens: number; requests: number };

export type Thread = {
  id: string;
  title: string;
  cwd: string;
  sessionId: string | null;
  blocks: Block[];
  usageBase: Usage; // 之前各轮的累计（每轮是一个新的 CLI 进程，usage 只算本轮）
  usage: Usage | null; // 本轮
  updated: number;
};

export type AgentEvent = Record<string, any>;

export const emptyUsage = (): Usage => ({ inputTokens: 0, outputTokens: 0, requests: 0 });

export const totalUsage = (t: Thread): Usage => {
  const turn = t.usage ?? emptyUsage();
  return {
    inputTokens: t.usageBase.inputTokens + turn.inputTokens,
    outputTokens: t.usageBase.outputTokens + turn.outputTokens,
    requests: t.usageBase.requests + turn.requests,
  };
};

/** 把一行事件应用到对话上（就地修改，调用方负责触发重绘）。 */
export function applyEvent(t: Thread, row: AgentEvent): void {
  if (row.kind === "turn") {
    if (row.sessionId) t.sessionId = row.sessionId;
    if (row.usage && Object.keys(row.usage).length) t.usage = row.usage;
    if (row.status === "failed" && row.finalAnswer && !t.blocks.some((b) => b.type === "assistant" || b.type === "error"))
      t.blocks.push({ type: "error", text: row.finalAnswer });
    return;
  }
  const blocks = t.blocks;
  switch (row.type) {
    case "operation.started":
      blocks.push({ type: "tool", id: row.operationId, name: row.name, input: row.input ?? {}, status: "running" });
      break;
    case "operation.finished": {
      const card = findLast(blocks, (b) => b.type === "tool" && b.id === row.operationId && b.status === "running");
      if (card && card.type === "tool") {
        card.output = row.output;
        card.status = row.metadata?.tool_status === "ok" ? "ok" : "failed";
        // 审批请求比"工具结束"先到：后面已有同名、未处理的审批，说明这次是被拦下等审批，不是失败
        if (card.status === "failed" && blocks.some((b) => b.type === "approval" && !b.decision && b.name === card.name))
          card.status = "waiting";
      }
      break;
    }
    case "thinking": {
      // provider_status（"请求完成"）和 status（"decision 3/150 | tools 2/50" 这类内部计数）每步都有，
      // 显示出来只会刷屏；只保留模型自己的思考摘要 reasoning_summary
      if (!row.text || row.source === "provider_status" || row.source === "status") break;
      const last = blocks[blocks.length - 1];
      if (last?.type === "thinking") last.text = row.text;
      else blocks.push({ type: "thinking", text: row.text });
      break;
    }
    case "message":
      blocks.push({ type: "assistant", text: row.content });
      break;
    case "input.requested": {
      // 被拦下等审批的那次调用不算失败
      const pending = findLast(blocks, (b) => b.type === "tool" && b.name === row.name);
      if (pending && pending.type === "tool" && pending.status === "failed") pending.status = "waiting";
      blocks.push({ type: "approval", inputId: row.inputId, question: row.question, name: row.name, args: row.arguments ?? {} });
      break;
    }
    case "usage":
      t.usage = row.usage;
      break;
    case "error":
      blocks.push({ type: "error", text: row.message });
      break;
  }
}

function findLast<T>(items: T[], predicate: (item: T) => boolean): T | undefined {
  for (let i = items.length - 1; i >= 0; i--) if (predicate(items[i])) return items[i];
  return undefined;
}

export type TurnRequest = {
  text: string;
  sessionId: string | null;
  responses?: { inputId: string; approved: boolean }[];
  cwd: string;
  approval: string;
  model: string;
};

/** 发起一轮：逐行回调事件。第一行是 {kind:"ui", turnId}，用于"停止"。 */
export async function streamTurn(request: TurnRequest, onEvent: (row: AgentEvent) => void, signal: AbortSignal) {
  const response = await fetch("/api/turn", {
    method: "POST",
    signal,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...request, responses: request.responses ?? [] }),
  });
  if (!response.ok || !response.body) throw new Error((await response.json().catch(() => ({}))).error || response.statusText);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline: number;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      if (line.trim()) {
        try {
          onEvent(JSON.parse(line));
        } catch {
          /* 非 JSON 行忽略 */
        }
      }
    }
  }
}

export const cancelTurn = (turnId: string) =>
  fetch("/api/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ turnId }) });

export async function fetchInfo(cwd: string) {
  return (await fetch("/api/info" + (cwd ? `?cwd=${encodeURIComponent(cwd)}` : ""))).json();
}

export async function fetchDiff(cwd: string): Promise<{ diff?: string; untracked?: string[]; error?: string }> {
  return (await fetch(`/api/diff?cwd=${encodeURIComponent(cwd)}`)).json();
}

export type DirListing = { path: string; parent: string | null; isRepo: boolean; dirs: { name: string; isRepo: boolean }[]; error?: string };

export async function listDirs(path: string): Promise<DirListing> {
  return (await fetch("/api/fs" + (path ? `?path=${encodeURIComponent(path)}` : ""))).json();
}

export async function listFiles(cwd: string, q: string): Promise<{ files: string[]; total: number; error?: string }> {
  return (await fetch(`/api/files?cwd=${encodeURIComponent(cwd)}&q=${encodeURIComponent(q)}`)).json();
}

/** 从电脑上传：读成 base64 交给服务端，存到 <工作区>/.hh/uploads/，返回相对路径。 */
export async function uploadFile(cwd: string, file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  const response = await fetch("/api/upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cwd, name: file.name, data: btoa(binary) }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || response.statusText);
  return result.path;
}

/**
 * 把选定的文件拼进发给 agent 的指令。范围靠提示词约束（软约束）：agent 本身没有"只许改这些文件"的硬限制，
 * 但会按指令执行；上传的文件放在 .hh/uploads/ 下，只作参考资料。
 */
export function buildPrompt(text: string, files: Attachment[]): string {
  if (!files.length) return text;
  const picked = files.filter((f) => f.source === "workspace").map((f) => `- ${f.path}`);
  const uploaded = files.filter((f) => f.source === "upload").map((f) => `- ${f.path}`);
  const parts = [text];
  if (picked.length) parts.push(`[选定文件] 只修改下面这些文件；需要理解上下文时可以阅读其他文件。\n${picked.join("\n")}`);
  if (uploaded.length) parts.push(`[用户上传的参考文件] 先阅读它们，不要修改。\n${uploaded.join("\n")}`);
  return parts.join("\n\n");
}

/** 工具卡片标题后面的一句话摘要。 */
export function toolSummary(input: Record<string, any>): string {
  if (input.command) return String(input.command);
  if (input.pattern) return input.pattern + (input.path && input.path !== "." ? `  in ${input.path}` : "");
  if (Array.isArray(input.paths)) return input.paths.join(", ");
  if (input.path) return String(input.path);
  if (input.task) return String(input.task);
  return JSON.stringify(input).slice(0, 120);
}

/** localStorage 在隐私模式等情况下会抛异常：读写都兜底。 */
export const store = {
  get<T>(key: string, fallback: T): T {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : (JSON.parse(raw) as T);
    } catch {
      return fallback;
    }
  },
  set(key: string, value: unknown) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* 存不了就算了，只影响刷新后的恢复 */
    }
  },
};
