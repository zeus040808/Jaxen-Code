import { randomUUID } from "node:crypto";

import type { JsonValue, StreamEvent, Turn, TurnInput, Usage } from "niceeval";
import {
  completeEvidenceCoverage,
  createSessionSlot,
  defineSandboxAgent,
  notCommandProjection,
  opaqueCommandProjection,
} from "niceeval/adapter";
import type { SandboxAgentContext } from "niceeval/adapter";
import { command } from "niceeval/sandbox";

type ProtocolEvent = Record<string, JsonValue> & { kind: "event"; type: string };
type ProtocolTurn = Record<string, JsonValue> & {
  kind: "turn";
  status: "completed" | "failed" | "waiting";
  sessionId: string | null;
  finalAnswer: string;
  usage?: Record<string, JsonValue>;
};

interface SessionState {
  pendingOperationId?: string;
}

interface EvalPolicy {
  approval: "ask" | "auto" | "never";
  readOnly: boolean;
  maxSteps: number;
  maxNewTokens: number;
  timeoutMs: number;
}

const sessionStateSlot = createSessionSlot<SessionState>("hh-agent/jsonl-state-v1");
const POLICY_PATH = "/tmp/hh-eval-policy.json";
const FAKE_SCRIPT_PATH = "/tmp/hh-eval-fake-script.json";
const FAKE_STATE_PATH = "/tmp/hh-eval-fake-state.json";

const toolNames: Record<string, "file_read" | "file_write" | "file_edit" | "shell" | "grep" | "list_dir" | "unknown"> = {
  read_file: "file_read",
  read_files: "file_read",
  write_file: "file_write",
  patch_file: "file_edit",
  run_shell: "shell",
  search: "grep",
  list_files: "list_dir",
};

function asObject(value: JsonValue | undefined): Record<string, JsonValue> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, JsonValue>)
    : {};
}

function asString(value: JsonValue | undefined, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asNumber(value: JsonValue | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

async function readPolicy(ctx: SandboxAgentContext): Promise<EvalPolicy> {
  if (!(await ctx.sandbox.pathExists(POLICY_PATH))) {
    throw new Error(`NiceEval policy file is missing: ${POLICY_PATH}`);
  }
  const raw = JSON.parse(await ctx.sandbox.readText(POLICY_PATH)) as Record<string, JsonValue>;
  const approval = asString(raw.approval, "ask");
  if (!(["ask", "auto", "never"] as const).includes(approval as EvalPolicy["approval"])) {
    throw new Error(`Invalid eval approval policy: ${approval}`);
  }
  return {
    approval: approval as EvalPolicy["approval"],
    readOnly: raw.readOnly === true,
    maxSteps: Math.max(1, Math.trunc(asNumber(raw.maxSteps, 12))),
    maxNewTokens: Math.max(1, Math.trunc(asNumber(raw.maxNewTokens, 2048))),
    timeoutMs: Math.max(1, Math.trunc(asNumber(raw.timeoutMs, 180_000))),
  };
}

function inputResponses(input: TurnInput): Array<Record<string, JsonValue>> {
  return (input.responses ?? []).map<Record<string, JsonValue>>((response) => {
    if ("optionId" in response) {
      const optionId = response.optionId ?? "";
      const row: Record<string, JsonValue> = {
        inputId: response.requestId,
        decision: optionId === "approve" ? "approve" : optionId === "deny" ? "deny" : optionId,
      };
      return row;
    }
    const row: Record<string, JsonValue> = {
      inputId: response.requestId,
      value: response.text,
      text: response.text,
    };
    return row;
  });
}

function usageOf(turn: ProtocolTurn): Usage | undefined {
  const raw = turn.usage ?? {};
  const requests = asNumber(raw.requests, 0);
  if (requests <= 0) return undefined;
  return {
    inputTokens: asNumber(raw.inputTokens, 0),
    outputTokens: asNumber(raw.outputTokens, 0),
    cacheReadTokens: asNumber(raw.cacheReadTokens, 0),
    cacheCreationTokens: asNumber(raw.cacheWriteTokens, 0),
    requests,
  };
}

function finishStatus(metadata: Record<string, JsonValue>): "completed" | "failed" {
  const status = asString(metadata.tool_status);
  return ["ok", "cached", "partial_success"].includes(status) ? "completed" : "failed";
}

function mapEvents(
  rawEvents: ProtocolEvent[],
  requestId: string,
  input: TurnInput,
  state: SessionState,
): { events: StreamEvent[]; state: SessionState } {
  const events: StreamEvent[] = [];
  const operationIds = new Map<string, string>();
  let pendingOperationId = state.pendingOperationId;
  const denial = (input.responses ?? []).some(
    (response) => "optionId" in response && response.optionId === "deny",
  );

  if (pendingOperationId && denial) {
    events.push({
      type: "operation.finished",
      operationId: pendingOperationId,
      kind: "tool",
      status: "rejected",
      output: "Approval denied; the pending action was not executed.",
    });
    pendingOperationId = undefined;
  }

  for (const raw of rawEvents) {
    if (raw.type === "usage") continue;
    if (raw.type === "message") {
      const text = asString(raw.content);
      if (text) events.push({ type: "message", role: "assistant", text });
      continue;
    }
    if (raw.type === "thinking") {
      const text = asString(raw.text);
      if (text) events.push({ type: "thinking", text });
      continue;
    }
    if (raw.type === "compaction") {
      events.push({ type: "compaction", reason: asString(raw.reason) || undefined });
      continue;
    }
    if (raw.type === "error") {
      events.push({ type: "error", message: asString(raw.message, "HH Agent runtime error") });
      continue;
    }
    if (raw.type === "input.requested") {
      const inputType = asString(raw.inputType);
      events.push({
        type: "input.requested",
        request: {
          id: asString(raw.inputId),
          prompt: asString(raw.question),
          action: asString(raw.name) || undefined,
          input: raw.arguments,
          options: inputType === "approval"
            ? [{ id: "approve", label: "Approve" }, { id: "deny", label: "Deny" }]
            : undefined,
        },
      });
      continue;
    }
    if (raw.type === "operation.started") {
      const rawId = asString(raw.operationId);
      if (raw.resumed === true && pendingOperationId) {
        operationIds.set(rawId, pendingOperationId);
        continue;
      }
      const operationId = `${requestId}:${rawId || randomUUID()}`;
      operationIds.set(rawId, operationId);
      const name = asString(raw.name, "unknown");
      if (asString(raw.operationType) === "subagent" || name === "delegate") {
        events.push({
          type: "operation.started",
          operationId,
          operation: { kind: "subagent", name },
        });
      } else {
        events.push({
          type: "operation.started",
          operationId,
          operation: {
            kind: "tool",
            name,
            input: raw.input ?? {},
            tool: toolNames[name] ?? "unknown",
            command: name === "run_shell"
              ? opaqueCommandProjection("unsupported-protocol")
              : notCommandProjection(),
          },
        });
      }
      continue;
    }
    if (raw.type === "operation.finished") {
      const rawId = asString(raw.operationId);
      const operationId = operationIds.get(rawId) ?? pendingOperationId ?? `${requestId}:${rawId}`;
      const metadata = asObject(raw.metadata);
      if (asString(metadata.tool_status) === "waiting") {
        pendingOperationId = operationId;
        continue;
      }
      const kind = asString(raw.name) === "delegate" ? "subagent" : "tool";
      events.push({
        type: "operation.finished",
        operationId,
        kind,
        output: raw.output,
        status: finishStatus(metadata),
      } as StreamEvent);
      if (operationId === pendingOperationId) pendingOperationId = undefined;
    }
  }
  return { events, state: { pendingOperationId } };
}

const hhAgent = defineSandboxAgent({
  name: "hh-agent",
  evidenceCoverage: completeEvidenceCoverage,
  ensure: {
    identity: { agent: "hh-agent", version: "2.0.0", revision: "eval-v2" },
    probe: command("python3", ["-c", "import hh_agent_pro; print('hh-agent 2.0.0')"]),
    progress: { checking: "Checking preinstalled HH Agent", ready: "HH Agent ready" },
  },
  async send(input, ctx): Promise<Turn> {
    const policy = await readPolicy(ctx);
    const provider = asString(ctx.flags.provider, "fake");
    if (!(["fake", "deepseek"] as const).includes(provider as "fake" | "deepseek")) {
      throw new Error(`Unsupported HH eval provider: ${provider}`);
    }

    const requestId = `niceeval_${randomUUID()}`;
    const protocolRequest = {
      protocolVersion: 1,
      requestId,
      text: input.text,
      sessionId: ctx.session.id ?? null,
      responses: inputResponses(input),
    };
    const requestPath = `/tmp/${requestId}.jsonl`;
    await ctx.sandbox.writeText(requestPath, `${JSON.stringify(protocolRequest)}\n`);

    const args = [
      "-m", "hh_agent_pro", "--jsonl",
      "--cwd", ctx.sandbox.workdir,
      "--provider", provider,
      "--approval", policy.approval,
      "--max-steps", String(policy.maxSteps),
      "--max-new-tokens", String(policy.maxNewTokens),
      "--episode-timeout-ms", String(policy.timeoutMs),
      "--no-auto-skills",
    ];
    if (policy.readOnly) args.push("--read-only");
    if (ctx.model) args.push("--model", ctx.model);

    const env: Record<string, string> = { PYTHONUNBUFFERED: "1" };
    const sensitiveValues: string[] = [];
    if (provider === "fake") {
      env.HH_ALLOW_EVAL_FAKE_PROVIDER = "1";
      env.HH_EVAL_FAKE_SCRIPT_PATH = FAKE_SCRIPT_PATH;
      env.HH_EVAL_FAKE_STATE_PATH = FAKE_STATE_PATH;
    } else {
      const apiKey = process.env.HH_DEEPSEEK_API_KEY ?? process.env.DEEPSEEK_API_KEY;
      if (!apiKey) throw new Error("HH_DEEPSEEK_API_KEY is required for the DeepSeek experiment");
      env.HH_DEEPSEEK_API_KEY = apiKey;
      sensitiveValues.push(apiKey);
      const baseUrl = process.env.HH_DEEPSEEK_API_BASE ?? process.env.DEEPSEEK_API_BASE;
      if (baseUrl) env.HH_DEEPSEEK_API_BASE = baseUrl;
    }

    ctx.progress({ message: `Running HH Agent JSONL (${provider})` });
    const result = await ctx.sandbox.runShell(
      `python3 ${args.map((part) => JSON.stringify(part)).join(" ")} < ${JSON.stringify(requestPath)}`,
      {
        cwd: ctx.sandbox.workdir,
        env,
        sensitiveValues,
        signal: ctx.signal,
        timeoutMs: policy.timeoutMs,
        stream: true,
      },
    );

    const rows = result.stdout.split(/\r?\n/).filter((line) => line.trim()).map((line) => {
      try {
        return JSON.parse(line) as Record<string, JsonValue>;
      } catch (error) {
        throw new Error(`HH JSONL emitted a non-JSON line: ${line.slice(0, 200)}`, { cause: error });
      }
    });
    const protocolTurn = [...rows].reverse().find((row) => row.kind === "turn") as ProtocolTurn | undefined;
    if (!protocolTurn) {
      throw new Error(`HH JSONL produced no final Turn (exit=${result.exitCode})`);
    }
    if (protocolTurn.failureKind === "infra_error") {
      throw new Error(`HH provider/runtime infrastructure error: ${protocolTurn.finalAnswer}`);
    }

    const rawEvents = rows.filter((row) => row.kind === "event") as ProtocolEvent[];
    const previousState = ctx.session.get(sessionStateSlot) ?? {};
    const mapped = mapEvents(rawEvents, requestId, input, previousState);
    ctx.session.set(sessionStateSlot, mapped.state);
    if (protocolTurn.sessionId) ctx.session.capture(protocolTurn.sessionId);
    if (!mapped.events.some((event) => event.type === "message") && protocolTurn.finalAnswer) {
      mapped.events.push({ type: "message", role: "assistant", text: protocolTurn.finalAnswer });
    }

    return {
      status: protocolTurn.status,
      events: mapped.events,
      usage: usageOf(protocolTurn),
      data: { protocolTurn, rawEvents, commandExitCode: result.exitCode },
    };
  },
});

export default hhAgent;
