---
name: sec-skill-local-evolve
namespace: bootstrap
description: |
  Evolve a local skill based on execution feedback, entirely offline. Reads
  failure cases, runs LLM-as-Judge scoring, applies targeted edits, version-
  bumps via git tags (vX.Y.Z), and validates against a retry budget. Never
  talks to Gitea or skill-recall — pushing proposals is skill-recall-propose's
  job. Requires the target skill to already be managed (has .git pointing at
  our Gitea).
  Triggers on: "evolve this skill", "reflect and evolve", "基于反馈进化",
  "improve skill based on execution feedback".
tags: [skill-recall, evolution, infra, bootstrap]
---

# sec-skill-local-evolve

## What this skill does

Take one existing skill that underperformed, figure out why, improve it,
bump its version, and leave the result committed+tagged locally. That's
it. Pushing proposals to Gitea is skill-recall-propose's job — run it
afterwards.

This skill is **local and offline**. It never does `git push`, `git fetch`,
or HTTP. That separation is deliberate: evolve can fail mid-way without
creating half-published remote state.

## Pre-conditions

| Check | Why |
|-------|-----|
| Target skill dir exists and has `SKILL.md` | It's a skill |
| Target skill dir has `.git` with origin pointing at our Gitea | It's already onboarded; we know what version history to bump |
| Working tree is clean (no uncommitted changes) | We refuse to accidentally bundle the user's in-flight edits into an evolution |

The `evolve.py preflight` subcommand verifies all of the above and exits
nonzero if any is violated. Always run it first.

## When to invoke this skill

- User says "evolve cao-reflect", "基于本次执行反思并进化", "the feedback-fetch skill failed on X"
- CAO heartbeat injects an evolution prompt with `evolution_signals`
- `skill-recall-propose` reports a skill is under-performing

Do **not** invoke this skill to:
- Create a brand-new skill (no git history to bump) → write files by hand + run `skill-recall-onboard` to register, then `skill-recall-propose` for changes
- Make one-off edits the user doesn't want versioned → just edit the file

## Auto-discovery mode

When invoked without a specific skill target, this skill can automatically
discover which skills need evolution based on the session context.

### Discovery logic

1. Reads the session transcript to find which skills were invoked
2. Checks for vulnerability findings (CWE codes) in the transcript
3. Maps CWE codes to corresponding security audit skills via CWE_SKILL_MAP
4. Builds a **candidates** list:
   - CWE-matched skills → `reason: "cwe_findings"` (hard evidence of a gap)
   - All other invoked skills → `reason: "used_in_session"` (soft evaluation needed)
5. Every candidate gets evaluated by the LLM; only skills judged **flawless**
   (score == 10, is_correct == true) are skipped

### Soft evaluation protocol

For `used_in_session` candidates (no explicit failure), the agent performs a
**soft evaluation** using the judge protocol:

1. Read the session transcript and the skill's SKILL.md
2. Assess whether the skill's instructions could be improved based on what
   actually happened during execution — even if there was no outright failure
3. Write the judge result to `<skill-dir>/.evolve/judge_result.json`:
   ```json
   {
     "binary": {"is_correct": true, "confidence": 0.8, "rationale": "..."},
     "soft_score": {"score": 7, "rationale": "...", "strengths": [...], "weaknesses": [...]},
     "improvement_hints": ["..."]
   }
   ```
4. If `is_correct == true && score == 10` → skill is flawless, skip evolution
5. If `score < 10` → the skill has room for improvement, proceed to evolve

The key principle: **a skill must prove itself flawless to avoid evolution**.
Being "good enough" is not sufficient — any weakness the LLM can identify
is grounds for improvement.

### Auto-evolve flow

When `auto-evolve` subcommand is used, it will:

1. Discover candidates (all invoked skills, not just CWE-matched)
2. For each candidate, check `.evolve/judge_result.json`:
   - `is_correct == true && score == 10` → skip (judged flawless)
   - Otherwise → proceed to evolve
3. For each skill needing evolution, with unpushed changes:
   - Call `propose.py` to create a Pull Request via `skill-recall-propose`
   - Push commits to `proposal/<uuid>` branch
   - Create Gitea PR (head=proposal/<uuid>, base=main)
   - POST to skill-recall `/proposals`
4. For skills without unpushed changes but with `.evolve/llm_content.md`:
   - Call `auto-evolve-llm` to clone, write content, push proposal branch
5. Output summary of proposed/skipped/failed

### Auto-evolve-LLM flow (agent-generated content)

When `auto-evolve-llm` subcommand is used, it will:

1. Clone skill repo to a temp directory
2. Write provided improved content to SKILL.md in temp dir (local skill dir remains untouched)
3. Push to proposal branch and create PR via propose.py
4. Clean up temp directory

**Interface:**
```bash
# Recommended: write content to file first to avoid YAML/shell escaping issues
python3 scripts/evolve.py auto-evolve-llm \
  --skill <skill-name> \
  --cwe <CWE-code> \
  --content-file /tmp/evolved-<skill-name>.md

# Deprecated: --content passes YAML inline via shell — fragile with multi-line YAML
python3 scripts/evolve.py auto-evolve-llm \
  --skill <skill-name> \
  --cwe <CWE-code> \
  --content '<improved SKILL.md content>'
```

**NOTE:** The executing agent (LLM) generates the improved content itself. This script only handles temp dir → push → propose flow. No LLM API call is made here.

### CWE → Skill mapping

| CWE | Skill |
|-----|-------|
| CWE-78 | 03-command-injection |
| CWE-89 | 04-sql-injection |
| CWE-79 | 05-cross-site-scripting |
| CWE-22 | 06-path-traversal |
| CWE-918 | 07-ssrf |
| CWE-94/95 | 11-unsafe-code-execution |
| CWE-502 | 01-unsafe-deserialization |
| CWE-327 | 13-cryptographic-failures |
| CWE-352 | 17-csrf |
| CWE-915 | 20-api-mass-assignment |

## Tools

All git + version work goes through `scripts/evolve.py`. It only orchestrates
— it does not call an LLM. The main agent (you) does the reading, judging,
and editing using its own Read/Edit tools.

```
scripts/evolve.py preflight  <skill-dir>
scripts/evolve.py snapshot   <skill-dir> [--message ...]
scripts/evolve.py bump       <skill-dir> [--kind minor|patch|major]
scripts/evolve.py commit     <skill-dir> --message "..."
scripts/evolve.py tag        <skill-dir> [--version vX.Y.Z]
scripts/evolve.py revert     <skill-dir> --to vX.Y.Z
scripts/evolve.py status     <skill-dir>
scripts/evolve.py discover   [--session-id SESSION_ID]
scripts/evolve.py auto-evolve [--session-id SESSION_ID] [--dry-run]
scripts/evolve.py auto-evolve-llm [--session-id SESSION_ID]
```

All subcommands print JSON on stdout. They exit nonzero with a human error
message on failure. Read the exit code before continuing.

## The loop

### 1. Preflight

```bash
python3 <this-skill>/scripts/evolve.py preflight <skill-dir>
```

Exit 0 means: `.git` present, origin looks right, tree clean, current
version known. If exit nonzero, stop — fix the precondition first
(typically: ask the user to commit or stash, or run `skill-recall-onboard` to register first).

### 2. Gather failure context

Get the user to provide (or extract from conversation / heartbeat signals):

- **What went wrong** — failed execution details, error messages
- **Expected vs actual** — what should have happened
- At least one concrete **failure case** we can re-judge after editing

Read the target skill's `SKILL.md` and any referenced scripts or agent
files so you understand the current logic before changing it.

### 3. Judge current performance

Read `agents/judge.md` for the binary + soft-score + hints protocol.
For each failure case, produce the JSON record it describes. Save to
`<skill-dir>/.evolve/judge_before.json` (the `.evolve/` dir is git-ignored,
so this scratch file never pollutes the history).

### 4. Plan + apply improvements

Categorize each failure:

| Category | Typical fix |
|----------|-------------|
| **Logic error** | Rewrite the misleading section; explain the *why* |
| **Missing capability** | Add new instructions or a helper script |
| **Edge case** | Add explicit handling for the input shape |
| **Wrong approach** | Restructure — bump `--kind minor` or `major` |

Make minimal, targeted edits. Don't rewrite what wasn't broken. Keep
SKILL.md under 500 lines; move heavy content to `references/` or `scripts/`.

### 5. Bump + commit

Pick the bump kind based on the change's scope:

- **patch** — wording tweak, typo, added a clarifying sentence
- **minor** — new behavior, new script, new section (default)
- **major** — removed or changed a public contract (trigger phrases, script CLI, frontmatter keys)

```bash
python3 <this-skill>/scripts/evolve.py bump <skill-dir> --kind minor
python3 <this-skill>/scripts/evolve.py commit <skill-dir> \
    --message "evolve: <one-line summary of what changed and why>"
python3 <this-skill>/scripts/evolve.py tag <skill-dir>
```

`bump` just records the intended next-version in `.evolve/next_version`
— it doesn't tag. That lets you commit, re-judge, and only tag once
validation passes in step 6.

### 6. Validate with judge, then decide

Re-run the same judge protocol from step 3 on the same failure cases,
saving to `<skill-dir>/.evolve/judge_after.json`.

Compare the soft scores:

- **Improved or equal on every case** → keep the tag, continue to step 7.
- **Regressed on any case** → this approach was wrong. Go to retry (step 8).

### 7. Capture learning

Write or append to `<skill-dir>/TIP.md` with an entry like:

```markdown
## <YYYY-MM-DD> — v<previous> → v<new>

- **Trigger:** <why we evolved>
- **Change:** <what was modified>
- **Why it helped:** <the insight, stated so next evolution can reuse it>
- **Gotcha:** <anything that burned significant time>
```

This is **not optional**. Dated entries, never overwrite — the history of
mistakes is the main value.

Commit the TIP update (no bump; it's documentation):

```bash
python3 <this-skill>/scripts/evolve.py commit <skill-dir> \
    --message "docs: TIP for v<new>"
```

### 8. Retry loop (max 2) or abandon

If step 6 shows regression:

1. Revert the tag and the working tree in one shot:
   ```bash
   python3 <this-skill>/scripts/evolve.py revert <skill-dir> --to v<previous>
   ```
   This hard-resets to the previous tag and deletes any strictly-newer
   tags, so the next `bump` computes the correct number again.

2. Diagnose the regression — which case got worse, and why did the edit
   cause it? Often the fix for case A created a contradiction that
   broke case B.

3. Apply a **different** approach (not a minor tweak of the same edit)
   and return to step 5.

After 2 failed retries, revert once more and stop. Report to the user:

- The approaches tried and why each regressed
- The cases still failing
- Whether the failure pattern suggests a deeper issue (bad evals, skill's
  core design is wrong, missing context)

Do not leave the skill tagged at a worse version.

## After this skill finishes

Run `skill-recall-propose` to submit the evolution as a proposal. It will
push a proposal branch to Gitea, open a PR, and register with skill-recall
for review.

```bash
python3 <skills-dir>/skill-recall-propose/scripts/propose.py --skill <skill-dir> --summary "evolve: <summary>"
```

## Self-skip

This skill's own SKILL.md has `namespace: bootstrap`, so
`skill-recall-onboard` skips it — and this skill refuses to evolve any
other bootstrap skill for the same reason (they aren't managed by Gitea).

## Reference files

- `agents/judge.md` — LLM-as-Judge protocol (binary + 0-10 soft score + hints)
- `scripts/evolve.py` — git + version orchestration (no LLM calls)

