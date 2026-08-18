#!/usr/bin/env python3
"""
Lambda Restart Tool for Flight Matrix
重启Lambda函数 - 通过更新配置触发冷启动

Usage:
    python scripts/restart_lambda.py                    # 使用默认函数名
    python scripts/restart_lambda.py -f my-function     # 指定函数名
    python scripts/restart_lambda.py -e prod            # 指定环境
    python scripts/restart_lambda.py --region us-east-1 # 指定区域
    python scripts/restart_lambda.py --sync             # 同步代码后重启
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from typing import Optional


def get_aws_region() -> str:
    """获取当前AWS区域"""
    try:
        result = subprocess.run(
            ["aws", "configure", "get", "region"], capture_output=True, text=True, check=True
        )
        return result.stdout.strip() or "ap-northeast-1"
    except subprocess.CalledProcessError:
        return "ap-northeast-1"


def get_function_config(function_name: str, region: str) -> Optional[dict]:
    """获取Lambda函数配置"""
    try:
        result = subprocess.run(
            [
                "aws",
                "lambda",
                "get-function-configuration",
                "--function-name",
                function_name,
                "--region",
                region,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"Error getting function config: {e.stderr}")
        return None


def update_function_config(
    function_name: str,
    region: str,
    description: Optional[str] = None,
    env_vars: Optional[dict] = None,
) -> bool:
    """更新Lambda函数配置以触发重启"""
    cmd = [
        "aws",
        "lambda",
        "update-function-configuration",
        "--function-name",
        function_name,
        "--region",
        region,
    ]

    if description:
        cmd.extend(["--description", description])

    if env_vars:
        cmd.extend(["--environment", json.dumps({"Variables": env_vars})])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error updating function: {e.stderr}")
        return False


def get_function_state(function_name: str, region: str) -> Optional[str]:
    """获取Lambda函数状态"""
    config = get_function_config(function_name, region)
    if config:
        return config.get("State", "Unknown")
    return None


def wait_for_function_active(function_name: str, region: str, timeout: int = 60) -> bool:
    """等待函数变为Active状态"""
    import time

    start = time.time()
    while time.time() - start < timeout:
        state = get_function_state(function_name, region)
        if state == "Active":
            return True
        if state == "Failed":
            print(f"Function state is Failed!")
            return False
        print(f"  Function state: {state}, waiting...")
        time.sleep(2)
    return False


def restart_lambda(function_name: str, region: str, method: str = "env") -> bool:
    """
    重启Lambda函数

    Args:
        function_name: Lambda函数名
        region: AWS区域
        method: 重启方式 - "env" (更新环境变量) 或 "desc" (更新描述)

    Returns:
        是否成功
    """
    print(f"Restarting Lambda function: {function_name}")
    print(f"Region: {region}")
    print(f"Method: {method}")
    print("-" * 50)

    # 获取当前配置
    config = get_function_config(function_name, region)
    if not config:
        print("Failed to get function configuration")
        return False

    print(f"Current state: {config.get('State', 'Unknown')}")
    print(f"Last modified: {config.get('LastModified', 'Unknown')}")

    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    if method == "env":
        # 通过更新环境变量触发重启
        current_env = config.get("Environment", {}).get("Variables", {})
        current_env["RESTART_TIMESTAMP"] = timestamp
        print(f"\nUpdating environment variable RESTART_TIMESTAMP={timestamp}")
        success = update_function_config(function_name, region, env_vars=current_env)
    else:
        # 通过更新描述触发重启
        new_desc = f"Flight Matrix Aircraft Tracking API (Restarted: {timestamp})"
        print(f"\nUpdating description to: {new_desc}")
        success = update_function_config(function_name, region, description=new_desc)

    if not success:
        print("Failed to update function configuration")
        return False

    print("\nWaiting for function to become active...")
    if wait_for_function_active(function_name, region):
        print("\nFunction restarted successfully!")
        # 获取新配置验证
        new_config = get_function_config(function_name, region)
        if new_config:
            print(f"New last modified: {new_config.get('LastModified', 'Unknown')}")
        return True
    else:
        print("\nTimeout waiting for function to become active")
        return False


def sync_and_restart(function_name: str, region: str) -> bool:
    """同步代码并重启Lambda - 直接使用AWS CLI更新代码"""
    import os
    import tempfile
    import zipfile

    print("Syncing Lambda code...")
    print("-" * 50)

    # 创建代码zip包
    lambda_code_dir = "lambda_code"
    if not os.path.exists(lambda_code_dir):
        print(f"Lambda code directory not found: {lambda_code_dir}")
        print("Falling back to regular restart...")
        return restart_lambda(function_name, region)

    # 创建临时zip文件
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_file:
        zip_path = tmp_file.name

    try:
        print(f"Creating deployment package from {lambda_code_dir}/...")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(lambda_code_dir):
                # 排除不需要的目录
                dirs[:] = [d for d in dirs if d not in ["__pycache__", ".git", ".venv"]]
                for file in files:
                    if file.endswith(".pyc") or file.endswith(".log"):
                        continue
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, lambda_code_dir)
                    zipf.write(file_path, arcname)
                    print(f"  Added: {arcname}")

        zip_size = os.path.getsize(zip_path)
        print(f"\nDeployment package size: {zip_size / 1024:.1f} KB")

        # 使用AWS CLI更新Lambda代码
        print(f"\nUpdating Lambda function code: {function_name}")
        result = subprocess.run(
            [
                "aws",
                "lambda",
                "update-function-code",
                "--function-name",
                function_name,
                "--zip-file",
                f"fileb://{zip_path}",
                "--region",
                region,
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            print(f"Failed to update code: {result.stderr}")
            return False

        print("Code updated successfully!")

        # 等待函数变为Active
        print("\nWaiting for function to become active...")
        if wait_for_function_active(function_name, region):
            print("Function is active!")

            # 获取新配置
            new_config = get_function_config(function_name, region)
            if new_config:
                print(f"Last modified: {new_config.get('LastModified', 'Unknown')}")
                print(f"Code SHA256: {new_config.get('CodeSha256', 'Unknown')[:16]}...")
            return True
        else:
            print("Timeout waiting for function")
            return False

    finally:
        # 清理临时文件
        if os.path.exists(zip_path):
            os.remove(zip_path)


def invoke_test(function_name: str, region: str) -> bool:
    """测试调用Lambda函数"""
    print("\nTesting Lambda invocation...")
    try:
        result = subprocess.run(
            [
                "aws",
                "lambda",
                "invoke",
                "--function-name",
                function_name,
                "--region",
                region,
                "--payload",
                '{"httpMethod": "GET", "path": "/health"}',
                "--cli-binary-format",
                "raw-in-base64-out",
                "/tmp/lambda_response.json",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        response_info = json.loads(result.stdout)
        print(f"Status code: {response_info.get('StatusCode')}")

        # 读取响应
        try:
            with open("/tmp/lambda_response.json", "r") as f:
                response = json.load(f)
                print(f"Response status: {response.get('statusCode', 'N/A')}")
        except Exception:
            pass

        return response_info.get("StatusCode") == 200
    except subprocess.CalledProcessError as e:
        print(f"Invocation failed: {e.stderr}")
        return False


def list_functions(region: str, prefix: str = "flight-matrix") -> list:
    """列出匹配的Lambda函数"""
    try:
        result = subprocess.run(
            [
                "aws",
                "lambda",
                "list-functions",
                "--region",
                region,
                "--query",
                f"Functions[?starts_with(FunctionName, '{prefix}')].FunctionName",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError:
        return []


def main():
    parser = argparse.ArgumentParser(
        description="Restart Lambda function for Flight Matrix",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # Restart default function
  %(prog)s -f flight-matrix-api-prod  # Restart specific function
  %(prog)s -e dev                   # Restart dev environment
  %(prog)s --sync                   # Sync code and restart
  %(prog)s --list                   # List available functions
  %(prog)s --test                   # Restart and test invocation
        """,
    )
    parser.add_argument(
        "-f", "--function", help="Lambda function name (default: flight-matrix-api-prod)"
    )
    parser.add_argument(
        "-e",
        "--env",
        choices=["dev", "staging", "prod"],
        default="prod",
        help="Environment (used to construct function name)",
    )
    parser.add_argument("-r", "--region", help="AWS region (default: from AWS config)")
    parser.add_argument(
        "-m",
        "--method",
        choices=["env", "desc"],
        default="env",
        help="Restart method: env (update env var) or desc (update description)",
    )
    parser.add_argument("--sync", action="store_true", help="Sync code and deploy before restart")
    parser.add_argument("--test", action="store_true", help="Test invocation after restart")
    parser.add_argument("--list", action="store_true", help="List available Lambda functions")

    args = parser.parse_args()

    # 确定区域
    region = args.region or get_aws_region()

    # 列出函数
    if args.list:
        print(f"Lambda functions in {region}:")
        functions = list_functions(region)
        if functions:
            for func in functions:
                print(f"  - {func}")
        else:
            print("  No flight-matrix functions found")
        return 0

    # 确定函数名
    if args.function:
        function_name = args.function
    else:
        function_name = f"flight-matrix-api-{args.env}"

    print("=" * 60)
    print("  Flight Matrix Lambda Restart Tool")
    print("=" * 60)

    # 执行操作
    if args.sync:
        success = sync_and_restart(function_name, region)
    else:
        success = restart_lambda(function_name, region, args.method)

    # 测试调用
    if success and args.test:
        invoke_test(function_name, region)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
