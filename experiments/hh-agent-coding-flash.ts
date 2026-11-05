import { defineExperiment } from "niceeval";
import { dockerSandbox } from "niceeval/sandbox";

import hhAgent from "../evals/niceeval/agents/hh-agent.js";

// A/B experiment on DeepSeek Flash: identical to nightly except for the model,
// one attempt, and a separate results file so Flash rows never mix into the
// Pro nightly sidecar. Run with `--tag coding-capability`.
export default defineExperiment({
  description: "HH Agent coding A/B on deepseek-flash (one trial per task)",
  agent: hhAgent,
  model: "deepseek-flash",
  flags: {
    provider: "deepseek",
    profile: "flash-ab",
    resultsPath: "artifacts/eval-v2/trial-results-flash-ab-v2.jsonl",
  },
  evals: ["enterprise"],
  sandbox: dockerSandbox({
    source: { type: "dockerfile", context: new URL("../", import.meta.url), file: "evals/niceeval/Dockerfile" },
    resources: { cpus: 2, memoryBytes: 2 * 1024 * 1024 * 1024, pidsLimit: 256 },
    lifetimeMs: 240_000,
  }),
  attempts: 1,
  earlyExit: false,
  timeoutMs: 180_000,
  maxConcurrency: 1,
});
