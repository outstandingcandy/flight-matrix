#!/bin/bash

# Flight Matrix 依赖安装脚本
# Installation script for Flight Matrix

set -e  # 遇到错误立即退出

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 打印带颜色的消息
print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_info() {
    echo -e "${YELLOW}ℹ${NC} $1"
}

echo "======================================"
echo "   Flight Matrix 依赖安装脚本"
echo "======================================"
echo

# 检查 Python 版本
print_info "检查 Python 版本..."
if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
    print_success "Python $PYTHON_VERSION 已安装"

    # 检查版本是否 >= 3.8
    if python3 -c 'import sys; exit(0 if sys.version_info >= (3, 8) else 1)'; then
        print_success "Python 版本满足要求 (>= 3.8)"
    else
        print_error "Python 版本过低，需要 3.8 或更高版本"
        exit 1
    fi
else
    print_error "未找到 Python 3，请先安装 Python"
    exit 1
fi

# 检查 pip
print_info "检查 pip..."
if command -v pip3 &> /dev/null; then
    print_success "pip 已安装"
else
    print_error "未找到 pip，正在尝试安装..."
    python3 -m ensurepip --default-pip
fi

# 询问是否使用虚拟环境
echo
read -p "是否创建虚拟环境？(推荐) [Y/n]: " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    print_info "创建虚拟环境..."
    python3 -m venv venv

    # 激活虚拟环境
    print_info "激活虚拟环境..."
    source venv/bin/activate
    print_success "虚拟环境已激活"
fi

# 升级 pip
print_info "升级 pip..."
pip install --upgrade pip -q
print_success "pip 已升级"

# 询问安装类型
echo
echo "请选择安装类型："
echo "1) 完整安装 (包含所有功能)"
echo "2) 核心安装 (仅核心功能)"
echo "3) 自定义安装"
read -p "请输入选择 [1-3]: " -n 1 -r
echo
echo

case $REPLY in
    1)
        print_info "开始完整安装..."
        pip install -r requirements.txt
        print_success "完整安装完成"
        ;;
    2)
        print_info "开始核心安装..."
        pip install requests pyyaml python-dotenv sqlalchemy boto3
        print_success "核心安装完成"
        ;;
    3)
        echo "请选择要安装的模块 (可多选，用空格分隔)："
        echo "1) 核心功能 (requests, yaml, dotenv, sqlalchemy)"
        echo "2) AWS 邮件 (boto3)"
        echo "3) 地图生成 (plotly, kaleido, numpy)"
        echo "4) 地理定位 (reverse-geocoder)"
        echo "5) AI 分析 (tavily-python)"
        echo "6) 测试工具 (pytest, pytest-asyncio)"
        read -p "请输入选择 (如: 1 2 3): " modules

        for module in $modules; do
            case $module in
                1)
                    print_info "安装核心功能..."
                    pip install requests pyyaml python-dotenv sqlalchemy
                    ;;
                2)
                    print_info "安装 AWS 邮件支持..."
                    pip install boto3
                    ;;
                3)
                    print_info "安装地图生成..."
                    pip install plotly kaleido numpy
                    ;;
                4)
                    print_info "安装地理定位..."
                    pip install reverse-geocoder
                    ;;
                5)
                    print_info "安装 AI 分析..."
                    pip install tavily-python
                    ;;
                6)
                    print_info "安装测试工具..."
                    pip install pytest pytest-asyncio
                    ;;
            esac
        done
        print_success "自定义安装完成"
        ;;
esac

# 验证安装
echo
print_info "验证安装..."

# 检查核心模块
if python3 -c "import requests, yaml, sqlalchemy" 2>/dev/null; then
    print_success "核心模块安装成功"
else
    print_error "核心模块安装失败"
fi

# 检查项目模块
if python3 -c "from src.utils.database import DatabaseManager" 2>/dev/null; then
    print_success "数据库模块可用"
else
    print_error "数据库模块不可用"
fi

# 配置文件设置
echo
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    read -p "是否从模板创建 .env 配置文件？[Y/n]: " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        cp .env.example .env
        print_success ".env 文件已创建，请编辑并填入您的配置"
        echo "使用以下命令编辑: nano .env"
    fi
fi

# 运行测试
echo
read -p "是否运行测试以验证安装？[Y/n]: " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    print_info "运行测试..."
    if python3 tests/test_tracking_cycle_mock.py; then
        print_success "测试通过"
    else
        print_error "测试失败，请检查安装"
    fi
fi

echo
echo "======================================"
echo "        安装完成！"
echo "======================================"
echo
echo "下一步："
echo "1. 编辑 .env 文件配置 API 密钥"
echo "2. 编辑 config.yaml 配置追踪参数"
echo "3. 运行: python3 -m src.main --config config.yaml"
echo

if [[ -d "venv" ]]; then
    echo "提示：下次使用前请激活虚拟环境："
    echo "source venv/bin/activate"
fi