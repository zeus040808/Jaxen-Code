import { defineConfig } from "niceeval";

export default defineConfig({
  name: { en: "HH Agent Enterprise Evals", "zh-CN": "HH Agent 企业级评测" },
  maxConcurrency: 1,
  timeoutMs: 180_000,
});

