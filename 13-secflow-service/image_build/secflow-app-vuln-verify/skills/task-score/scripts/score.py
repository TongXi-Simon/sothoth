#!/usr/bin/env python3
"""Write task score JSON to ~/.cache/task-score/{session_id}.json."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Load centralized config (setdefault semantics)
_env_file = Path.home() / ".config" / "secocto" / ".env"
if _env_file.is_file():
    for _line in _env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#"):
            continue
        _k, _, _v = _line.partition("=")
        _v = _v.strip().strip("\"'")
        if _k and _k not in os.environ:
            os.environ[_k] = _v

CACHE_DIR = Path.home() / ".cache" / "task-score"
DIMENSIONS = ["evidence", "results", "process", "evolution", "consistency"]
WEIGHTS = [40, 30, 15, 10, 5]

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


def detect_session_id() -> str:
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
        return sid
    print(f"ERROR: no session id for agent={agent}", file=sys.stderr)
    sys.exit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Write task score")
    parser.add_argument("--vector", type=str, required=True,
                        help="Comma-separated: evidence,results,process,evolution,consistency")
    parser.add_argument("--reasoning", type=str, required=True)
    args = parser.parse_args()

    try:
        vector = [float(x.strip()) for x in args.vector.split(",")]
    except ValueError:
        print("ERROR: vector must contain only numbers", file=sys.stderr)
        return 2
    if len(vector) != 5:
        print("ERROR: vector must have exactly 5 values", file=sys.stderr)
        return 2

    for i, (v, w) in enumerate(zip(vector, WEIGHTS)):
        if v < 0 or v > w:
            print(f"WARN: {DIMENSIONS[i]}={v} exceeds max {w}", file=sys.stderr)

    score = sum(vector)

    session_id = detect_session_id()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    result = {
        "task_id": session_id,
        "agent_type": get_agent_type(),
        "app_name": os.environ.get("SECOCTO_APP_NAME", ""),
        "score": score,
        "vector": vector,
        "dimensions": DIMENSIONS,
        "weights": WEIGHTS,
        "reasoning": args.reasoning,
    }

    out_path = CACHE_DIR / f"{session_id}.json"
    result["path"] = str(out_path)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
