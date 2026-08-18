#!/usr/bin/env python3
"""运行带环境变量配置的测试脚本"""

import os
import sys
from pathlib import Path


def load_env_file(env_file_path=".env"):
    """加载 .env 文件中的环境变量"""
    if not Path(env_file_path).exists():
        print(f"⚠️  {env_file_path} 文件不存在")
        return False

    print(f"📋 加载环境变量从 {env_file_path}")
    with open(env_file_path, "r") as f:
        for line in f:
            line = line.strip()
            # 跳过空行和注释
            if not line or line.startswith("#"):
                continue
            # 解析 KEY=value 格式
            if "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()
                print(f"  ✅ 设置 {key.strip()}")

    return True


def main():
    """主函数"""
    # 尝试加载 .env 文件
    if load_env_file():
        print("-" * 60)

    # 运行测试
    from test_bedrock_agent import test_bedrock_agent

    success = test_bedrock_agent()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
