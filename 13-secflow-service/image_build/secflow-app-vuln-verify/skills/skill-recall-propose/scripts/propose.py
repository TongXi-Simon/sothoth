#!/usr/bin/env python3
"""Submit a local skill evolution as a Proposal.

Steps:
  1. Validate the target skill dir (managed, clean, has unpushed commits).
  2. Require a previously generated task-trace result for provenance.
  3. Push HEAD to proposal/<uuid> on Gitea.
  4. Open a Gitea Pull Request.
  5. POST /proposals to skill-recall.
"""
from __future__ import annotations

# --- secocto config: load ~/.config/secocto/.env (setdefault semantics) ---
from pathlib import Path as _P
_env_file = _P.home() / ".config" / "secocto" / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            __import__("os").environ.setdefault(_k.strip(), _v.strip().strip("\x27\""))

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import yaml


GITEA_URL = os.environ.get("GITEA_URL", "http://localhost:3010")
GITEA_TOKEN = os.environ.get("GITEA_TOKEN", "")
RECALL_URL = os.environ.get("SKILL_RECALL_URL", "http://localhost:8090")


def die(msg: str, code: int = 2) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


class MissingTaskTrace(RuntimeError):
    pass


def skip_missing_trace(detail: str) -> None:
    raise MissingTaskTrace(detail)


def run(cmd: list[str], cwd: Path, *, capture: bool = True) -> str:
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=capture, text=True)
    if p.returncode != 0:
        die(f"command failed: {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout.strip()


# ----- pre-flight on skill dir -----
def _fix_yaml_frontmatter(text: str) -> str:
    """Fix common YAML formatting issues that break yaml.safe_load().

    Handles:
    1. Unicode arrows → ASCII arrows
    2. Block scalar indicators (>, |, -) appearing as bare text in list items
    3. Unquoted metadata values containing colons (e.g. attack_class: CWE-xxx: Description)
    4. Lines that look like examples list items with -> in them
    """
    lines = text.split('\n')
    fixed = []
    in_frontmatter = True
    in_examples = False

    for line in lines:
        if in_frontmatter:
            # Detect frontmatter end
            if line.strip() == '---' or (fixed and not line.startswith(' ') and line.strip() == '---'):
                if fixed and fixed[-1].strip() == '':
                    in_frontmatter = False
                    fixed.append(line)
                    continue

            # Skip examples list entirely (often contains -> that triggers block scalar)
            if re.match(r'^examples:', line):
                in_examples = True
                continue
            if in_examples:
                if line and not line.startswith(' ') and not line.startswith('-'):
                    in_examples = False
                    fixed.append(line)
                continue

            # Fix 1: Unicode arrow → ASCII
            line = line.replace('→', '->')

            # Fix 2: Bare -> at end of line (looks like block scalar)
            stripped = line.rstrip()
            if stripped.endswith('->') and not stripped.startswith('#'):
                line = '"' + line.rstrip() + '"'

            # Fix 3: Unquoted metadata values with colons
            # e.g. "  attack_class: CWE-287/CWE-306: Authentication Bypass"
            # Only match lines that look like key: value where value starts with CWE
            m = re.match(r'^  (\w+): (CWE-.+)$', line)
            if m:
                key, val = m.group(1), m.group(2)
                if not val.startswith('"'):
                    line = f'  {key}: "{val}"'

            fixed.append(line)
        else:
            fixed.append(line)

    return '\n'.join(fixed)


def parse_frontmatter(skill_md: Path) -> dict:
    text = skill_md.read_text(encoding="utf-8")
    if not text.lstrip().startswith("---"):
        die(f"{skill_md} has no YAML frontmatter")
    body = text.lstrip()[3:]
    end = body.find("\n---")
    if end < 0:
        die(f"{skill_md} frontmatter is unterminated")
    frontmatter_text = body[:end]
    frontmatter_text = _fix_yaml_frontmatter(frontmatter_text)
    return yaml.safe_load(frontmatter_text) or {}


def detect_repo(skill_dir: Path) -> tuple[str, str]:
    """Return (namespace, slug) from the skill's origin URL."""
    if not (skill_dir / ".git").is_dir():
        die(f"{skill_dir} is not a git repo")
    remote = run(["git", "config", "remote.origin.url"], skill_dir)
    # http(s)?://host/<ns>/<slug>(.git)?
    m = re.search(r"://[^/]+/([^/]+)/([^/]+?)(?:\.git)?/?$", remote)
    if not m:
        die(f"cannot parse origin URL: {remote}")
    return m.group(1), m.group(2)


def latest_tag(skill_dir: Path) -> str | None:
    run(["git", "fetch", "--tags", "origin"], skill_dir)
    out = run(["git", "tag", "--list", "v*"], skill_dir)
    tags = [t for t in out.splitlines() if re.match(r"^v\d+\.\d+\.\d+$", t)]
    if not tags:
        return None

    def key(t: str) -> tuple[int, int, int]:
        return tuple(int(x) for x in t.lstrip("v").split("."))

    return sorted(tags, key=key)[-1]


def bump(version: str, kind: str = "minor") -> str:
    parts = [int(x) for x in version.lstrip("v").split(".")]
    while len(parts) < 3:
        parts.append(0)
    maj, mnr, pat = parts[:3]
    if kind == "major":
        maj, mnr, pat = maj + 1, 0, 0
    elif kind == "minor":
        mnr, pat = mnr + 1, 0
    else:
        pat += 1
    return f"v{maj}.{mnr}.{pat}"


# ----- task-trace (required provenance) -----
def grab_trace() -> dict:
    """Read task-trace result. Fail if trace is unavailable."""
    candidates = [
        Path(__file__).resolve().parents[2] / "task-trace" / "scripts" / "trace.py",
        Path.home() / ".claude" / "skills" / "task-trace" / "scripts" / "trace.py",
        Path.home() / ".config" / "opencode" / "skills" / "task-trace" / "scripts" / "trace.py",
        Path.home() / ".config" / "kilo" / "skills" / "task-trace" / "scripts" / "trace.py",
    ]
    trace_script = next((p for p in candidates if p.exists()), None)
    if not trace_script:
        skip_missing_trace(f"task-trace not installed (checked: {', '.join(str(p) for p in candidates)})")
    try:
        p = subprocess.run(
            ["python3", str(trace_script)],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        skip_missing_trace(f"task-trace invocation failed: {e}")
    if p.returncode != 0:
        skip_missing_trace((p.stderr or "task-trace exited nonzero").strip()[:200])
    try:
        trace = json.loads(p.stdout)
    except Exception:
        skip_missing_trace("task-trace stdout was not JSON")
    if not trace.get("session_id") or not trace.get("local_path"):
        skip_missing_trace("task-trace result missing session_id/local_path")
    return trace


# ----- main -----
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", required=True, help="path to skill dir")
    ap.add_argument("--summary", required=True)
    ap.add_argument("--bump", default="minor", choices=("major", "minor", "patch"),
                    help="how to compute proposed_version from base_version "
                         "(used only if --proposed-version is omitted)")
    ap.add_argument("--proposed-version", default=None)
    ap.add_argument("--branch", default=None,
                    help="override branch name; default is proposal/<uuid>")
    ap.add_argument("--actor", default=None,
                    help="created_by string; default reads $USER")
    ap.add_argument("--gitea-url", default=GITEA_URL)
    ap.add_argument("--gitea-token", default=GITEA_TOKEN)
    ap.add_argument("--recall-url", default=RECALL_URL)
    args = ap.parse_args()

    if not args.gitea_token:
        die("GITEA_TOKEN env or --gitea-token required")
    if not shutil.which("git"):
        die("git CLI not on PATH")

    skill_dir = Path(args.skill).expanduser().resolve()
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        die(f"no SKILL.md in {skill_dir}")
    fm = parse_frontmatter(skill_md)
    if fm.get("namespace") == "bootstrap":
        die("refusing to propose a bootstrap skill")

    ns, slug = detect_repo(skill_dir)
    full = f"{ns}/{slug}"

    # working tree must be clean
    status = run(["git", "status", "--porcelain"], skill_dir)
    if status:
        die("working tree has uncommitted changes — commit or stash first")

    base = latest_tag(skill_dir)
    if not base:
        die("no v*.*.* tag on origin yet — onboard the skill first")

    head = run(["git", "rev-parse", "HEAD"], skill_dir)
    tag_sha = run(["git", "rev-parse", base], skill_dir)
    if head == tag_sha:
        die(f"HEAD ({head[:8]}) is at latest_tag {base}; nothing to propose")

    proposed = args.proposed_version or bump(base, args.bump)
    branch = args.branch or f"proposal/{uuid.uuid4().hex[:12]}"

    # Require provenance before any remote side effects. If task-trace is missing,
    # skip cleanly and do not push/create PR.
    try:
        trace = grab_trace()
    except MissingTaskTrace as e:
        print(json.dumps({
            "ok": False,
            "skipped": True,
            "skipped_reason": "missing_task_trace",
            "detail": str(e),
        }, indent=2, ensure_ascii=False))
        return 0
    print(f"[propose] session_trace: agent={trace.get('agent')} "
          f"session_id={trace.get('session_id')}", file=sys.stderr, flush=True)

    print(f"[propose] {full}: {base} → {proposed} via {branch}", file=sys.stderr, flush=True)

    # push proposal branch
    push_url = f"{args.gitea_url.rstrip('/')}/{ns}/{slug}.git"
    env = {**os.environ, "http_proxy": "", "https_proxy": "",
           "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": ""}
    push = subprocess.run(
        ["git", "-c", "credential.helper=",
         "-c", f"http.extraheader=Authorization: token {args.gitea_token}",
         "push", push_url, f"HEAD:{branch}"],
        cwd=str(skill_dir), env=env, capture_output=True, text=True,
    )
    if push.returncode != 0:
        die(f"git push proposal branch failed: {(push.stderr or push.stdout).strip()[:300]}")

    # open PR (idempotent if already open for this head)
    with httpx.Client(base_url=args.gitea_url + "/api/v1",
                      headers={"Authorization": f"token {args.gitea_token}"},
                      timeout=30, trust_env=False) as cli:
        r = cli.post(
            f"/repos/{ns}/{slug}/pulls",
            json={"head": branch, "base": "main",
                  "title": f"propose {proposed}: {args.summary[:60]}",
                  "body": args.summary},
        )
        if r.status_code in (200, 201):
            pr_number = int(r.json().get("number"))
        elif r.status_code == 409:
            # already exists — find it
            r = cli.get(f"/repos/{ns}/{slug}/pulls", params={"state": "open", "limit": 50})
            r.raise_for_status()
            matching = [p for p in r.json() if (p.get("head") or {}).get("ref") == branch]
            pr_number = int(matching[0]["number"]) if matching else None
        else:
            die(f"open PR failed: {r.status_code} {r.text[:200]}")

    # submit to skill-recall
    actor = args.actor or os.environ.get("USER") or "unknown"
    body = {
        "full_name": full,
        "branch": branch,
        "pr_number": pr_number,
        "base_version": base,
        "proposed_version": proposed,
        "summary": args.summary,
        "created_by": actor,
        "session_trace": trace,
    }
    with httpx.Client(timeout=30, trust_env=False) as cli:
        r = cli.post(f"{args.recall_url}/proposals", json=body)
    if r.status_code != 200:
        die(f"skill-recall /proposals failed: {r.status_code} {r.text[:200]}")
    data = r.json()
    out = {
        "proposal_id": data.get("id"),
        "branch": branch,
        "pr_number": pr_number,
        "base_version": base,
        "proposed_version": proposed,
        "session_trace": trace,
        "pr_url": f"{args.gitea_url}/{ns}/{slug}/pulls/{pr_number}" if pr_number else None,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
