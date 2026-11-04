---
name: code-review
description: Review a code change against the request it was meant to satisfy; report only problems that make it wrong or incomplete.
---

# Code review checklist

You are reviewing a change you did not write. You see the request and the diff, not the author's reasoning.
Read the changed functions and the code around them before judging. For each item, look for concrete evidence.

1. **Root cause or symptom?** Does the change fix the place where the wrong value or state is produced, or does it
   add a guard where the problem happens to show up? A guard at the call site usually leaves other callers broken.
2. **Who else is affected?** For every changed function, class, or behavior, search for its callers, wrappers,
   subclasses, and code that does the same job before or after it. Do any of them need the same change?
3. **Is every requirement covered?** List what the request asks for or clearly implies, including follow-up state
   (data that must be updated, migrated, or kept consistent after the change). Is each one handled?
4. **Is the scope right?** Does the change accept more inputs than required, alter unrelated behavior, or edit
   tests to make them pass?
5. **Is old code now contradictory?** If the request states a principle (for example "an explicit flag should
   decide"), is there leftover code, a check, or an error message that still follows the old rule?

Report only issues that would make the change wrong or incomplete, each with the file, the problem, and what to
change. Do not report style preferences or speculative improvements. If nothing is wrong, approve.
