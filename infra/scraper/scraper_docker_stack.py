#!/usr/bin/env python3
"""
AWS CDK Stack for Docker-based Scraper Workers

Creates an Auto Scaling Group of EC2 instances that:
- Pull Docker image from ECR
- Run scraper workers in containers
- Connect to the shared PostgreSQL database

Usage:
    # First, build and push Docker image
    ./scripts/build_and_push.sh

    # Then deploy
    cd infra/scraper
    cdk deploy ScraperDockerStack

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                        VPC                                   │
    │  ┌─────────────────────────────────────────────────────┐    │
    │  │              Auto Scaling Group                      │    │
    │  │   ┌──────────┐  ┌──────────┐  ┌──────────┐         │    │
    │  │   │ Docker   │  │ Docker   │  │ Docker   │  ...    │    │
    │  │   │ Worker 1 │  │ Worker 2 │  │ Worker N │         │    │
    │  │   └──────────┘  └──────────┘  └──────────┘         │    │
    │  └─────────────────────────────────────────────────────┘    │
    │                           │                                  │
    │                           ▼                                  │
    │      ┌───────────┐   ┌───────────────┐                      │
    │      │    ECR    │   │ Aurora (RDS)  │                      │
    │      │   Image   │   │  PostgreSQL   │                      │
    │      └───────────┘   └───────────────┘                      │
    └─────────────────────────────────────────────────────────────┘
"""

import os

from pathlib import Path

from aws_cdk import (
    App,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
    aws_autoscaling as autoscaling,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_ssm as ssm,
)
from constructs import Construct


class ScraperDockerStack(Stack):
    """CDK Stack for Docker-based scraper worker Auto Scaling Group."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc_id: str | None = None,
        db_security_group_id: str | None = None,
        min_capacity: int = 1,
        max_capacity: int = 5,
        desired_capacity: int = 2,
        instance_type: str = "t3.medium",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Get environment variables
        db_password = os.environ.get("DB_PASSWORD", "")
        aws_account_id = os.environ.get("AWS_ACCOUNT_ID", self.account)
        s3_bucket_name = os.environ.get("S3_BUCKET_NAME", f"flight-matrix-static-{aws_account_id}")

        # Build and push Docker image using DockerImageAsset
        # This automatically builds the image and pushes to ECR during CDK deployment
        project_root = Path(__file__).parent.parent.parent
        docker_image = ecr_assets.DockerImageAsset(
            self,
            "ScraperImage",
            directory=str(project_root),
            file="Dockerfile.scraper",
            platform=ecr_assets.Platform.LINUX_AMD64,
            exclude=[
                "data",
                "*.dump",
                "*.zip",
                "*.log",
                "*.mhtml",
                "*.png",
                "nohup*",
                ".venv",
                ".venv-cdk",
                ".git",
                "cdk.out",
                "__pycache__",
                "*.pyc",
                "infra/scraper/cdk.out",
                "layers",
                "lambda_code",
            ],
        )

        # Store the image URI for use in user data
        image_uri = docker_image.image_uri

        # Look up existing VPC or use default
        if vpc_id:
            vpc = ec2.Vpc.from_lookup(self, "VPC", vpc_id=vpc_id)
        else:
            vpc = ec2.Vpc.from_lookup(self, "VPC", is_default=True)

        # Security Group for workers
        worker_sg = ec2.SecurityGroup(
            self,
            "WorkerSecurityGroup",
            vpc=vpc,
            description="Security group for Docker scraper workers",
            allow_all_outbound=True,
        )

        # Allow SSH access (optional, for debugging)
        worker_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(22),
            "Allow SSH access",
        )

        # If DB security group is provided, allow access to it
        if db_security_group_id:
            db_sg = ec2.SecurityGroup.from_security_group_id(
                self, "DbSecurityGroup", db_security_group_id
            )
            db_sg.add_ingress_rule(
                worker_sg,
                ec2.Port.tcp(5432),
                "Allow scraper workers to access Aurora",
            )

        # IAM Role for EC2 instances
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
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonEC2ContainerRegistryReadOnly"
                ),
            ],
        )

        # S3 permissions for image uploads
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:PutObject",
                    "s3:GetObject",
                    "s3:ListBucket",
                ],
                resources=[
                    f"arn:aws:s3:::{s3_bucket_name}",
                    f"arn:aws:s3:::{s3_bucket_name}/*",
                ],
            )
        )

        # SSM Parameter Store permissions for secrets
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ssm:GetParameter",
                    "ssm:GetParameters",
                ],
                resources=[
                    f"arn:aws:ssm:{self.region}:{aws_account_id}:parameter/flight-matrix/*",
                ],
            )
        )

        # RDS IAM Authentication - allows EC2 to connect to Aurora without password
        # The DB resource ID can be found in RDS console or via AWS CLI
        db_resource_id = os.environ.get("DB_RESOURCE_ID", "cluster-*")
        db_user = os.environ.get("DB_IAM_USER", "scraper_iam")
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["rds-db:connect"],
                resources=[
                    f"arn:aws:rds-db:{self.region}:{aws_account_id}:dbuser:{db_resource_id}/{db_user}",
                ],
            )
        )

        # Note: SSM parameter /flight-matrix/scraper/db-password is created by
        # ScraperWorkerStack or manually. This stack only reads from it.

        # User data script for Docker deployment
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "#!/bin/bash",
            "set -ex",
            "",
            "# Log all output",
            "exec > >(tee /var/log/user-data.log) 2>&1",
            "",
            "echo '=== Starting Docker Worker Setup ==='",
            "",
            "# Update system",
            "apt-get update",
            "",
            "# Install Docker and dependencies",
            "apt-get install -y docker.io unzip curl",
            "systemctl enable docker",
            "systemctl start docker",
            "usermod -aG docker ubuntu",
            "",
            "# Install AWS CLI v2",
            'curl -s "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"',
            "unzip -q awscliv2.zip",
            "./aws/install",
            "rm -rf aws awscliv2.zip",
            "",
            "# Login to ECR",
            f'aws ecr get-login-password --region {self.region} | docker login --username AWS --password-stdin {aws_account_id}.dkr.ecr.{self.region}.amazonaws.com',
            "",
            "# Pull latest image (built and pushed by CDK)",
            f"docker pull {image_uri}",
            "",
            "# Create environment file (password auth, not IAM)",
            f'echo "AWS_DEFAULT_REGION={self.region}" > /etc/default/scraper-worker',
            f'echo "ECR_IMAGE={image_uri}" >> /etc/default/scraper-worker',
            'echo "USE_IAM_AUTH=false" >> /etc/default/scraper-worker',
            f'echo "S3_BUCKET_NAME={s3_bucket_name}" >> /etc/default/scraper-worker',
            "",
            "# Create systemd service for Docker container with IAM authentication",
            f"cat > /etc/systemd/system/scraper-worker.service << 'EOFSERVICE'",
            "[Unit]",
            "Description=Flight Matrix Scraper Worker (Docker with IAM Auth)",
            "After=docker.service",
            "Requires=docker.service",
            "",
            "[Service]",
            "Type=simple",
            "EnvironmentFile=/etc/default/scraper-worker",
            "Restart=always",
            "RestartSec=10",
            "ExecStartPre=-/usr/bin/docker stop scraper-worker",
            "ExecStartPre=-/usr/bin/docker rm scraper-worker",
            f"ExecStartPre=/bin/bash -c 'aws ecr get-login-password --region {self.region} | docker login --username AWS --password-stdin {aws_account_id}.dkr.ecr.{self.region}.amazonaws.com'",
            "ExecStartPre=/usr/bin/docker pull ${ECR_IMAGE}",
            'ExecStart=/usr/bin/docker run --name scraper-worker --rm --shm-size=2g --network=host -e USE_IAM_AUTH=${USE_IAM_AUTH} -e DB_IAM_USER=${DB_IAM_USER} -e AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION} -e S3_BUCKET_NAME=${S3_BUCKET_NAME} ${ECR_IMAGE}',
            "ExecStop=/usr/bin/docker stop scraper-worker",
            "StandardOutput=append:/var/log/scraper-worker/worker.log",
            "StandardError=append:/var/log/scraper-worker/worker.log",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "EOFSERVICE",
            "",
            "# Create log directory",
            "mkdir -p /var/log/scraper-worker",
            "chown ubuntu:ubuntu /var/log/scraper-worker",
            "",
            "# Start service",
            "systemctl daemon-reload",
            "systemctl enable scraper-worker",
            "systemctl start scraper-worker",
            "",
            "echo '=== Docker Worker Setup Complete ==='",
        )

        # Launch template
        launch_template = ec2.LaunchTemplate(
            self,
            "WorkerLaunchTemplate",
            instance_type=ec2.InstanceType(instance_type),
            machine_image=ec2.MachineImage.generic_linux(
                {
                    "us-west-2": "ami-0cf2b4e024cdb6960",  # Ubuntu 22.04 LTS
                    "us-east-1": "ami-0c7217cdde317cfec",
                    "eu-west-1": "ami-0905a3c97561e0b69",
                }
            ),
            security_group=worker_sg,
            role=role,
            user_data=user_data,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/sda1",
                    volume=ec2.BlockDeviceVolume.ebs(
                        volume_size=30,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        delete_on_termination=True,
                    ),
                )
            ],
        )

        # Auto Scaling Group
        asg = autoscaling.AutoScalingGroup(
            self,
            "WorkerASG",
            vpc=vpc,
            launch_template=launch_template,
            min_capacity=min_capacity,
            max_capacity=max_capacity,
            desired_capacity=desired_capacity,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            health_check=autoscaling.HealthCheck.ec2(
                grace=Duration.minutes(5),  # Faster health check for Docker
            ),
            update_policy=autoscaling.UpdatePolicy.rolling_update(
                max_batch_size=1,
                min_instances_in_service=1,
            ),
        )

        # Add tags
        Tags.of(asg).add("Name", "scraper-worker-docker")
        Tags.of(asg).add("Project", "flight-matrix")
        Tags.of(asg).add("Component", "scraper")
        Tags.of(asg).add("Deployment", "docker")

        # Outputs
        CfnOutput(
            self,
            "DockerImageUri",
            value=image_uri,
            description="Docker image URI (built and pushed by CDK)",
        )

        CfnOutput(
            self,
            "ASGName",
            value=asg.auto_scaling_group_name,
            description="Auto Scaling Group name",
        )

        CfnOutput(
            self,
            "SecurityGroupId",
            value=worker_sg.security_group_id,
            description="Worker security group ID",
        )


# CDK App
app = App()

ScraperDockerStack(
    app,
    "ScraperDockerStack",
    env={
        "account": os.environ.get("CDK_DEFAULT_ACCOUNT", os.environ.get("AWS_ACCOUNT_ID")),
        "region": os.environ.get("CDK_DEFAULT_REGION", "us-west-2"),
    },
    # Configuration
    vpc_id=os.environ.get("VPC_ID"),
    db_security_group_id=os.environ.get("DB_SECURITY_GROUP_ID"),
    min_capacity=int(os.environ.get("MIN_WORKERS", "1")),
    max_capacity=int(os.environ.get("MAX_WORKERS", "5")),
    desired_capacity=int(os.environ.get("DESIRED_WORKERS", "2")),
    instance_type=os.environ.get("INSTANCE_TYPE", "t3.medium"),
)

app.synth()
