# HH Benchmark Core Report

这轮 benchmark 覆盖 Harness regression、context ablation、working memory ablation、recovery ablation 和 security regression；不把真实 Provider 能力与 deterministic runtime 指标混写。

## Harness Regression
- 固定 regression 任务数：14
- pass_rate：100.00%
- within_budget_rate：100.00%
- verifier_pass_rate：100.00%

## Context Ablation
- 配置数：12
- 重复次数：5
- 总样本数：60
- avg_full_prompt_chars：6362.67
- avg_raw_prompt_chars：9541.33
- avg_prompt_compression_ratio：31.89%
- max_prompt_compression_ratio：41.09%
- current_request_preserved_rate：100.00%

## Working Memory Ablation
- 任务类型数：12
- 每个变体样本数：60
- memory_on repeated_reads：0
- memory_off repeated_reads：60
- memory_on avg_tool_steps：0.00
- memory_off avg_tool_steps：1.00
- memory_on correct_rate：100.00%
- memory_hit_rate：100.00%

## Recovery / Resume Ablation
- 恢复场景数：10
- resume_enabled 样本数：30
- resume_success_rate：100.00%
- stale_reanchor_rate：100.00%
- workspace_drift_detection_rate：100.00%
- resume_false_accept_rate：0.00%

## Security Regression
- 场景数：10
- 每场景计划重复：3
- 已执行样本：27
- 跳过样本：3
- pass_rate：100.00%

## 可以安全写进简历的指标
- avg_full_prompt_chars
- avg_raw_prompt_chars
- avg_prompt_compression_ratio
- max_prompt_compression_ratio
- repeated_reads
- avg_tool_steps
- correct_rate
- resume_success_rate
- workspace_drift_detection_rate
- resume_false_accept_rate
- security pass_rate

## 只适合放文档/面试展开的指标
- current_request_preserved_rate
- memory_hit_rate
- stale_reanchor_rate
- failure_category_counts

## 口径边界
- 所有 Core Report 数据均来自 scripted deterministic model，证明 Runtime 合同和模块收益，不代表真实模型智能上限。
- Harness regression 只证明 runtime 合同稳定，不证明 provider 上限。
- Context、memory、recovery 这三层只证明模块收益，不和 provider benchmark 混写。
