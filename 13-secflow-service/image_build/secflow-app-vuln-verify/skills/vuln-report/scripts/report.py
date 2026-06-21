#!/usr/bin/env python3
"""Collect vulnerability findings and POST to vuln-restore API."""
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
import subprocess
import sys
from pathlib import Path


VULN_RESTORE_URL = os.environ.get("VULN_RESTORE_URL", "http://localhost:8301")
TRACE_CACHE_DIR = Path.home() / ".cache" / "task-trace"

_SESSION_ID_CWD_RE = re.compile(r"/data/files/[^/]+/app/[^/]+/([A-Za-z0-9_-]{8,})")


def _session_id_from_pwd() -> str | None:
    m = _SESSION_ID_CWD_RE.search(os.getcwd())
    return m.group(1) if m else None

SEVERITY_TO_LEVEL = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
    "note": "note",
}


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


def detect_repo() -> tuple[str, str]:
    repo_url = ""
    repo_version = ""
    try:
        repo_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        pass
    try:
        repo_version = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        pass
    return (repo_url, repo_version)


def is_high_confidence(finding: dict) -> bool:
    confidence = str(finding.get("confidence", "")).strip().lower()
    if confidence in {"medium", "low", "unknown", "speculative", "possible"}:
        return False
    has_evidence = bool(
        finding.get("rule_id")
        and finding.get("message")
        and finding.get("file_path")
        and finding.get("start_line")
        and finding.get("evidence_chain")
    )
    if confidence in {"high", "confirmed"}:
        return has_evidence
    return has_evidence


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


def resolve_trace_url(agent_type: str, session_id: str) -> str:
    trace = load_trace_metadata(agent_type, session_id)
    if not trace or not trace.get("trace_url"):
        trace = run_task_trace()
    if not trace:
        return ""
    trace_url = trace.get("trace_url")
    return trace_url if isinstance(trace_url, str) else ""


def build_sarif(findings: list[dict]) -> dict:
    """Build a SARIF 2.1.0 document from flat finding dicts."""
    results = []
    for f in findings:
        result: dict = {
            "ruleId": f.get("rule_id", "unknown"),
            "level": SEVERITY_TO_LEVEL.get(f.get("severity", "medium"), "warning"),
            "message": {"text": f.get("message", "")},
        }
        if f.get("file_path"):
            location: dict = {
                "physicalLocation": {
                    "artifactLocation": {"uri": f["file_path"]},
                    "region": {},
                }
            }
            if f.get("start_line"):
                location["physicalLocation"]["region"]["startLine"] = f["start_line"]
            if f.get("end_line"):
                location["physicalLocation"]["region"]["endLine"] = f["end_line"]
            result["locations"] = [location]

        evidence = f.get("evidence_chain")
        if evidence:
            thread_locations = []
            for loc in evidence:
                tl: dict = {"location": {
                    "physicalLocation": {
                        "artifactLocation": {"uri": loc.get("file_path", "")},
                        "region": {"startLine": loc.get("start_line", 1)},
                    },
                    "message": {"text": loc.get("message", "")},
                }}
                thread_locations.append(tl)
            result["codeFlows"] = [{"threadFlows": [{"locations": thread_locations}]}]

        results.append(result)

    return {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "sec-agent", "version": "1.0"}},
            "results": results,
        }],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Report vulnerabilities → vuln-restore")
    parser.add_argument("--findings", type=str, default="",
                        help="JSON array of finding objects (or pass via stdin if omitted)")
    parser.add_argument("--repo-url", type=str, default="",
                        help="Override auto-detected repo URL")
    parser.add_argument("--repo-version", type=str, default="",
                        help="Override auto-detected repo version")
    args = parser.parse_args()

    raw = args.findings
    if not raw and not sys.stdin.isatty():
        raw = sys.stdin.read()
    if not raw:
        print("ERROR: no findings provided (use --findings or pipe via stdin)", file=sys.stderr)
        return 1

    try:
        findings = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON in findings: {e}", file=sys.stderr)
        return 1

    if not findings:
        print("No findings to report, skipping.")
        return 0

    findings = [f for f in findings if isinstance(f, dict) and is_high_confidence(f)]
    if not findings:
        print("No high-confidence findings to report, skipping.")
        return 0

    agent_type, session_id = detect_agent()
    repo_url, repo_version = detect_repo()
    if args.repo_url:
        repo_url = args.repo_url
    if args.repo_version:
        repo_version = args.repo_version

    sarif = build_sarif(findings)
    trace_url = resolve_trace_url(agent_type, session_id)

    payload = {
        "task_id": session_id,
        "repo_url": repo_url,
        "repo_version": repo_version,
        "agent_type": agent_type,
        "app_name": os.environ.get("SECOCTO_APP_NAME", ""),
        "sarif_document": sarif,
    }
    if trace_url:
        payload["trace_url"] = trace_url
        payload["trace_status"] = "available"
    else:
        payload["trace_status"] = "missing"
        payload["trace_export_failed"] = True

    try:
        import httpx
    except ImportError:
        print("ERROR: httpx not installed", file=sys.stderr)
        return 1

    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            resp = client.post(f"{VULN_RESTORE_URL}/reports", json=payload)
        if resp.status_code != 201:
            print(f"ERROR: vuln-restore returned {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
            return 1
        result = resp.json()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print(f"ERROR: failed to submit report: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
