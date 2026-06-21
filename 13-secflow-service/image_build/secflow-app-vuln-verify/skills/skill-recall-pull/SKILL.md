---
name: skill-recall-pull
namespace: bootstrap
description: |
  Discover and install skills from skill-recall into the local
  <skills-dir> directory. Supports semantic search (via /recall),
  install (git clone), update (fast-forward pull), and status (compare
  local vs remote versions). Triggers on: "search for a skill", "install
  skill X", "pull skill updates", "find a skill that can do Y",
  "召回 skill", "拉取 skill".
tags: [skill-recall, pull, discovery, infra, bootstrap]
---

# skill-recall-pull

> `<skills-dir>` is the root directory containing all skills. Scripts auto-detect it via `Path(__file__).resolve().parents[2]` — no manual configuration needed.

## What this skill does

Three verbs for working with the skill-recall service from the client side:

| Verb | What it does |
|------|--------------|
| `search <query>` | Semantic search via `POST /recall`. Prints top-K matches with score, version, description. |
| `install <ns/slug>` | `git clone` the Gitea repo into `<skills-dir>/<slug>/`. Refuses if dir already exists. |
| `update [<ns/slug>]` | `git fetch --tags && git pull --ff-only` on one or all managed skills. Non-fast-forward = skip + warn (the user may have local evolve commits). |
| `status` | Scan `<skills-dir>/`, compare each managed skill's local HEAD/tag to its origin, report which are behind/ahead/diverged. |

This is the **read side** of the registry. Writing flows through:
- `skill-recall-onboard` — local → Gitea (new skills + push local evolution commits)
- `sec-skill-local-evolve` — local edits + version bump (never talks to network)

## When to invoke this skill

- User says "search for a skill about X", "find something that can do Y"
- User says "install demo/foo", "pull foo", "grab the latest version of bar"
- User says "check for skill updates", "are my skills current"
- Periodically at session start if the user wants their local copy synced with Gitea

## How to invoke

## 配置约束

Gitea 和 skill-recall 服务地址必须来自环境变量 `GITEA_URL`、`SKILL_RECALL_URL`，认证来自 `GITEA_TOKEN`。执行命令时应先加载 `~/.config/secocto/.env`；除非用户显式要求临时覆盖，否则不要在命令参数中写死服务端地址。

Required env (or use flags):

| Variable | Default | Required? |
|----------|---------|-----------|
| `GITEA_URL` | from env / `~/.config/secocto/.env` | no |
| `SKILL_RECALL_URL` | from env / `~/.config/secocto/.env` | no |
| `GITEA_TOKEN` | — | only for **private** repos; public `demo/*` works without |

Commands:

```bash
PULL=<skills-dir>/skill-recall-pull/scripts/pull.py

# Semantic search
python3 $PULL search "reflect on recent failures" --top-k 5

# Install one skill (default org: demo)
python3 $PULL install cao-reflect
python3 $PULL install demo/cao-reflect      # fully qualified

# Update
python3 $PULL update cao-reflect            # one
python3 $PULL update                        # all managed skills

# Status (diff local vs Gitea)
python3 $PULL status
python3 $PULL status --json
```

## Design decisions

- **No auto-install.** Search prints candidates; the user picks. Install is always explicit — we do not overwrite directories we did not create.
- **`pull --ff-only` always.** If the local branch has evolved past origin (typical after `sec-skill-local-evolve`), skip with a warning. The user resolves: either `skill-recall-onboard` to push, or manually reconcile.
- **Dirty trees are left alone.** If `git status --porcelain` shows uncommitted changes, update refuses — that's in-flight work.
- **Install is clone, not copy.** The cloned dir has `.git` pointing at Gitea, so `skill-recall-onboard` sees it as `MANAGED_LOCAL` on the next run and any subsequent `sec-skill-local-evolve` + `onboard` loop works without extra setup.

## Exit codes

- `0` — success
- `1` — one or more operations failed, skipped, or produced warnings (still usable; check the report)
- `2` — pre-flight failed: skill-recall unreachable, bad args, no such skill

## Dependencies

- Python 3.10+
- `httpx` (usually already installed)
- `git` CLI

## Self-skip mechanism

This skill has `namespace: bootstrap`, so `skill-recall-onboard` skips it — bootstrap infra isn't managed by Gitea itself.
