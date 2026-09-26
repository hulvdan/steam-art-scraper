# Agent rules

Rules live in two places and must stay in sync: this file (Claude) and `.cursor/rules/*.mdc` (Cursor).
Whenever you add, change or remove a rule, update both.

## Git commits

- After finishing a code change and before committing, run a reviewer subagent on the diff
  (e.g. `caveman:cavecrew-reviewer`). Fix or explicitly dismiss its findings, then commit.
- Write commit messages in Russian, as a single line. Use impersonal passive past form
  ("Сделан X", "Добавлен Y", "Исправлена Z"), not first person ("Сделал X").
- Never add `Co-Authored-By` (or any AI attribution) trailers to commit messages or PR descriptions.

## Python

- Prefix module-level names (constants, functions, classes, variables) with `_` unless they are
  used outside their module (including tests).
