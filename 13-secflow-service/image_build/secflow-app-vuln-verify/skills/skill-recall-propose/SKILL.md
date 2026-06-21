---
name: skill-recall-propose
namespace: bootstrap
description: |
  Submit a local skill evolution as a Proposal to skill-recall (not directly
  to main). Pushes a proposal/<uuid> branch to Gitea, opens a PR, registers
  the proposal with skill-recall, and requires a previously generated
  task-trace result for provenance. Triggers on: "propose this evolution",
  "submit a proposal", "提交进化提案".
tags: [skill-recall, propose, evolution, infra, bootstrap]
---

# skill-recall-propose

## What this skill does

After a local evolution (typically driven by `sec-skill-local-evolve`), this
skill publishes the result as a **Proposal** rather than pushing straight to
main. The flow:

1. Read the previously generated `task-trace` result and require its
   `session_id` / `local_path` as proposal provenance. If missing, print
   `skipped_reason=missing_task_trace` and exit without remote side effects.
2. Push the local commits to `proposal/<uuid>` on Gitea (never to main).
3. Open a Pull Request (head=proposal/<uuid>, base=main).
4. POST `/proposals` to skill-recall with the branch, PR number, base/
   proposed versions, summary, and session_trace.

The proposal then waits in `pending` until an operator triggers a decision
via `POST /skills/{ns}/{slug}/decide`.

Note: eval tasks are NOT submitted with proposals. They are provided
separately at decide time by the frontend (sourced from task-restore).

## Pre-conditions

| Check | Why |
|-------|-----|
| Target skill dir is a git repo with origin pointing at our Gitea | Must already be onboarded |
| Working tree is clean | Refuse to bundle in-flight edits |
| HEAD has a commit beyond the current `latest_tag` on origin | Otherwise there is no diff to propose |

## Required environment

Gitea 和 skill-recall 服务地址必须来自环境变量 `GITEA_URL`、`SKILL_RECALL_URL`，认证来自 `GITEA_TOKEN`。执行命令时应先加载 `~/.config/secocto/.env`；除非用户显式要求临时覆盖，否则不要在命令参数或 proposal payload 中写死服务端地址。

| Var | Purpose | Default |
|-----|---------|---------|
| `GITEA_URL` | Gitea base | from env / `~/.config/secocto/.env` |
| `GITEA_TOKEN` | token with `write:repository` for the skill | — |
| `SKILL_RECALL_URL` | skill-recall base | from env / `~/.config/secocto/.env` |

## How to invoke

```bash
PROPOSE=<skills-dir>/skill-recall-propose/scripts/propose.py

# minimal: lets the script compute base/proposed versions from git tags
python3 $PROPOSE --skill <skills-dir>/cao-reflect \
                 --summary "tighten judge protocol for empty hints"
```

## Output

The script prints a JSON record on success. If task-trace is unavailable, it prints `{ "skipped": true, "skipped_reason": "missing_task_trace" }` and does not push or open a PR.

Success output:

```json
{
  "proposal_id": 17,
  "branch": "proposal/8f3c…",
  "pr_number": 12,
  "base_version": "v1.0.0",
  "proposed_version": "v1.1.0",
  "session_trace": {"agent": "claude", "session_id": "…"}
}
```

The agent should surface the URL `<GITEA_URL>/<ns>/<slug>/pulls/<pr_number>`
for the operator to inspect.

## When NOT to use

- Brand-new skill that has never been onboarded → use `skill-recall-onboard`
  first to create the Gitea repo, then propose.
- A skill with `namespace: bootstrap` in its frontmatter → bootstrap skills
  are not managed by skill-recall.

## Self-skip

This skill's frontmatter is `namespace: bootstrap`, so it is never published
through itself.
