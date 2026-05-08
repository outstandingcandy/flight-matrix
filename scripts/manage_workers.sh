#!/bin/bash
#
# Manage Scraper Workers in AWS
#
# Usage:
#   ./scripts/manage_workers.sh <command> [options]
#
# Commands:
#   status              Show worker status and instance details
#   scale <N>           Scale to N workers
#   logs [instance-id]  View worker logs (SSM Session Manager)
#   ssh [instance-id]   SSH to a worker instance
#   restart             Restart scraper service on all workers
#   stop                Scale down to 0 workers
#   terminate           Terminate all workers and cleanup ASG
#
# Examples:
#   ./scripts/manage_workers.sh status
#   ./scripts/manage_workers.sh scale 5
#   ./scripts/manage_workers.sh logs
#   ./scripts/manage_workers.sh ssh i-0abc123def456
#

set -e

REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="flight-matrix"
COMPONENT="scraper-worker"
ASG_NAME="${PROJECT_NAME}-${COMPONENT}-asg"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_header() {
    echo -e "${BLUE}=== $1 ===${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

# Get list of worker instance IDs
get_instance_ids() {
    aws autoscaling describe-auto-scaling-groups \
        --auto-scaling-group-names "$ASG_NAME" \
        --query "AutoScalingGroups[0].Instances[*].InstanceId" \
        --output text --region "$REGION" 2>/dev/null || echo ""
}

# Get instance details
get_instance_details() {
    local instance_ids="$1"
    if [ -z "$instance_ids" ]; then
        echo "No instances found"
        return
    fi

    aws ec2 describe-instances \
        --instance-ids $instance_ids \
        --query "Reservations[*].Instances[*].[InstanceId,InstanceType,State.Name,PublicIpAddress,LaunchTime]" \
        --output table --region "$REGION"
}

# Show status
cmd_status() {
    print_header "Scraper Worker Status"

    # Check if ASG exists
    ASG_EXISTS=$(aws autoscaling describe-auto-scaling-groups \
        --auto-scaling-group-names "$ASG_NAME" \
        --query "AutoScalingGroups[0].AutoScalingGroupName" \
        --output text --region "$REGION" 2>/dev/null || echo "None")

    if [ "$ASG_EXISTS" == "None" ] || [ -z "$ASG_EXISTS" ]; then
        print_warning "Auto Scaling Group '$ASG_NAME' not found"
        echo "Run ./scripts/deploy_workers.sh to create workers"
        return 1
    fi

    # Get ASG details
    echo ""
    echo "Auto Scaling Group: $ASG_NAME"
    aws autoscaling describe-auto-scaling-groups \
        --auto-scaling-group-names "$ASG_NAME" \
        --query "AutoScalingGroups[0].{MinSize:MinSize,MaxSize:MaxSize,DesiredCapacity:DesiredCapacity,InServiceInstances:length(Instances[?LifecycleState=='InService'])}" \
        --output table --region "$REGION"

    # Get instance details
    echo ""
    print_header "Worker Instances"
    INSTANCE_IDS=$(get_instance_ids)
    if [ -n "$INSTANCE_IDS" ]; then
        get_instance_details "$INSTANCE_IDS"

        # Show service status via SSM (if available)
        echo ""
        print_header "Service Status (via SSM)"
        for INSTANCE_ID in $INSTANCE_IDS; do
            echo -n "Instance $INSTANCE_ID: "
            STATUS=$(aws ssm send-command \
                --instance-ids "$INSTANCE_ID" \
                --document-name "AWS-RunShellScript" \
                --parameters 'commands=["systemctl is-active scraper-worker 2>/dev/null || echo inactive"]' \
                --query "Command.CommandId" \
                --output text --region "$REGION" 2>/dev/null || echo "")

            if [ -n "$STATUS" ]; then
                sleep 2
                RESULT=$(aws ssm get-command-invocation \
                    --command-id "$STATUS" \
                    --instance-id "$INSTANCE_ID" \
                    --query "StandardOutputContent" \
                    --output text --region "$REGION" 2>/dev/null | tr -d '\n' || echo "unknown")

                if [ "$RESULT" == "active" ]; then
                    print_success "active"
                else
                    print_warning "$RESULT"
                fi
            else
                echo "SSM not available"
            fi
        done
    else
        echo "No instances running"
    fi

    # Show database worker status
    echo ""
    print_header "Database Worker Registry"
    echo "Run: psql -c \"SELECT worker_id, status, last_heartbeat, tasks_completed FROM scraper_workers ORDER BY last_heartbeat DESC;\""
}

# Scale workers
cmd_scale() {
    local count="${1:-}"

    if [ -z "$count" ]; then
        print_error "Usage: $0 scale <N>"
        exit 1
    fi

    print_header "Scaling Workers to $count"

    aws autoscaling update-auto-scaling-group \
        --auto-scaling-group-name "$ASG_NAME" \
        --desired-capacity "$count" \
        --region "$REGION"

    print_success "Scaling initiated. Workers will be added/removed shortly."
    echo ""
    echo "Monitor progress with: $0 status"
}

# View logs
cmd_logs() {
    local instance_id="${1:-}"

    # If no instance ID provided, get first one
    if [ -z "$instance_id" ]; then
        instance_id=$(get_instance_ids | awk '{print $1}')
    fi

    if [ -z "$instance_id" ]; then
        print_error "No worker instances found"
        exit 1
    fi

    print_header "Viewing logs for $instance_id"
    echo "Starting SSM session to fetch logs..."
    echo ""

    # Use SSM to tail logs
    aws ssm start-session \
        --target "$instance_id" \
        --document-name "AWS-StartInteractiveCommand" \
        --parameters 'command=["tail -f /var/log/scraper-worker/worker.log"]' \
        --region "$REGION"
}

# SSH to worker
cmd_ssh() {
    local instance_id="${1:-}"

    # If no instance ID provided, get first one
    if [ -z "$instance_id" ]; then
        instance_id=$(get_instance_ids | awk '{print $1}')
    fi

    if [ -z "$instance_id" ]; then
        print_error "No worker instances found"
        exit 1
    fi

    print_header "Connecting to $instance_id"

    # Try SSM Session Manager first
    echo "Attempting SSM Session Manager connection..."
    aws ssm start-session \
        --target "$instance_id" \
        --region "$REGION" 2>/dev/null && exit 0

    # Fallback to SSH
    echo "SSM failed, trying SSH..."
    PUBLIC_IP=$(aws ec2 describe-instances \
        --instance-ids "$instance_id" \
        --query "Reservations[0].Instances[0].PublicIpAddress" \
        --output text --region "$REGION")

    if [ -n "$PUBLIC_IP" ] && [ "$PUBLIC_IP" != "None" ]; then
        ssh -o StrictHostKeyChecking=no ubuntu@"$PUBLIC_IP"
    else
        print_error "Could not connect to instance (no public IP and SSM unavailable)"
        exit 1
    fi
}

# Restart service on all workers
cmd_restart() {
    print_header "Restarting Scraper Service on All Workers"

    INSTANCE_IDS=$(get_instance_ids)
    if [ -z "$INSTANCE_IDS" ]; then
        print_error "No worker instances found"
        exit 1
    fi

    for INSTANCE_ID in $INSTANCE_IDS; do
        echo -n "Restarting on $INSTANCE_ID... "
        aws ssm send-command \
            --instance-ids "$INSTANCE_ID" \
            --document-name "AWS-RunShellScript" \
            --parameters 'commands=["sudo systemctl restart scraper-worker"]' \
            --region "$REGION" >/dev/null 2>&1 && print_success "sent" || print_warning "failed"
    done

    echo ""
    print_success "Restart commands sent to all workers"
}

# Stop all workers
cmd_stop() {
    print_header "Stopping All Workers"

    aws autoscaling update-auto-scaling-group \
        --auto-scaling-group-name "$ASG_NAME" \
        --desired-capacity 0 \
        --region "$REGION"

    print_success "Scaling down to 0 workers"
    echo "Instances will be terminated shortly."
}

# Terminate and cleanup
cmd_terminate() {
    print_header "Terminating Scraper Worker Infrastructure"

    read -p "Are you sure you want to terminate all workers? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted"
        exit 0
    fi

    # Scale to 0
    echo "Scaling to 0..."
    aws autoscaling update-auto-scaling-group \
        --auto-scaling-group-name "$ASG_NAME" \
        --min-size 0 \
        --desired-capacity 0 \
        --region "$REGION" 2>/dev/null || true

    # Wait for instances to terminate
    echo "Waiting for instances to terminate..."
    sleep 30

    # Delete ASG
    echo "Deleting Auto Scaling Group..."
    aws autoscaling delete-auto-scaling-group \
        --auto-scaling-group-name "$ASG_NAME" \
        --force-delete \
        --region "$REGION" 2>/dev/null || true

    # Delete Launch Template
    LT_NAME="${PROJECT_NAME}-${COMPONENT}-lt"
    echo "Deleting Launch Template..."
    aws ec2 delete-launch-template \
        --launch-template-name "$LT_NAME" \
        --region "$REGION" 2>/dev/null || true

    print_success "Worker infrastructure terminated"
    echo ""
    echo "Note: Security group and IAM role retained for future deployments."
    echo "Delete manually if no longer needed:"
    echo "  aws ec2 delete-security-group --group-name ${PROJECT_NAME}-${COMPONENT}-sg"
    echo "  aws iam delete-role --role-name ${PROJECT_NAME}-${COMPONENT}-role"
}

# Show queue status
cmd_queue() {
    print_header "Task Queue Status"

    echo "Connect to database and run:"
    echo ""
    cat << 'EOF'
SELECT
    status,
    COUNT(*) as count
FROM scraper_tasks
GROUP BY status
ORDER BY status;

-- Recent activity
SELECT
    task_type,
    task_key,
    status,
    claimed_by,
    attempts,
    completed_at
FROM scraper_tasks
ORDER BY COALESCE(completed_at, created_at) DESC
LIMIT 10;
EOF
}

# Main command router
case "${1:-}" in
    status)
        cmd_status
        ;;
    scale)
        cmd_scale "$2"
        ;;
    logs)
        cmd_logs "$2"
        ;;
    ssh)
        cmd_ssh "$2"
        ;;
    restart)
        cmd_restart
        ;;
    stop)
        cmd_stop
        ;;
    terminate)
        cmd_terminate
        ;;
    queue)
        cmd_queue
        ;;
    -h|--help|help)
        head -25 "$0" | tail -23
        ;;
    *)
        echo "Usage: $0 <command> [options]"
        echo ""
        echo "Commands:"
        echo "  status              Show worker status"
        echo "  scale <N>           Scale to N workers"
        echo "  logs [instance-id]  View logs"
        echo "  ssh [instance-id]   SSH to worker"
        echo "  restart             Restart service on all workers"
        echo "  stop                Scale to 0 workers"
        echo "  terminate           Delete all infrastructure"
        echo "  queue               Show queue status SQL"
        echo ""
        echo "Run '$0 --help' for detailed usage"
        ;;
esac
