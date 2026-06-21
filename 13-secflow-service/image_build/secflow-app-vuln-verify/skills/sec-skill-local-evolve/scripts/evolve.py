#!/usr/bin/env python3
"""Local, offline evolution helper for skills managed by skill-recall.

This script only orchestrates git and filesystem operations. It never calls
an LLM — the main agent drives judge + edit + TIP.md authoring itself, and
uses this script to snapshot, version-bump, commit, and revert.

Subcommands (see --help):
  preflight   verify .git + origin + clean tree; print current version
  snapshot    git commit a baseline before any edits (idempotent if clean)
  bump        compute the next semver tag and remember it (minor|patch|major)
  commit      commit any staged/working changes with a message
  tag         tag HEAD with the previously-computed next version
  revert      git reset --hard back to a given tag; use after failed retries
  status      print a summary for the agent
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


SEMVER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
EVOLVE_STATE_DIR = ".evolve"
NEXT_VERSION_FILE = "next_version"

_SESSION_ID_CWD_RE = re.compile(r"/data/files/[^/]+/app/[^/]+/([A-Za-z0-9_-]{8,})")


def _session_id_from_pwd() -> str | None:
    m = _SESSION_ID_CWD_RE.search(os.getcwd())
    return m.group(1) if m else None


def _agent_type() -> str:
    agent = os.environ.get("SECOCTO_AGENT_TYPE", "").strip().lower()
    if agent not in {"opencode", "kilocode", "pi", "claude"}:
        raise SystemExit("ERROR: SECOCTO_AGENT_TYPE must be one of: opencode, kilocode, pi, claude")
    return agent


def _session_id() -> str | None:
    agent = _agent_type()
    if agent == "kilocode":
        return os.environ.get("KILO_SESSION_ID")
    if agent == "opencode":
        return os.environ.get("OPENCODE_SESSION_ID")
    if agent == "claude":
        return os.environ.get("CLAUDE_CODE_SESSION_ID")
    if agent == "pi":
        return _session_id_from_pwd()
    return None



def _find_trace_metadata(trace_cache: Path, agent: str, session_id: str) -> Path:
    candidates = [agent, "claude", "opencode", "kilocode", "pi"]
    seen = set()
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        p = trace_cache / name / f"trace-{session_id}.json"
        if p.exists():
            return p
    return trace_cache / agent / f"trace-{session_id}.json"


def _run(cwd: Path, args: list[str], *, check: bool = True, env: dict = None) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=str(cwd),
                       capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


# ---------- repo discovery ----------
def _resolve_skill_dir(arg: str) -> Path:
    p = Path(arg).expanduser().resolve()
    if not p.is_dir():
        raise SystemExit(f"not a directory: {p}")
    if not (p / "SKILL.md").is_file():
        raise SystemExit(f"no SKILL.md in {p} — is this really a skill dir?")
    if not (p / ".git").exists():
        raise SystemExit(
            f"{p} has no .git — this skill is not under skill-recall management.\n"
            "Run skill-recall-onboard first, then retry."
        )
    return p


def _origin_url(skill_dir: Path) -> str:
    r = _run(skill_dir, ["remote", "get-url", "origin"], check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def _require_managed(skill_dir: Path, expected_host: str | None) -> str:
    url = _origin_url(skill_dir)
    if not url:
        raise SystemExit(
            f"{skill_dir}/.git has no origin remote. This is unexpected state.\n"
            "Run skill-recall-onboard to re-register, or fix the remote manually."
        )
    if expected_host and expected_host not in url:
        raise SystemExit(
            f"{skill_dir} origin is {url}, which does not contain host {expected_host!r}.\n"
            "Foreign remote — refusing to evolve. Pass --no-host-check to override."
        )
    return url


# ---------- version helpers ----------
def _parsed_tags(skill_dir: Path) -> list[tuple[int, int, int]]:
    out = _run(skill_dir, ["tag"]).stdout.splitlines()
    parsed = []
    for t in out:
        m = SEMVER_RE.match(t.strip())
        if m:
            parsed.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    parsed.sort()
    return parsed


def _current_version(skill_dir: Path) -> str:
    tags = _parsed_tags(skill_dir)
    if not tags:
        return "v0.0.0"
    maj, mnr, pat = tags[-1]
    return f"v{maj}.{mnr}.{pat}"


def _bump(current: str, kind: str) -> str:
    m = SEMVER_RE.match(current)
    if not m:
        raise SystemExit(f"current version {current!r} is not semver; fix manually")
    maj, mnr, pat = map(int, m.groups())
    if kind == "major":
        return f"v{maj + 1}.0.0"
    if kind == "minor":
        return f"v{maj}.{mnr + 1}.0"
    if kind == "patch":
        return f"v{maj}.{mnr}.{pat + 1}"
    raise SystemExit(f"unknown bump kind: {kind}")


def _state_dir(skill_dir: Path) -> Path:
    d = skill_dir / EVOLVE_STATE_DIR
    d.mkdir(exist_ok=True)
    gitignore = d / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n")
    return d


def _read_next(skill_dir: Path) -> str | None:
    f = skill_dir / EVOLVE_STATE_DIR / NEXT_VERSION_FILE
    return f.read_text().strip() if f.is_file() else None


def _write_next(skill_dir: Path, version: str) -> None:
    f = _state_dir(skill_dir) / NEXT_VERSION_FILE
    f.write_text(version + "\n")


def _clear_next(skill_dir: Path) -> None:
    f = skill_dir / EVOLVE_STATE_DIR / NEXT_VERSION_FILE
    if f.is_file():
        f.unlink()


def _is_dirty(skill_dir: Path) -> bool:
    return bool(_run(skill_dir, ["status", "--porcelain"]).stdout.strip())


# ---------- subcommands ----------
def cmd_preflight(args: argparse.Namespace) -> int:
    p = _resolve_skill_dir(args.skill_dir)
    expected = None if args.no_host_check else args.gitea_host
    origin = _require_managed(p, expected)
    cur = _current_version(p)
    dirty = _is_dirty(p)
    branch = _run(p, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    _emit({
        "ok": True,
        "skill_dir": str(p),
        "origin": origin,
        "branch": branch,
        "current_version": cur,
        "dirty": dirty,
        "pending_next_version": _read_next(p),
    })
    return 1 if dirty and not args.allow_dirty else 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Commit the current tree as a baseline, if dirty. No tag."""
    p = _resolve_skill_dir(args.skill_dir)
    _require_managed(p, None if args.no_host_check else args.gitea_host)
    if not _is_dirty(p):
        _emit({"ok": True, "action": "skipped", "reason": "tree is clean"})
        return 0
    _run(p, ["add", "-A"])
    _run(p, ["commit", "-m", args.message or "evolve: snapshot before edits"])
    _emit({"ok": True, "action": "committed", "sha": _run(p, ["rev-parse", "HEAD"]).stdout.strip()})
    return 0


def cmd_bump(args: argparse.Namespace) -> int:
    p = _resolve_skill_dir(args.skill_dir)
    _require_managed(p, None if args.no_host_check else args.gitea_host)
    cur = _current_version(p)
    nxt = _bump(cur, args.kind)
    _write_next(p, nxt)
    _emit({"ok": True, "current_version": cur, "next_version": nxt, "kind": args.kind})
    return 0


def cmd_commit(args: argparse.Namespace) -> int:
    p = _resolve_skill_dir(args.skill_dir)
    _require_managed(p, None if args.no_host_check else args.gitea_host)
    if not _is_dirty(p):
        _emit({"ok": True, "action": "skipped", "reason": "no changes to commit"})
        return 0
    _run(p, ["add", "-A"])
    _run(p, ["commit", "-m", args.message])
    _emit({"ok": True, "action": "committed", "sha": _run(p, ["rev-parse", "HEAD"]).stdout.strip()})
    return 0


def cmd_tag(args: argparse.Namespace) -> int:
    p = _resolve_skill_dir(args.skill_dir)
    _require_managed(p, None if args.no_host_check else args.gitea_host)
    target = args.version or _read_next(p)
    if not target:
        raise SystemExit("no version to tag. Run `bump` first or pass --version.")
    if not SEMVER_RE.match(target):
        raise SystemExit(f"version {target!r} is not semver (vX.Y.Z)")
    existing = set(_run(p, ["tag"]).stdout.splitlines())
    if target in existing:
        raise SystemExit(f"tag {target} already exists; bump again or revert first")
    if _is_dirty(p):
        raise SystemExit("working tree dirty — commit first, then tag")
    _run(p, ["tag", "-a", target, "-m", args.message or f"evolve: {target}"])
    _clear_next(p)
    _emit({"ok": True, "action": "tagged", "version": target,
           "sha": _run(p, ["rev-parse", "HEAD"]).stdout.strip()})
    return 0


def cmd_revert(args: argparse.Namespace) -> int:
    """Hard-reset back to a tag. Destroys anything not tagged after it."""
    p = _resolve_skill_dir(args.skill_dir)
    _require_managed(p, None if args.no_host_check else args.gitea_host)
    target = args.to
    if not SEMVER_RE.match(target):
        raise SystemExit(f"target {target!r} is not semver (vX.Y.Z)")
    existing = set(_run(p, ["tag"]).stdout.splitlines())
    if target not in existing:
        raise SystemExit(f"tag {target} does not exist locally")
    # Remove any tags strictly newer than target so a subsequent bump doesn't
    # think we've moved past a version we just discarded.
    tgt_parts = tuple(map(int, SEMVER_RE.match(target).groups()))
    newer = [f"v{a}.{b}.{c}" for (a, b, c) in _parsed_tags(p) if (a, b, c) > tgt_parts]
    for t in newer:
        _run(p, ["tag", "-d", t])
    _run(p, ["reset", "--hard", target])
    _clear_next(p)
    _emit({"ok": True, "action": "reverted", "to": target, "dropped_tags": newer})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    p = _resolve_skill_dir(args.skill_dir)
    origin = _origin_url(p)
    cur = _current_version(p)
    dirty = _is_dirty(p)
    last = _run(p, ["log", "-1", "--pretty=%h %s"]).stdout.strip()
    _emit({
        "skill_dir": str(p),
        "origin": origin,
        "current_version": cur,
        "pending_next_version": _read_next(p),
        "dirty": dirty,
        "last_commit": last,
        "all_versions": [f"v{a}.{b}.{c}" for (a, b, c) in _parsed_tags(p)],
    })
    return 0


# ---------- discover subcommand ----------
CWE_SKILL_MAP = {
    "CWE-78": "03-command-injection",
    "CWE-89": "04-sql-injection",
    "CWE-79": "05-cross-site-scripting",
    "CWE-22": "06-path-traversal",
    "CWE-918": "07-ssrf",
    "CWE-94": "11-unsafe-code-execution",
    "CWE-95": "11-unsafe-code-execution",
    "CWE-502": "01-unsafe-deserialization",
    "CWE-327": "13-cryptographic-failures",
    "CWE-352": "17-csrf",
    "CWE-915": "20-api-mass-assignment",
}


def cmd_discover(args: argparse.Namespace) -> int:
    """Discover which skills need evolution based on session usage and CWE findings."""
    import json
    import os
    import re
    from pathlib import Path

    session_id = _session_id()
    if not session_id:
        print("ERROR: no session ID found in environment", file=sys.stderr)
        return 2

    agent = _agent_type()

    # Load trace metadata to get trace_url
    trace_cache = Path.home() / ".cache" / "task-trace"
    trace_file = _find_trace_metadata(trace_cache, agent, session_id)

    trace_url = ""
    trace_jsonl = None
    if trace_file.exists():
        try:
            trace_data = json.loads(trace_file.read_text())
            trace_url = trace_data.get("trace_url", "")
            trace_jsonl = Path(trace_data.get("local_path", ""))
        except Exception:
            pass

    # Parse the transcript JSONL for skill invocations and CWE codes
    cwes_found = set()
    invoked_skills = set()
    skill_pattern = re.compile(r'"commandName"\s*:\s*"([^"]+)"', re.IGNORECASE)
    cwe_pattern = re.compile(r'CWE-(\d+)', re.IGNORECASE)

    if trace_jsonl and trace_jsonl.exists():
        try:
            content = trace_jsonl.read_text()
            # Parse CWE codes
            for match in cwe_pattern.finditer(content):
                cwe_num = match.group(1)
                cwes_found.add(f"CWE-{cwe_num}")
            # Parse skill invocations
            for match in skill_pattern.finditer(content):
                skill_name = match.group(1).strip()
                if skill_name and not skill_name.startswith("_"):
                    invoked_skills.add(skill_name)
        except Exception:
            pass

    skills_dir = Path(__file__).resolve().parents[2]

    # CWE matched skills → reason = "cwe_findings"
    candidates = []
    cwe_matched_skills = set()
    for cwe in cwes_found:
        if cwe in CWE_SKILL_MAP:
            skill = CWE_SKILL_MAP[cwe]
            if (skills_dir / skill).is_dir():
                candidates.append({
                    "skill": skill,
                    "reason": "cwe_findings",
                    "cwes": [cwe],
                })
                cwe_matched_skills.add(skill)

    # Invoked but not CWE matched → reason = "used_in_session"
    for skill_name in invoked_skills:
        if skill_name not in cwe_matched_skills and (skills_dir / skill_name).is_dir():
            candidates.append({
                "skill": skill_name,
                "reason": "used_in_session",
                "cwes": [],
            })

    # Legacy field: suggested_skills for backward compat
    suggested_skills = [c for c in candidates if c["reason"] == "cwe_findings"]

    audit_skills = {
        "01-unsafe-deserialization", "03-command-injection", "04-sql-injection",
        "05-cross-site-scripting", "06-path-traversal", "07-ssrf",
        "11-unsafe-code-execution", "13-cryptographic-failures",
        "17-csrf", "20-api-mass-assignment"
    }

    available_audit_skills = [s for s in audit_skills if (skills_dir / s).is_dir()]

    result = {
        "session_id": session_id,
        "agent": agent,
        "invoked_skills": sorted(list(invoked_skills)),
        "cwes_found": sorted(list(cwes_found)),
        "available_audit_skills": sorted(available_audit_skills),
        "suggested_skills": suggested_skills,
        "candidates": candidates,
        "trace_url": trace_url,
    }
    _emit(result)
    return 0


# ---------- auto-evolve subcommand (original - requires local changes) ----------
def cmd_auto_evolve(args: argparse.Namespace) -> int:
    """Automatically discover and evolve skills based on session findings via proposal.

    Candidates include both CWE-matched skills and all invoked skills.
    Before proposing, checks .evolve/judge_result.json: if score == 10
    and is_correct == true, the skill is considered flawless and skipped.
    """
    import json
    import os
    import re
    import subprocess
    from pathlib import Path

    session_id = _session_id()
    if not session_id:
        print("ERROR: no session ID found in environment", file=sys.stderr)
        return 2

    agent = _agent_type()

    trace_cache = Path.home() / ".cache" / "task-trace"
    trace_file = _find_trace_metadata(trace_cache, agent, session_id)

    trace_jsonl = None
    if trace_file.exists():
        try:
            trace_data = json.loads(trace_file.read_text())
            trace_jsonl = Path(trace_data.get("local_path", ""))
        except Exception:
            pass

    cwes_found = set()
    invoked_skills = set()
    cwe_pattern = re.compile(r'CWE-(\d+)', re.IGNORECASE)
    skill_pattern = re.compile(r'"commandName"\s*:\s*"([^"]+)"', re.IGNORECASE)

    if trace_jsonl and trace_jsonl.exists():
        try:
            content = trace_jsonl.read_text()
            for match in cwe_pattern.finditer(content):
                cwes_found.add(f"CWE-{match.group(1)}")
            for match in skill_pattern.finditer(content):
                skill_name = match.group(1).strip()
                if skill_name and not skill_name.startswith("_"):
                    invoked_skills.add(skill_name)
        except Exception:
            pass

    skills_dir = Path(__file__).resolve().parents[2]

    # Build candidates: CWE matched + invoked skills
    candidates = []
    cwe_matched_skills = set()
    for cwe in cwes_found:
        if cwe in CWE_SKILL_MAP:
            skill_name = CWE_SKILL_MAP[cwe]
            if (skills_dir / skill_name).is_dir() and skill_name not in cwe_matched_skills:
                candidates.append({
                    "skill": skill_name,
                    "reason": "cwe_findings",
                    "cwes": [cwe],
                })
                cwe_matched_skills.add(skill_name)

    for skill_name in invoked_skills:
        if skill_name not in cwe_matched_skills and (skills_dir / skill_name).is_dir():
            candidates.append({
                "skill": skill_name,
                "reason": "used_in_session",
                "cwes": [],
            })

    if not candidates:
        result = {
            "action": "no_evolution_needed",
            "reason": "No skills were invoked in this session",
            "session_id": session_id,
        }
        _emit(result)
        return 0

    proposed = []
    skipped = []
    failed = []

    for candidate in candidates:
        skill_name = candidate["skill"]
        skill_dir = skills_dir / skill_name
        try:
            # Check if skill is managed
            if not (skill_dir / ".git").exists():
                skipped.append({
                    "skill": skill_name,
                    "reason": "not a managed skill (no .git)"
                })
                continue

            origin = _origin_url(skill_dir)
            if not origin:
                skipped.append({
                    "skill": skill_name,
                    "reason": "no origin remote"
                })
                continue

            # Check .evolve/judge_result.json — skip if LLM judged flawless
            judge_file = skill_dir / ".evolve" / "judge_result.json"
            if judge_file.is_file():
                try:
                    judge_data = json.loads(judge_file.read_text())
                    is_correct = judge_data.get("binary", {}).get("is_correct", False)
                    score = judge_data.get("soft_score", {}).get("score", 0)
                    if is_correct and score == 10:
                        skipped.append({
                            "skill": skill_name,
                            "reason": "judged_flawless",
                            "score": score,
                        })
                        continue
                except Exception:
                    pass

            # Check for unpushed commits
            branch = _run(skill_dir, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
            subprocess.run(
                ["git", "fetch", "--tags", "origin"],
                cwd=str(skill_dir), capture_output=True, text=True,
            )

            unpushed_commits = _run(skill_dir, ["log", f"origin/{branch}..HEAD", "--oneline"]).stdout.strip().splitlines() if _run(skill_dir, ["rev-parse", "--verify", f"origin/{branch}"], check=False).returncode == 0 else []

            local_tags = set(_run(skill_dir, ["tag"]).stdout.splitlines())
            remote_tag_lines = _run(skill_dir, ["ls-remote", "--tags", "origin"]).stdout.splitlines()
            remote_tags = {
                line.rsplit("refs/tags/", 1)[-1].replace("^{}", "")
                for line in remote_tag_lines if "refs/tags/" in line
            }
            unpushed_tags = sorted(local_tags - remote_tags)

            has_changes = bool(unpushed_commits) or bool(unpushed_tags)

            if not has_changes:
                skill_md_path = skill_dir / "SKILL.md"
                if not skill_md_path.exists():
                    skipped.append({
                        "skill": skill_name,
                        "reason": "no unpushed changes and no SKILL.md"
                    })
                    continue

                cwe_for_skill = candidate.get("cwes", [])

                tmp_content_file = skill_dir / ".evolve" / "llm_content.md"

                if not tmp_content_file.exists():
                    skipped.append({
                        "skill": skill_name,
                        "reason": "no evolved content yet (agent should generate content in .evolve/llm_content.md)"
                    })
                    continue

                new_content = tmp_content_file.read_text(encoding="utf-8")
                current_content = skill_md_path.read_text(encoding="utf-8")

                if new_content.strip() == current_content.strip():
                    skipped.append({
                        "skill": skill_name,
                        "reason": "evolved content is identical to current content"
                    })
                    continue

                evolve_llm_script = Path(__file__)
                result_llm = subprocess.run(
                    [
                        sys.executable, str(evolve_llm_script),
                        "auto-evolve-llm",
                        "--skill", skill_name,
                        "--cwe", ",".join(cwe_for_skill),
                        "--content-file", str(tmp_content_file),
                    ],
                    cwd=str(skill_dir),
                    capture_output=True, text=True,
                )
                if result_llm.returncode == 0:
                    proposed.append({"skill": skill_name, "action": "auto-evolve-llm"})
                else:
                    skipped.append({
                        "skill": skill_name,
                        "reason": f"auto-evolve-llm failed: {result_llm.stderr[:200]}"
                    })
                continue

            cwe_for_skill = candidate.get("cwes", [])
            summary = f"auto-propose: evolved based on {', '.join(cwe_for_skill)} findings" if cwe_for_skill else f"auto-propose: evolved {skill_name} based on session usage"

            propose_script = Path(__file__).parent.parent.parent / "skill-recall-propose" / "scripts" / "propose.py"

            if not propose_script.exists():
                failed.append({
                    "skill": skill_name,
                    "error": f"propose.py not found at {propose_script}"
                })
                continue

            gitea_url = os.environ.get("GITEA_URL", "http://localhost:3010")
            gitea_token = os.environ.get("GITEA_TOKEN", "")
            recall_url = os.environ.get("SKILL_RECALL_URL", "http://localhost:8090")

            cmd = [
                "python3", str(propose_script),
                "--skill", str(skill_dir),
                "--summary", summary,
                "--gitea-url", gitea_url,
                "--gitea-token", gitea_token,
                "--recall-url", recall_url,
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode != 0:
                failed.append({
                    "skill": skill_name,
                    "error": result.stderr.strip() or result.stdout.strip()
                })
                continue

            try:
                propose_result = json.loads(result.stdout)
                proposed.append({
                    "skill": skill_name,
                    "proposal_id": propose_result.get("proposal_id"),
                    "pr_number": propose_result.get("pr_number"),
                    "pr_url": propose_result.get("pr_url"),
                    "base_version": propose_result.get("base_version"),
                    "proposed_version": propose_result.get("proposed_version"),
                    "cwes": cwe_for_skill,
                })
            except json.JSONDecodeError:
                proposed.append({
                    "skill": skill_name,
                    "raw_output": result.stdout.strip(),
                    "cwes": cwe_for_skill,
                })

        except Exception as e:
            failed.append({
                "skill": skill_name,
                "error": str(e)
            })

    result = {
        "session_id": session_id,
        "proposed": proposed,
        "skipped": skipped,
        "failed": failed,
        "total_proposed": len(proposed),
        "total_skipped": len(skipped),
        "total_failed": len(failed),
    }
    _emit(result)

    return 1 if failed else 0


# ---------- auto-evolve-llm subcommand ----------
def cmd_auto_evolve_llm(args: argparse.Namespace) -> int:
    """Automatically evolve skills by applying agent-generated content via proposal.

    Flow:
    1. Clone skill to temp dir (from origin)
    2. Write improved content to SKILL.md in temp dir
    3. Push to proposal branch
    4. Create PR via propose.py
    5. Clean up temp dir

    NOTE: Agent generates the improved content itself (no LLM API call here).
    """
    import json
    import os
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    skill_name = args.skill
    cwe = args.cwe

    # Prefer --content-file to avoid shell escaping issues with multi-line YAML
    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8")
    elif args.content:
        content = args.content
    else:
        print("ERROR: --skill and --content or --content-file are required", file=sys.stderr)
        return 2

    session_id = _session_id() or "unknown"

    # Find skill dir
    skills_dir = Path(__file__).resolve().parents[2]
    skill_dir = skills_dir / skill_name

    if not skill_dir.exists():
        print(f"ERROR: skill dir not found: {skill_dir}", file=sys.stderr)
        return 1

    # Get origin URL
    origin = _origin_url(skill_dir)
    if not origin:
        print(f"ERROR: no origin remote for {skill_name}", file=sys.stderr)
        return 1

    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp(prefix=f"evolve-{skill_name}-")
        clone_dir = Path(temp_dir) / skill_name

        # Clone skill to temp dir
        subprocess.run(
            ["git", "clone", origin, str(clone_dir)],
            capture_output=True, text=True, timeout=30
        )

        # Write improved content to SKILL.md in temp dir
        (clone_dir / "SKILL.md").write_text(content, encoding="utf-8")

        # Configure git user for commit
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "sec-skill-local-evolve",
            "GIT_AUTHOR_EMAIL": "evolve@local",
            "GIT_COMMITTER_NAME": "sec-skill-local-evolve",
            "GIT_COMMITTER_EMAIL": "evolve@local",
        }

        # Commit changes in temp dir
        subprocess.run(["git", "add", "-A"], cwd=str(clone_dir), env=git_env)
        commit_msg = f"evolve: auto-evolve based on {cwe} findings" if cwe else f"evolve: auto-evolve {skill_name}"
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=str(clone_dir), env=git_env, capture_output=True
        )

        propose_script = Path(__file__).parent.parent.parent / "skill-recall-propose" / "scripts" / "propose.py"
        if not propose_script.exists():
            print(f"ERROR: propose.py not found at {propose_script}", file=sys.stderr)
            return 1

        gitea_url = os.environ.get("GITEA_URL", "http://localhost:3010")
        gitea_token = os.environ.get("GITEA_TOKEN", "")
        recall_url = os.environ.get("SKILL_RECALL_URL", "http://localhost:8090")

        summary = f"auto-propose: evolved {skill_name} based on {cwe} findings" if cwe else f"auto-propose: evolved {skill_name}"

        cmd = [
            "python3", str(propose_script),
            "--skill", str(clone_dir),
            "--summary", summary,
            "--gitea-url", gitea_url,
            "--gitea-token", gitea_token,
            "--recall-url", recall_url,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode == 0:
            try:
                propose_result = json.loads(result.stdout)
                _emit({
                    "skill": skill_name,
                    "cwe": cwe,
                    "proposal_id": propose_result.get("proposal_id"),
                    "pr_number": propose_result.get("pr_number"),
                    "pr_url": propose_result.get("pr_url"),
                    "base_version": propose_result.get("base_version"),
                    "proposed_version": propose_result.get("proposed_version"),
                })
            except json.JSONDecodeError:
                _emit({
                    "skill": skill_name,
                    "cwe": cwe,
                    "raw_output": result.stdout.strip(),
                })
        else:
            print(f"ERROR: propose failed: {result.stderr.strip()}", file=sys.stderr)
            return 1

        return 0

    except Exception as e:
        print(f"ERROR: {str(e)}", file=sys.stderr)
        return 1
    finally:
        if temp_dir and Path(temp_dir).exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


# ---------- CLI ----------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    _default_gitea_host = ""
    if os.environ.get("GITEA_URL"):
        from urllib.parse import urlparse
        parsed = urlparse(os.environ["GITEA_URL"])
        _default_gitea_host = parsed.netloc or parsed.hostname or ""
    ap.add_argument("--gitea-host", default=_default_gitea_host,
                    help="substring origin URL must contain (default: from GITEA_URL env)")
    ap.add_argument("--no-host-check", action="store_true",
                    help="skip the origin-host check (use for foreign test setups)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("preflight", help="check repo state before evolving")
    pf.add_argument("skill_dir")
    pf.add_argument("--allow-dirty", action="store_true")
    pf.set_defaults(func=cmd_preflight)

    snap = sub.add_parser("snapshot", help="commit current tree as baseline")
    snap.add_argument("skill_dir")
    snap.add_argument("--message", default="")
    snap.set_defaults(func=cmd_snapshot)

    bp = sub.add_parser("bump", help="compute and remember the next version")
    bp.add_argument("skill_dir")
    bp.add_argument("--kind", choices=["patch", "minor", "major"], default="minor")
    bp.set_defaults(func=cmd_bump)

    cm = sub.add_parser("commit", help="commit current changes")
    cm.add_argument("skill_dir")
    cm.add_argument("--message", required=True)
    cm.set_defaults(func=cmd_commit)

    tg = sub.add_parser("tag", help="tag HEAD with the pending next-version")
    tg.add_argument("skill_dir")
    tg.add_argument("--version", help="override the pending next version")
    tg.add_argument("--message", default="")
    tg.set_defaults(func=cmd_tag)

    rv = sub.add_parser("revert", help="hard-reset back to a tag")
    rv.add_argument("skill_dir")
    rv.add_argument("--to", required=True)
    rv.set_defaults(func=cmd_revert)

    st = sub.add_parser("status", help="print repo state as JSON")
    st.add_argument("skill_dir")
    st.set_defaults(func=cmd_status)

    disc = sub.add_parser("discover", help="auto-discover skills needing evolution based on session")
    disc.add_argument("--session-id", default=None, help="session ID (auto-detected if omitted)")
    disc.set_defaults(func=cmd_discover)

    auto = sub.add_parser("auto-evolve", help="automatically discover and evolve skills based on session findings")
    auto.add_argument("--session-id", default=None, help="session ID (auto-detected if omitted)")
    auto.add_argument("--dry-run", action="store_true", help="show what would be evolved without making changes")
    auto.set_defaults(func=cmd_auto_evolve)

    auto_llm = sub.add_parser("auto-evolve-llm", help="apply agent-generated improved content via proposal (no LLM call here)")
    auto_llm.add_argument("--skill", required=True, help="skill name to evolve")
    auto_llm.add_argument("--cwe", default=None, help="CWE code that triggered the evolution")
    auto_llm.add_argument("--content", required=False, default=None,
                         help="improved SKILL.md content (generated by the executing agent). "
                              "Prefer --content-file to avoid shell escaping issues.")
    auto_llm.add_argument("--content-file", required=False, default=None,
                         help="path to file containing improved SKILL.md content. "
                              "Recommended over --content to avoid shell escaping issues.")
    auto_llm.set_defaults(func=cmd_auto_evolve_llm)

    args = ap.parse_args()
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as e:  # pragma: no cover
        _emit({"ok": False, "error": str(e)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
