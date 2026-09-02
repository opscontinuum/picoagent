---
name: safe-refactor
description: Checklist for refactors that must not change behaviour (run tests before/after, small commits)
---
Before refactoring:
1. Run the existing test suite with `shell` and record the result.
2. Make one logical change at a time with `edit`; re-run tests after each.
3. Never touch files matching the protected patterns in the permission-gate config.
Summarise the diff at the end. $ARGUMENTS
