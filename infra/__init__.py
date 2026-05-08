# AWS CDK Infrastructure for Flight Matrix
#
# This package contains modular CDK stacks for Flight Matrix infrastructure:
# - NetworkStack: VPC, NAT Gateway, Security Groups
# - DatabaseStack: Aurora Serverless v2 PostgreSQL
# - StorageStack: S3 bucket, CloudFront CDN
# - ComputeStack: EC2 ASG for Scraper workers

from infra.stacks import (
    ComputeStack,
    DatabaseStack,
    NetworkStack,
    StorageStack,
)

__all__ = [
    "NetworkStack",
    "DatabaseStack",
    "StorageStack",
    "ComputeStack",
]
