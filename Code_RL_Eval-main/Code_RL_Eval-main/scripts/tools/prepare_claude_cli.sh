#!/bin/bash
"""
准备 Claude Code CLI（Linux 版本）

用途：在 macOS 上下载 Linux 版本的 Claude Code CLI，用于 Docker 镜像
"""

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_DIR="$SCRIPT_DIR"
CLAUDE_CLI="$TOOLS_DIR/claude-code-cli"
CONFIG_DIR="$TOOLS_DIR/claude-config"

echo "=================================================="
echo "准备 Claude Code CLI（Linux 版本）"
echo "=================================================="
echo ""

# 1. 检查是否已存在
if [ -f "$CLAUDE_CLI" ]; then
    echo "⚠️  $CLAUDE_CLI 已存在"
    read -p "是否覆盖？(y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "跳过下载"
        exit 0
    fi
    rm -f "$CLAUDE_CLI"
fi

echo ""
echo "选择下载方法："
echo "1. 使用 Docker 容器下载（推荐）"
echo "2. 手动提供下载 URL"
echo "3. 退出"
echo ""
read -p "请选择 (1-3): " choice

case $choice in
    1)
        echo ""
        echo "使用 Docker 容器下载 Linux 版本..."
        echo ""

        # 检查 Docker 是否运行
        if ! docker info > /dev/null 2>&1; then
            echo "❌ Docker 未运行，请先启动 Docker"
            exit 1
        fi

        # 创建临时目录
        TMP_DIR=$(mktemp -d)
        echo "临时目录: $TMP_DIR"

        # 在 Docker 中下载
        echo "在 Ubuntu 容器中安装 Claude CLI..."
        docker run --rm -v "$TMP_DIR:/output" ubuntu:22.04 bash -c '
            apt-get update -qq && \
            apt-get install -y -qq curl && \
            echo "下载 Claude CLI..." && \
            curl -sSL https://install.claude.ai/cli -o /tmp/install.sh && \
            bash /tmp/install.sh && \
            cp $(which claude) /output/claude-code-cli && \
            echo "Claude CLI 版本:" && \
            /output/claude-code-cli --version
        '

        # 移动到目标位置
        if [ -f "$TMP_DIR/claude-code-cli" ]; then
            mv "$TMP_DIR/claude-code-cli" "$CLAUDE_CLI"
            chmod +x "$CLAUDE_CLI"
            echo "✅ Claude CLI 已保存到: $CLAUDE_CLI"
        else
            echo "❌ 下载失败"
            rm -rf "$TMP_DIR"
            exit 1
        fi

        # 清理
        rm -rf "$TMP_DIR"
        ;;

    2)
        echo ""
        read -p "请输入 Linux 版本的下载 URL: " url

        if [ -z "$url" ]; then
            echo "❌ URL 不能为空"
            exit 1
        fi

        echo "下载中..."
        curl -L -o "$CLAUDE_CLI" "$url"
        chmod +x "$CLAUDE_CLI"
        echo "✅ Claude CLI 已保存到: $CLAUDE_CLI"
        ;;

    3)
        echo "退出"
        exit 0
        ;;

    *)
        echo "❌ 无效选择"
        exit 1
        ;;
esac

echo ""
echo "=================================================="
echo "验证下载的文件"
echo "=================================================="
echo ""

# 验证文件类型
echo "文件类型:"
file "$CLAUDE_CLI"

echo ""
echo "文件大小:"
ls -lh "$CLAUDE_CLI"

echo ""
echo "尝试运行:"
"$CLAUDE_CLI" --version 2>&1 || echo "⚠️  无法在 macOS 上直接运行（这是正常的，因为这是 Linux 版本）"

echo ""
echo "=================================================="
echo "准备配置文件"
echo "=================================================="
echo ""

# 创建配置目录
mkdir -p "$CONFIG_DIR"

# 检查是否有现有配置
if [ -f "$HOME/.claude/config.json" ]; then
    echo "发现本地 Claude 配置: $HOME/.claude/config.json"
    read -p "是否复制到项目？(Y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
        cp "$HOME/.claude/config.json" "$CONFIG_DIR/"
        echo "✅ 配置文件已复制"

        # 警告：检查敏感信息
        echo ""
        echo "⚠️  请检查配置文件，移除敏感信息（API Key 等）"
        echo "    配置文件位置: $CONFIG_DIR/config.json"
    fi
else
    echo "本地未找到 Claude 配置"
    read -p "是否创建默认配置？(Y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
        cat > "$CONFIG_DIR/config.json" << 'EOF'
{
  "version": "1.0",
  "model": "claude-3-5-sonnet-20241022",
  "temperature": 0.2,
  "max_tokens": 4096,
  "timeout": 300
}
EOF
        echo "✅ 默认配置已创建: $CONFIG_DIR/config.json"
    fi
fi

echo ""
echo "=================================================="
echo "完成！"
echo "=================================================="
echo ""
echo "下一步："
echo "1. 检查配置文件，移除敏感信息:"
echo "   cat $CONFIG_DIR/config.json"
echo ""
echo "2. 验证文件结构:"
echo "   ls -lh $TOOLS_DIR"
echo ""
echo "3. 运行镜像构建:"
echo "   python scripts/evaluation/02_build_image.py --case manual_tasks/xxx"
echo ""
