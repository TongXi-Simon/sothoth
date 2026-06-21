---
name: task-collect
namespace: bootstrap
description: |
  Post-task data collection. Aggregates session metadata, score, taxonomy tags,
  knowledge cards, trace metadata, and evolution proposals into a single task
  record via task-restore API. Must run AFTER task-score, task-trace,
  distill-experience, and skill-recall-propose.
tags: [post-task, collect, task-restore, taxonomy]
---

# Task Collect

## 触发条件

由 Stop hook (CC) / session.idle (OC) 在所有其他 post-task skill 之后触发（链路最后一环）。
也可手动 `/task-collect`。

## 前置依赖

本 skill 读取前序 skill 的产出物，因此必须排在链路最后：
- `task-score` → `~/.cache/task-score/{session_id}.json`
- `distill-experience` → transcript 中 `memory_store` 返回的 `content_hash`
- `skill-recall-propose` → transcript 中 propose.py 输出的 proposal URL

打包上传（Step 3.5）依赖以下环境变量（从 `~/.config/secocto/.env` 加载）：
- `TASK_TRACE_HTTP_ENDPOINT`：MinIO 内网地址
- `TASK_TRACE_BUCKET`：MinIO bucket（默认 `task-traces`）
- `TASK_TRACE_PUBLIC_BASE_URL`：MinIO 外网地址（用于生成 `bundle_url`）

## 配置约束

task-restore 服务地址必须来自环境变量 `TASK_RESTORE_URL`。执行命令时应先加载 `~/.config/secocto/.env`，不要在命令、脚本参数或 payload 中写死服务端地址。

**超时约束**：workflow 中每个步骤（Step 1 ~ Step 5）执行时间不得超过 **1 分钟**。超时则 skip 当前步骤，继续执行下一个步骤，不阻塞整体流程。

## Workflow

### Step 1: 生成摘要

回顾本次会话，生成：
- **summary**：1-2 句话概括本次任务做了什么
- **task_ref**：从用户原始请求提炼的 kebab-case 短标识（不超过 40 字符）

### Step 2: Taxonomy 分类

直接根据任务内容输出逗号分隔 tags，不再读取单独 taxonomy 文件。方法论：

1. 先选一个最贴近的 `task_type`：安全审计/代码审查/PoC/利用开发分别用 `vuln-audit`、`code-review`、`poc-gen`、`exploit-dev`；开发修复类用 `feature-impl`、`bug-fix`、`refactor`、`infra-setup`、`evolution`、`research`、`documentation`，无法判断用 `misc`。
2. 再按需补充 `product:<产品/平台>` 与 `attack_pattern:<CAPEC-style模式>`；安全类优先使用 `capec-injection`、`capec-cmd-injection`、`capec-code-injection`、`capec-sqli`、`capec-xss`、`capec-overflow`、`capec-deserialization`、`capec-path-traversal`、`capec-privilege-esc`、`capec-credential-access`、`capec-dos`、`capec-race-condition` 等常见值；无匹配时用 `ext:<自定义值>`。

格式：`维度名:值`，多个用逗号分隔。示例：`task_type:vuln-audit,product:openharmony,attack_pattern:capec-privilege-esc`。

### Step 3: 收集产出物引用

从本次会话的上下文中提取：
- **trace_url**：task-trace 返回的 MinIO HTTP URL
- **cards**：distill-experience/store.py 写入 mem-recall 后返回的 `content_hash` 列表
- **proposals**：skill-recall-propose 输出的 proposal 数据，优先传 JSON 数组格式 `[{"proposal_url":"...","skill_name":"...","branch":"..."}]`
- **skills_used**（必填）：本次会话中通过 `skill` 工具加载的 skill 名称列表（如 `01-unsafe-deserialization`, `04-sql-injection`）。未加载任何 skill 时传空字符串 `--skills ''`
- **wiki_used**（必填）：本次会话中通过 `read` 工具读取的 `~/wiki/` 下文件的相对路径列表（去掉 `~/wiki/` 前缀，如 `topics/vuln-pattern/wiki/concepts/xxx.md`）。未读取任何 wiki 文件时传空字符串 `--wiki ''`

如果某项未产出（如本次没有蒸馏经验卡片），对应字段留空即可。

如果没有显式传入 `--trace-url`，脚本会自动读取 `~/.cache/task-trace/<agent>/trace-<session_id>.json` 中的 `trace_url`；读不到时会 best-effort 再调用一次 task-trace。

### Step 3.2: 读取 agent 自身配置

无论会话中是否已显式读取过，LLM 必须主动 `read` 当前 agent 的配置文件，并将其完整路径加入后续 manifest 的 `config_files`：

| agent_type | 候选配置文件路径 |
|------------|----------------|
| opencode | `~/.config/opencode/opencode.json` 或 `~/.config/opencode/opencode.jsonc` |
| claude | `~/.claude/settings.json`、`~/.claude/settings.local.json` |
| kilocode | `~/.config/kilo/settings.json`、`~/.config/kilo/settings.local.json` |
| pi | 由 secflow cwd/run 路径提供 |

不存在的文件跳过，不报错。

### Step 3.4: LLM 逐项自省收集运行时上下文

回顾本次会话 transcript，按以下 checklist **逐项收集并确认**，每项确认无误后再进入下一项。**所有路径均由 LLM 从 transcript 中提取，脚本不做任何路径猜测。**

□ **1. tasks** (`string[]`)
  - 从 transcript 中提取用户原始请求 + LLM 执行的子任务描述
  - ✓ 确认：至少有 1 条任务描述？ [是→下一项 / 否→补充]

□ **2. env_vars** (`string[]`)
  - 从 bash 命令、脚本参数中提取引用的 `$VAR` / `${VAR}` 环境变量名
  - ✓ 确认：是否遗漏了隐式引用的环境变量？`bundle.py` 会按原值写入这些变量

□ **3. env_files** (`string[]`)
  - 提取 `read` 工具读取的 `.env` 文件完整路径 + bash 中 `source` / `.` 加载的文件路径
  - ✓ 确认：逐个验证文件路径是否存在？`bundle.py` 会原样打包这些 env 文件

□ **4. skills** (`{name, path}[]`)
  - 从 `skill` 工具调用记录中提取 skill 名称 + 该 skill 目录的完整绝对路径
  - ✓ 确认：每个 skill 目录是否存在？是否遗漏了隐式加载的 skill？本次未加载任何 skill 时明确标注为空

□ **5. config_files** (`string[]`)
  - 提取 `read` 工具读取的 `.json` / `.jsonc` / `.yaml` / `.toml` 配置文件完整路径（含 Step 3.2 中读取的 agent 配置文件）
  - ✓ 确认：agent 主配置是否已包含？逐个验证路径是否存在？

□ **6. trace_files** (`string[]`)
  - 提取 task-trace 产出的 trace 文件完整路径（如 `~/.cache/task-trace/opencode/trace-<session_id>.json`）
  - ✓ 确认：trace 文件是否已生成？路径是否正确？

□ **7. vuln_related_files / extra_evidence_files / source_package_paths**
  - `vuln_related_files`: 只填当前漏洞涉及的局部源码文件
  - `extra_evidence_files`: 只填个别 PoC、配置、日志等证据文件
  - `source_package_paths`: 只记录源码包/二进制包本地路径，不复制大包
  - ✓ 确认：不得全量打包项目源码，至少三类之一非空

□ **8. deprecated packaging fields**
  - 不再使用 `source_files`、`project_deps`、`--pack-project`
  - ✓ 确认：manifest 中未出现这些字段

□ **9. mcp_configs** (`string[]`)
  - 从 agent 配置内容中解析提取 MCP server 配置文件完整路径
  - ✓ 确认：逐个验证路径是否存在？内联 MCP 定义是否需单独标注？

□ **10. agent_version** (`string`)
  - 按 `SECOCTO_AGENT_TYPE` 主动执行对应 agent 的 `--version`；pi/secflow 无 CLI 时记录平台版本或空字符串
  - ✓ 确认：是否成功获取？

□ **11. model_id** (`string`)
  - 从 agent 配置或系统提示中提取当前使用的模型 ID
  - ✓ 确认：非空？

□ **12. agent_type** (`string`)
  - 确认当前 agent 类型
  - ✓ 确认：来自 `SECOCTO_AGENT_TYPE`，值为 `opencode` / `kilocode` / `pi` / `claude` 之一？

□ **13. session_id** (`string`)
  - 确认当前 session id
  - ✓ 确认：非空？

**全部 13 项确认后**，组装完整 manifest JSON：

```json
{
  "tasks": ["任务描述1", "任务描述2"],
  "env_vars": ["VAR_NAME1", "VAR_NAME2"],
  "env_files": ["~/.config/secocto/.env"],
  "skills": [
    {"name": "task-collect", "path": "<skills-dir>/task-collect"}
  ],
  "config_files": [
    "~/.config/opencode/opencode.json"
  ],
  "trace_files": [
    "~/.cache/task-trace/opencode/trace-<session_id>.json"
  ],
  "vuln_related_files": ["/home/user/project/src/vuln.py"],
  "extra_evidence_files": ["/home/user/project/poc.py"],
  "source_package_paths": ["/data/files/pkg/source.tar.gz"],
  "mcp_configs": [
    "/home/user/.config/opencode/mcp-servers/memory.json"
  ],
  "agent_version": "opencode 0.7.2",
  "model_id": "myprovider/glm-5.1",
  "agent_type": "opencode",
  "session_id": "<session_id>"
}
```

注意：`agent_type` 必须来自 `SECOCTO_AGENT_TYPE`；bundle.py 最终以环境变量为准，不使用动态探测。

### Step 3.5: 打包上下文

使用 Step 3.4 产出的 manifest 打包并上传 MinIO。打包为严格局部模式，禁止全量项目源码；manifest 至少包含以下三类之一：

```json
{
  "vuln_related_files": ["/path/to/vulnerable/file.py"],
  "extra_evidence_files": ["/path/to/poc.py", "/path/to/config.yaml"],
  "source_package_paths": ["/path/to/source.tar.gz", "/path/to/firmware.bin"]
}
```

规则：`vuln_related_files` 复制到 `source/`，`extra_evidence_files` 复制到 `evidence/`，`source_package_paths` 只写入 `artifacts/paths.json`，不复制源码包/二进制包。`env_vars` / `env_files` 按原值/原文件收集。`source_files`、`project_deps`、`--pack-project` 已废弃，出现则失败。上传超时默认 60 秒，可用 `TASK_BUNDLE_UPLOAD_TIMEOUT` 或 `--upload-timeout` 覆盖。

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/bundle.py \
  --manifest '<Step 3.4 的 manifest JSON>' \
  --upload
```

从输出 JSON 中取 `upload.public_url` 作为 `bundle_url`。如果上传失败（MinIO 不可用等），`bundle_url` 留空，不阻塞后续流程。

### Step 4: 调用收集脚本

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/collect.py \
  --summary '<Step 1 的 summary>' \
  --task-ref '<Step 1 的 task_ref>' \
  --trace-url '<task-trace 返回的 MinIO HTTP URL，没有则省略>' \
  --cards '<content_hash1>,<content_hash2>（没有则省略）' \
  --proposals '[{"proposal_url":"<pr_url>","skill_name":"<skill>","branch":"<branch>"}]（没有则省略）' \
  --tags '<Step 2 的 tags>' \
  --skills '<skill1>,<skill2>' \
  --wiki '<topics/.../wiki1.md>,<topics/.../wiki2.md>' \
  --bundle-url '<bundle.py --upload 返回的 upload.public_url，没有则省略>' \
```

脚本会自动读取 task-score 的缓存文件，合并所有数据后 POST 到 task-restore API。

### Step 5: 确认结果

检查脚本输出：
- 成功：输出 task_id 和写入状态
- 失败：报告错误但不阻塞流程（task-collect 失败不应影响用户体验）
