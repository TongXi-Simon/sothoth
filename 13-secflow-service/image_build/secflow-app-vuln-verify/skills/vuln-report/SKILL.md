---
name: vuln-report
namespace: bootstrap
description: |
  Post-task vulnerability reporting. Collects discovered vulnerabilities from
  the current session, formats them as SARIF 2.1.0 with evidence chains, and
  uploads high-confidence findings to vuln-restore API with task-trace URL.
tags: [post-task, vuln-report, sarif, vulnerability]
---

# Vuln Report

## 触发条件

由 Stop hook (CC) / session.idle (OC) 触发。仅在漏洞相关任务中执行（vuln-audit, code-review, exploit-dev, poc-gen），且只有发现一个或多个高置信度漏洞时才提交。
非漏洞任务、没有漏洞、或只有低置信度/推测性发现时直接跳过。

## 配置约束

vuln-restore 服务地址必须来自环境变量 `VULN_RESTORE_URL`；task trace 对象存储地址必须来自 `TASK_TRACE_*` 环境变量和 task-trace metadata。执行命令时应先加载 `~/.config/secocto/.env`，不要在命令、脚本参数、SARIF 或 payload 中写死服务端地址。

## Workflow

### Step 1: 检查任务类型

回顾本次会话内容。如果本次任务**不是**以下类型之一，输出 "Not a vuln task, skipping vuln-report" 并停止：
- 漏洞审计 (vuln-audit)
- 代码审查中发现安全问题 (code-review with security findings)
- 漏洞利用开发 (exploit-dev)
- PoC 生成 (poc-gen)

如果本次会话中**没有发现任何高置信度漏洞**，同样跳过。

高置信度漏洞必须满足：
- 有明确的漏洞类型或 CWE/rule_id
- 有明确文件路径和行号
- 有可解释的 evidence_chain
- 不是仅凭猜测、可能性或风格问题推断出来的风险

### Step 2: 收集高置信度 findings

遍历本次会话，只提取**高置信度**漏洞。对每条漏洞提取：

- **rule_id**：CWE 编号（如 `CWE-79`）或自定义规则 ID
- **severity**：`critical` | `high` | `medium` | `low` | `note`
- **confidence**：必须为 `high` 或 `confirmed`
- **message**：一句话描述漏洞（如 "SQL injection in user login query"）
- **file_path**：漏洞所在文件路径
- **start_line** / **end_line**：漏洞代码行范围
- **evidence_chain**：有序的代码位置列表，展示数据流或控制流如何导致漏洞。每个位置包含：
  - `file_path`：文件路径
  - `start_line`：行号
  - `message`：该位置在证据链中的角色说明（如 "user input enters here", "passed to SQL query without sanitization"）

### Step 3: 分类

不要读取额外 taxonomy 文件；直接按以下轻量方法分类：

1. `rule_id` 优先使用标准 CWE 编号（如 `CWE-79`、`CWE-89`、`CWE-78`、`CWE-22`、`CWE-502`、`CWE-287`、`CWE-798`），无法准确对应时使用稳定的自定义规则 ID。
2. `severity` 按可利用性和影响评估：可直接 RCE/认证绕过/敏感数据泄露通常为 `critical`/`high`；需要复杂前置条件或影响有限通常为 `medium`/`low`；只作说明用 `note`。

### Step 4: 获取 repo 信息

```bash
git remote get-url origin 2>/dev/null || echo ""
git rev-parse HEAD 2>/dev/null || echo ""
```

### Step 5: 构建 SARIF 文档

将所有 findings 组装为 SARIF 2.1.0 格式的 JSON。结构如下：

```json
{
  "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json",
  "version": "2.1.0",
  "runs": [{
    "tool": {"driver": {"name": "sec-agent", "version": "1.0"}},
    "results": [
      {
        "ruleId": "<rule_id>",
        "level": "<error|warning|note>",
        "message": {"text": "<message>"},
        "locations": [{
          "physicalLocation": {
            "artifactLocation": {"uri": "<file_path>"},
            "region": {"startLine": N, "endLine": M}
          }
        }],
        "codeFlows": [{
          "threadFlows": [{
            "locations": [
              {
                "location": {
                  "physicalLocation": {
                    "artifactLocation": {"uri": "<evidence_file>"},
                    "region": {"startLine": N}
                  },
                  "message": {"text": "<evidence_message>"}
                }
              }
            ]
          }]
        }]
      }
    ]
  }]
}
```

severity 到 SARIF level 的映射：
- critical/high → `error`
- medium → `warning`
- low/note → `note`

### Step 6: 调用上报脚本

将 findings 作为 JSON 数组传给脚本：

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/report.py --findings '<JSON array of findings>'
```

每条 finding 的 JSON 结构：
```json
{
  "rule_id": "CWE-79",
  "severity": "high",
  "confidence": "high",
  "message": "XSS via unsanitized user input",
  "file_path": "src/handler.py",
  "start_line": 42,
  "end_line": 45,
  "evidence_chain": [
    {"file_path": "src/handler.py", "start_line": 10, "message": "user input from request.args"},
    {"file_path": "src/handler.py", "start_line": 42, "message": "rendered in template without escaping"}
  ]
}
```

脚本会自动检测 session ID、读取 task-trace 的 MinIO `trace_url`、构建完整 SARIF、POST 到 vuln-restore。若未显式标注 `confidence`，脚本只会提交具备 rule_id、message、file_path、start_line、evidence_chain 的完整 finding。

### Step 7: 确认结果

检查脚本输出：
- 成功：输出 `report_id` 和 `finding_count`（应等于 Step 2 收集到的漏洞数）
- 失败：报告错误但不阻塞 post-task 链路
