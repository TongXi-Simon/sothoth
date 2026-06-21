---
name: distill-experience
namespace: bootstrap
description: |
  Post-task experience distillation. Extracts lessons learned from the
  completed task and writes to mem-recall via HTTP for curator compilation.
  Run explicitly after a task when the session produced reusable lessons.
tags: [post-task, experience, distill, mem-recall]
---

# Distill Experience

## 触发条件

任务完成后如有可复用经验，显式执行本 skill。

## Workflow

## 配置约束

连接 mem-recall 的地址必须来自环境变量 `MEM_RECALL_URL`；Bearer token 认证通过 `MEM_RECALL_API_TOKEN` 传入（非空时脚本自动附加 `Authorization: Bearer` 头）。执行命令时应先加载 `~/.config/secocto/.env`，不要在命令、脚本参数或正文中写死服务端地址或 token。

### Step 1: 评估是否值得蒸馏

回顾刚才完成的任务，判断是否产生了值得记录的经验：

**值得蒸馏的情况**：
- 遇到了非显而易见的坑或失败路径
- 发现了反直觉的解决方案
- 涉及安全漏洞发现、利用或修复
- 调试过程中有关键转折点

**跳过蒸馏的情况**：
- 纯粹的代码格式化或简单重命名
- 按照已有文档直接完成的常规操作
- 没有遇到任何意外的简单任务

如果判断不值得蒸馏，输出"本次任务无需蒸馏"并结束。

### Step 2: 提取经验

从任务过程中提取以下内容：

**Title**: 一句话概括经验主题（如"FastAPI 子路由挂载顺序导致 404"）

**Tags**: 从内容中提取 3-5 个标签，小写，连字符分隔（如 `fastapi,routing,debug-pattern`）

**Body** 按以下结构组织：

```markdown
## 失败路径
- 尝试方向1 → 失败原因（具体错误信息或现象）
- 尝试方向2 → 失败原因
（至少 2 条，记录走过的弯路）

## 成功路径
关键转折点描述；包含具体文件路径、接口、命令或配置

## Action 准则
1. 高密度规则（优先记录反直觉发现）
2. ...
3. ...
（3-5 条，禁止写通用常识如"要做好错误处理"）
```

### Step 3: 写入 mem-recall

`task_ref` 编入 tags 中，格式 `task:<ref>`（如 `task:sqli-audit-login`）。

同时给出建议的 wiki scope，用于 mem-recall 编译路由：
- 安全审计、漏洞模式、CWE、注入、反序列化、RCE 等经验：`scope_root=topics`, `scope_name=vuln-pattern`
- 项目架构、部署、服务、API、配置等经验：`scope_root=proj`, `scope_name=default`
- 任务执行过程、工作流、调试路径等经验：`scope_root=task`, `scope_name=default`
- 无法判断：`scope_root=topics`, `scope_name=default`

通过脚本调用 mem-recall HTTP API 写入：

```bash
python3 <skills-dir>/distill-experience/scripts/store.py \
  --content '# <title>\n\n<Step 2 中提取的完整 body>' \
  --tags '<逗号分隔标签>' \
  --scope-root '<建议 scope_root，可省略让脚本推断>' \
  --scope-name '<建议 scope_name，可省略让脚本推断>' \
  --task-ref '<task_ref，可省略>'
```

脚本通过 `MEM_RECALL_URL` 连接 mem-recall 的 `/experiences` HTTP API，使用 stdlib 的 urllib，不依赖任何第三方库。

### Step 4: 确认结果

检查返回值：
- 脚本输出 JSON 含 `content_hash` 和 `metadata_path` → 写入成功或已存在，并已缓存到 `~/.cache/distill-experience/{session_id}.json`
- `status=ok` 表示写入 raw note，`status=exists` 表示同一经验已存在
- exit 0 = 成功，exit 1 = 失败
- 如果 mem-recall 不可用，报告错误但不阻塞流程

输出示例：
```
经验蒸馏完成: mem-recall hash=a1b2c3d4 tags=[sqlite,wal,concurrency,task:sqlite-lock-debug]
```
