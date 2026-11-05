import { defineExperiment } from "niceeval";
import { dockerSandbox } from "niceeval/sandbox";

import Jaxen from "../evals/niceeval/agents/jaxen.js";

export default defineExperiment({
  description: "Jaxen enterprise v2 deterministic reference precheck",
  agent: Jaxen,
  model: "scripted-eval",
  flags: {
    provider: "fake",
    profile: "precheck",
    resultsPath: "artifacts/eval-v2/trial-results-precheck-v2.jsonl",
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
