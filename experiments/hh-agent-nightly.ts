import { defineExperiment } from "niceeval";
import { dockerSandbox } from "niceeval/sandbox";

import hhAgent from "../evals/niceeval/agents/hh-agent.js";

export default defineExperiment({
  description: "HH Agent nightly enterprise v2 (three valid trials per task)",
  agent: hhAgent,
  model: "deepseek-v4-pro",
  flags: { provider: "deepseek", profile: "nightly" },
  evals: ["enterprise"],
  sandbox: dockerSandbox({
    source: { type: "dockerfile", context: new URL("../", import.meta.url), file: "evals/niceeval/Dockerfile" },
    resources: { cpus: 2, memoryBytes: 2 * 1024 * 1024 * 1024, pidsLimit: 256 },
    lifetimeMs: 240_000,
  }),
  attempts: 3,
  earlyExit: false,
  timeoutMs: 180_000,
  maxConcurrency: 1,
});
