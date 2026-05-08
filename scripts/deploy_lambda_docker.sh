#!/bin/bash
#
# Flight Matrix Docker Lambda 部署脚本
# 使用 Docker 容器化 Lambda 进行部署
#
# 用法:
#   ./scripts/deploy_lambda_docker.sh              # 完整部署
#   ./scripts/deploy_lambda_docker.sh --synth      # 仅生成 CloudFormation 模板
#   ./scripts/deploy_lambda_docker.sh --diff       # 查看变更差异
#   ./scripts/deploy_lambda_docker.sh --destroy    # 销毁堆栈
#   ./scripts/deploy_lambda_docker.sh --quick      # 快速部署（跳过CDK，只更新静态文件）
#   ./scripts/deploy_lambda_docker.sh --build-test # 仅测试 Docker 构建
#
# 配置方式:
#   1. 环境变量文件: .env
#   2. 命令行参数: -c db_password=xxx
#   3. 交互式输入: 如果未提供密码则提示输入
#

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# 配置
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CDK_APP="python3 cdk_lambda_app.py"
CDK_CONFIG="${PROJECT_ROOT}/cdk-lambda.json"
ENV_FILE="${PROJECT_ROOT}/.env"
DOCKERFILE="${PROJECT_ROOT}/Dockerfile.lambda"

# 默认配置（可被 .env 或命令行覆盖）
S3_BUCKET="${S3_BUCKET:-${S3_BUCKET_NAME:?S3_BUCKET_NAME must be set}}"
CLOUDFRONT_DISTRIBUTION_ID="${CLOUDFRONT_DISTRIBUTION_ID:?CLOUDFRONT_DISTRIBUTION_ID must be set}"
AWS_REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-prod}"

# CDK 上下文参数
CDK_CONTEXT_ARGS=""

# 日志函数
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    echo -e "${CYAN}[STEP]${NC} $1"
}

# 显示帮助
show_help() {
    echo "Flight Matrix Docker Lambda 部署脚本"
    echo ""
    echo "用法: $0 [选项] [-c key=value]..."
    echo ""
    echo "部署模式:"
    echo "  (无参数)       完整部署 (Docker Lambda + S3 + CloudFront)"
    echo "  --synth        仅生成 CloudFormation 模板"
    echo "  --diff         查看与当前部署的差异"
    echo "  --destroy      销毁堆栈 (危险操作，需确认)"
    echo "  --quick, -q    快速部署 (仅 S3 + CloudFront 缓存清除)"
    echo "  --build-test   仅测试 Docker 镜像构建"
    echo ""
    echo "配置选项:"
    echo "  -c key=value   传递 CDK 上下文参数"
    echo "                 例: -c db_password=xxx -c environment=staging"
    echo ""
    echo "其他选项:"
    echo "  --no-cache     Docker 构建时不使用缓存"
    echo "  --skip-static  跳过 S3 静态文件同步"
    echo "  --skip-cache   跳过 CloudFront 缓存清除"
    echo "  --help, -h     显示此帮助信息"
    echo ""
    echo "环境变量 (可在 .env 中设置):"
    echo "  DB_PASSWORD                 数据库密码 (必需, 至少16字符)"
    echo "  DB_USERNAME                 数据库用户名 (默认: aircraft_admin)"
    echo "  DB_NAME                     数据库名称 (默认: aircraft_data)"
    echo "  ENVIRONMENT                 部署环境 (默认: prod)"
    echo "  AWS_REGION                  AWS 区域 (默认: us-east-1)"
    echo "  S3_BUCKET                   S3 存储桶名称"
    echo "  CLOUDFRONT_DISTRIBUTION_ID  CloudFront 分发 ID"
    echo ""
}

# 检查必要工具
check_prerequisites() {
    log_step "检查必要工具..."

    local missing_tools=()

    command -v aws >/dev/null 2>&1 || missing_tools+=("aws-cli")
    command -v cdk >/dev/null 2>&1 || missing_tools+=("aws-cdk")
    command -v docker >/dev/null 2>&1 || missing_tools+=("docker")
    command -v python3 >/dev/null 2>&1 || missing_tools+=("python3")

    if [ ${#missing_tools[@]} -ne 0 ]; then
        log_error "缺少必要工具: ${missing_tools[*]}"
        echo ""
        echo "安装指南:"
        echo "  aws-cli:  pip install awscli"
        echo "  aws-cdk:  npm install -g aws-cdk"
        echo "  docker:   https://docs.docker.com/get-docker/"
        exit 1
    fi

    # 检查 Docker 是否运行
    if ! docker info >/dev/null 2>&1; then
        log_error "Docker 未运行或当前用户无权限"
        echo "请启动 Docker 或将用户添加到 docker 组"
        exit 1
    fi

    # 检查 AWS 配置
    if ! aws sts get-caller-identity >/dev/null 2>&1; then
        log_error "AWS CLI 未配置或凭证无效"
        echo "请运行 'aws configure' 配置 AWS 凭证"
        exit 1
    fi

    # 检查 Dockerfile
    if [ ! -f "$DOCKERFILE" ]; then
        log_error "Dockerfile 不存在: $DOCKERFILE"
        exit 1
    fi

    # 检查 CDK 配置文件
    if [ ! -f "$CDK_CONFIG" ]; then
        log_error "CDK 配置文件不存在: $CDK_CONFIG"
        exit 1
    fi

    log_success "工具检查完成"
}

# 加载环境变量
load_env() {
    log_step "加载环境变量..."

    if [ -f "$ENV_FILE" ]; then
        log_info "从 $ENV_FILE 加载环境变量"
        set -a
        # shellcheck source=/dev/null
        source "$ENV_FILE"
        set +a
    else
        log_warning "环境变量文件不存在: $ENV_FILE"
    fi

    # Refresh config from env (required vars validated at top of script)
    S3_BUCKET="${S3_BUCKET:-${S3_BUCKET_NAME:?S3_BUCKET_NAME must be set}}"
    CLOUDFRONT_DISTRIBUTION_ID="${CLOUDFRONT_DISTRIBUTION_ID:?CLOUDFRONT_DISTRIBUTION_ID must be set}"
    AWS_REGION="${AWS_REGION:-us-east-1}"
    ENVIRONMENT="${ENVIRONMENT:-prod}"

    log_success "环境变量加载完成"
}

# 验证必要配置
validate_config() {
    log_step "验证配置..."

    # 检查数据库密码
    if [ -z "$DB_PASSWORD" ]; then
        log_warning "未设置 DB_PASSWORD"
        echo -n "请输入数据库密码 (至少16字符): "
        read -r -s DB_PASSWORD
        echo ""
        export DB_PASSWORD
    fi

    if [ ${#DB_PASSWORD} -lt 16 ]; then
        log_error "数据库密码长度必须至少为16字符"
        exit 1
    fi

    log_success "配置验证完成"
}

# 同步 Lambda 代码目录
sync_lambda_code() {
    log_step "同步代码到 lambda_code 目录..."

    local lambda_code_dir="${PROJECT_ROOT}/lambda_code"

    # 创建目录
    mkdir -p "${lambda_code_dir}/src"
    mkdir -p "${lambda_code_dir}/web_templates"
    mkdir -p "${lambda_code_dir}/web_static/js"
    mkdir -p "${lambda_code_dir}/web_static/css"

    # 同步 Python 源代码
    cp -r "${PROJECT_ROOT}/src/"* "${lambda_code_dir}/src/" 2>/dev/null || true

    # 同步 Web 应用文件
    cp "${PROJECT_ROOT}/web_app.py" "${lambda_code_dir}/"
    cp "${PROJECT_ROOT}/config.yaml" "${lambda_code_dir}/" 2>/dev/null || true

    # 同步模板
    cp -r "${PROJECT_ROOT}/web_templates/"* "${lambda_code_dir}/web_templates/" 2>/dev/null || true

    # 同步静态文件
    cp -r "${PROJECT_ROOT}/web_static/"* "${lambda_code_dir}/web_static/" 2>/dev/null || true

    log_success "代码同步完成"
}

# 测试 Docker 构建
build_docker_test() {
    log_step "测试 Docker 镜像构建..."

    local build_args=""
    if [ "$NO_CACHE" = "true" ]; then
        build_args="--no-cache"
    fi

    cd "$PROJECT_ROOT"

    # 构建测试镜像
    docker build $build_args -f "$DOCKERFILE" -t flight-matrix-lambda-test:latest .

    if [ $? -eq 0 ]; then
        log_success "Docker 镜像构建成功"
        echo ""
        echo "镜像信息:"
        docker images flight-matrix-lambda-test:latest --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}"
    else
        log_error "Docker 镜像构建失败"
        exit 1
    fi
}

# CDK Synth - 生成 CloudFormation 模板
cdk_synth() {
    log_step "生成 CloudFormation 模板..."

    cd "$PROJECT_ROOT"

    cdk synth \
        --app "$CDK_APP" \
        $CDK_CONTEXT_ARGS \
        --output cdk.out

    log_success "模板生成完成"
    echo ""
    echo "模板输出目录: ${PROJECT_ROOT}/cdk.out"
    ls -la "${PROJECT_ROOT}/cdk.out/"*.template.json 2>/dev/null || echo "无模板文件"
}

# CDK Diff - 查看变更差异
cdk_diff() {
    log_step "查看部署差异..."

    cd "$PROJECT_ROOT"

    cdk diff \
        --app "$CDK_APP" \
        $CDK_CONTEXT_ARGS \
        || true  # diff 返回非零不表示错误

    log_success "差异检查完成"
}

# CDK Deploy - 执行部署
cdk_deploy() {
    log_step "执行 CDK 部署..."

    cd "$PROJECT_ROOT"

    # 部署堆栈
    cdk deploy \
        --app "$CDK_APP" \
        $CDK_CONTEXT_ARGS \
        --require-approval never \
        --outputs-file cdk-outputs.json

    log_success "CDK 部署完成"

    # 显示输出
    if [ -f "${PROJECT_ROOT}/cdk-outputs.json" ]; then
        echo ""
        echo "堆栈输出:"
        cat "${PROJECT_ROOT}/cdk-outputs.json"
    fi
}

# CDK Destroy - 销毁堆栈
cdk_destroy() {
    log_warning "警告: 此操作将销毁整个堆栈，包括所有资源！"
    echo ""
    echo -n "确认销毁堆栈? 输入 'yes' 继续: "
    read -r confirm

    if [ "$confirm" != "yes" ]; then
        log_info "操作已取消"
        exit 0
    fi

    log_step "销毁 CDK 堆栈..."

    cd "$PROJECT_ROOT"

    cdk destroy \
        --app "$CDK_APP" \
        $CDK_CONTEXT_ARGS \
        --force

    log_success "堆栈销毁完成"
}

# 同步静态文件到 S3
sync_static_files() {
    log_step "同步静态文件到 S3..."

    # 同步 JS 文件
    aws s3 sync "${PROJECT_ROOT}/web_static/js/" "s3://${S3_BUCKET}/static/js/" \
        --delete \
        --cache-control "max-age=3600"

    # 同步 CSS 文件
    aws s3 sync "${PROJECT_ROOT}/web_static/css/" "s3://${S3_BUCKET}/static/css/" \
        --delete \
        --cache-control "max-age=3600"

    # 同步图片等其他静态资源（如果存在）
    if [ -d "${PROJECT_ROOT}/web_static/images" ]; then
        aws s3 sync "${PROJECT_ROOT}/web_static/images/" "s3://${S3_BUCKET}/static/images/" \
            --delete \
            --cache-control "max-age=86400"
    fi

    log_success "S3 同步完成"
}

# 清除 CloudFront 缓存
invalidate_cache() {
    log_step "清除 CloudFront 缓存..."

    local invalidation_id
    invalidation_id=$(aws cloudfront create-invalidation \
        --distribution-id "${CLOUDFRONT_DISTRIBUTION_ID}" \
        --paths "/static/*" "/*" \
        --query 'Invalidation.Id' \
        --output text)

    log_info "缓存清除请求已提交 (ID: ${invalidation_id})"

    # 等待缓存清除完成
    log_info "等待缓存清除完成..."
    local wait_time=0
    local max_wait=120

    while [ $wait_time -lt $max_wait ]; do
        local status
        status=$(aws cloudfront get-invalidation \
            --distribution-id "${CLOUDFRONT_DISTRIBUTION_ID}" \
            --id "${invalidation_id}" \
            --query 'Invalidation.Status' \
            --output text)

        if [ "$status" = "Completed" ]; then
            log_success "CloudFront 缓存清除完成"
            return 0
        fi

        sleep 5
        wait_time=$((wait_time + 5))
        echo -n "."
    done

    echo ""
    log_warning "缓存清除仍在进行中，可能需要几分钟完成"
}

# 显示部署结果
show_result() {
    echo ""
    echo "=========================================="
    echo -e "${GREEN}Docker Lambda 部署完成！${NC}"
    echo "=========================================="
    echo ""
    echo "部署信息:"
    echo "  - 环境: ${ENVIRONMENT}"
    echo "  - 区域: ${AWS_REGION}"
    echo "  - S3 存储桶: ${S3_BUCKET}"
    echo ""

    # 尝试从 CDK 输出获取 URL
    if [ -f "${PROJECT_ROOT}/cdk-outputs.json" ]; then
        local api_url
        api_url=$(cat "${PROJECT_ROOT}/cdk-outputs.json" | grep -o '"ApiUrl": "[^"]*"' | cut -d'"' -f4 || echo "")
        local cloudfront_url
        cloudfront_url=$(cat "${PROJECT_ROOT}/cdk-outputs.json" | grep -o '"CloudFrontUrl": "[^"]*"' | cut -d'"' -f4 || echo "")

        if [ -n "$api_url" ]; then
            echo "  - API Gateway: $api_url"
        fi
        if [ -n "$cloudfront_url" ]; then
            echo "  - CloudFront: $cloudfront_url"
        fi
    fi

    echo ""
    echo "提示: 如果浏览器仍显示旧版本，请按 Ctrl+Shift+R 强制刷新"
    echo ""
}

# 快速部署（仅静态文件）
quick_deploy() {
    log_info "执行快速部署（仅静态文件）..."

    if [ "$SKIP_STATIC" != "true" ]; then
        sync_static_files
    fi

    if [ "$SKIP_CACHE" != "true" ]; then
        invalidate_cache
    fi

    show_result
}

# 完整部署
full_deploy() {
    log_info "执行完整部署..."

    # 同步代码
    sync_lambda_code

    # 同步静态文件
    if [ "$SKIP_STATIC" != "true" ]; then
        sync_static_files
    fi

    # CDK 部署
    cdk_deploy

    # 清除缓存
    if [ "$SKIP_CACHE" != "true" ]; then
        invalidate_cache
    fi

    show_result
}

# 解析命令行参数
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --synth)
                MODE="synth"
                shift
                ;;
            --diff)
                MODE="diff"
                shift
                ;;
            --destroy)
                MODE="destroy"
                shift
                ;;
            --quick|-q)
                MODE="quick"
                shift
                ;;
            --build-test)
                MODE="build-test"
                shift
                ;;
            --no-cache)
                NO_CACHE="true"
                shift
                ;;
            --skip-static)
                SKIP_STATIC="true"
                shift
                ;;
            --skip-cache)
                SKIP_CACHE="true"
                shift
                ;;
            -c)
                if [[ -n "$2" ]]; then
                    CDK_CONTEXT_ARGS="$CDK_CONTEXT_ARGS -c $2"
                    shift 2
                else
                    log_error "-c 需要参数"
                    exit 1
                fi
                ;;
            --help|-h)
                show_help
                exit 0
                ;;
            *)
                log_error "未知参数: $1"
                show_help
                exit 1
                ;;
        esac
    done
}

# 主函数
main() {
    echo ""
    echo "=========================================="
    echo "  Flight Matrix Docker Lambda 部署工具"
    echo "=========================================="
    echo ""

    # 解析参数
    parse_args "$@"

    # 检查依赖
    check_prerequisites

    # 加载环境变量
    load_env

    # 根据模式执行
    case "${MODE:-}" in
        synth)
            validate_config
            cdk_synth
            ;;
        diff)
            validate_config
            cdk_diff
            ;;
        destroy)
            cdk_destroy
            ;;
        quick)
            quick_deploy
            ;;
        build-test)
            sync_lambda_code
            build_docker_test
            ;;
        *)
            validate_config
            full_deploy
            ;;
    esac
}

main "$@"
