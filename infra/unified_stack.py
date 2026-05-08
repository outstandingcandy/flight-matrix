"""
AWS CDK Unified Stack for Flight-Matrix Aircraft Tracking System

Supports two modes:
1. Fresh deployment - Creates all resources from scratch
2. Import mode - Imports existing VPC, Database, S3, CloudFront and creates new compute resources

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                     FlightMatrixUnifiedStack                 │
    ├─────────────────────────────────────────────────────────────┤
    │  VPC (10.0.0.0/16)                                          │
    │  ├── Public Subnets (NAT Gateway, Scraper Workers)          │
    │  ├── Private Subnets (Reserved)                             │
    │  └── Isolated Subnets (Lambda, Aurora)                      │
    │                                                              │
    │  Lambda (Docker) ──> API Gateway ──> External Access        │
    │       │                                                      │
    │       └──> Aurora PostgreSQL <── Scraper Workers (EC2 ASG)  │
    │                                                              │
    │  S3 Bucket <── CloudFront CDN                               │
    └─────────────────────────────────────────────────────────────┘
"""

from pathlib import Path
from typing import Optional

import secrets

from aws_cdk import (
    CfnOutput,
    Duration,
    Fn,
    RemovalPolicy,
    SecretValue,
    Size,
    Stack,
    Tags,
    aws_autoscaling as autoscaling,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_logs as logs,
    aws_rds as rds,
    aws_s3 as s3,
    aws_ssm as ssm,
)
from constructs import Construct


class FlightMatrixUnifiedStack(Stack):
    """Unified CDK stack for Flight Matrix infrastructure."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        db_username: str,
        db_password: str,
        db_name: str,
        # Import existing resources (set these to import instead of create)
        existing_vpc_id: Optional[str] = None,
        existing_db_endpoint: Optional[str] = None,
        existing_db_security_group_id: Optional[str] = None,
        existing_s3_bucket_name: Optional[str] = None,
        existing_cloudfront_distribution_id: Optional[str] = None,
        existing_cloudfront_domain: Optional[str] = None,
        existing_private_subnet_ids: Optional[list[str]] = None,
        existing_isolated_subnet_ids: Optional[list[str]] = None,
        existing_public_subnet_ids: Optional[list[str]] = None,
        existing_services_security_group_id: Optional[str] = None,
        # Scraper configuration
        scraper_min_capacity: int = 1,
        scraper_max_capacity: int = 5,
        scraper_desired_capacity: int = 2,
        scraper_instance_type: str = "t3.medium",
        enable_nat_gateway: bool = True,
        # Cognito authentication (optional - provide existing OR enable creation)
        cognito_user_pool_id: Optional[str] = None,
        cognito_client_id: Optional[str] = None,
        cognito_client_secret: Optional[str] = None,
        cognito_domain: Optional[str] = None,
        cognito_callback_url: Optional[str] = None,
        cognito_logout_url: Optional[str] = None,
        cognito_jwks: Optional[str] = None,  # JWKS JSON for offline JWT verification
        flask_secret_key: Optional[str] = None,
        # Set to True to create Cognito resources automatically
        enable_cognito_auth: bool = False,
        cognito_domain_prefix: Optional[str] = None,
        # Custom domain for the application (e.g., example.com)
        # Used for correct URL generation when behind API Gateway
        app_domain: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.env_name = environment
        self.db_username = db_username
        self.db_password = db_password
        self.db_name = db_name
        self.scraper_min_capacity = scraper_min_capacity
        self.scraper_max_capacity = scraper_max_capacity
        self.scraper_desired_capacity = scraper_desired_capacity
        self.scraper_instance_type = scraper_instance_type
        self.enable_nat_gateway = enable_nat_gateway

        # Cognito configuration (can be provided or created)
        self._cognito_user_pool_id = cognito_user_pool_id
        self._cognito_client_id = cognito_client_id
        self._cognito_client_secret = cognito_client_secret
        self._cognito_domain = cognito_domain
        self._cognito_callback_url = cognito_callback_url
        self._cognito_logout_url = cognito_logout_url
        self._cognito_jwks = cognito_jwks  # JWKS JSON for offline JWT verification
        self._flask_secret_key = flask_secret_key or secrets.token_hex(32)
        self._enable_cognito_auth = enable_cognito_auth
        self._cognito_domain_prefix = cognito_domain_prefix or f"flight-matrix-{environment}"
        self._app_domain = app_domain  # Custom domain for URL generation

        # Store import flags
        self._import_mode = existing_vpc_id is not None
        self._existing_vpc_id = existing_vpc_id
        self._existing_db_endpoint = existing_db_endpoint
        self._existing_db_security_group_id = existing_db_security_group_id
        self._existing_s3_bucket_name = existing_s3_bucket_name
        self._existing_cloudfront_distribution_id = existing_cloudfront_distribution_id
        self._existing_cloudfront_domain = existing_cloudfront_domain
        self._existing_private_subnet_ids = existing_private_subnet_ids or []
        self._existing_isolated_subnet_ids = existing_isolated_subnet_ids or []
        self._existing_public_subnet_ids = existing_public_subnet_ids or []
        self._existing_services_security_group_id = existing_services_security_group_id

        # Create or import infrastructure
        if self._import_mode:
            self._import_existing_resources()
        else:
            self._create_new_resources()

        # Always create new compute resources
        self.lambda_function = self._create_lambda()
        self.api_gateway = self._create_api_gateway()
        self.scraper_asg = self._create_scraper_workers()

        # Create Cognito resources if enabled (after API Gateway to get URL)
        if self._enable_cognito_auth and not self._cognito_user_pool_id:
            self._create_cognito_resources()
            # Update Lambda environment with Cognito config
            self._update_lambda_cognito_env()

        # Create outputs
        self._create_outputs()

    def _import_existing_resources(self) -> None:
        """Import existing VPC, Database, S3, and CloudFront resources."""
        # Import VPC
        self.vpc = ec2.Vpc.from_lookup(
            self,
            "VPC",
            vpc_id=self._existing_vpc_id,
        )

        # Import or create DB security group
        if self._existing_db_security_group_id:
            self.db_security_group = ec2.SecurityGroup.from_security_group_id(
                self,
                "DBSecurityGroup",
                self._existing_db_security_group_id,
                mutable=True,
            )
        else:
            self.db_security_group = ec2.SecurityGroup(
                self,
                "DBSecurityGroup",
                vpc=self.vpc,
                description="Security group for Aurora RDS",
                allow_all_outbound=False,
            )

        # Import services security group if provided
        if self._existing_services_security_group_id:
            self.services_security_group = ec2.SecurityGroup.from_security_group_id(
                self,
                "ServicesSecurityGroup",
                self._existing_services_security_group_id,
                mutable=True,
            )
        else:
            self.services_security_group = None

        # Store database endpoint for Lambda
        self.db_endpoint = self._existing_db_endpoint

        # Import S3 bucket
        if self._existing_s3_bucket_name:
            self.s3_bucket = s3.Bucket.from_bucket_name(
                self,
                "StaticFilesBucket",
                self._existing_s3_bucket_name,
            )
        else:
            self.s3_bucket = self._create_s3_bucket()

        # Store CloudFront info
        self.cloudfront_domain = self._existing_cloudfront_domain or ""
        self.cloudfront_distribution_id = self._existing_cloudfront_distribution_id or ""

        # Store subnet info for later use
        # Private subnets have NAT gateway access - needed for Lambda DNS resolution
        self._private_subnets = self._existing_private_subnet_ids
        self._isolated_subnets = self._existing_isolated_subnet_ids
        self._public_subnets = self._existing_public_subnet_ids

    def _create_new_resources(self) -> None:
        """Create all new infrastructure resources."""
        self.vpc = self._create_vpc()
        self._create_vpc_endpoints()
        database = self._create_database()
        self.db_endpoint = database.cluster_endpoint.hostname
        self.s3_bucket = self._create_s3_bucket()
        cloudfront_dist = self._create_cloudfront()
        self.cloudfront_domain = cloudfront_dist.distribution_domain_name
        self.cloudfront_distribution_id = cloudfront_dist.distribution_id

        # For new resources, subnets are auto-selected
        self._private_subnets = None
        self._isolated_subnets = None
        self._public_subnets = None

    def _create_vpc(self) -> ec2.Vpc:
        """Create VPC with three-tier subnet architecture."""
        subnet_configuration = [
            ec2.SubnetConfiguration(
                name="Public",
                subnet_type=ec2.SubnetType.PUBLIC,
                cidr_mask=24,
            ),
            ec2.SubnetConfiguration(
                name="Private",
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                cidr_mask=24,
            ),
            ec2.SubnetConfiguration(
                name="Isolated",
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                cidr_mask=24,
            ),
        ]

        vpc = ec2.Vpc(
            self,
            "VPC",
            ip_addresses=ec2.IpAddresses.cidr("10.0.0.0/16"),
            max_azs=2,
            nat_gateways=1 if self.enable_nat_gateway else 0,
            subnet_configuration=subnet_configuration,
            enable_dns_hostnames=True,
            enable_dns_support=True,
        )

        Tags.of(vpc).add("Name", f"flight-matrix-{self.env_name}")
        Tags.of(vpc).add("Project", "flight-matrix")

        return vpc

    def _create_vpc_endpoints(self) -> None:
        """Create VPC endpoints for AWS services."""
        self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        self.vpc.add_interface_endpoint(
            "SSMEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SSM,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        )

        self.vpc.add_interface_endpoint(
            "SSMMessagesEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SSM_MESSAGES,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        )

        self.vpc.add_interface_endpoint(
            "EC2MessagesEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.EC2_MESSAGES,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        )

        self.vpc.add_interface_endpoint(
            "ECRApiEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.ECR,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        )

        self.vpc.add_interface_endpoint(
            "ECRDockerEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        )

        self.vpc.add_interface_endpoint(
            "CloudWatchLogsEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        )

    def _create_database(self) -> rds.DatabaseCluster:
        """Create Aurora Serverless v2 PostgreSQL cluster."""
        self.db_security_group = ec2.SecurityGroup(
            self,
            "RDSSecurityGroup",
            vpc=self.vpc,
            description="Security group for Aurora RDS",
            allow_all_outbound=False,
        )

        cluster = rds.DatabaseCluster(
            self,
            "AuroraCluster",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_15_8
            ),
            writer=rds.ClusterInstance.serverless_v2("Writer"),
            serverless_v2_min_capacity=0.5,
            serverless_v2_max_capacity=4,
            default_database_name=self.db_name,
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            security_groups=[self.db_security_group],
            storage_encrypted=True,
            backup=rds.BackupProps(
                retention=Duration.days(7),
                preferred_window="03:00-04:00",
            ),
            preferred_maintenance_window="mon:04:00-mon:05:00",
            removal_policy=RemovalPolicy.SNAPSHOT,
            credentials=rds.Credentials.from_password(
                username=self.db_username,
                password=SecretValue.unsafe_plain_text(self.db_password),
            ),
        )

        Tags.of(cluster).add("Project", "flight-matrix")

        return cluster

    def _create_s3_bucket(self) -> s3.Bucket:
        """Create S3 bucket for static files and aircraft images."""
        bucket = s3.Bucket(
            self,
            "StaticFilesBucket",
            bucket_name=f"flight-matrix-static-{self.account}",
            removal_policy=RemovalPolicy.RETAIN,
            auto_delete_objects=False,
            public_read_access=False,
            block_public_access=s3.BlockPublicAccess(
                block_public_acls=False,
                block_public_policy=False,
                ignore_public_acls=False,
                restrict_public_buckets=False,
            ),
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.GET, s3.HttpMethods.HEAD],
                    allowed_origins=["*"],
                    allowed_headers=["*"],
                    max_age=3600,
                )
            ],
        )

        return bucket

    def _create_cloudfront(self) -> cloudfront.Distribution:
        """Create CloudFront distribution for S3 static files."""
        oai = cloudfront.OriginAccessIdentity(
            self,
            "CloudFrontOAI",
            comment="OAI for Flight Matrix static files",
        )

        self.s3_bucket.grant_read(oai)

        cache_policy = cloudfront.CachePolicy.CACHING_OPTIMIZED

        distribution = cloudfront.Distribution(
            self,
            "CloudFrontDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3Origin(
                    self.s3_bucket,
                    origin_access_identity=oai,
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS,
                compress=True,
                cache_policy=cache_policy,
            ),
            additional_behaviors={
                "static/css/*": cloudfront.BehaviorOptions(
                    origin=origins.S3Origin(
                        self.s3_bucket,
                        origin_access_identity=oai,
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    compress=True,
                    cache_policy=cache_policy,
                ),
                "static/js/*": cloudfront.BehaviorOptions(
                    origin=origins.S3Origin(
                        self.s3_bucket,
                        origin_access_identity=oai,
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    compress=True,
                    cache_policy=cache_policy,
                ),
                "data/*": cloudfront.BehaviorOptions(
                    origin=origins.S3Origin(
                        self.s3_bucket,
                        origin_access_identity=oai,
                    ),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    compress=True,
                    cache_policy=cache_policy,
                ),
            },
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            comment=f"Flight Matrix Static Files CDN - {self.env_name}",
        )

        return distribution

    def _get_lambda_environment(self, db_url: str) -> dict:
        """Get Lambda environment variables including Cognito config.

        Args:
            db_url: Database connection URL

        Returns:
            Dictionary of environment variables for Lambda
        """
        env_vars = {
            "DATABASE_URL": db_url,
            "S3_BUCKET_NAME": self.s3_bucket.bucket_name,
            "CLOUDFRONT_DOMAIN": self.cloudfront_domain,
            "CONFIG_PATH": "/var/task/config.yaml",
            "STAGE": self.env_name,
        }

        # Add Cognito configuration if provided
        if self._cognito_user_pool_id:
            env_vars["COGNITO_USER_POOL_ID"] = self._cognito_user_pool_id
        if self._cognito_client_id:
            env_vars["COGNITO_CLIENT_ID"] = self._cognito_client_id
        if self._cognito_client_secret:
            env_vars["COGNITO_CLIENT_SECRET"] = self._cognito_client_secret
        if self._cognito_domain:
            env_vars["COGNITO_DOMAIN"] = self._cognito_domain
        if self._cognito_callback_url:
            env_vars["COGNITO_CALLBACK_URL"] = self._cognito_callback_url
        if self._cognito_logout_url:
            env_vars["COGNITO_LOGOUT_URL"] = self._cognito_logout_url
        if self._cognito_jwks:
            env_vars["COGNITO_JWKS"] = self._cognito_jwks
        if self._flask_secret_key:
            env_vars["FLASK_SECRET_KEY"] = self._flask_secret_key
        if self._app_domain:
            env_vars["APP_DOMAIN"] = self._app_domain

        return env_vars

    def _create_lambda(self) -> lambda_.DockerImageFunction:
        """Create Lambda function using Docker image."""
        # Create new security group for Lambda
        # Note: allow_all_outbound=True is needed for DNS resolution within VPC
        # Lambda is in isolated subnet so it has no internet access anyway
        self.lambda_security_group = ec2.SecurityGroup(
            self,
            "LambdaSecurityGroup",
            vpc=self.vpc,
            description="Security group for Lambda function",
            allow_all_outbound=True,
        )

        # Add ingress rule to DB security group
        self.db_security_group.add_ingress_rule(
            peer=self.lambda_security_group,
            connection=ec2.Port.tcp(5432),
            description="Allow Lambda to connect to RDS",
        )

        # Database connection string
        db_url = (
            f"postgresql+psycopg2://{self.db_username}:{self.db_password}"
            f"@{self.db_endpoint}:5432/{self.db_name}"
        )

        # Determine subnet selection
        # Use private subnets (with NAT gateway) for Lambda - needed for DNS resolution
        if self._private_subnets:
            # Import mode: use specific private subnets
            vpc_subnets = ec2.SubnetSelection(
                subnets=[
                    ec2.Subnet.from_subnet_id(self, f"PrivateSubnet{i}", subnet_id)
                    for i, subnet_id in enumerate(self._private_subnets)
                ]
            )
        else:
            # New mode: use private subnet type (with NAT egress)
            vpc_subnets = ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            )

        function = lambda_.DockerImageFunction(
            self,
            "FlightMatrixFunction",
            function_name=f"flight-matrix-unified-{self.env_name}",
            code=lambda_.DockerImageCode.from_image_asset(
                directory=".",
                file="Dockerfile.lambda",
                exclude=[
                    ".git",
                    ".venv",
                    ".venv-cdk",
                    "cdk.out",
                    "infra/scraper/cdk.out",
                    "__pycache__",
                    "**/__pycache__",
                    "*.pyc",
                    "*.log",
                    "*.db",
                    "*.dump",
                    "*.mhtml",
                    "*.png",
                    "nohup*.out",
                    "layers",
                    "tests",
                    "data/jetphotos_images",
                    "data/jetphotos_thumbnails",
                ],
            ),
            memory_size=2048,
            timeout=Duration.seconds(30),
            ephemeral_storage_size=Size.mebibytes(1024),
            vpc=self.vpc,
            vpc_subnets=vpc_subnets,
            security_groups=[self.lambda_security_group],
            environment=self._get_lambda_environment(db_url),
            log_retention=logs.RetentionDays.ONE_WEEK,
            description="Flight Matrix Aircraft Tracking API (Docker)",
        )

        self.s3_bucket.grant_read(function)

        return function

    def _create_api_gateway(self) -> apigwv2.HttpApi:
        """Create API Gateway HTTP API."""
        api = apigwv2.HttpApi(
            self,
            "HttpApi",
            api_name=f"flight-matrix-unified-api-{self.env_name}",
            description=f"Flight Matrix Unified API - {self.env_name}",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.PUT,
                    apigwv2.CorsHttpMethod.DELETE,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["*"],
                max_age=Duration.seconds(600),
            ),
        )

        integration = apigwv2_integrations.HttpLambdaIntegration(
            "LambdaIntegration",
            self.lambda_function,
        )

        api.add_routes(
            path="/{proxy+}",
            methods=[apigwv2.HttpMethod.ANY],
            integration=integration,
        )

        api.add_routes(
            path="/",
            methods=[apigwv2.HttpMethod.GET],
            integration=integration,
        )

        return api

    def _create_scraper_workers(self) -> autoscaling.AutoScalingGroup:
        """Create EC2 Auto Scaling Group for scraper workers."""
        project_root = Path(__file__).parent.parent
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

        image_uri = docker_image.image_uri

        # Create new security group for workers
        self.worker_security_group = ec2.SecurityGroup(
            self,
            "WorkerSecurityGroup",
            vpc=self.vpc,
            description="Security group for scraper workers",
            allow_all_outbound=True,
        )

        # Allow workers to access RDS
        self.db_security_group.add_ingress_rule(
            peer=self.worker_security_group,
            connection=ec2.Port.tcp(5432),
            description="Allow scraper workers to access Aurora",
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

        # S3 permissions
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:PutObject",
                    "s3:GetObject",
                    "s3:ListBucket",
                ],
                resources=[
                    self.s3_bucket.bucket_arn,
                    f"{self.s3_bucket.bucket_arn}/*",
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

        # User data script
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "#!/bin/bash",
            "set -ex",
            "",
            "exec > >(tee /var/log/user-data.log) 2>&1",
            "",
            "echo '=== Starting Docker Worker Setup ==='",
            "",
            "apt-get update",
            "apt-get install -y docker.io unzip curl",
            "systemctl enable docker",
            "systemctl start docker",
            "usermod -aG docker ubuntu",
            "",
            'curl -s "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"',
            "unzip -q awscliv2.zip",
            "./aws/install",
            "rm -rf aws awscliv2.zip",
            "",
            f"aws ecr get-login-password --region {self.region} | docker login --username AWS --password-stdin {self.account}.dkr.ecr.{self.region}.amazonaws.com",
            "",
            f"docker pull {image_uri}",
            "",
            f'echo "AWS_DEFAULT_REGION={self.region}" > /etc/default/scraper-worker',
            f'echo "ECR_IMAGE={image_uri}" >> /etc/default/scraper-worker',
            'echo "USE_IAM_AUTH=false" >> /etc/default/scraper-worker',
            f'echo "DB_HOST={self.db_endpoint}" >> /etc/default/scraper-worker',
            "",
            f"cat > /etc/systemd/system/scraper-worker.service << 'EOFSERVICE'",
            "[Unit]",
            "Description=Flight Matrix Scraper Worker (Docker)",
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
            f"ExecStartPre=/bin/bash -c 'aws ecr get-login-password --region {self.region} | docker login --username AWS --password-stdin {self.account}.dkr.ecr.{self.region}.amazonaws.com'",
            "ExecStartPre=/usr/bin/docker pull ${ECR_IMAGE}",
            'ExecStart=/usr/bin/docker run --name scraper-worker --rm --shm-size=2g --network=host -e USE_IAM_AUTH=${USE_IAM_AUTH} -e DB_HOST=${DB_HOST} -e AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION} ${ECR_IMAGE}',
            "ExecStop=/usr/bin/docker stop scraper-worker",
            "StandardOutput=append:/var/log/scraper-worker/worker.log",
            "StandardError=append:/var/log/scraper-worker/worker.log",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "EOFSERVICE",
            "",
            "mkdir -p /var/log/scraper-worker",
            "chown ubuntu:ubuntu /var/log/scraper-worker",
            "",
            "systemctl daemon-reload",
            "systemctl enable scraper-worker",
            "systemctl start scraper-worker",
            "",
            "echo '=== Docker Worker Setup Complete ==='",
        )

        # Determine subnet selection for workers
        if self._public_subnets:
            vpc_subnets = ec2.SubnetSelection(
                subnets=[
                    ec2.Subnet.from_subnet_id(self, f"PublicSubnet{i}", subnet_id)
                    for i, subnet_id in enumerate(self._public_subnets)
                ]
            )
        else:
            vpc_subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC)

        # Launch template
        launch_template = ec2.LaunchTemplate(
            self,
            "WorkerLaunchTemplate",
            instance_type=ec2.InstanceType(self.scraper_instance_type),
            machine_image=ec2.MachineImage.generic_linux(
                {
                    "us-west-2": "ami-0cf2b4e024cdb6960",
                    "us-east-1": "ami-0c7217cdde317cfec",
                    "eu-west-1": "ami-0905a3c97561e0b69",
                }
            ),
            security_group=self.worker_security_group,
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
            vpc=self.vpc,
            launch_template=launch_template,
            min_capacity=self.scraper_min_capacity,
            max_capacity=self.scraper_max_capacity,
            desired_capacity=self.scraper_desired_capacity,
            vpc_subnets=vpc_subnets,
            health_check=autoscaling.HealthCheck.ec2(
                grace=Duration.minutes(5),
            ),
            update_policy=autoscaling.UpdatePolicy.rolling_update(
                max_batch_size=1,
                min_instances_in_service=1,
            ),
        )

        Tags.of(asg).add("Name", f"scraper-worker-{self.env_name}")
        Tags.of(asg).add("Project", "flight-matrix")
        Tags.of(asg).add("Component", "scraper")

        self.asg_name = asg.auto_scaling_group_name
        self.docker_image_uri = image_uri

        return asg

    def _create_cognito_resources(self) -> None:
        """Create Cognito User Pool, App Client, and Domain."""
        # Create User Pool
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"flight-matrix-users-{self.env_name}",
            sign_in_aliases=cognito.SignInAliases(
                email=True,
                username=False,
            ),
            self_sign_up_enabled=False,
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
                temp_password_validity=Duration.days(7),
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        Tags.of(self.user_pool).add("Project", "FlightMatrix")
        Tags.of(self.user_pool).add("Environment", self.env_name)

        # Create Cognito Groups for access control
        self._create_cognito_groups()

        # Create Domain
        self.user_pool_domain = self.user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=self._cognito_domain_prefix,
            ),
        )

        # Get API Gateway URL for callback
        api_url = self.api_gateway.url or ""
        callback_url = f"{api_url}auth/callback"
        logout_url = f"{api_url}flight-schedules"

        # Create App Client
        self.app_client = self.user_pool.add_client(
            "AppClient",
            user_pool_client_name=f"flight-matrix-web-{self.env_name}",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(
                    authorization_code_grant=True,
                    implicit_code_grant=False,
                ),
                scopes=[
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=[callback_url],
                logout_urls=[logout_url],
            ),
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
            prevent_user_existence_errors=True,
            auth_flows=cognito.AuthFlow(
                user_srp=True,
                user_password=False,
                admin_user_password=False,
            ),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO,
            ],
        )

        # Store Cognito config for Lambda
        self._cognito_user_pool_id = self.user_pool.user_pool_id
        self._cognito_client_id = self.app_client.user_pool_client_id
        self._cognito_domain = f"{self._cognito_domain_prefix}.auth.{self.region}.amazoncognito.com"
        self._cognito_callback_url = callback_url
        self._cognito_logout_url = logout_url

    def _create_cognito_groups(self) -> None:
        """Create Cognito Groups for access control.

        Groups:
        - admins: Full access to all protected resources
        - flight-schedules-viewers: Access to /flight-schedules page
        """
        # Admin group - full access
        cognito.CfnUserPoolGroup(
            self,
            "AdminsGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="admins",
            description="Administrators with full access",
            precedence=1,
        )

        # Flight schedules viewers group
        cognito.CfnUserPoolGroup(
            self,
            "FlightSchedulesViewersGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="flight-schedules-viewers",
            description="Users who can view flight schedules",
            precedence=10,
        )

    def _update_lambda_cognito_env(self) -> None:
        """Update Lambda environment variables with Cognito configuration."""
        # We need to use CfnFunction to update environment variables
        # because the L2 construct doesn't expose this after creation
        cfn_function = self.lambda_function.node.default_child

        # Get current environment
        current_env = cfn_function.environment or {}
        variables = current_env.get("variables", {}) if isinstance(current_env, dict) else {}

        # Add Cognito variables
        variables["COGNITO_USER_POOL_ID"] = self.user_pool.user_pool_id
        variables["COGNITO_CLIENT_ID"] = self.app_client.user_pool_client_id
        # Client secret requires a custom resource to retrieve
        variables["COGNITO_DOMAIN"] = self._cognito_domain
        variables["COGNITO_CALLBACK_URL"] = self._cognito_callback_url
        variables["COGNITO_LOGOUT_URL"] = self._cognito_logout_url
        variables["FLASK_SECRET_KEY"] = self._flask_secret_key

        # Update the function
        cfn_function.add_property_override("Environment.Variables", variables)

    def _create_outputs(self) -> None:
        """Create CloudFormation outputs (without exports to avoid conflicts)."""
        CfnOutput(
            self,
            "ApiUrl",
            value=self.api_gateway.url or "",
            description="API Gateway HTTP API URL",
        )

        CfnOutput(
            self,
            "CloudFrontURL",
            value=f"https://{self.cloudfront_domain}",
            description="CloudFront Distribution URL",
        )

        CfnOutput(
            self,
            "CloudFrontDistributionId",
            value=self.cloudfront_distribution_id,
            description="CloudFront Distribution ID",
        )

        CfnOutput(
            self,
            "DatabaseEndpoint",
            value=self.db_endpoint,
            description="Aurora PostgreSQL cluster endpoint",
        )

        CfnOutput(
            self,
            "S3BucketName",
            value=self.s3_bucket.bucket_name,
            description="S3 bucket for static files",
        )

        CfnOutput(
            self,
            "LambdaFunctionArn",
            value=self.lambda_function.function_arn,
            description="Lambda function ARN",
        )

        CfnOutput(
            self,
            "VPCId",
            value=self.vpc.vpc_id,
            description="VPC ID",
        )

        CfnOutput(
            self,
            "ASGName",
            value=self.scraper_asg.auto_scaling_group_name,
            description="Scraper Worker ASG Name",
        )

        CfnOutput(
            self,
            "ScraperImageUri",
            value=self.docker_image_uri,
            description="Scraper Docker Image URI",
        )

        # Cognito outputs (if created)
        if self._enable_cognito_auth and hasattr(self, "user_pool"):
            CfnOutput(
                self,
                "CognitoUserPoolId",
                value=self.user_pool.user_pool_id,
                description="Cognito User Pool ID",
            )

            CfnOutput(
                self,
                "CognitoClientId",
                value=self.app_client.user_pool_client_id,
                description="Cognito App Client ID",
            )

            CfnOutput(
                self,
                "CognitoDomain",
                value=self._cognito_domain,
                description="Cognito Domain",
            )

            CfnOutput(
                self,
                "CognitoLoginUrl",
                value=f"https://{self._cognito_domain}/login?client_id={self.app_client.user_pool_client_id}&response_type=code&scope=email+openid+profile&redirect_uri={self._cognito_callback_url}",
                description="Cognito Hosted UI Login URL",
            )
