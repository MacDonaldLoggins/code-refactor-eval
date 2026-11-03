# Claude Code CLI 安装包准备指南

## 快速开始

```bash
# 进入 tools 目录
cd scripts/tools

# 运行准备脚本（自动下载 Linux 版本）
./prepare_claude_cli.sh

# 验证文件
ls -lh claude-code-cli
ls -lh claude-config/
```

---

## 目录结构

```
scripts/
└── tools/
    ├── prepare_claude_cli.sh    # 准备脚本（自动下载）
    ├── claude-code-cli          # Claude Code CLI 可执行文件（Linux 版本）
    ├── claude-config/           # Claude Code 配置文件目录
    │   ├── config.json          # 主配置文件
    │   └── preferences.json     # 用户偏好设置（可选）
    └── README_claude_setup.md   # 本文档
```

---

## 重要提示

⚠️ **必须使用 Linux 版本**

因为 Docker 容器是 Linux 环境，所以需要下载 **Linux 版本**的 Claude Code CLI，而不是 macOS 版本。

❌ **错误**: 复制 macOS 版本
```bash
# 这是错的！
cp /usr/local/bin/claude scripts/tools/claude-code-cli  # macOS 版本
```

✅ **正确**: 使用准备脚本下载 Linux 版本
```bash
cd scripts/tools
./prepare_claude_cli.sh  # 自动下载 Linux 版本
```

---

## 准备脚本说明

### `prepare_claude_cli.sh` 会做什么？

1. ✅ 检查是否已有 `claude-code-cli`
2. ✅ 使用 Docker 容器下载 Linux 版本
3. ✅ 验证文件类型（确认是 Linux ELF）
4. ✅ 创建/复制配置文件
5. ✅ 提供下一步指引

### 使用方法

```bash
cd scripts/tools
./prepare_claude_cli.sh
```

**交互选项**:
- 选项 1: 使用 Docker 容器下载（推荐）✅
- 选项 2: 手动提供下载 URL
- 选项 3: 退出

---

## 手动准备（如果脚本不工作）

### 方法 1: 使用 Docker 临时容器

```bash
cd scripts/tools

# 在 Ubuntu 容器中下载 Linux 版本
docker run --rm -v $(pwd):/output ubuntu:22.04 bash -c "
  apt-get update -qq && \
  apt-get install -y -qq curl && \
  curl -sSL https://install.claude.ai/cli | bash && \
  cp /usr/local/bin/claude /output/claude-code-cli && \
  chmod +x /output/claude-code-cli
"

# 验证
file claude-code-cli
# 应该显示: ELF 64-bit LSB executable
```

### 方法 2: 手动下载

如果 Claude 提供直接下载链接：

```bash
cd scripts/tools

# 下载 Linux 版本
curl -L -o claude-code-cli https://downloads.claude.ai/cli/linux/x86_64/claude

chmod +x claude-code-cli

# 验证
file claude-code-cli
```

---

## 准备配置文件

### 自动准备（推荐）

运行 `prepare_claude_cli.sh` 会自动提示你：
- 复制现有配置（如果你有）
- 或创建默认配置

### 手动准备

```bash
cd scripts/tools

# 创建配置目录
mkdir -p claude-config

# 方法 1: 复制现有配置
cp ~/.claude/config.json claude-config/

# 方法 2: 创建默认配置
cat > claude-config/config.json << 'EOF'
{
  "version": "1.0",
  "model": "claude-3-5-sonnet-20241022",
  "temperature": 0.2,
  "max_tokens": 4096,
  "timeout": 300
}
EOF
```

### 清理敏感信息

⚠️ 编辑 `claude-config/config.json`，移除 API Key：

```json
{
  "api_key": "WILL_BE_SET_BY_ENV",  // ← 改为占位符，或删除这行
  "model": "claude-3-5-sonnet-20241022",
  "temperature": 0.2
}
```

**API Key 会通过环境变量注入，不要硬编码！**

---

## 验证

### 1. 验证文件类型

```bash
file scripts/tools/claude-code-cli

# 应该看到:
# claude-code-cli: ELF 64-bit LSB executable, x86-64, ... (Linux)
```

✅ **正确**: `ELF 64-bit` (Linux)  
❌ **错误**: `Mach-O` (macOS)

### 2. 验证文件结构

```bash
ls -lh scripts/tools/

# 应该看到:
# claude-code-cli          (可执行文件)
# claude-config/           (目录)
#   └── config.json
```

### 3. 测试在 Docker 中运行

```bash
# 启动一个 Linux 容器测试
docker run --rm -v $(pwd)/scripts/tools:/tools ubuntu:22.04 /tools/claude-code-cli --version

# 如果能显示版本号，说明文件正确
```

---

## 在 02_build_image.py 中的使用

### 构建镜像时

`02_build_image.py` 会：

1. 检查 `scripts/tools/claude-code-cli` 是否存在
   - **存在**: 复制到镜像 ✅ 快速、离线
   - **不存在**: 在线下载 ⚠️ 慢、需要网络

2. 检查 `scripts/tools/claude-config/` 是否存在
   - **存在**: 复制配置到镜像 `/root/.claude/`
   - **不存在**: 使用默认配置

### Dockerfile 示例

```dockerfile
FROM repolaunch-base/xxx

# 复制 Claude CLI（Linux 版本）
COPY claude-code-cli /usr/local/bin/claude
RUN chmod +x /usr/local/bin/claude

# 复制配置文件
COPY claude-config/* /root/.claude/

# API Key 在运行时注入
ENV ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}

WORKDIR /workspace
```

---

## 常见问题

### Q1: 为什么不能用 macOS 版本？

Docker 容器是 Linux 环境，macOS 的二进制文件无法在 Linux 中运行。

```bash
# macOS 版本在 Docker 中会报错
exec format error
```

### Q2: 如何验证是 Linux 版本？

```bash
file claude-code-cli

# Linux:  ELF 64-bit LSB executable  ✅
# macOS:  Mach-O 64-bit executable  ❌
```

### Q3: prepare_claude_cli.sh 需要什么？

- ✅ Docker 必须在运行
- ✅ 网络连接（下载时）
- ✅ 执行权限 (`chmod +x`)

### Q4: 配置文件是跨平台的吗？

是的！JSON 配置文件是跨平台的，可以直接从 macOS 复制。

---

## 安全注意事项

⚠️ **不要提交敏感文件到 Git**:

已在 `.gitignore` 中：
```
scripts/tools/claude-code-cli
scripts/tools/claude-config/config.json
```

⚠️ **不要硬编码 API Key**:
- 使用环境变量 `ANTHROPIC_API_KEY`
- 在容器运行时注入

---

## 完成后

```bash
# 1. 验证准备完成
ls -lh scripts/tools/claude-code-cli
ls -lh scripts/tools/claude-config/
file scripts/tools/claude-code-cli

# 2. 构建镜像
python scripts/evaluation/02_build_image.py --case manual_tasks/xxx

# 3. 测试镜像
docker run -it -e ANTHROPIC_API_KEY=sk-xxx refactor-eval/xxx bash
$ claude --version
$ cat /root/.claude/config.json
```

---

更新时间: 2024-07-22


---

## 1. 准备 Claude Code CLI 可执行文件

### 方法 1: 从现有安装复制（推荐）

如果你本地已经安装了 Claude Code CLI：

```bash
# macOS/Linux
which claude
# 输出: /usr/local/bin/claude 或类似路径

# 复制到项目
mkdir -p tools
cp /usr/local/bin/claude tools/claude-code-cli
chmod +x tools/claude-code-cli
```

### 方法 2: 下载官方发行版

```bash
# 从 Claude 官网下载 CLI
# TODO: 补充实际的下载地址

# 或者使用安装脚本下载但不安装
curl -sSL https://install.claude.ai/cli > /tmp/claude-installer.sh
# 从脚本中提取下载 URL，手动下载到 tools/claude-code-cli
```

### 验证

```bash
# 检查文件是否存在
ls -lh tools/claude-code-cli

# 检查是否可执行
file tools/claude-code-cli

# 测试运行
./tools/claude-code-cli --version
```

---

## 2. 准备 Claude Code 配置文件

### 2.1 找到现有配置

Claude Code 的配置通常在：

```bash
# macOS/Linux
~/.claude/

# Windows
%USERPROFILE%\.claude\
```

查看配置文件：

```bash
ls -la ~/.claude/
# 常见文件:
# - config.json          - 主配置
# - preferences.json     - 用户偏好
# - session.json         - 会话信息（不要复制）
# - credentials.json     - 凭证信息（不要复制）
```

---

### 2.2 复制配置文件

```bash
# 创建配置目录
mkdir -p tools/claude-config

# 复制配置文件
cp ~/.claude/config.json tools/claude-config/
cp ~/.claude/preferences.json tools/claude-config/ 2>/dev/null || true

# 不要复制敏感信息
# ❌ 不要复制: session.json, credentials.json, api_keys.json
```

---

### 2.3 清理敏感信息

编辑 `tools/claude-config/config.json`，移除敏感信息：

```json
{
  "api_key": "WILL_BE_SET_BY_ENV",  // ← 改为占位符
  "model": "claude-3.5-sonnet",
  "temperature": 0.2,
  "max_tokens": 4096,
  "other_settings": "..."
}
```

**注意**: 
- API Key 会通过环境变量 `ANTHROPIC_API_KEY` 注入
- 不要在配置文件中硬编码 API Key

---

### 2.4 创建模板配置（如果没有现有配置）

创建 `tools/claude-config/config.json`:

```json
{
  "version": "1.0",
  "model": "claude-3.5-sonnet",
  "temperature": 0.2,
  "max_tokens": 4096,
  "timeout": 300,
  "retry": {
    "max_attempts": 3,
    "backoff": "exponential"
  },
  "logging": {
    "level": "info",
    "output": "/workspace/.claude/logs"
  }
}
```

创建 `tools/claude-config/preferences.json`:

```json
{
  "theme": "dark",
  "auto_save": true,
  "show_hints": false,
  "enable_telemetry": false
}
```

---

## 3. 项目目录结构

完成后的目录结构：

```
refactor/
├── tools/
│   ├── claude-code-cli          # 可执行文件
│   └── claude-config/           # 配置目录
│       ├── config.json          # 主配置
│       └── preferences.json     # 偏好设置
├── scripts/
│   └── evaluation/
│       └── 02_build_image.py    # 会读取 tools/ 目录
└── README.md
```

---

## 4. 在 Docker 镜像中的使用

### 镜像构建时

`02_build_image.py` 会：

1. 检查 `tools/claude-code-cli` 是否存在
   - **存在**: 复制到镜像，快速构建 ✅
   - **不存在**: 在线下载（慢，且需要网络）⚠️

2. 检查 `tools/claude-config/` 是否存在
   - **存在**: 复制配置文件到镜像的 `/root/.claude/`
   - **不存在**: 使用默认配置

### Dockerfile 示例

```dockerfile
FROM repolaunch-base/xxx

# 复制 Claude CLI
COPY claude-code-cli /usr/local/bin/claude
RUN chmod +x /usr/local/bin/claude

# 复制配置文件
COPY claude-config/* /root/.claude/

# 设置环境变量（API Key 在运行时注入）
ENV ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}

WORKDIR /workspace
```

### 容器运行时

```bash
# 启动容器时注入 API Key
docker run -e ANTHROPIC_API_KEY=sk-xxx refactor-eval/xxx

# 在容器内
$ claude --version
Claude Code CLI v1.2.3

$ cat /root/.claude/config.json
{...}

$ claude chat "Hello"
# 使用环境变量中的 API Key
```

---

## 5. 优势

### 使用本地安装包

✅ **快速构建**: 不需要每次下载，节省 30-60 秒  
✅ **离线构建**: 不依赖网络  
✅ **版本固定**: 使用特定版本，避免版本更新导致的问题  
✅ **可复现**: 所有镜像使用相同版本的 CLI  

### 预置配置文件

✅ **一致性**: 所有评测使用相同配置  
✅ **可定制**: 可以针对评测场景优化配置  
✅ **无需手动配置**: 容器启动即可用  

---

## 6. 故障排除

### Q1: claude-code-cli 文件不可执行

```bash
chmod +x tools/claude-code-cli
```

### Q2: 配置文件格式错误

```bash
# 验证 JSON 格式
cat tools/claude-config/config.json | jq .
```

### Q3: API Key 未生效

容器内检查：

```bash
docker run -it refactor-eval/xxx bash
$ echo $ANTHROPIC_API_KEY
# 应该显示你的 API Key
```

### Q4: 找不到 claude 命令

容器内检查：

```bash
$ which claude
/usr/local/bin/claude

$ ls -l /usr/local/bin/claude
-rwxr-xr-x 1 root root ... /usr/local/bin/claude
```

---

## 7. 安全注意事项

⚠️ **不要提交到 Git**:

```bash
# 添加到 .gitignore
echo "tools/claude-code-cli" >> .gitignore
echo "tools/claude-config/config.json" >> .gitignore
```

⚠️ **不要在配置文件中硬编码 API Key**:
- 使用环境变量 `ANTHROPIC_API_KEY`
- 或在运行时动态注入

⚠️ **定期更新 CLI 版本**:
```bash
# 检查新版本
./tools/claude-code-cli --version

# 更新到最新版本（如果需要）
```

---

## 8. 下一步

准备好 Claude Code CLI 后：

```bash
# 1. 验证文件
ls -lh tools/claude-code-cli
ls -lh tools/claude-config/

# 2. 构建镜像
python scripts/evaluation/02_build_image.py --case manual_tasks/xxx

# 3. 检查镜像
docker run -it refactor-eval/xxx bash
$ claude --version
$ cat /root/.claude/config.json
```

---

生成时间: 2024-07-22
