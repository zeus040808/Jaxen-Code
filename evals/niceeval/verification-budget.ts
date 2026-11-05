// Keep in sync with jaxen/progress.py:VERIFICATION_REPAIR_STEPS.
const VERIFICATION_REPAIR_STEPS = 4;
const CODE_PATH = /\.(?:py|js|jsx|ts|tsx|go|rs|java|c|cc|cpp|h)$/i;

export function evalToolLimit(task: { budgets: { max_steps: number }; allowed_changed_paths: string[] }): number {
  // NiceEval needs a ceiling before the run. Jaxen grants these steps only after a code edit.
  return task.budgets.max_steps
    + (task.allowed_changed_paths.some((path) => CODE_PATH.test(path)) ? VERIFICATION_REPAIR_STEPS : 0);
}
