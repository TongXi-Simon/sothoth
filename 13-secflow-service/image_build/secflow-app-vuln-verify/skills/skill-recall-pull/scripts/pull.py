#!/usr/bin/env python3
"""Pull skills from skill-recall into local skills dir.

Read-side counterpart to skill-recall-onboard:
  search   semantic recall via POST /recall
  install  git clone gitea/<ns>/<slug> -> <skills-dir>/<slug>/
  update   git fetch + ff-only pull on one or all managed skills
  status   diff local vs remote for every managed skill

See ../SKILL.md for behavior and design choices.
"""
from __future__ import annotations

# --- secocto config: load ~/.config/secocto/.env (setdefault semantics) ---
from pathlib import Path as _P
_env_file = _P.home() / ".config" / "secocto" / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            __import__("os").environ.setdefault(_k.strip(), _v.strip().strip("\x27\""))

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("missing dependency (httpx); run: pip install httpx")


_NO_PROXY_ENV = {
    **os.environ,
    "http_proxy": "", "https_proxy": "",
    "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
}


# ============================ args ============================
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--skills-dir", default=str(Path(__file__).resolve().parents[2]))
    p.add_argument("--gitea-url",
                   default=os.environ.get("GITEA_URL", "http://localhost:3010"))
    p.add_argument("--recall-url",
                   default=os.environ.get("SKILL_RECALL_URL", "http://localhost:8090"))
    p.add_argument("--gitea-token", default=os.environ.get("GITEA_TOKEN", ""),
                   help="only needed for private repos; public demo/* works without")
    p.add_argument("--org", default="demo",
                   help="default org when slug given without namespace (default: demo)")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="semantic search via /recall")
    sp.add_argument("query", help="natural language query")
    sp.add_argument("--top-k", type=int, default=5)
    sp.add_argument("--namespace", help="restrict to one namespace")
    sp.add_argument("--tag", action="append", default=[],
                    help="repeatable; require all tags")

    ip = sub.add_parser("install", help="git clone a skill into <skills-dir>")
    ip.add_argument("ref", help="<slug> or <namespace>/<slug>")

    up = sub.add_parser("update", help="git fetch + ff-only pull")
    up.add_argument("ref", nargs="?", help="<slug> or <namespace>/<slug>; omit for all")

    sub.add_parser("status", help="diff local vs remote for every managed skill")

    return p.parse_args()


def resolve_ref(ref: str, default_org: str) -> tuple[str, str]:
    if "/" in ref:
        ns, slug = ref.split("/", 1)
        return ns, slug
    return default_org, ref


# ============================ git helpers ============================
def _git(cwd: Path, args: list[str], *, env: dict | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    e = env or _NO_PROXY_ENV
    r = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True, env=e)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r


def _git_capture(cwd: Path, args: list[str]) -> str:
    r = _git(cwd, args, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def _is_dirty(skill_dir: Path) -> bool:
    return bool(_git_capture(skill_dir, ["status", "--porcelain"]))


def _origin_url(skill_dir: Path) -> str:
    return _git_capture(skill_dir, ["remote", "get-url", "origin"])


def _local_head_short(skill_dir: Path) -> str:
    return _git_capture(skill_dir, ["rev-parse", "--short", "HEAD"])


def _local_latest_tag(skill_dir: Path) -> str:
    """Highest semver-looking tag reachable from HEAD, or '' if none.

    Uses `git describe --tags --abbrev=0` so a tag that's only present
    via `git fetch` (but whose commit isn't in HEAD's history) doesn't
    show up — that would lie about what version is actually checked out.
    """
    import re
    sem = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
    out = _git_capture(skill_dir, ["describe", "--tags", "--abbrev=0", "--match", "v*"])
    return out if sem.match(out) else ""


# ============================ commands ============================
def cmd_search(args: argparse.Namespace) -> int:
    body = {"task": args.query, "top_k": args.top_k}
    if args.namespace:
        body["namespace"] = args.namespace
    if args.tag:
        body["tags"] = args.tag
    try:
        r = httpx.post(f"{args.recall_url}/recall", json=body,
                       timeout=30, trust_env=False)
    except httpx.HTTPError as e:
        sys.exit(f"ERROR: cannot reach skill-recall at {args.recall_url}: {e}")
    if r.status_code != 200:
        sys.exit(f"ERROR: /recall returned {r.status_code}: {r.text[:200]}")
    hits = r.json()
    if args.json:
        print(json.dumps(hits, indent=2, ensure_ascii=False))
        return 0
    if not hits:
        print(f"No skills matched {args.query!r}.")
        return 1
    print(f"Top {len(hits)} match(es) for: {args.query}\n")
    for i, h in enumerate(hits, 1):
        score = h.get("score") or 0.0
        ver = h.get("latest_version") or "—"
        desc = (h.get("description") or "").strip().splitlines()[0][:120]
        print(f"  {i}. {h['full_name']:30}  score={score:.3f}  {ver}")
        if desc:
            print(f"     {desc}")
    print(f"\nInstall with: pull.py install {hits[0]['full_name']}")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    ns, slug = resolve_ref(args.ref, args.org)
    target = Path(args.skills_dir) / slug
    if target.exists():
        msg = f"target exists: {target} (refusing to overwrite)"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"ERROR: {msg}")
        return 1

    # Verify the platform actually has it (gives a nicer error than
    # letting `git clone` fail with HTML 404).
    try:
        r = httpx.get(f"{args.recall_url}/skills/{ns}/{slug}",
                      timeout=10, trust_env=False)
    except httpx.HTTPError as e:
        sys.exit(f"ERROR: cannot reach skill-recall at {args.recall_url}: {e}")
    if r.status_code == 404:
        sys.exit(f"ERROR: skill-recall has no record of {ns}/{slug}; check spelling")
    if r.status_code != 200:
        sys.exit(f"ERROR: skill-recall returned {r.status_code} for {ns}/{slug}")
    rec = r.json()

    clone_url = f"{args.gitea_url.rstrip('/')}/{ns}/{slug}.git"
    env = dict(_NO_PROXY_ENV)
    extra = []
    if args.gitea_token:
        extra = ["-c", "credential.helper=",
                 "-c", f"http.extraheader=Authorization: token {args.gitea_token}"]

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git(target.parent, [*extra, "clone", clone_url, slug], env=env)
    except RuntimeError as e:
        # Mask the auth header in any error echo
        msg = str(e).replace(args.gitea_token, "***") if args.gitea_token else str(e)
        sys.exit(f"ERROR: clone failed: {msg}")

    head = _local_head_short(target)
    tag = _local_latest_tag(target)
    out = {
        "ok": True, "ref": f"{ns}/{slug}", "path": str(target),
        "head": head, "latest_local_tag": tag,
        "platform_latest": rec.get("latest_version"),
    }
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"Installed {ns}/{slug} -> {target}")
        print(f"  HEAD={head}  tag={tag or '(none)'}  platform={rec.get('latest_version')}")
    return 0


def _update_one(skill_dir: Path, args: argparse.Namespace) -> dict:
    slug = skill_dir.name
    if not (skill_dir / ".git").exists():
        return {"slug": slug, "ok": False, "skipped": True,
                "reason": "no .git (not managed)"}
    if _is_dirty(skill_dir):
        return {"slug": slug, "ok": False, "skipped": True,
                "reason": "dirty tree (commit/stash first)"}
    origin = _origin_url(skill_dir)
    if args.gitea_url.rstrip("/") not in origin:
        return {"slug": slug, "ok": False, "skipped": True,
                "reason": f"foreign origin: {origin}"}

    branch = _git_capture(skill_dir, ["rev-parse", "--abbrev-ref", "HEAD"]) or "main"

    env = dict(_NO_PROXY_ENV)
    extra = []
    if args.gitea_token:
        extra = ["-c", "credential.helper=",
                 "-c", f"http.extraheader=Authorization: token {args.gitea_token}"]
    try:
        _git(skill_dir, [*extra, "fetch", "--tags", "origin"], env=env)
    except RuntimeError as e:
        return {"slug": slug, "ok": False, "skipped": True,
                "reason": f"fetch failed: {e}"}

    pre_head = _git_capture(skill_dir, ["rev-parse", "HEAD"])
    pre_tag = _local_latest_tag(skill_dir)

    # Figure out our position vs origin before touching the working tree.
    ahead = behind = 0
    if _git_capture(skill_dir, ["rev-parse", "--verify", f"origin/{branch}"]):
        counts = _git_capture(
            skill_dir, ["rev-list", "--left-right", "--count",
                        f"HEAD...origin/{branch}"]
        ).split()
        if len(counts) == 2:
            ahead, behind = int(counts[0]), int(counts[1])

    if ahead and not behind:
        return {"slug": slug, "ok": False, "skipped": True,
                "reason": f"local ahead by {ahead} commit(s) (run skill-recall-onboard to push)"}
    if ahead and behind:
        return {"slug": slug, "ok": False, "skipped": True,
                "reason": f"diverged ({ahead} ahead, {behind} behind) — manual reconcile"}
    if not behind:
        return {"slug": slug, "ok": True, "skipped": False, "changed": False,
                "head": pre_head[:7], "tag": pre_tag,
                "from": pre_tag, "to": pre_tag}

    pull = _git(skill_dir, [*extra, "pull", "--ff-only", "origin", branch],
                env=env, check=False)
    if pull.returncode != 0:
        msg = (pull.stderr or pull.stdout).strip()
        return {"slug": slug, "ok": False, "skipped": True,
                "reason": f"pull failed: {msg[:140]}"}

    post_head = _git_capture(skill_dir, ["rev-parse", "HEAD"])
    post_tag = _local_latest_tag(skill_dir)
    return {
        "slug": slug, "ok": True, "skipped": False,
        "changed": pre_head != post_head or pre_tag != post_tag,
        "head": post_head[:7],
        "tag": post_tag,
        "from": pre_tag, "to": post_tag,
    }


def cmd_update(args: argparse.Namespace) -> int:
    skills_root = Path(args.skills_dir)
    if args.ref:
        _, slug = resolve_ref(args.ref, args.org)
        targets = [skills_root / slug]
        if not targets[0].is_dir():
            sys.exit(f"ERROR: {targets[0]} not found; install it first")
    else:
        targets = sorted(d for d in skills_root.iterdir()
                         if d.is_dir() and (d / ".git").exists())

    results = [_update_one(t, args) for t in targets]
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        changed = [r for r in results if r["ok"] and r.get("changed")]
        unchanged = [r for r in results if r["ok"] and not r.get("changed")]
        skipped = [r for r in results if not r["ok"]]
        for r in changed:
            print(f"  {r['slug']:30}  {r['from'] or '(none)'} -> {r['tag'] or '(none)'}  HEAD={r['head']}")
        if unchanged and not args.ref:
            print(f"  ({len(unchanged)} already current)")
        for r in skipped:
            print(f"  {r['slug']:30}  SKIPPED: {r['reason']}")
        print(f"\nDone: {len(changed)} updated, {len(unchanged)} current, {len(skipped)} skipped.")
    return 1 if any(not r["ok"] for r in results) else 0


def cmd_status(args: argparse.Namespace) -> int:
    skills_root = Path(args.skills_dir)
    rows = []
    env = dict(_NO_PROXY_ENV)
    extra = []
    if args.gitea_token:
        extra = ["-c", "credential.helper=",
                 "-c", f"http.extraheader=Authorization: token {args.gitea_token}"]
    for d in sorted(p for p in skills_root.iterdir() if p.is_dir()):
        if not (d / ".git").exists():
            continue
        origin = _origin_url(d)
        if args.gitea_url.rstrip("/") not in origin:
            rows.append({"slug": d.name, "state": "foreign", "origin": origin})
            continue
        # best-effort fetch so the comparison is fresh
        _git(d, [*extra, "fetch", "--tags", "origin"], env=env, check=False)

        local_tag = _local_latest_tag(d)
        # Get remote latest tag from skill-recall (cheaper than parsing ls-remote)
        try:
            r = httpx.get(f"{args.recall_url}/skills/demo/{d.name}",
                          timeout=10, trust_env=False)
            remote_tag = r.json().get("latest_version") if r.status_code == 200 else "?"
        except httpx.HTTPError:
            remote_tag = "?"

        branch = _git_capture(d, ["rev-parse", "--abbrev-ref", "HEAD"]) or "main"
        ahead = behind = 0
        if _git_capture(d, ["rev-parse", "--verify", f"origin/{branch}"]):
            counts = _git_capture(
                d, ["rev-list", "--left-right", "--count",
                    f"HEAD...origin/{branch}"]
            ).split()
            if len(counts) == 2:
                ahead, behind = int(counts[0]), int(counts[1])

        if _is_dirty(d):
            state = "dirty"
        elif ahead and behind:
            state = "diverged"
        elif ahead:
            state = "ahead"
        elif behind:
            state = "behind"
        elif local_tag != remote_tag:
            state = "tag-mismatch"
        else:
            state = "current"

        rows.append({
            "slug": d.name, "state": state,
            "local_tag": local_tag, "remote_tag": remote_tag,
            "ahead": ahead, "behind": behind,
        })

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        if not rows:
            print(f"No managed skills found under {skills_root}.")
            return 0
        print(f"{'SKILL':32} {'STATE':14} {'LOCAL':10} {'REMOTE':10} AHEAD/BEHIND")
        for r in rows:
            extra_col = ""
            if r["state"] in ("ahead", "behind", "diverged"):
                extra_col = f"{r['ahead']}/{r['behind']}"
            print(f"  {r['slug']:30} {r['state']:14} "
                  f"{r.get('local_tag','—'):10} {r.get('remote_tag','—'):10} {extra_col}")
    bad = [r for r in rows if r["state"] in ("dirty", "diverged", "foreign")]
    return 1 if bad else 0


# ============================ main ============================
def main() -> int:
    args = parse_args()
    if args.cmd == "search":
        return cmd_search(args)
    if args.cmd == "install":
        return cmd_install(args)
    if args.cmd == "update":
        return cmd_update(args)
    if args.cmd == "status":
        return cmd_status(args)
    sys.exit(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
