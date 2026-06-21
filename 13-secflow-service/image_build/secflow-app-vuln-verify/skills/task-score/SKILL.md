---
name: task-score
namespace: bootstrap
description: |
  Post-task self-evaluation. Scores the current session across 5 dimensions
  (evidence, results, process, evolution, consistency) and writes a structured
  JSON to ~/.cache/task-score/{session_id}.json for task-collect to consume.
  Triggered by Stop hook (CC) / session.idle (OC) before task-collect.
tags: [post-task, score, evaluation]
---

# Task Score

## 触发条件

由 Stop hook (CC) / session.idle (OC) 在任务完成后自动触发，排在 task-collect 之前。
也可手动 `/task-score`。

## Workflow

### Step 1: 回顾会话

回顾本次会话的完整过程，关注：
- 用户的原始目标是什么
- 执行过程中的关键决策点
- 最终产出物

### Step 2: 按维度评分

按以下 rubric 对本次 session 打分（每个维度满分为其权重值）：

| 维度 | 满分 | 评分标准 |
|------|------|----------|
| evidence | 40 | 每个关键步骤是否有充分的证据支撑（日志输出、代码引用、命令结果、API 响应），结论是否可追溯到具体证据。无证据=0，部分有=20，充分=40 |
| results | 30 | 最终产出是否正确、完整、可验证。完全失败=0，部分完成=15，完全达成=30 |
| process | 15 | 是否有清晰的探索→尝试→修正路径。遇到失败时是否有效恢复而非重复尝试。混乱=0，基本清晰=8，高效=15 |
| evolution | 10 | 是否产出了有价值的知识卡片或 skill 改进 proposal。无产出=0，仅有卡片=5，有 proposal（无论是否有卡片）=10 |
| consistency | 5 | 任务目标→执行过程→最终产出之间是否语义连贯，有无跑题或遗漏。不一致=0，基本一致=3，完全一致=5 |

### Step 3: 输出结构化 JSON

构造评分结果并调用脚本写入（总分由 vector 自动加总，无需手动传入）：

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/score.py \
  --vector '<evidence>,<results>,<process>,<evolution>,<consistency>' \
  --reasoning '<一句话评分理由>'
```

示例：
```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/score.py \
  --vector '30,22,12,5,3' \
  --reasoning '证据链完整但结果有一处未验证的边界情况'
```
# 输出 score=72 (30+22+12+5+3)，并显式包含 path=~/.cache/task-score/{session_id}.json

### 评分原则

- 诚实评估，不要自我膨胀
- evidence 维度最重要：如果过程中有"我认为"但没有实际验证的步骤，evidence 应扣分
- 简单任务（如改个 typo）如果完成得好，各维度仍可得高分
- evolution 维度：如果任务本身不涉及进化（如简单 bug fix），给 5 分基础分即可
