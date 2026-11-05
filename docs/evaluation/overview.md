# HH Agent Evaluation

For a detailed Chinese beginner guide to methodology, metrics, manual testing, automation, and result interpretation, see
[`HH_AGENT_EVALUATION_GUIDE_ZH.md`](../learning/evaluation-guide.md).

For the complete zero-to-interview textbook based on the current Reliability-v1 results, see
[`HH_AGENT_EVALUATION_TEXTBOOK_ZH.md`](../learning/evaluation-textbook.md).

## Evaluation Layers

The project keeps deterministic runtime evaluation separate from real-model evaluation.

1. Unit and integration tests validate individual modules and cross-module contracts.
2. Deterministic harness tasks validate tool execution, verifier results, budgets, and run artifacts.
3. Context, memory, recovery, and security experiments measure module behavior with fixed inputs.
4. Real-provider tasks validate end-to-end model protocol compliance and simple file-edit completion.
5. Reliability-v1 evaluates normal completion, missing information, tool failure recovery, high-risk actions, and context noise.

## Reproduction

```powershell
python -m pytest -q
python -m scripts.run_hh_benchmarks
python -m scripts.run_real_provider_benchmark --max-new-tokens 512
python -m scripts.run_reliability_benchmark --dry-run
python -m scripts.run_reliability_benchmark --provider deepseek --max-new-tokens 1024
```

The real-provider command uses configured API keys and can incur provider charges.

## Results

### Tests

- Pytest: 121/121 passed.
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
- Average prompt compression: 31.89%.
- Maximum prompt compression: 41.09%.
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

### Reliability-v1

- Provider/model: DeepSeek `deepseek-chat`.
- Tasks: 15 across five reliability categories.
- Pre-fix auto-acceptable outcomes: 9/15 (60.0%).
- Post-fix auto-acceptable outcomes: 9/15 (60.0%).
- Post-fix hard verification: 11/15 (73.3%), up from 10/15 (66.7%).
- Post-fix correct-failure candidates: 5/6 (83.3%), up from 2/6 (33.3%).
- Post-fix exact repeated calls: 2, down from 8.
- Post-fix average tool steps: 3.60, down from 4.27.
- Post-fix step-limit stops: 4/15, down from 6/15.
- Implicit background task starts/uncollected results: 6/6 before the fix, 0/0 after the fix.
- Safety violations: 0.

These are two single-run real-model samples, so changes cannot be attributed only to the runtime patch. Automatic labels are not completed human review. The post-fix review queue is `artifacts/human-review-deepseek-after-loop-fixes.csv`.

### Latest TaskState/Delivery-Gate Rerun

- Latest raw auto-acceptable outcomes: 7/15 (46.7%).
- Latest raw hard verification: 9/15 (60.0%).
- Latest raw correct-failure candidates: 3/6 (50.0%).
- Average tool steps: 3.53.
- Exact repeated calls: 3.
- Step-limit stops: 4/15.
- Tool failures or rejections: 11/53 (20.8%).
- Safety violations: 0.

An evaluator audit found two semantically valid clarification answers rejected by narrow text patterns and one fixture with an extra blank line omitted from its verifier expectation. The same-run audit interpretation is therefore 10/15 auto-acceptable and 12/15 hard-pass, but these calibrated numbers are not presented as the raw script output. A four-task calibration rerun passed 3/4; `missing_target_file` still ended at the malformed-response retry limit.

The latest real-model run did not reproduce the previous post-fix score. With one run per task and one provider timeout, this is evidence of unresolved variance rather than proof that the TaskState changes caused a regression.

## Resume-Safe Claims

- Built a 14-task deterministic Agent runtime benchmark with 100% verifier and tool-budget pass rates.
- Reduced repeated file reads from 60 to 0 across 60 memory-dependent follow-up samples while preserving 100% correctness.
- Reduced prompt size by 31.89% on average and up to 41.09% across 60 context configurations, preserving the latest user request in every sample.
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
- `artifacts/reliability-v1-deepseek.json`
- `artifacts/human-review-deepseek.csv`
- `artifacts/reliability-v1-deepseek-after-loop-fixes.json`
- `artifacts/human-review-deepseek-after-loop-fixes.csv`
- `artifacts/reliability-v1-deepseek-after-task-state.json`
- `artifacts/human-review-deepseek-after-task-state.csv`
- `artifacts/reliability-v1-deepseek-calibration-rerun.json`
- `artifacts/human-review-deepseek-calibration-rerun.csv`
- `docs/metrics/hh-benchmark-core-report.md`
- `docs/metrics/HH_RELIABILITY_DEEPSEEK_RESULT_V1_ZH.md`
- `docs/metrics/HH_LOOP_RELIABILITY_FIXES_ZH.md`
