#!/usr/bin/env python3
"""Collect task data and POST to task-restore API."""
from __future__ import annotations

# --- secocto config: load ~/.config/secocto/.env (setdefault semantics) ---
from pathlib import Path as _P
_env_file = _P.home() / ".config" / "secocto" / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            __import__("os").environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TASK_RESTORE_URL = os.environ.get("TASK_RESTORE_URL", "http://localhost:8300")
SCORE_CACHE_DIR = Path.home() / ".cache" / "task-score"
TRACE_CACHE_DIR = Path.home() / ".cache" / "task-trace"

_SESSION_ID_CWD_RE = re.compile(r"/data/files/[^/]+/app/[^/]+/([A-Za-z0-9_-]{8,})")


def _session_id_from_pwd() -> str | None:
    m = _SESSION_ID_CWD_RE.search(os.getcwd())
    return m.group(1) if m else None


def get_agent_type() -> str:
    agent = os.environ.get("SECOCTO_AGENT_TYPE", "").strip().lower()
    if agent not in {"opencode", "kilocode", "pi", "claude"}:
        print("ERROR: SECOCTO_AGENT_TYPE must be one of: opencode, kilocode, pi, claude", file=sys.stderr)
        sys.exit(2)
    return agent


def detect_agent() -> tuple[str, str]:
    agent = get_agent_type()
    if agent == "kilocode":
        sid = os.environ.get("KILO_SESSION_ID")
    elif agent == "opencode":
        sid = os.environ.get("OPENCODE_SESSION_ID")
    elif agent == "claude":
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    else:  # pi
        sid = _session_id_from_pwd()
    if sid:
        return (agent, sid)
    print(f"ERROR: no session id for agent={agent}", file=sys.stderr)
    sys.exit(2)


def load_score(session_id: str) -> dict | None:
    path = SCORE_CACHE_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"WARN: failed to read score cache: {e}", file=sys.stderr)
        return None


def load_trace_metadata(agent_type: str, session_id: str) -> dict | None:
    candidates = [agent_type, "claude", "opencode", "kilocode", "pi"]
    seen: set[str] = set()
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        path = TRACE_CACHE_DIR / name / f"trace-{session_id}.json"
        if not path.exists():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"WARN: failed to read trace metadata {path}: {e}", file=sys.stderr)
            return None
    return None


def find_trace_script() -> Path | None:
    candidates = [
        Path(__file__).resolve().parents[2] / "task-trace" / "scripts" / "trace.py",
        Path.home() / ".claude" / "skills" / "task-trace" / "scripts" / "trace.py",
        Path.home() / ".config" / "opencode" / "skills" / "task-trace" / "scripts" / "trace.py",
        Path.home() / ".config" / "kilo" / "skills" / "task-trace" / "scripts" / "trace.py",
    ]
    return next((p for p in candidates if p.exists()), None)


def run_task_trace() -> dict | None:
    trace_script = find_trace_script()
    if not trace_script:
        return None
    try:
        proc = subprocess.run(
            ["python3", str(trace_script)],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except Exception as e:
        print(f"WARN: failed to run task-trace: {e}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        print(f"WARN: task-trace failed: {err[:200]}", file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"WARN: task-trace stdout was not JSON: {e}", file=sys.stderr)
        return None


def resolve_trace_url(agent_type: str, session_id: str, explicit_url: str) -> str:
    if explicit_url:
        return explicit_url

    trace = load_trace_metadata(agent_type, session_id)
    if not trace or not trace.get("trace_url"):
        trace = run_task_trace()
    if not trace:
        return ""

    trace_url = trace.get("trace_url")
    if isinstance(trace_url, str) and trace_url:
        return trace_url
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect task data → task-restore")
    parser.add_argument("--summary", type=str, default="")
    parser.add_argument("--task-ref", type=str, default="")
    parser.add_argument("--trace-url", type=str, default="")
    parser.add_argument("--cards", type=str, default="",
                        help="Comma-separated card URLs")
    parser.add_argument("--proposals", type=str, default="",
                        help='Proposal JSON array or comma-separated URLs, e.g. \'[{"proposal_url":"...","skill_name":"...","branch":"..."}, ...]\'')
    parser.add_argument("--tags", type=str, default="",
                        help="Comma-separated taxonomy tags (from task-collect tagger)")
    parser.add_argument("--skills", type=str, required=True,
                        help="Comma-separated skill names used in this session (required, pass '' if none)")
    parser.add_argument("--wiki", type=str, required=True,
                        help="Comma-separated wiki file names read in this session (required, pass '' if none)")
    parser.add_argument("--bundle-url", type=str, default="",
                        help="Audit bundle zip public URL from bundle.py --upload")
    args = parser.parse_args()

    agent_type, session_id = detect_agent()

    score_data = load_score(session_id)
    trace_url = resolve_trace_url(agent_type, session_id, args.trace_url)

    payload: dict = {
        "task_id": session_id,
        "agent_type": agent_type,
        "app_name": os.environ.get("SECOCTO_APP_NAME", ""),
        "status": "completed",
    }

    if args.summary:
        payload["summary"] = args.summary
    if args.task_ref:
        payload["task_ref"] = args.task_ref
    if trace_url:
        payload["trace_url"] = trace_url

    if score_data:
        payload["score"] = score_data.get("score")
        payload["score_vector"] = score_data.get("vector")
        payload["score_reasoning"] = score_data.get("reasoning")

    if args.cards:
        payload["cards"] = [
            {"card_url": url.strip()}
            for url in args.cards.split(",") if url.strip()
        ]

    if args.proposals:
        raw = args.proposals.strip()
        if raw.startswith("["):
            # JSON 数组格式，直接解析
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    payload["proposals"] = parsed
                else:
                    payload["proposals"] = [parsed]
            except json.JSONDecodeError:
                # JSON 解析失败，回退到 URL 列表
                payload["proposals"] = [
                    {"proposal_url": url.strip()}
                    for url in raw.split(",") if url.strip()
                ]
        else:
            # 逗号分隔的 URL 列表
            payload["proposals"] = [
                {"proposal_url": url.strip()}
                for url in raw.split(",") if url.strip()
            ]

    if args.tags:
        payload["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]

    if args.skills:
        payload["skills_used"] = [s.strip() for s in args.skills.split(",") if s.strip()]

    if args.wiki:
        payload["wiki_used"] = [w.strip() for w in args.wiki.split(",") if w.strip()]

    if args.bundle_url:
        payload["bundle_url"] = args.bundle_url

    try:
        import httpx
    except ImportError:
        print("ERROR: httpx not installed", file=sys.stderr)
        return 1

    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            resp = client.post(
                f"{TASK_RESTORE_URL}/tasks/upsert",
                json=payload,
            )
        if resp.status_code != 200:
            print(f"ERROR: task-restore returned {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            return 1
        result = resp.json()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print(f"ERROR: failed to submit task: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
