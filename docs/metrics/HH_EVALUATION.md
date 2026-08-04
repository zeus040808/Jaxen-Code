# HH Agent Evaluation

For a detailed Chinese beginner guide to methodology, metrics, manual testing, automation, and result interpretation, see
[`HH_AGENT_EVALUATION_GUIDE_ZH.md`](HH_AGENT_EVALUATION_GUIDE_ZH.md).

## Evaluation Layers

The project keeps deterministic runtime evaluation separate from real-model evaluation.

1. Unit and integration tests validate individual modules and cross-module contracts.
2. Deterministic harness tasks validate tool execution, verifier results, budgets, and run artifacts.
3. Context, memory, recovery, and security experiments measure module behavior with fixed inputs.
4. Real-provider tasks validate end-to-end model protocol compliance and file-edit completion.

## Reproduction

```powershell
python -m pytest -q
python -m scripts.run_hh_benchmarks
python -m scripts.run_real_provider_benchmark --max-new-tokens 512
```

The real-provider command uses configured API keys and can incur provider charges.

## Results

### Tests

- Pytest: 101/101 passed.
- Coverage areas: context compaction, memory, project tasks, background tasks, cron, Session/Run/Checkpoint consistency, tool protocol, and security boundaries.

### Deterministic Harness

- Fixed tasks: 14.
- Passed: 14/14.
- Within tool budget: 14/14.
- Independent artifact verifier: 14/14.

This result demonstrates runtime contract stability. It is not a model intelligence score.

### Context Ablation

- Configurations: 12.
- Repetitions per configuration: 5.
- Samples: 60.
- Average prompt compression: 16.30%.
- Maximum prompt compression: 32.16%.
- Current request preservation: 100%.

### Working Memory Ablation

- Task types: 12.
- Samples per variant: 60.
- Repeated file reads with memory disabled: 60.
- Repeated file reads with memory enabled: 0.
- Average follow-up tool steps: 1.0 to 0.0.
- Correct answer rate: 100% in both variants.

The useful result is reduced repeated work while preserving correctness.

### Recovery Ablation

- Recovery scenarios: 10.
- Repetitions: 3.
- Samples per variant: 30.
- Resume success rate: 100%.
- Stale checkpoint re-anchor rate: 100%.
- Workspace drift detection rate: 100%.
- Invalid-resume false acceptance rate: 0%.

### Security Regression

- Security scenarios: 10.
- Planned runs: 30.
- Executed on Windows: 27.
- Passed: 27/27.
- Skipped: 3 symlink-escape runs because the current Windows account lacks symlink creation privilege.

Skipped scenarios are not counted as passes.

### Real Provider

- Provider: DeepSeek.
- Model: `deepseek-chat`.
- Fixed tool-use tasks: 8.
- Categories: six single-edit tasks and two multi-edit tasks.
- Passed independent verifier: 8/8.
- Average tool steps: 2.25.
- Average model attempts: 3.25.

This is a small end-to-end compatibility sample, not a statistically strong general coding benchmark. It should be reported with the model name and sample count.

## Resume-Safe Claims

- Built a 14-task deterministic Agent runtime benchmark with 100% verifier and tool-budget pass rates.
- Reduced repeated file reads from 60 to 0 across 60 memory-dependent follow-up samples while preserving 100% correctness.
- Reduced prompt size by 16.30% on average and up to 32.16% across 60 context configurations, preserving the latest user request in every sample.
- Achieved 100% resume success and workspace-drift detection with 0% invalid-resume false acceptance across 30 recovery samples.
- Passed 8/8 isolated DeepSeek tool-use editing tasks with independent filesystem verifiers.

## Evidence

- `artifacts/harness-regression-v2.json`
- `artifacts/context-ablation-v2.json`
- `artifacts/memory-ablation-v2.json`
- `artifacts/recovery-ablation-v2.json`
- `artifacts/security-regression-v1.json`
- `artifacts/real-provider-benchmark-v1.json`
- `artifacts/providers/deepseek-benchmark.json`
- `docs/metrics/hh-benchmark-core-report.md`
