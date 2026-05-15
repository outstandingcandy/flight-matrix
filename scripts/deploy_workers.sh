#!/bin/bash
#
# Deploy Scraper Workers to AWS EC2
#
# This script creates an Auto Scaling Group with scraper workers.
# Workers automatically connect to the PostgreSQL database and start processing tasks.
#
# Prerequisites:
#   - AWS CLI configured with appropriate credentials
#   - VPC with public subnets
#   - Aurora PostgreSQL database accessible
#
# Usage:
#   ./scripts/deploy_workers.sh [options]
#
# Options:
#   --workers N          Number of workers to create (default: 2)
#   --instance-type TYPE EC2 instance type (default: t3.medium)
#   --key-name NAME      SSH key pair name for access
#   --dry-run            Show what would be created without creating
#
# Examples:
#   ./scripts/deploy_workers.sh --workers 3
#   ./scripts/deploy_workers.sh --workers 5 --instance-type t3.large
#

set -e

# Default configuration
WORKERS=2
INSTANCE_TYPE="t3.medium"
KEY_NAME=""
DRY_RUN=false
REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="flight-matrix"
COMPONENT="scraper-worker"
# ROLE picks which systemd unit(s) the instance enables at boot:
#   worker     → flight-matrix-xvfb + flight-matrix-scraper (ASG N)
#   scheduler  → flight-matrix-scheduler only (single-instance ASG)
ROLE="worker"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --workers)
            WORKERS="$2"
            shift 2
            ;;
        --instance-type)
            INSTANCE_TYPE="$2"
            shift 2
            ;;
        --key-name)
            KEY_NAME="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --region)
            REGION="$2"
            shift 2
            ;;
        --role)
            ROLE="$2"
            shift 2
            ;;
        -h|--help)
            head -30 "$0" | tail -25
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

case "$ROLE" in
    worker|scheduler) ;;
    *)
        echo "Unknown --role: $ROLE (expected: worker|scheduler)" >&2
        exit 1
        ;;
esac
COMPONENT="scraper-${ROLE}"

echo "=== Flight Matrix ${ROLE^} Deployment ==="
echo "Region:        $REGION"
echo "Role:          $ROLE"
echo "Workers:       $WORKERS"
echo "Instance Type: $INSTANCE_TYPE"
echo ""

# Single-node ASG for scheduler role — enforce capacity 1.
if [ "$ROLE" = "scheduler" ] && [ "$WORKERS" != "1" ]; then
    echo "Note: --role scheduler runs a single-instance ASG; overriding --workers to 1." >&2
    WORKERS=1
fi

# Get AWS Account ID
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "AWS Account: $AWS_ACCOUNT_ID"

# Get default VPC
VPC_ID=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" --query "Vpcs[0].VpcId" --output text --region $REGION)
echo "VPC: $VPC_ID"

# Get public subnets
SUBNET_IDS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" "Name=map-public-ip-on-launch,Values=true" --query "Subnets[*].SubnetId" --output text --region $REGION | tr '\t' ',')
echo "Subnets: $SUBNET_IDS"

# Ubuntu 22.04 LTS AMI
AMI_ID=$(aws ec2 describe-images \
    --owners 099720109477 \
    --filters "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*" \
    --query "sort_by(Images, &CreationDate)[-1].ImageId" \
    --output text --region $REGION)
echo "AMI: $AMI_ID"

# Create security group if not exists
SG_NAME="${PROJECT_NAME}-${COMPONENT}-sg"
SG_ID=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=$SG_NAME" --query "SecurityGroups[0].GroupId" --output text --region $REGION 2>/dev/null || echo "None")

if [ "$SG_ID" == "None" ] || [ -z "$SG_ID" ]; then
    echo "Creating security group: $SG_NAME"
    if [ "$DRY_RUN" = false ]; then
        SG_ID=$(aws ec2 create-security-group \
            --group-name "$SG_NAME" \
            --description "Security group for scraper workers" \
            --vpc-id "$VPC_ID" \
            --query "GroupId" --output text --region $REGION)

        # Allow SSH
        aws ec2 authorize-security-group-ingress \
            --group-id "$SG_ID" \
            --protocol tcp --port 22 --cidr 0.0.0.0/0 --region $REGION

        # Allow outbound
        aws ec2 authorize-security-group-egress \
            --group-id "$SG_ID" \
            --protocol -1 --cidr 0.0.0.0/0 --region $REGION 2>/dev/null || true
    fi
fi
echo "Security Group: $SG_ID"

# Create IAM role if not exists
ROLE_NAME="${PROJECT_NAME}-${COMPONENT}-role"
if ! aws iam get-role --role-name "$ROLE_NAME" --region $REGION 2>/dev/null; then
    echo "Creating IAM role: $ROLE_NAME"
    if [ "$DRY_RUN" = false ]; then
        # Create trust policy
        cat > /tmp/trust-policy.json << 'EOF'
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }
    ]
}
EOF
        aws iam create-role \
            --role-name "$ROLE_NAME" \
            --assume-role-policy-document file:///tmp/trust-policy.json

        # Attach policies
        aws iam attach-role-policy \
            --role-name "$ROLE_NAME" \
            --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

        aws iam attach-role-policy \
            --role-name "$ROLE_NAME" \
            --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy

        # S3 policy for image uploads and code download
        cat > /tmp/s3-policy.json << EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
            "Resource": [
                "arn:aws:s3:::flight-matrix-static-${AWS_ACCOUNT_ID}",
                "arn:aws:s3:::flight-matrix-static-${AWS_ACCOUNT_ID}/*"
            ]
        }
    ]
}
EOF
        aws iam put-role-policy \
            --role-name "$ROLE_NAME" \
            --policy-name "S3Access" \
            --policy-document file:///tmp/s3-policy.json

        # SSM policy for reading secrets
        cat > /tmp/ssm-policy.json << EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["ssm:GetParameter", "ssm:GetParameters"],
            "Resource": "arn:aws:ssm:${REGION}:${AWS_ACCOUNT_ID}:parameter/flight-matrix/*"
        }
    ]
}
EOF
        aws iam put-role-policy \
            --role-name "$ROLE_NAME" \
            --policy-name "SSMAccess" \
            --policy-document file:///tmp/ssm-policy.json

        # Create instance profile
        aws iam create-instance-profile \
            --instance-profile-name "$ROLE_NAME" 2>/dev/null || true
        aws iam add-role-to-instance-profile \
            --instance-profile-name "$ROLE_NAME" \
            --role-name "$ROLE_NAME" 2>/dev/null || true

        # Wait for instance profile to be ready
        sleep 10
    fi
fi
echo "IAM Role: $ROLE_NAME"

# Create user data script
USER_DATA=$(cat << 'USERDATA'
#!/bin/bash
set -ex
exec > >(tee /var/log/user-data.log) 2>&1

echo "=== Starting Scraper Worker Setup ==="

# Update system
apt-get update
apt-get install -y python3-pip python3-venv git xvfb x11-utils unzip curl jq wget

# Install Google Chrome (not snap-based chromium)
wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
apt-get install -y /tmp/chrome.deb
rm /tmp/chrome.deb

# Install AWS CLI v2
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip -q awscliv2.zip
./aws/install --update
rm -rf aws awscliv2.zip

# Install uv package manager
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="/root/.local/bin:$PATH"

# Setup project directory
mkdir -p /home/ubuntu/Project/flight-matrix
cd /home/ubuntu/Project/flight-matrix

# Download code from S3 (more reliable than git for private repos)
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws s3 cp s3://flight-matrix-static-${AWS_ACCOUNT_ID}/deploy/flight-matrix-deploy.tar.gz /tmp/deploy.tar.gz
tar -xzf /tmp/deploy.tar.gz -C /home/ubuntu/Project/flight-matrix
rm /tmp/deploy.tar.gz

# Setup Python environment
/root/.local/bin/uv venv .venv
source .venv/bin/activate
/root/.local/bin/uv pip install pydantic sqlalchemy boto3 requests psycopg2-binary pyyaml python-dotenv structlog

# Install DrissionPage
/root/.local/bin/uv pip install DrissionPage

# Get DB password from SSM (if configured)
DB_PASSWORD=$(aws ssm get-parameter --name "/flight-matrix/db-password" --with-decryption --query "Parameter.Value" --output text 2>/dev/null || echo "")

# If SSM parameter doesn't exist, try environment or use placeholder
if [ -z "$DB_PASSWORD" ]; then
    echo "Warning: DB_PASSWORD not found in SSM, worker may not connect to database"
fi

export DB_PASSWORD

# Role-aware env file consumed by the shipped systemd units.
mkdir -p /etc/flight-matrix /var/log/flight-matrix
chown ubuntu:ubuntu /var/log/flight-matrix
cat > /etc/flight-matrix/env << EOF
STAGE=prod
DB_PASSWORD=${DB_PASSWORD}
EOF

# Install the canonical systemd units from the checkout. The same files
# ship to local dev machines, so prod and local stay in sync.
install -m 0644 scripts/systemd/flight-matrix-xvfb.service      /etc/systemd/system/
install -m 0644 scripts/systemd/flight-matrix-scraper.service   /etc/systemd/system/
install -m 0644 scripts/systemd/flight-matrix-scheduler.service /etc/systemd/system/

# Rewrite the WorkingDirectory baked into the unit files to match the
# legacy prod path (/home/ubuntu/Project/...). Local installs keep the
# default /home/ubuntu/flight-matrix.
sed -i "s|/home/ubuntu/flight-matrix|/home/ubuntu/Project/flight-matrix|g" \
    /etc/systemd/system/flight-matrix-*.service

# Update config.yaml with DB password (same as before — unit file reads
# it via EnvironmentFile but some YAML also interpolates it).
sed -i "s/\${DB_PASSWORD}/${DB_PASSWORD}/g" config.yaml

# Fix permissions + start services according to role.
chown -R ubuntu:ubuntu /home/ubuntu/Project

systemctl daemon-reload
if [ "__FLIGHT_MATRIX_ROLE__" = "scheduler" ]; then
    systemctl enable flight-matrix-scheduler
    systemctl start flight-matrix-scheduler
else
    # worker role: xvfb + scraper
    systemctl enable flight-matrix-xvfb flight-matrix-scraper
    systemctl start flight-matrix-xvfb
    sleep 3
    systemctl start flight-matrix-scraper
fi

echo "=== __FLIGHT_MATRIX_ROLE__ Setup Complete ==="
USERDATA
)

# Fill in the role placeholder (heredoc is quoted so shell vars don't expand).
USER_DATA=$(echo "$USER_DATA" | sed "s/__FLIGHT_MATRIX_ROLE__/${ROLE}/g")

# Base64 encode user data
USER_DATA_B64=$(echo "$USER_DATA" | base64 -w 0)

# Create launch template
LT_NAME="${PROJECT_NAME}-${COMPONENT}-lt"
LT_EXISTS=$(aws ec2 describe-launch-templates --launch-template-names "$LT_NAME" --query "LaunchTemplates[0].LaunchTemplateId" --output text --region $REGION 2>/dev/null || echo "None")

if [ "$LT_EXISTS" == "None" ] || [ -z "$LT_EXISTS" ]; then
    echo "Creating launch template: $LT_NAME"
    if [ "$DRY_RUN" = false ]; then
        KEY_SPEC=""
        if [ -n "$KEY_NAME" ]; then
            KEY_SPEC="\"KeyName\": \"$KEY_NAME\","
        fi

        cat > /tmp/lt-spec.json << EOF
{
    "ImageId": "$AMI_ID",
    "InstanceType": "$INSTANCE_TYPE",
    $KEY_SPEC
    "SecurityGroupIds": ["$SG_ID"],
    "IamInstanceProfile": {"Name": "$ROLE_NAME"},
    "UserData": "$USER_DATA_B64",
    "BlockDeviceMappings": [
        {
            "DeviceName": "/dev/sda1",
            "Ebs": {
                "VolumeSize": 30,
                "VolumeType": "gp3",
                "DeleteOnTermination": true
            }
        }
    ],
    "TagSpecifications": [
        {
            "ResourceType": "instance",
            "Tags": [
                {"Key": "Name", "Value": "$COMPONENT"},
                {"Key": "Project", "Value": "$PROJECT_NAME"}
            ]
        }
    ]
}
EOF
        LT_ID=$(aws ec2 create-launch-template \
            --launch-template-name "$LT_NAME" \
            --launch-template-data file:///tmp/lt-spec.json \
            --query "LaunchTemplate.LaunchTemplateId" --output text --region $REGION)
    fi
else
    LT_ID=$LT_EXISTS
    echo "Launch template exists: $LT_ID"
fi

# Create or update Auto Scaling Group
ASG_NAME="${PROJECT_NAME}-${COMPONENT}-asg"
ASG_EXISTS=$(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG_NAME" --query "AutoScalingGroups[0].AutoScalingGroupName" --output text --region $REGION 2>/dev/null || echo "None")

if [ "$ASG_EXISTS" == "None" ] || [ -z "$ASG_EXISTS" ]; then
    echo "Creating Auto Scaling Group: $ASG_NAME with $WORKERS workers"
    if [ "$DRY_RUN" = false ]; then
        aws autoscaling create-auto-scaling-group \
            --auto-scaling-group-name "$ASG_NAME" \
            --launch-template "LaunchTemplateId=$LT_ID,Version=\$Latest" \
            --min-size 0 \
            --max-size 10 \
            --desired-capacity "$WORKERS" \
            --vpc-zone-identifier "$SUBNET_IDS" \
            --health-check-type EC2 \
            --health-check-grace-period 600 \
            --tags "Key=Name,Value=$COMPONENT,PropagateAtLaunch=true" \
                   "Key=Project,Value=$PROJECT_NAME,PropagateAtLaunch=true" \
            --region $REGION
    fi
else
    echo "Updating Auto Scaling Group: $ASG_NAME to $WORKERS workers"
    if [ "$DRY_RUN" = false ]; then
        aws autoscaling update-auto-scaling-group \
            --auto-scaling-group-name "$ASG_NAME" \
            --desired-capacity "$WORKERS" \
            --region $REGION
    fi
fi

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "Auto Scaling Group: $ASG_NAME"
echo "Desired Workers: $WORKERS"
echo ""
echo "Useful commands:"
echo "  # Check worker status"
echo "  ./scripts/manage_workers.sh status"
echo ""
echo "  # Scale workers"
echo "  ./scripts/manage_workers.sh scale 5"
echo ""
echo "  # View logs"
echo "  ./scripts/manage_workers.sh logs"
echo ""
echo "  # SSH to a worker"
echo "  ./scripts/manage_workers.sh ssh"
