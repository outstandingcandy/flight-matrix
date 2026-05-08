#!/usr/bin/env python3
"""
依赖检查脚本 - 验证所有必需的包是否已安装
Dependency checker - Verify all required packages are installed
"""

import sys
import importlib
from typing import Tuple, List

# 定义依赖和它们的导入名称
DEPENDENCIES = {
    # 核心依赖
    "requests": "requests",
    "PyYAML": "yaml",
    "python-dotenv": "dotenv",
    "SQLAlchemy": "sqlalchemy",

    # 邮件功能
    "boto3": "boto3",

    # 地理定位
    "reverse-geocoder": "reverse_geocoder",

    # 地图生成（可选）
    "plotly": "plotly",
    "kaleido": "kaleido",
    "numpy": "numpy",

    # AI分析（可选）
    "tavily-python": "tavily",

    # 测试框架（可选）
    "pytest": "pytest",
    "pytest-asyncio": "pytest_asyncio",
}

# 颜色代码
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'


def check_import(module_name: str) -> Tuple[bool, str]:
    """检查模块是否可以导入"""
    try:
        importlib.import_module(module_name)
        return True, None
    except ImportError as e:
        return False, str(e)


def check_project_modules() -> List[Tuple[str, bool, str]]:
    """检查项目模块是否可用"""
    project_modules = [
        ("数据库模块", "src.utils.database"),
        ("邮件通知", "src.utils.email_notifier"),
        ("AWS邮件", "src.utils.aws_email_notifier"),
        ("配置管理", "src.utils.yaml_config"),
        ("追踪系统", "src.utils.enhanced_tracker"),
        ("地图生成", "src.media.map_generator"),
        ("报告管理", "src.reporting.manager"),
        ("SQL过滤", "src.reporting.sql_filter"),
    ]

    results = []
    for name, module_path in project_modules:
        success, error = check_import(module_path)
        results.append((name, success, error))

    return results


def check_stdlib_modules():
    """检查标准库模块"""
    stdlib_modules = [
        ("unittest.mock", "unittest.mock", "测试模拟"),
        ("asyncio", "asyncio", "异步支持"),
        ("json", "json", "JSON处理"),
        ("datetime", "datetime", "日期时间"),
    ]

    all_available = True
    for name, import_name, description in stdlib_modules:
        try:
            importlib.import_module(import_name)
        except ImportError:
            all_available = False

    return all_available


def main():
    """主函数"""
    print("=" * 60)
    print("     Flight Matrix 依赖检查")
    print("=" * 60)
    print()

    # 检查 Python 版本
    python_version = sys.version_info
    print(f"{BLUE}Python 版本:{RESET} {python_version.major}.{python_version.minor}.{python_version.micro}")

    if python_version >= (3, 8):
        print(f"{GREEN}✓ Python 版本满足要求 (>= 3.8){RESET}")
    else:
        print(f"{RED}✗ Python 版本过低，需要 3.8 或更高版本{RESET}")
        sys.exit(1)

    # 检查标准库
    stdlib_ok = check_stdlib_modules()
    if stdlib_ok:
        print(f"{GREEN}✓ Python 标准库模块正常 (包括 unittest.mock){RESET}")
    else:
        print(f"{RED}✗ Python 标准库模块异常{RESET}")

    print()
    print("-" * 60)
    print("检查第三方依赖:")
    print("-" * 60)

    installed_count = 0
    missing_packages = []
    optional_missing = []

    for package_name, import_name in DEPENDENCIES.items():
        success, error = check_import(import_name)

        # 判断是否为可选包
        is_optional = package_name in ["plotly", "kaleido", "numpy", "tavily-python", "pytest", "pytest-asyncio"]

        if success:
            status = f"{GREEN}✓ 已安装{RESET}"
            installed_count += 1
        else:
            if is_optional:
                status = f"{YELLOW}○ 未安装 (可选){RESET}"
                optional_missing.append(package_name)
            else:
                status = f"{RED}✗ 未安装{RESET}"
                missing_packages.append(package_name)

        print(f"  {package_name:20} {status}")

    print()
    print("-" * 60)
    print("检查项目模块:")
    print("-" * 60)

    project_results = check_project_modules()
    project_success = 0

    for name, success, error in project_results:
        if success:
            status = f"{GREEN}✓ 可用{RESET}"
            project_success += 1
        else:
            status = f"{RED}✗ 不可用{RESET}"

        print(f"  {name:15} {status}")

    # 总结
    print()
    print("=" * 60)
    print("检查结果总结:")
    print("=" * 60)

    print(f"\n第三方依赖: {installed_count}/{len(DEPENDENCIES)} 已安装")
    print(f"项目模块: {project_success}/{len(project_results)} 可用")

    if missing_packages:
        print(f"\n{RED}缺少必需的依赖:{RESET}")
        for pkg in missing_packages:
            print(f"  - {pkg}")
        print(f"\n{YELLOW}安装命令:{RESET}")
        print(f"  pip install {' '.join(missing_packages)}")

    if optional_missing:
        print(f"\n{YELLOW}可选依赖未安装:{RESET}")
        for pkg in optional_missing:
            print(f"  - {pkg}")
        print(f"\n如需完整功能，运行:")
        print(f"  pip install {' '.join(optional_missing)}")

    if not missing_packages and project_success == len(project_results):
        print(f"\n{GREEN}✓ 所有核心依赖已安装，系统可以正常运行！{RESET}")

        # 提供快速测试命令
        print(f"\n{BLUE}快速测试命令:{RESET}")
        print("  python3 tests/test_tracking_cycle_mock.py")
        print("\n运行系统:")
        print("  python3 -m src.main --config config.yaml")

        return 0
    else:
        print(f"\n{RED}✗ 存在缺失的依赖，请先安装{RESET}")
        return 1


if __name__ == "__main__":
    sys.exit(main())