---
name: task-trace
namespace: bootstrap
description: |
  将当前任务轨迹缓存到本地，并在启用配置时上传到 MinIO。
  OpenCode/KiloCode 场景会尽量合并 dispatcher + 所有 subagent transcript。
  作为默认 post-task 生命周期的一环，必须先于 skill-recall-propose 执行，
  为 proposal 提供 provenance。
tags: [post-task, trace]
---

# Task Trace

将本次任务的 transcript 缓存到本地 `~/.cache/task-trace/`，并在启用配置时上传到 MinIO，供 task-collect、vuln-report 和 skill-recall-propose 引用。

OpenCode/KiloCode 场景下，脚本会先解析真实 dispatcher/root session，再沿 DB `parent_id` 递归发现 subagent session，将 dispatcher + subagent 的消息按时间全局排序后合并到同一个 JSONL 文件。每条 record 会附带：

- `_trace_session_id`：该消息所属 session ID
- `_trace_agent`：该消息所属 agent 名称

如果 DB 不可用或没有 subagent，则导出当前/root session。

在 `SECOCTO_AGENT_TYPE=pi` 的 secflow 平台环境下，`cwd/run/*.jsonl` 是 transcript 来源。


## Agent 类型配置

Agent 类型不再自动探测，必须从 `~/.config/secocto/.env` 读取：

```env
SECOCTO_AGENT_TYPE=opencode
# allowed: opencode | kilocode | pi | claude
SECOCTO_APP_NAME=secflow
```

Session ID 规则：`opencode` 使用 `OPENCODE_SESSION_ID`，`kilocode` 使用 `KILO_SESSION_ID`，`claude` 使用 `CLAUDE_CODE_SESSION_ID`，`pi` 只使用 cwd 路径 fallback。

Claude subagent / worktree：优先使用 `CLAUDE_SUBAGENT_TRANSCRIPT` 指定 jsonl；其次用 `CLAUDE_SUBAGENT_ID` 或从 cwd 的 `.claude/worktrees/agent-<id>` 自动提取 subagent id，在 root project 的 `<session_id>/subagents/*.jsonl` 中匹配；`CLAUDE_PROJECT_DIR` 可显式指定 root project。找不到时会回退扫描 `~/.claude/projects/*/{session_id}.jsonl` 和 `*/{session_id}/subagents/*.jsonl`。

## Transcript 来源

| agent | 来源 | 说明 |
|---|---|---|
| `pi` | `cwd/run/*.jsonl` | 只走 secflow 路径模式；不查 DB / Claude 目录 |
| `claude` | Claude jsonl | 主 session jsonl；subagent/worktree 时自动回溯 root project 并查找 `subagents/*.jsonl` |
| `opencode` | OpenCode DB | 导出 dispatcher + subagent 轨迹 |
| `kilocode` | KiloCode DB | 导出 dispatcher + subagent 轨迹 |

## 触发条件

这是默认 post-task 生命周期的一环，排在 `task-score` 之后、`skill-recall-propose` 之前。
也可手动 `/task-trace`。

## Session ID 规则

脚本不自动判断 agent 类型，只读取 `SECOCTO_AGENT_TYPE`。Session ID 规则：

| agent | session 规则 |
|---|---|
| `opencode` | `OPENCODE_SESSION_ID`；如是 subagent session，沿 DB `parent_id` 找 root dispatcher；缺失时查当前 cwd 的 DB root session |
| `kilocode` | `KILO_SESSION_ID`；同 opencode 的 DB root/subagent 逻辑 |
| `claude` | `CLAUDE_CODE_SESSION_ID` |
| `pi` | 只从 cwd 路径 `/data/files/<...>/app/<...>/<session_id>` 解析 |

`TASK_TRACE_DISPATCHER_AGENT` 可配置 opencode/kilocode 的 dispatcher agent 名称。

## 配置约束

MinIO / trace 对象存储地址必须来自 `TASK_TRACE_UPLOAD_ENABLED`、`TASK_TRACE_HTTP_ENDPOINT`、`TASK_TRACE_PUBLIC_BASE_URL`、`TASK_TRACE_BUCKET`、`TASK_TRACE_PREFIX` 等环境变量。执行命令时应先加载 `~/.config/secocto/.env`，不要在命令或脚本参数中写死 MinIO 地址。

```bash
python3 <skills-dir>/task-trace/scripts/trace.py

# 如需显式指定 session 或 agent：
python3 <skills-dir>/task-trace/scripts/trace.py --agent opencode --session-id ses_xxxxxxxx
```

脚本按 `SECOCTO_AGENT_TYPE` 定位 transcript 文件，写入 `~/.cache/task-trace/<agent>/trace-<session_id>.jsonl`。

如果 `TASK_TRACE_UPLOAD_ENABLED=1`，脚本会用 HTTP PUT 上传到：

```text
${TASK_TRACE_PUBLIC_BASE_URL}/${TASK_TRACE_BUCKET}/${TASK_TRACE_PREFIX}/<agent>/trace-<session_id>.jsonl
```

MinIO bucket 由 `docker-compose.lifecycle.yml` 初始化为内网匿名读写，因此上传不需要额外 SDK 或 CLI 依赖。

## 输出

```json
{
  "agent": "opencode",
  "session_id": "ses_xxx",
  "local_path": "~/.cache/task-trace/opencode/trace-ses_xxx.jsonl",
  "subagent_count": 3,
  "trace_url": "${TASK_TRACE_PUBLIC_BASE_URL}/${TASK_TRACE_BUCKET}/${TASK_TRACE_PREFIX}/opencode/trace-ses_xxx.jsonl"
}
```

同一份结果会写入 `~/.cache/task-trace/<agent>/trace-<session_id>.json`，供后续 skill 自动读取。

报告输出结果即可（agent 类型、session ID、本地路径、subagent_count、trace_url 或 upload_error）。
