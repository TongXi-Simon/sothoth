#!/usr/bin/env python3
"""Bring local Claude skills under skill-recall management.

See ../SKILL.md for the full state-machine description.
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
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

try:
    import httpx
    import yaml
except ImportError as e:  # pragma: no cover
    sys.exit(f"missing dependency ({e.name}); run: pip install httpx pyyaml")


# ============================ args / env ============================
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    default_skills_dir = str(Path(__file__).resolve().parents[2])
    p.add_argument("--skills-dir", default=default_skills_dir,
                   help="default auto-detected from script location")
    p.add_argument("--org", default="demo",
                   help="Gitea org/namespace for new repos (default: demo)")
    p.add_argument("--gitea-url",
                   default=os.environ.get("GITEA_URL", "http://localhost:3010"))
    p.add_argument("--recall-url",
                   default=os.environ.get("SKILL_RECALL_URL", "http://localhost:8090"))
    p.add_argument("--gitea-token",
                   default=os.environ.get("GITEA_TOKEN", ""))
    p.add_argument("--skill", help="only process this one skill (debugging)")
    p.add_argument("--dry-run", action="store_true",
                   help="show the plan but do not write")
    p.add_argument("--no-project", action="store_true",
                   help="skip auto-discovery of project-level skills")
    p.add_argument("--json", action="store_true",
                   help="machine-readable JSON output")
    return p.parse_args()


# ========================= frontmatter parse =========================
def read_frontmatter(skill_md: Path) -> dict | None:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    s = text.lstrip("﻿").lstrip()
    if not s.startswith("---"):
        return None
    body = s[3:]
    # find closing '---' on its own line
    idx = 0
    while True:
        nl = body.find("\n---", idx)
        if nl == -1:
            return None
        after = nl + 4
        if after >= len(body) or body[after] in ("\n", "\r"):
            break
        idx = nl + 1
    fm_text = body[:nl]
    try:
        d = yaml.safe_load(fm_text)
        return d if isinstance(d, dict) else None
    except yaml.YAMLError:
        return None


# ============================ classification ============================
class Status(str, Enum):
    BOOTSTRAP        = "BOOTSTRAP"
    MANAGED_LOCAL    = "MANAGED_LOCAL"
    FOREIGN_REMOTE   = "FOREIGN_REMOTE"
    PLATFORM_HAS_IT  = "PLATFORM_HAS_IT"
    NEW              = "NEW"
    NO_SKILL_MD      = "NO_SKILL_MD"


@dataclasses.dataclass
class SkillEntry:
    slug: str
    path: Path
    status: Status
    source: str = "global"
    note: str = ""


def git_remote_url(skill_dir: Path) -> str | None:
    if not (skill_dir / ".git").exists():
        return None
    r = subprocess.run(
        ["git", "-C", str(skill_dir), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def origin_is_current_gitea(remote: str, gitea_url: str) -> bool:
    if not remote:
        return False
    if "://" not in remote:
        return False
    parsed_remote = urlparse(remote)
    parsed_gitea = urlparse(gitea_url)
    base_path = parsed_gitea.path.rstrip("/")
    return (
        parsed_remote.scheme in {"http", "https"}
        and parsed_remote.netloc == parsed_gitea.netloc
        and parsed_remote.path.startswith(f"{base_path}/" if base_path else "/")
    )


def classify(skill_dir: Path, gitea_url: str, org: str,
             recall: httpx.Client) -> SkillEntry:
    slug = skill_dir.name

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return SkillEntry(slug, skill_dir, Status.NO_SKILL_MD, note="no SKILL.md")

    fm = read_frontmatter(skill_md) or {}
    if (fm.get("namespace") or "").strip().lower() == "bootstrap":
        return SkillEntry(slug, skill_dir, Status.BOOTSTRAP, note="namespace=bootstrap")

    remote = git_remote_url(skill_dir)
    if remote is not None:
        if remote == "":
            return SkillEntry(slug, skill_dir, Status.MANAGED_LOCAL,
                              note=".git exists, no origin")
        if origin_is_current_gitea(remote, gitea_url) and remote_path_matches(remote, gitea_url, org, slug):
            return SkillEntry(slug, skill_dir, Status.MANAGED_LOCAL,
                              note=f"origin={_redact(remote)}")
        return SkillEntry(slug, skill_dir, Status.FOREIGN_REMOTE,
                          note=f"origin={_redact(remote)}")

    # No local .git -> consult skill-recall
    try:
        r = recall.get(f"/skills/{org}/{slug}")
    except httpx.HTTPError as e:
        return SkillEntry(slug, skill_dir, Status.NEW, note=f"recall check failed: {e}")
    if r.status_code == 200:
        return SkillEntry(slug, skill_dir, Status.PLATFORM_HAS_IT,
                          note="found on skill-recall")
    if r.status_code == 404:
        return SkillEntry(slug, skill_dir, Status.NEW)
    return SkillEntry(slug, skill_dir, Status.NEW,
                      note=f"recall returned {r.status_code} (treating as NEW)")


def _redact(url: str) -> str:
    # http://user:secret@host/x -> http://user:***@host/x
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            auth, host = rest.split("@", 1)
            if ":" in auth:
                u, _ = auth.split(":", 1)
                auth = f"{u}:***"
            return f"{scheme}://{auth}@{host}"
    return url



def expected_remote_url(gitea_url: str, org: str, slug: str) -> str:
    return f"{gitea_url.rstrip('/')}/{org}/{slug}.git"


def _remote_path(remote: str) -> str:
    """Extract repo path from http(s)/ssh/scp-like git remote."""
    if "://" in remote:
        path = urlparse(remote).path
    elif ":" in remote and not remote.startswith("/"):
        path = remote.split(":", 1)[1]
    else:
        path = remote
    return path.strip("/")


def remote_path_matches(remote: str, gitea_url: str, org: str, slug: str) -> bool:
    base_path = urlparse(gitea_url).path.strip("/")
    expected = f"{base_path}/{org}/{slug}.git" if base_path else f"{org}/{slug}.git"
    return _remote_path(remote).rstrip("/") == expected


def backup_git_dir(skill_dir: Path) -> Path:
    src = skill_dir / ".git"
    if not src.exists():
        raise RuntimeError(f"no .git to backup in {skill_dir}")
    stamp = time.strftime("%Y%m%d%H%M%S")
    dst = skill_dir / f".git.foreign-backup-{stamp}"
    i = 1
    while dst.exists():
        dst = skill_dir / f".git.foreign-backup-{stamp}-{i}"
        i += 1
    src.rename(dst)
    return dst


IGNORE_NAMES = {
    "__pycache__", "node_modules", "dist", "build", "venv", "env",
}
IGNORE_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp", ".swp"}


def ignored_for_compare(path: Path) -> bool:
    # Ignore all dot-prefixed files/dirs, including .git.foreign-backup-*.
    return any(part.startswith(".") for part in path.parts) or path.name in IGNORE_NAMES or any(path.name.endswith(s) for s in IGNORE_SUFFIXES)


def directory_fingerprint(root: Path) -> tuple[dict[str, str], str]:
    files: dict[str, str] = {}
    for cur, dirs, names in os.walk(root):
        cur_path = Path(cur)
        rel_dir = cur_path.relative_to(root)
        dirs[:] = [d for d in dirs if not ignored_for_compare(rel_dir / d)]
        for name in names:
            rel = rel_dir / name
            if ignored_for_compare(rel):
                continue
            full = cur_path / name
            if not full.is_file():
                continue
            h = hashlib.sha256(full.read_bytes()).hexdigest()
            files[str(rel).replace(os.sep, "/")] = h
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return files, digest


def clone_skill_repo(dest_parent: Path, *, gitea_url: str, gitea_token: str, org: str, slug: str) -> Path:
    dest = dest_parent / slug
    url = expected_remote_url(gitea_url, org, slug)
    args = ["clone", url, slug]
    if gitea_token:
        args = ["-c", "credential.helper=", "-c", f"http.extraheader=Authorization: token {gitea_token}", *args]
    _git(dest_parent, args, env=_NO_PROXY_ENV)
    return dest


def update_skill_name(skill_md: Path, new_name: str) -> None:
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    if text.lstrip("\ufeff").lstrip().startswith("---"):
        lines = text.splitlines(True)
        start = next((i for i, line in enumerate(lines) if line.strip() == "---"), None)
        end = None
        if start is not None:
            for i in range(start + 1, len(lines)):
                if lines[i].strip() == "---":
                    end = i
                    break
        if start is not None and end is not None:
            for i in range(start + 1, end):
                if lines[i].lstrip().startswith("name:"):
                    prefix = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                    lines[i] = f"{prefix}name: {new_name}\n"
                    skill_md.write_text("".join(lines), encoding="utf-8")
                    return
            lines.insert(start + 1, f"name: {new_name}\n")
            skill_md.write_text("".join(lines), encoding="utf-8")
            return
    skill_md.write_text(f"---\nname: {new_name}\n---\n\n{text}", encoding="utf-8")


def copy_local_variant(entry: SkillEntry, new_slug: str, *, gitea_url: str, org: str) -> SkillEntry:
    target = entry.path.parent / new_slug
    if target.exists():
        remote = git_remote_url(target)
        if remote and origin_is_current_gitea(remote, gitea_url) and remote_path_matches(remote, gitea_url, org, new_slug):
            return SkillEntry(new_slug, target, Status.MANAGED_LOCAL, entry.source, f"origin={_redact(remote)}")
        if remote is not None:
            raise RuntimeError(f"variant path exists with unmanaged .git: {target}")
        update_skill_name(target / "SKILL.md", new_slug)
        return SkillEntry(new_slug, target, Status.NEW, entry.source, "local variant already exists")

    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n.startswith(".") or n in IGNORE_NAMES or any(n.endswith(s) for s in IGNORE_SUFFIXES)}

    shutil.copytree(entry.path, target, ignore=ignore)
    update_skill_name(target / "SKILL.md", new_slug)
    return SkillEntry(new_slug, target, Status.NEW, entry.source, f"variant of {entry.slug}")


def classify_without_git(skill_dir: Path, gitea_url: str, org: str, recall: httpx.Client) -> SkillEntry:
    """Classify a valid non-bootstrap skill after .git has been removed/backed up."""
    slug = skill_dir.name
    try:
        r = recall.get(f"/skills/{org}/{slug}")
    except httpx.HTTPError as e:
        return SkillEntry(slug, skill_dir, Status.NEW, note=f"recall check failed: {e}")
    if r.status_code == 200:
        return SkillEntry(slug, skill_dir, Status.PLATFORM_HAS_IT, note="found on skill-recall")
    if r.status_code == 404:
        return SkillEntry(slug, skill_dir, Status.NEW)
    return SkillEntry(slug, skill_dir, Status.NEW, note=f"recall returned {r.status_code} (treating as NEW)")


def handle_foreign(entry: SkillEntry, *, gitea_url: str, org: str, recall: httpx.Client) -> tuple[dict, SkillEntry]:
    """Fix platform URL drift or backup foreign .git and reclassify as a plain skill."""
    remote = git_remote_url(entry.path) or ""
    expected = expected_remote_url(gitea_url, org, entry.slug)
    if remote and remote_path_matches(remote, gitea_url, org, entry.slug):
        _git(entry.path, ["remote", "set-url", "origin", expected], env=_NO_PROXY_ENV)
        return (
            {"slug": entry.slug, "ok": True, "actions": [f"remote set-url origin {expected}"]},
            SkillEntry(entry.slug, entry.path, Status.MANAGED_LOCAL, entry.source, f"origin={_redact(expected)}"),
        )

    backup = backup_git_dir(entry.path)
    reclassified = classify_without_git(entry.path, gitea_url, org, recall)
    reclassified.source = entry.source
    return (
        {"slug": entry.slug, "ok": True, "actions": [f"foreign .git backed up to {backup.name}", f"reclassified as {reclassified.status.value}"]},
        reclassified,
    )


def reconcile_platform_has_it(entry: SkillEntry, *, gitea_url: str, gitea_token: str, org: str) -> tuple[dict, SkillEntry | None]:
    """Compare local plain skill with platform skill; adopt if same, fork if different."""
    with tempfile.TemporaryDirectory(prefix="onboard-platform-") as td:
        remote_dir = clone_skill_repo(Path(td), gitea_url=gitea_url, gitea_token=gitea_token, org=org, slug=entry.slug)
        local_files, local_digest = directory_fingerprint(entry.path)
        remote_files, remote_digest = directory_fingerprint(remote_dir)

        if local_files == remote_files:
            shutil.rmtree(entry.path)
            shutil.move(str(remote_dir), str(entry.path))
            return (
                {"slug": entry.slug, "ok": True, "actions": ["local content equals platform; replaced with managed clone"]},
                SkillEntry(entry.slug, entry.path, Status.MANAGED_LOCAL, entry.source, f"origin={_redact(expected_remote_url(gitea_url, org, entry.slug))}"),
            )

        new_slug = f"{entry.slug}-local-{local_digest[:8]}"
        new_entry = copy_local_variant(entry, new_slug, gitea_url=gitea_url, org=org)
        return (
            {"slug": entry.slug, "ok": True, "actions": [f"content differs from platform; created local variant {new_slug}"]},
            new_entry,
        )




def dedupe_entries(entries: list[SkillEntry]) -> list[SkillEntry]:
    """De-duplicate entries after reconciliation, preferring actionable/managed ones."""
    rank = {
        Status.MANAGED_LOCAL: 60,
        Status.NEW: 50,
        Status.PLATFORM_HAS_IT: 40,
        Status.FOREIGN_REMOTE: 30,
        Status.BOOTSTRAP: 20,
        Status.NO_SKILL_MD: 10,
    }
    chosen: dict[str, SkillEntry] = {}
    order: list[str] = []
    for entry in entries:
        key = entry.slug
        if key not in chosen:
            chosen[key] = entry
            order.append(key)
            continue
        current = chosen[key]
        cur_score = rank.get(current.status, 0) + (1 if current.source == "project" else 0)
        new_score = rank.get(entry.status, 0) + (1 if entry.source == "project" else 0)
        if new_score > cur_score:
            chosen[key] = entry
    return [chosen[k] for k in order]


def find_project_skills_dir(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default cwd) looking for .opencode/skills or .claude/skills."""
    cur = start or Path.cwd()
    while True:
        for candidate in (cur / ".opencode" / "skills", cur / ".claude" / "skills"):
            if candidate.is_dir():
                return candidate
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


# ============================ onboarding ============================
LOCAL_GIT_EXCLUDES = [
    ".git.foreign-backup-*",
    ".evolve/",
    ".cache/",
    "__pycache__/",
    "node_modules/",
    "dist/",
    "build/",
    "venv/",
    "env/",
    ".DS_Store",
    "*.pyc",
    "*.pyo",
    "*.log",
    "*.tmp",
    "*.swp",
]


def ensure_local_git_excludes(skill_dir: Path) -> None:
    info = skill_dir / ".git" / "info"
    if not info.is_dir():
        return
    path = info / "exclude"
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    lines = existing.splitlines()
    changed = False
    for item in LOCAL_GIT_EXCLUDES:
        if item not in lines:
            lines.append(item)
            changed = True
    if changed:
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def git_add_skill_content(skill_dir: Path, env: dict) -> None:
    """Add skill content while excluding local/runtime noise via .git/info/exclude."""
    ensure_local_git_excludes(skill_dir)
    _git(skill_dir, ["add", "-A", "--", "."], env=env)


def onboard_one(entry: SkillEntry, *, gitea_url: str, gitea_token: str,
                recall_url: str, org: str) -> dict:
    slug = entry.slug
    actions: list[str] = []

    # 1) Create Gitea repo
    r = httpx.post(
        f"{gitea_url}/api/v1/orgs/{org}/repos",
        headers={"Authorization": f"token {gitea_token}"},
        json={"name": slug, "auto_init": False,
              "default_branch": "main", "private": False},
        timeout=15, trust_env=False,
    )
    if r.status_code == 201:
        actions.append("gitea repo created")
    elif r.status_code == 409:
        actions.append("gitea repo existed (reusing)")
    else:
        raise RuntimeError(f"create gitea repo: {r.status_code} {r.text[:200]}")

    # 2) git init / commit / tag / push
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "skill-recall-onboard",
        "GIT_AUTHOR_EMAIL": "onboard@local",
        "GIT_COMMITTER_NAME": "skill-recall-onboard",
        "GIT_COMMITTER_EMAIL": "onboard@local",
        # Strip any system proxy that could mangle localhost git pushes
        "http_proxy": "", "https_proxy": "",
        "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
    }
    _git(entry.path, ["init", "-b", "main"], env=env)
    ensure_local_git_excludes(entry.path)
    git_add_skill_content(entry.path, env)
    _git(entry.path, ["commit", "-m", f"onboard {slug} v1.0.0"], env=env)
    _git(entry.path, ["tag", "v1.0.0"], env=env)

    clean_remote = f"{gitea_url.rstrip('/')}/{org}/{slug}.git"
    _git(entry.path, ["remote", "add", "origin", clean_remote], env=env)
    _git(entry.path, [
        "-c", "credential.helper=",
        "-c", f"http.extraheader=Authorization: token {gitea_token}",
        "push", "-u", "origin", "main", "v1.0.0",
    ], env=env)
    actions.append("git init+commit+tag v1.0.0+push")

    # 3) Trigger immediate skill-recall sync (don't wait for webhook)
    try:
        r = httpx.post(f"{recall_url}/admin/sync/{org}/{slug}",
                       timeout=30, trust_env=False)
        if r.status_code == 200:
            actions.append(f"recall sync -> {r.json().get('action', '?')}")
        else:
            actions.append(f"recall sync returned {r.status_code}")
    except Exception as e:
        actions.append(f"recall sync error: {e}")

    return {"slug": slug, "ok": True, "actions": actions}


def _git(cwd: Path, args: list[str], env: dict) -> None:
    r = subprocess.run(["git", *args], cwd=str(cwd),
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        # Don't print the http.extraheader arg back at the user
        safe_args = ["***" if a.startswith("http.extraheader=") else a for a in args]
        raise RuntimeError(
            f"git {' '.join(safe_args)} failed: {(r.stderr or r.stdout).strip()}"
        )


_NO_PROXY_ENV = {
    **os.environ,
    "http_proxy": "", "https_proxy": "",
    "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
}


def _git_capture(cwd: Path, args: list[str]) -> str:
    """Run git, return stdout on success, empty string on failure."""
    r = subprocess.run(["git", *args], cwd=str(cwd),
                       capture_output=True, text=True, env=_NO_PROXY_ENV)
    return r.stdout.strip() if r.returncode == 0 else ""


def _pending_state(skill_dir: Path) -> dict:
    """Inspect a MANAGED_LOCAL skill for work that needs pushing.

    Returns {dirty, unpushed_commits, unpushed_tags}.
    """
    ensure_local_git_excludes(skill_dir)
    dirty = bool(_git_capture(skill_dir, ["status", "--porcelain"]))

    branch = _git_capture(skill_dir, ["rev-parse", "--abbrev-ref", "HEAD"]) or "main"

    # Fetch so we can compare against the remote's view. Best-effort —
    # if the remote is down we still want to report something useful.
    subprocess.run(
        ["git", "fetch", "--tags", "origin"],
        cwd=str(skill_dir), capture_output=True, text=True, env=_NO_PROXY_ENV,
    )

    unpushed_commits = _git_capture(
        skill_dir, ["log", f"origin/{branch}..HEAD", "--oneline"]
    ).splitlines() if _git_capture(
        skill_dir, ["rev-parse", "--verify", f"origin/{branch}"]
    ) else _git_capture(skill_dir, ["log", "--oneline"]).splitlines()

    local_tags = set(_git_capture(skill_dir, ["tag"]).splitlines())
    remote_tag_lines = _git_capture(
        skill_dir, ["ls-remote", "--tags", "origin"]
    ).splitlines()
    remote_tags = {
        line.rsplit("refs/tags/", 1)[-1].replace("^{}", "")
        for line in remote_tag_lines if "refs/tags/" in line
    }
    unpushed_tags = sorted(local_tags - remote_tags)

    return {
        "dirty": dirty,
        "branch": branch,
        "unpushed_commits": unpushed_commits,
        "unpushed_tags": unpushed_tags,
    }


def push_pending(entry: SkillEntry, *, gitea_token: str,
                 recall_url: str, org: str) -> dict:
    """Push any unpushed commits/tags for a MANAGED_LOCAL skill.

    Returns {slug, ok, actions, up_to_date}. Refuses to touch a dirty
    tree — the user may have in-flight evolve work we shouldn't auto-commit.
    """
    slug = entry.slug
    actions: list[str] = []

    state = _pending_state(entry.path)

    if state["dirty"]:
        return {
            "slug": slug, "ok": False, "up_to_date": False, "skipped": True,
            "error": "working tree dirty (commit or stash first)",
            "actions": actions,
        }

    if not state["unpushed_commits"] and not state["unpushed_tags"]:
        return {"slug": slug, "ok": True, "up_to_date": True, "actions": ["already up-to-date"]}

    env = {
        **os.environ,
        "http_proxy": "", "https_proxy": "",
        "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
    }
    push_args = [
        "-c", "credential.helper=",
        "-c", f"http.extraheader=Authorization: token {gitea_token}",
        "push", "origin", state["branch"],
    ]
    push_args.extend(state["unpushed_tags"])
    _git(entry.path, push_args, env=env)

    summary_parts = []
    if state["unpushed_commits"]:
        summary_parts.append(f"{len(state['unpushed_commits'])} commit(s)")
    if state["unpushed_tags"]:
        summary_parts.append(f"tag(s) {','.join(state['unpushed_tags'])}")
    actions.append(f"pushed {' + '.join(summary_parts)}")

    try:
        r = httpx.post(f"{recall_url}/admin/sync/{org}/{slug}",
                       timeout=30, trust_env=False)
        if r.status_code == 200:
            actions.append(f"recall sync -> {r.json().get('action', '?')}")
        else:
            actions.append(f"recall sync returned {r.status_code}")
    except Exception as e:
        actions.append(f"recall sync error: {e}")

    return {"slug": slug, "ok": True, "up_to_date": False, "actions": actions}


# ============================ pre-flight ============================
def preflight(args: argparse.Namespace) -> None:
    if not args.gitea_token:
        sys.exit("ERROR: GITEA_TOKEN env var or --gitea-token required")
    if not Path(args.skills_dir).is_dir():
        sys.exit(f"ERROR: skills dir not found: {args.skills_dir}")
    try:
        r = httpx.get(f"{args.gitea_url}/api/v1/version",
                      timeout=5, trust_env=False)
        if not r.json().get("version"):
            sys.exit(f"ERROR: Gitea at {args.gitea_url}: bad version response")
    except Exception as e:
        sys.exit(f"ERROR: cannot reach Gitea at {args.gitea_url}: {e}")
    try:
        r = httpx.get(f"{args.recall_url}/healthz",
                      timeout=5, trust_env=False)
        if r.json().get("status") != "ok":
            sys.exit(f"ERROR: skill-recall at {args.recall_url} unhealthy")
    except Exception as e:
        sys.exit(f"ERROR: cannot reach skill-recall at {args.recall_url}: {e}")


# ============================ output ============================
ORDER = [Status.NEW, Status.PLATFORM_HAS_IT, Status.MANAGED_LOCAL,
         Status.FOREIGN_REMOTE, Status.BOOTSTRAP, Status.NO_SKILL_MD]


def print_text_summary(entries: list[SkillEntry], skills_root: Path) -> None:
    by_status: dict[Status, list[SkillEntry]] = {}
    for e in entries:
        by_status.setdefault(e.status, []).append(e)
    print(f"Scanned {len(entries)} dir(s) in {skills_root}")
    for s in ORDER:
        items = by_status.get(s, [])
        if not items:
            continue
        print(f"\n  {s.value:18} ({len(items)})")
        for e in items:
            parts = []
            if e.source != "global":
                parts.append(e.source)
            if e.note:
                parts.append(e.note)
            extra = f"   [{', '.join(parts)}]" if parts else ""
            print(f"    - {e.slug}{extra}")


# ============================ main ============================
def main() -> int:
    args = parse_args()
    preflight(args)

    skills_root = Path(args.skills_dir)
    dirs_to_scan: list[tuple[Path, str]] = [(skills_root, "global")]

    if not args.no_project:
        project_dir = find_project_skills_dir()
        if project_dir:
            dirs_to_scan.append((project_dir, "project"))

    seen: dict[str, tuple[Path, str]] = {}
    for dir_path, source in dirs_to_scan:
        for child in sorted(dir_path.iterdir()):
            if not child.is_dir():
                continue
            # Global is scanned first; project-level skills intentionally override
            # a global skill with the same slug.
            if child.name not in seen or source == "project":
                seen[child.name] = (child, source)

    candidates = [path for path, _ in seen.values()]
    source_map = {slug: source for slug, (_, source) in seen.items()}

    if args.skill:
        candidates = [d for d in candidates if d.name == args.skill]
        if not candidates:
            sys.exit(f"--skill {args.skill}: not found under scanned dirs")

    recall = httpx.Client(base_url=args.recall_url, timeout=10, trust_env=False)
    entries = [classify(d, args.gitea_url, args.org, recall) for d in candidates]
    recall.close()

    for e in entries:
        e.source = source_map.get(e.slug, "global")

    to_onboard = [e for e in entries if e.status == Status.NEW]
    # Only push for MANAGED_LOCAL skills whose origin points at our Gitea.
    # Rows with `.git` but no origin are the user's in-flight work — we don't
    # know where to send them, so skip.
    to_push = [e for e in entries
               if e.status == Status.MANAGED_LOCAL and e.note.startswith("origin=")]
    foreign = [e for e in entries if e.status == Status.FOREIGN_REMOTE]

    # ----- summary -----
    if args.json:
        out: dict = {
            "skills_dir": str(skills_root),
            "org": args.org,
            "dry_run": args.dry_run,
            "summary": {s.value: sum(1 for e in entries if e.status == s) for s in Status},
            "entries": [{"slug": e.slug, "status": e.status.value, "source": e.source, "note": e.note}
                        for e in entries],
        }
    else:
        print_text_summary(entries, skills_root)

    if args.dry_run:
        parts = []
        if to_onboard:
            parts.append(f"onboard {len(to_onboard)} new skill(s)")
        platform_existing = [e for e in entries if e.status == Status.PLATFORM_HAS_IT]
        if foreign:
            parts.append(f"repair/reclassify {len(foreign)} foreign remote skill(s)")
        if platform_existing:
            parts.append(f"diff/reconcile {len(platform_existing)} platform-existing skill(s)")
        if to_push:
            parts.append(f"check {len(to_push)} managed skill(s) for unpushed commits/tags")
        if not args.json:
            if parts:
                print(f"\n[DRY RUN] would {', '.join(parts)}; re-run without --dry-run to apply.")
            else:
                print("\n[DRY RUN] nothing would change.")
        else:
            out["planned_actions"] = parts
            print(json.dumps(out, indent=2))
        return 0

    # ----- apply: normalize mixed local states first -----
    foreign_results: list[dict] = []
    platform_results: list[dict] = []

    recall = httpx.Client(base_url=args.recall_url, timeout=10, trust_env=False)
    try:
        normalized: list[SkillEntry] = []
        for e in entries:
            if e.status != Status.FOREIGN_REMOTE:
                normalized.append(e)
                continue
            if not args.json:
                print(f"\n  [foreign] {e.slug}")
            try:
                r, new_entry = handle_foreign(e, gitea_url=args.gitea_url, org=args.org, recall=recall)
            except Exception as exc:
                r = {"slug": e.slug, "ok": False, "error": str(exc), "actions": []}
                normalized.append(e)
            else:
                normalized.append(new_entry)
            foreign_results.append(r)
            if not args.json:
                if not r.get("ok"):
                    print(f"        FAILED: {r.get('error', 'unknown')}")
                for a in r.get("actions", []):
                    print(f"        {a}")

        reconciled: list[SkillEntry] = []
        for e in normalized:
            if e.status != Status.PLATFORM_HAS_IT:
                reconciled.append(e)
                continue
            if not args.json:
                print(f"\n  [platform-has-it] {e.slug}")
            try:
                r, followup = reconcile_platform_has_it(
                    e, gitea_url=args.gitea_url, gitea_token=args.gitea_token, org=args.org
                )
            except Exception as exc:
                r = {"slug": e.slug, "ok": False, "error": str(exc), "actions": []}
                reconciled.append(e)
            else:
                # If content differs, keep the original local slug untouched and onboard
                # the deterministic local variant. If equal, followup is the managed clone.
                if followup and followup.status == Status.NEW:
                    reconciled.append(e)
                    reconciled.append(followup)
                elif followup:
                    reconciled.append(followup)
                else:
                    reconciled.append(e)
            platform_results.append(r)
            if not args.json:
                if not r.get("ok"):
                    print(f"        FAILED: {r.get('error', 'unknown')}")
                for a in r.get("actions", []):
                    print(f"        {a}")
        entries = dedupe_entries(reconciled)
    finally:
        recall.close()

    to_onboard = [e for e in entries if e.status == Status.NEW]
    to_push = [e for e in entries
               if e.status == Status.MANAGED_LOCAL and e.note.startswith("origin=")]
    foreign = [e for e in entries if e.status == Status.FOREIGN_REMOTE]

    # ----- apply new / managed entries -----
    results: list[dict] = []
    for i, e in enumerate(to_onboard, 1):
        if not args.json:
            print(f"\n  [{i}/{len(to_onboard)}] {e.slug}")
        try:
            r = onboard_one(
                e, gitea_url=args.gitea_url, gitea_token=args.gitea_token,
                recall_url=args.recall_url, org=args.org,
            )
            results.append(r)
            if not args.json:
                for a in r["actions"]:
                    print(f"        {a}")
        except Exception as exc:
            results.append({"slug": e.slug, "ok": False, "error": str(exc)})
            if not args.json:
                print(f"        FAILED: {exc}")

    push_results: list[dict] = []
    pushed = 0
    dirty = 0
    push_fail = 0
    if to_push and not args.json:
        print(f"\n  Checking {len(to_push)} managed skill(s) for unpushed changes:")
    for e in to_push:
        try:
            r = push_pending(
                e, gitea_token=args.gitea_token,
                recall_url=args.recall_url, org=args.org,
            )
        except Exception as exc:
            r = {"slug": e.slug, "ok": False, "error": str(exc), "actions": []}
        push_results.append(r)
        if not args.json:
            if r.get("up_to_date"):
                continue  # don't spam the console with up-to-date skills
            print(f"    - {e.slug}")
            if not r.get("ok"):
                print(f"        SKIPPED: {r.get('error', 'unknown')}")
            else:
                pushed += 1
                for a in r["actions"]:
                    print(f"        {a}")
        else:
            if r.get("ok") and not r.get("up_to_date"):
                pushed += 1
        if not r.get("ok"):
            if r.get("skipped"):
                dirty += 1
            else:
                push_fail += 1

    ok = sum(1 for r in results if r["ok"])
    fail = len(results) - ok
    foreign_fail = sum(1 for r in foreign_results if not r.get("ok"))
    platform_fail = sum(1 for r in platform_results if not r.get("ok"))
    if args.json:
        out["entries_after_reconcile"] = [
            {"slug": e.slug, "status": e.status.value, "source": e.source, "note": e.note}
            for e in entries
        ]
        out["foreign_results"] = foreign_results
        out["platform_results"] = platform_results
        out["results"] = results
        out["push_results"] = push_results
        print(json.dumps(out, indent=2))
    else:
        summary = f"\nDone: {ok} onboarded, {fail} failed"
        if foreign_results:
            summary += f", {len(foreign_results) - foreign_fail} foreign handled, {foreign_fail} foreign failed"
        if platform_results:
            summary += f", {len(platform_results) - platform_fail} platform reconciled, {platform_fail} platform failed"
        if to_push:
            summary += f", {pushed} pushed, {dirty} dirty-skipped, {push_fail} push-failed"
        summary += f", {len(foreign)} foreign-remote skipped."
        print(summary)
    return 1 if (fail or push_fail or foreign_fail or platform_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
