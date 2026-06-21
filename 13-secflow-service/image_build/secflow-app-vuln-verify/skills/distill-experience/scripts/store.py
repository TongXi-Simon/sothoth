#!/usr/bin/env python3
"""Write an experience card to mem-recall via plain HTTP."""
from __future__ import annotations

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
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MEM_RECALL_URL = os.environ.get("MEM_RECALL_URL", "http://127.0.0.1:18790")
MEM_RECALL_API_TOKEN = os.environ.get("MEM_RECALL_API_TOKEN", "")
VALID_SCOPE_ROOTS = {"topics", "team", "proj", "task", "private"}
CACHE_DIR = _P.home() / ".cache" / "distill-experience"
SCOPE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
SECURITY_TAGS = {
    "security", "security-audit", "vuln", "vulnerability", "vuln-discovery",
    "cwe", "owasp", "sql-injection", "xss", "ssti", "ssrf", "path-traversal",
    "deserialization", "unsafe-deserialization", "rce", "command-injection",
    "code-injection", "authentication", "authorization", "audit",
}
_SESSION_ID_CWD_RE = re.compile(r"/data/files/[^/]+/app/[^/]+/([A-Za-z0-9_-]{8,})")


def _session_id_from_pwd() -> str | None:
    m = _SESSION_ID_CWD_RE.search(os.getcwd())
    return m.group(1) if m else None


PROJECT_TAGS = {
    "project", "architecture", "secocto", "docker", "docker-compose",
    "api", "service", "backend", "frontend", "deployment", "config",
}


def _split_tags(tags: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in tags.split(","):
        tag = item.strip().lower()
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def _title_from_content(content: str) -> str:
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        line = re.sub(r"\s+", " ", line).strip(" -*`\t")
        if line:
            return line[:80]
    return "untitled"


def _infer_scope(content: str, tags: str) -> tuple[str, str]:
    tag_set = set(_split_tags(tags))
    text = f"{tags}\n{content}".lower()
    if tag_set & SECURITY_TAGS or any(k in text for k in ("cwe-", "漏洞", "安全审计", "注入", "反序列化")):
        return "topics", "vuln-pattern"
    if tag_set & PROJECT_TAGS or any(k in text for k in ("架构", "部署", "docker compose", "服务")):
        return "proj", "default"
    if any(t.startswith("task:") for t in tag_set) or any(k in text for k in ("任务", "workflow", "工作流")):
        return "task", "default"
    return "topics", "default"


def _clean_scope(scope_root: str | None, scope_name: str | None,
                 content: str, tags: str) -> tuple[str, str]:
    if not scope_root or not scope_name:
        return _infer_scope(content, tags)
    root = scope_root.strip()
    name = scope_name.strip()
    if root not in VALID_SCOPE_ROOTS or not SCOPE_NAME_RE.fullmatch(name):
        return _infer_scope(content, tags)
    return root, name


def _post_experience(*, content: str, tags: str,
                     scope_root: str | None = None,
                     scope_name: str | None = None,
                     task_ref: str | None = None) -> dict:
    clean_tags = _split_tags(tags)
    clean_task_ref = (task_ref or "").strip()
    if clean_task_ref:
        task_tag = f"task:{clean_task_ref.lower()}"
        if task_tag not in clean_tags:
            clean_tags.append(task_tag)
    root, name = _clean_scope(scope_root, scope_name, content, ",".join(clean_tags))
    payload = {
        "title": _title_from_content(content),
        "content": content,
        "tags": clean_tags,
        "task_ref": clean_task_ref,
        "scope_root": root,
        "scope_name": name,
        "source": "distill-experience",
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if MEM_RECALL_API_TOKEN:
        headers["Authorization"] = f"Bearer {MEM_RECALL_API_TOKEN}"
    req = Request(
        f"{MEM_RECALL_URL.rstrip('/')}/experiences",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"mem-recall returned HTTP {e.code}: {detail}") from e
    except URLError as e:
        raise RuntimeError(f"cannot reach mem-recall at {MEM_RECALL_URL}: {e}") from e



def _session_id() -> str:
    agent = os.environ.get("SECOCTO_AGENT_TYPE", "").strip().lower()
    if agent == "kilocode":
        return os.environ.get("KILO_SESSION_ID") or "unknown"
    if agent == "opencode":
        return os.environ.get("OPENCODE_SESSION_ID") or "unknown"
    if agent == "claude":
        return os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown"
    if agent == "pi":
        return _session_id_from_pwd() or "unknown"
    return (
        os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("OPENCODE_SESSION_ID")
        or os.environ.get("KILO_SESSION_ID")
        or _session_id_from_pwd()
        or "unknown"
    )


def _write_metadata(result: dict) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{_session_id()}.json"
    result["metadata_path"] = str(path)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Write experience card to mem-recall")
    parser.add_argument("--content", required=True, help="Experience card content (markdown)")
    parser.add_argument("--tags", required=True, help="Comma-separated tags")
    parser.add_argument("--scope-root", choices=sorted(VALID_SCOPE_ROOTS), help="Optional explicit wiki scope root")
    parser.add_argument("--scope-name", help="Optional explicit wiki scope name")
    parser.add_argument("--task-ref", help="Optional task reference")
    args = parser.parse_args()

    try:
        result = _post_experience(
            content=args.content,
            tags=args.tags,
            scope_root=args.scope_root,
            scope_name=args.scope_name,
            task_ref=args.task_ref,
        )
        _write_metadata(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
