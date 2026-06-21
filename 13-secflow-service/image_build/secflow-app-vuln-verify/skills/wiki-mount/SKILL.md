---
name: wiki-mount
namespace: bootstrap
description: |
  Sync all content from central WebDAV to ~/wiki/ via rclone copy.
  After sync, agent can read wiki articles directly at ~/wiki/.
tags: [setup, wiki, rclone, knowledge]
---

# Wiki Mount

## 触发条件

手动执行 `/wiki-mount`，或 agent 首次需要读取 wiki 知识时自动触发。

## Workflow

## 配置约束

中心 WebDAV 服务地址必须来自环境变量 `WIKI_WEBDAV_URL`。如果该变量为空，可以向用户询问；除用户明确提供外，不要在命令或 rclone 配置中写死服务端地址。

### Step 1: 确保 rclone 已安装

```bash
which rclone 2>/dev/null
```

如果不存在，自动安装：

```bash
curl -sSL https://rclone.org/install.sh | sudo bash
```

如果 sudo 不可用或安装失败，输出错误信息并结束。

### Step 2: 获取中心节点地址

```bash
WIKI_WEBDAV_URL="${WIKI_WEBDAV_URL:-}"
```

如果为空，问用户：
> 请提供中心节点的 WebDAV 地址（如 `http://192.168.1.100:18780`）

### Step 3: 配置 rclone 远程

> **重要**：rclone 1.60.x 的 inline remote 语法存在 bug（Propfind 路径解析错误）。
> 必须使用命名远程。

```bash
mkdir -p ~/.config/rclone
grep -q "secocto-wiki" ~/.config/rclone/rclone.conf 2>/dev/null || cat >> ~/.config/rclone/rclone.conf << EOF
[secocto-wiki]
type = webdav
url = ${WIKI_WEBDAV_URL}
vendor = other
EOF
```

### Step 4: 同步 wiki

从根目录拉取所有文件，保留原始目录结构：

```bash
mkdir -p ~/wiki
rclone copy secocto-wiki: ~/wiki/ --no-check-dest
```

### Step 5: 验证

```bash
find ~/wiki/ -type f | head -20
```

输出示例：
```
Wiki 已同步: ~/wiki/
  来源: http://192.168.1.100:18780/
  文件:
    ~/wiki/topics/default/wiki/concepts/goroutine-leak-context.md
    ~/wiki/topics/vuln-pattern/wiki/concepts/buffer-overflow-patterns.md
    ~/wiki/topics/default/wiki/concepts/sql-injection-patterns.md
```
