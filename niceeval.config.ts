import { defineConfig } from "niceeval";

export default defineConfig({
  name: { en: "Jaxen Enterprise Evals", "zh-CN": "Jaxen 企业级评测" },
  maxConcurrency: 1,
  timeoutMs: 180_000,
});

