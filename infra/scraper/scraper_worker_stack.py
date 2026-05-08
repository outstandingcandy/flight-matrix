#!/usr/bin/env python3
"""
AWS CDK Stack for Distributed Scraper Workers

Creates an Auto Scaling Group of EC2 instances that automatically:
- Pull code from the repository
- Configure the environment
- Start scraper workers
- Connect to the shared PostgreSQL database

This stack is designed to use the main Flight-Matrix VPC from NetworkStack.
Workers are deployed in private subnets with NAT Gateway access for internet.

Usage:
    # Deploy using main CDK app (recommended)
    cd /path/to/flight-matrix
    cdk deploy FlightMatrix-Compute-prod

    # Or standalone with VPC ID
    cd infra/scraper
    VPC_ID=vpc-xxx DB_SECURITY_GROUP_ID=sg-xxx cdk deploy ScraperWorkerStack

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                   Flight-Matrix VPC                         │
    │  ┌─────────────────────────────────────────────────────┐    │
    │  │              Private Subnets (with NAT)             │    │
    │  │   ┌──────────┐  ┌──────────┐  ┌──────────┐         │    │
    │  │   │ Worker 1 │  │ Worker 2 │  │ Worker N │  ...    │    │
    │  │   │ (Spot)   │  │ (Spot)   │  │ (Spot)   │         │    │
    │  │   └──────────┘  └──────────┘  └──────────┘         │    │
    │  └─────────────────────────────────────────────────────┘    │
    │                           │                                  │
    │                           ▼                                  │
    │  ┌─────────────────────────────────────────────────────┐    │
    │  │              Isolated Subnets                        │    │
    │  │                   ┌───────────────┐                  │    │
    │  │                   │ Aurora (RDS)  │                  │    │
    │  │                   │  PostgreSQL   │                  │    │
    │  │                   └───────────────┘                  │    │
    │  └─────────────────────────────────────────────────────┘    │
    └─────────────────────────────────────────────────────────────┘
"""

import os

from aws_cdk import (
    App,
    CfnOutput,
    Duration,
    Fn,
    Stack,
    Tags,
    aws_autoscaling as autoscaling,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_ssm as ssm,
)
from constructs import Construct


class ScraperWorkerStack(Stack):
    """CDK Stack for scraper worker Auto Scaling Group."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc | None = None,
        vpc_id: str | None = None,
        services_security_group: ec2.ISecurityGroup | None = None,
        db_security_group_id: str | None = None,
        db_endpoint: str | None = None,
        db_name: str = "aircraft_data",
        db_username: str = "aircraft_admin",
        s3_bucket_name: str | None = None,
        min_capacity: int = 1,
        max_capacity: int = 5,
        desired_capacity: int = 2,
        instance_type: str = "t3.medium",
        use_spot_instances: bool = True,
        environment: str = "prod",
        **kwargs,
    ) -> None:
        """Initialize the scraper worker stack.

        Args:
            scope: CDK construct scope.
            construct_id: Stack ID.
            vpc: VPC object (from NetworkStack).
            vpc_id: VPC ID string (for standalone deployment).
            services_security_group: Security group (from NetworkStack).
            db_security_group_id: Database security group ID (for standalone).
            db_endpoint: Aurora database endpoint.
            db_name: Database name.
            db_username: Database username.
            s3_bucket_name: S3 bucket for images.
            min_capacity: Minimum ASG capacity.
            max_capacity: Maximum ASG capacity.
            desired_capacity: Desired ASG capacity.
            instance_type: EC2 instance type.
            use_spot_instances: Whether to use Spot instances.
            environment: Environment name.
        """
        super().__init__(scope, construct_id, **kwargs)

        self.env_name = environment
        self.db_endpoint = db_endpoint or ""
        self.db_name = db_name
        self.db_username = db_username
        self.s3_bucket_name = s3_bucket_name or f"flight-matrix-static-{self.account}"

        # Get environment variables for standalone deployment
        db_password = os.environ.get("DB_PASSWORD", "")
        github_token = os.environ.get("GITHUB_TOKEN", "")

        # Resolve VPC
        if vpc:
            self._vpc = vpc
        elif vpc_id:
            self._vpc = ec2.Vpc.from_lookup(self, "VPC", vpc_id=vpc_id)
        else:
            # Try to import from NetworkStack export
            try:
                imported_vpc_id = Fn.import_value(
                    f"FlightMatrix-Network-{environment}-VpcId"
                )
                self._vpc = ec2.Vpc.from_lookup(
                    self, "VPC", vpc_id=imported_vpc_id
                )
            except Exception:
                # Fallback to default VPC (not recommended)
                self._vpc = ec2.Vpc.from_lookup(self, "VPC", is_default=True)

        # Create or use security group
        if services_security_group:
            self._worker_sg = services_security_group
        else:
            self._worker_sg = ec2.SecurityGroup(
                self,
                "WorkerSecurityGroup",
                vpc=self._vpc,
                description="Security group for scraper workers",
                allow_all_outbound=True,
            )

            # Allow SSH access
            self._worker_sg.add_ingress_rule(
                ec2.Peer.any_ipv4(),
                ec2.Port.tcp(22),
                "Allow SSH access",
            )

        # Add database access if security group ID provided
        if db_security_group_id:
            db_sg = ec2.SecurityGroup.from_security_group_id(
                self, "DbSecurityGroup", db_security_group_id
            )
            db_sg.add_ingress_rule(
                self._worker_sg,
                ec2.Port.tcp(5432),
                "Allow scraper workers to access Aurora",
            )

        # Create IAM role
        self._role = self._create_iam_role()

        # Store secrets in SSM
        self._create_ssm_parameters(db_password, github_token)

        # Create ASG
        self._asg = self._create_asg(
            instance_type=instance_type,
            min_capacity=min_capacity,
            max_capacity=max_capacity,
            desired_capacity=desired_capacity,
            use_spot=use_spot_instances,
        )

        # Create outputs
        self._create_outputs()

    def _create_iam_role(self) -> iam.Role:
        """Create IAM role for EC2 instances."""
        role = iam.Role(
            self,
            "WorkerRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "CloudWatchAgentServerPolicy"
                ),
            ],
        )

        # S3 permissions
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:PutObject",
                    "s3:GetObject",
                    "s3:ListBucket",
                ],
                resources=[
                    f"arn:aws:s3:::{self.s3_bucket_name}",
                    f"arn:aws:s3:::{self.s3_bucket_name}/*",
                ],
            )
        )

        # SSM Parameter Store permissions
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ssm:GetParameter",
                    "ssm:GetParameters",
                ],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/flight-matrix/*",
                ],
            )
        )

        # RDS IAM authentication
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["rds-db:connect"],
                resources=[
                    f"arn:aws:rds-db:{self.region}:{self.account}:dbuser:*/{self.db_username}",
                ],
            )
        )

        # Bedrock permissions for AI analysis
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=["*"],
            )
        )

        return role

    def _create_ssm_parameters(self, db_password: str, github_token: str) -> None:
        """Store secrets in SSM Parameter Store."""
        ssm.StringParameter(
            self,
            "DbPasswordParam",
            parameter_name="/flight-matrix/scraper/db-password",
            string_value=db_password or "PLACEHOLDER_UPDATE_MANUALLY",
            description="Database password for scraper workers",
        )

        ssm.StringParameter(
            self,
            "GitHubTokenParam",
            parameter_name="/flight-matrix/scraper/github-token",
            string_value=github_token or "PLACEHOLDER_UPDATE_MANUALLY",
            description="GitHub token for cloning private repository",
        )

    def _create_asg(
        self,
        instance_type: str,
        min_capacity: int,
        max_capacity: int,
        desired_capacity: int,
        use_spot: bool,
    ) -> autoscaling.AutoScalingGroup:
        """Create Auto Scaling Group for scraper workers."""
        # User data script
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "#!/bin/bash",
            "set -ex",
            "",
            "# Log all output",
            "exec > >(tee /var/log/user-data.log) 2>&1",
            "",
            "echo '=== Starting EC2 Worker Setup ==='",
            "",
            "# Update system and install dependencies",
            "apt-get update",
            "apt-get install -y python3-pip python3-venv git xvfb chromium-browser unzip curl jq",
            "",
            "# Get secrets from SSM",
            'REGION=$(curl -s http://169.254.169.254/latest/meta-data/placement/region)',
            'DB_PASSWORD=$(aws ssm get-parameter --name "/flight-matrix/scraper/db-password" --with-decryption --query "Parameter.Value" --output text --region $REGION 2>/dev/null || echo "")',
            'GITHUB_TOKEN=$(aws ssm get-parameter --name "/flight-matrix/scraper/github-token" --with-decryption --query "Parameter.Value" --output text --region $REGION 2>/dev/null || echo "")',
            'GITHUB_REPO=$(aws ssm get-parameter --name "/flight-matrix/scraper/github-repo" --query "Parameter.Value" --output text --region $REGION 2>/dev/null || echo "")',
            "",
            "# Clone repository (GITHUB_REPO is owner/repo, e.g. acme/flight-matrix)",
            "cd /home/ubuntu",
            'if [ -z "$GITHUB_REPO" ]; then',
            '    echo "ERROR: SSM parameter /flight-matrix/scraper/github-repo is not set"',
            "    exit 1",
            "fi",
            'if [ -n "$GITHUB_TOKEN" ] && [ "$GITHUB_TOKEN" != "PLACEHOLDER_UPDATE_MANUALLY" ]; then',
            '    git clone https://${GITHUB_TOKEN}@github.com/${GITHUB_REPO}.git Project/flight-matrix || true',
            "else",
            '    git clone https://github.com/${GITHUB_REPO}.git Project/flight-matrix || true',
            "fi",
            "cd Project/flight-matrix",
            "git pull origin main || true",
            "",
            "# Setup Python environment",
            "echo 'Creating Python virtual environment...'",
            "python3 -m venv .venv",
            "source .venv/bin/activate",
            "pip install --upgrade pip",
            "",
            "# Install dependencies",
            "echo 'Installing Python dependencies...'",
            "pip install pyyaml DrissionPage pydantic sqlalchemy boto3 requests psycopg2-binary",
            "",
            "# Verify dependencies",
            "echo 'Verifying dependencies...'",
            "python -c 'import yaml; from DrissionPage import ChromiumPage; import pydantic; import sqlalchemy; import boto3; print(\"All dependencies OK\")' || { echo 'Dependency verification FAILED!'; exit 1; }",
            "",
            "# Update config with database password",
            'if [ -n "$DB_PASSWORD" ] && [ "$DB_PASSWORD" != "PLACEHOLDER_UPDATE_MANUALLY" ]; then',
            '    sed -i "s|\\${DB_PASSWORD}|$DB_PASSWORD|g" config.yaml',
            "fi",
            "",
            "# Setup Xvfb service",
            "cat > /etc/systemd/system/xvfb.service << 'EOF'",
            "[Unit]",
            "Description=X Virtual Framebuffer",
            "After=network.target",
            "",
            "[Service]",
            "Type=simple",
            "ExecStart=/usr/bin/Xvfb :55 -screen 0 1920x1080x24",
            "Restart=always",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "EOF",
            "",
            "# Setup scraper worker service",
            "cat > /etc/systemd/system/scraper-worker.service << 'EOF'",
            "[Unit]",
            "Description=Flight Matrix Scraper Worker",
            "After=network.target xvfb.service",
            "Requires=xvfb.service",
            "",
            "[Service]",
            "Type=simple",
            "User=ubuntu",
            "Group=ubuntu",
            "WorkingDirectory=/home/ubuntu/Project/flight-matrix",
            "Environment=DISPLAY=:55",
            "Environment=PYTHONUNBUFFERED=1",
            "ExecStart=/home/ubuntu/Project/flight-matrix/.venv/bin/python -m src.scraper_main --config config/config.yaml",
            "Restart=always",
            "RestartSec=10",
            "StandardOutput=append:/var/log/scraper-worker/worker.log",
            "StandardError=append:/var/log/scraper-worker/worker.log",
            "LimitNOFILE=65536",
            "MemoryMax=4G",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "EOF",
            "",
            "# Create log directory",
            "mkdir -p /var/log/scraper-worker",
            "chown ubuntu:ubuntu /var/log/scraper-worker",
            "",
            "# Fix permissions",
            "chown -R ubuntu:ubuntu /home/ubuntu/Project",
            "",
            "# Start services",
            "systemctl daemon-reload",
            "systemctl enable xvfb scraper-worker",
            "systemctl start xvfb",
            "sleep 2",
            "systemctl start scraper-worker",
            "",
            "echo '=== EC2 Worker Setup Complete ==='",
        )

        # Launch template options
        launch_template_props: dict = {
            "instance_type": ec2.InstanceType(instance_type),
            "machine_image": ec2.MachineImage.generic_linux(
                {
                    "us-west-2": "ami-0cf2b4e024cdb6960",  # Ubuntu 22.04 LTS
                    "us-east-1": "ami-0c7217cdde317cfec",
                    "eu-west-1": "ami-0905a3c97561e0b69",
                }
            ),
            "security_group": self._worker_sg,
            "role": self._role,
            "user_data": user_data,
            "block_devices": [
                ec2.BlockDevice(
                    device_name="/dev/sda1",
                    volume=ec2.BlockDeviceVolume.ebs(
                        volume_size=30,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        delete_on_termination=True,
                    ),
                )
            ],
        }

        # Add Spot options if enabled
        if use_spot:
            launch_template_props["spot_options"] = ec2.LaunchTemplateSpotOptions(
                interruption_behavior=ec2.SpotInstanceInterruption.TERMINATE,
                max_price=0.05,
                request_type=ec2.SpotRequestType.ONE_TIME,
            )

        launch_template = ec2.LaunchTemplate(
            self,
            "WorkerLaunchTemplate",
            **launch_template_props,
        )

        # Determine subnet type based on VPC configuration
        # Use private subnets if available (with NAT), otherwise public
        try:
            private_subnets = self._vpc.select_subnets(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            )
            subnet_selection = ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            )
        except Exception:
            subnet_selection = ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC
            )

        # Auto Scaling Group
        asg = autoscaling.AutoScalingGroup(
            self,
            "WorkerASG",
            auto_scaling_group_name=f"scraper-worker-asg-{self.env_name}",
            vpc=self._vpc,
            launch_template=launch_template,
            min_capacity=min_capacity,
            max_capacity=max_capacity,
            desired_capacity=desired_capacity,
            vpc_subnets=subnet_selection,
            health_checks=autoscaling.HealthChecks.ec2(
                grace_period=Duration.minutes(10),
            ),
            update_policy=autoscaling.UpdatePolicy.rolling_update(
                max_batch_size=1,
                min_instances_in_service=1,
            ),
        )

        # Add tags
        Tags.of(asg).add("Name", f"scraper-worker-{self.env_name}")
        Tags.of(asg).add("Project", "flight-matrix")
        Tags.of(asg).add("Component", "scraper")

        return asg

    def _create_outputs(self) -> None:
        """Create CloudFormation outputs."""
        CfnOutput(
            self,
            "ASGName",
            value=self._asg.auto_scaling_group_name,
            description="Auto Scaling Group name",
        )

        CfnOutput(
            self,
            "SecurityGroupId",
            value=self._worker_sg.security_group_id,
            description="Worker security group ID",
        )


# Standalone CDK App (for independent deployment)
if __name__ == "__main__":
    app = App()

    ScraperWorkerStack(
        app,
        "ScraperWorkerStack",
        env={
            "account": os.environ.get(
                "CDK_DEFAULT_ACCOUNT", os.environ.get("AWS_ACCOUNT_ID")
            ),
            "region": os.environ.get("CDK_DEFAULT_REGION", "us-west-2"),
        },
        vpc_id=os.environ.get("VPC_ID"),
        db_security_group_id=os.environ.get("DB_SECURITY_GROUP_ID"),
        db_endpoint=os.environ.get("DB_ENDPOINT"),
        min_capacity=int(os.environ.get("MIN_WORKERS", "1")),
        max_capacity=int(os.environ.get("MAX_WORKERS", "5")),
        desired_capacity=int(os.environ.get("DESIRED_WORKERS", "2")),
        instance_type=os.environ.get("INSTANCE_TYPE", "t3.medium"),
        use_spot_instances=os.environ.get("USE_SPOT", "true").lower() == "true",
    )

    app.synth()
