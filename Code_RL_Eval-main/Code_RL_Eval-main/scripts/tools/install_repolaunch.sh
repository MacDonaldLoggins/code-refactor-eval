#!/bin/bash
"""
安装 RepoLaunch 到 Python 3.12
"""

set -e

echo "安装 RepoLaunch..."

# 检查是否已安装
if command -v launch &> /dev/null; then
    echo "✅ RepoLaunch 已安装"
    launch --version
    exit 0
fi

# 克隆仓库
echo "克隆 RepoLaunch 仓库..."
TMP_DIR=$(mktemp -d)
git clone https://github.com/microsoft/RepoLaunch.git "$TMP_DIR"

# 使用 uv 安装
echo "安装 RepoLaunch..."
cd "$TMP_DIR"
uv pip install --python 3.12 -e .

# 验证
echo "验证安装..."
launch --version

echo "✅ RepoLaunch 安装完成"
