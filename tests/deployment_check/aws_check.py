"""AWS services health checks.

This module verifies AWS service connectivity and permissions for
S3, SES, and Bedrock services used by flight-matrix.
"""

import logging
import time
import uuid
from typing import Any

from tests.deployment_check.base import BaseHealthCheck, CheckResult, CheckStatus

logger = logging.getLogger("deployment_check.aws")


class AWSHealthCheck(BaseHealthCheck):
    """Health checks for AWS services.

    Checks performed:
    - AWS credentials validity (STS GetCallerIdentity)
    - S3 bucket access
    - S3 write/read/delete cycle
    - SES sender identity verification
    - SES send quota
    - Bedrock model access
    - Bedrock invoke test
    """

    category = "AWS Services"

    def __init__(self, config: Any | None = None) -> None:
        """Initialize AWS health check.

        Args:
            config: YAMLConfig instance for accessing AWS configuration.
        """
        super().__init__(config)
        self._session: Any | None = None

    async def run(self) -> list[CheckResult]:
        """Execute all AWS checks.

        Returns:
            List of CheckResult objects for each AWS check.
        """
        results: list[CheckResult] = []

        # Check AWS credentials
        creds_result = await self._check_aws_credentials()
        results.append(creds_result)

        if creds_result.status == CheckStatus.FAIL:
            return results

        # S3 checks
        results.extend(await self._check_s3())

        # SES checks
        results.extend(await self._check_ses())

        # Bedrock checks
        results.extend(await self._check_bedrock())

        return results

    async def _check_aws_credentials(self) -> CheckResult:
        """Check AWS credentials validity using STS GetCallerIdentity.

        Returns:
            CheckResult indicating if AWS credentials are valid.
        """
        start_time = time.perf_counter()

        try:
            import boto3
            from botocore.exceptions import ClientError, NoCredentialsError

            self._session = boto3.Session()
            sts_client = self._session.client("sts")

            response = sts_client.get_caller_identity()
            account_id = response.get("Account", "unknown")
            arn = response.get("Arn", "unknown")

            # Mask ARN for security
            if "/" in arn:
                masked_arn = arn.rsplit("/", 1)[0] + "/***"
            else:
                masked_arn = arn

            return self._pass(
                "AWS credentials",
                f"Valid (Account: {account_id})",
                start_time,
                {"account_id": account_id, "arn": masked_arn},
            )

        except NoCredentialsError:
            return self._fail(
                "AWS credentials",
                "No credentials found",
                start_time,
            )
        except ClientError as e:
            return self._fail(
                "AWS credentials",
                f"Invalid credentials: {e.response['Error']['Message']}",
                start_time,
            )
        except ImportError:
            return self._fail(
                "AWS credentials",
                "boto3 not installed",
                start_time,
            )
        except Exception as e:
            return self._fail(
                "AWS credentials",
                f"Error: {e}",
                start_time,
            )

    async def _check_s3(self) -> list[CheckResult]:
        """Check S3 bucket access and operations.

        Returns:
            List of CheckResult objects for S3 checks.
        """
        results: list[CheckResult] = []

        if not self.config or not self._session:
            return results

        # Get S3 bucket from config
        aws_config = self.config.get_aws_config()
        bucket_name = aws_config.get("s3_bucket")

        if not bucket_name:
            start_time = time.perf_counter()
            results.append(
                self._skip(
                    "S3 bucket access",
                    "No S3 bucket configured",
                    start_time,
                )
            )
            return results

        # Check bucket access
        results.append(await self._check_s3_bucket_access(bucket_name))

        # Check write/read/delete if bucket accessible
        if results[-1].status == CheckStatus.PASS:
            results.append(await self._check_s3_write_read_delete(bucket_name))

        return results

    async def _check_s3_bucket_access(self, bucket_name: str) -> CheckResult:
        """Check S3 bucket exists and is accessible.

        Args:
            bucket_name: Name of the S3 bucket to check.

        Returns:
            CheckResult indicating if bucket is accessible.
        """
        start_time = time.perf_counter()

        try:
            from botocore.exceptions import ClientError

            s3_client = self._session.client("s3")
            s3_client.head_bucket(Bucket=bucket_name)

            return self._pass(
                "S3 bucket access",
                f"Bucket accessible: {bucket_name}",
                start_time,
                {"bucket": bucket_name},
            )

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "404":
                return self._fail(
                    "S3 bucket access",
                    f"Bucket not found: {bucket_name}",
                    start_time,
                )
            elif error_code == "403":
                return self._fail(
                    "S3 bucket access",
                    f"Access denied to bucket: {bucket_name}",
                    start_time,
                )
            return self._fail(
                "S3 bucket access",
                f"Error: {e.response['Error']['Message']}",
                start_time,
            )
        except Exception as e:
            return self._fail(
                "S3 bucket access",
                f"Error: {e}",
                start_time,
            )

    async def _check_s3_write_read_delete(self, bucket_name: str) -> CheckResult:
        """Check S3 write/read/delete operations.

        Args:
            bucket_name: Name of the S3 bucket to test.

        Returns:
            CheckResult indicating if CRUD operations succeed.
        """
        start_time = time.perf_counter()
        test_key = f"health-check/test-{uuid.uuid4().hex}.txt"
        test_content = b"flight-matrix health check test"

        try:
            from botocore.exceptions import ClientError

            s3_client = self._session.client("s3")

            # Write
            s3_client.put_object(
                Bucket=bucket_name,
                Key=test_key,
                Body=test_content,
            )

            # Read
            response = s3_client.get_object(Bucket=bucket_name, Key=test_key)
            read_content = response["Body"].read()

            if read_content != test_content:
                return self._fail(
                    "S3 write/read/delete",
                    "Content mismatch after read",
                    start_time,
                )

            # Delete
            s3_client.delete_object(Bucket=bucket_name, Key=test_key)

            return self._pass(
                "S3 write/read/delete",
                "CRUD cycle completed",
                start_time,
            )

        except ClientError as e:
            # Cleanup attempt
            try:
                s3_client.delete_object(Bucket=bucket_name, Key=test_key)
            except Exception:
                pass

            return self._fail(
                "S3 write/read/delete",
                f"Operation failed: {e.response['Error']['Message']}",
                start_time,
            )
        except Exception as e:
            return self._fail(
                "S3 write/read/delete",
                f"Error: {e}",
                start_time,
            )

    async def _check_ses(self) -> list[CheckResult]:
        """Check SES configuration and quotas.

        Returns:
            List of CheckResult objects for SES checks.
        """
        results: list[CheckResult] = []

        if not self.config or not self._session:
            return results

        email_config = self.config.get_email_config()

        # Only check SES if provider is aws_ses
        if email_config.get("provider") != "aws_ses":
            start_time = time.perf_counter()
            results.append(
                self._skip(
                    "SES identity verified",
                    "Email provider is not AWS SES",
                    start_time,
                )
            )
            results.append(
                self._skip(
                    "SES send quota",
                    "Email provider is not AWS SES",
                    start_time,
                )
            )
            return results

        from_address = email_config.get("from_address")

        # Check identity verification
        results.append(await self._check_ses_identity(from_address))

        # Check send quota
        results.append(await self._check_ses_quota())

        return results

    async def _check_ses_identity(self, from_address: str | None) -> CheckResult:
        """Check SES sender identity is verified.

        Args:
            from_address: Email address or domain to check.

        Returns:
            CheckResult indicating if identity is verified.
        """
        start_time = time.perf_counter()

        if not from_address:
            return self._fail(
                "SES identity verified",
                "No from_address configured",
                start_time,
            )

        try:
            from botocore.exceptions import ClientError

            ses_client = self._session.client("ses")

            # Check email identity
            response = ses_client.get_identity_verification_attributes(Identities=[from_address])

            attributes = response.get("VerificationAttributes", {})
            identity_attr = attributes.get(from_address, {})
            status = identity_attr.get("VerificationStatus")

            if status == "Success":
                return self._pass(
                    "SES identity verified",
                    f"Identity verified: {from_address}",
                    start_time,
                )
            elif status:
                return self._fail(
                    "SES identity verified",
                    f"Identity status: {status}",
                    start_time,
                )

            # Check domain if email not verified
            domain = from_address.split("@")[-1] if "@" in from_address else from_address
            response = ses_client.get_identity_verification_attributes(Identities=[domain])
            attributes = response.get("VerificationAttributes", {})
            domain_attr = attributes.get(domain, {})
            domain_status = domain_attr.get("VerificationStatus")

            if domain_status == "Success":
                return self._pass(
                    "SES identity verified",
                    f"Domain verified: {domain}",
                    start_time,
                )

            return self._fail(
                "SES identity verified",
                f"Neither email nor domain verified for: {from_address}",
                start_time,
            )

        except ClientError as e:
            return self._fail(
                "SES identity verified",
                f"Error: {e.response['Error']['Message']}",
                start_time,
            )
        except Exception as e:
            return self._fail(
                "SES identity verified",
                f"Error: {e}",
                start_time,
            )

    async def _check_ses_quota(self) -> CheckResult:
        """Check SES sending quota.

        Returns:
            CheckResult indicating if SES quota is available.
        """
        start_time = time.perf_counter()

        try:
            from botocore.exceptions import ClientError

            ses_client = self._session.client("ses")
            response = ses_client.get_send_quota()

            max_24hr = response.get("Max24HourSend", 0)
            sent_24hr = response.get("SentLast24Hours", 0)
            remaining = max_24hr - sent_24hr

            if remaining > 0:
                return self._pass(
                    "SES send quota",
                    f"Available: {int(remaining)}/{int(max_24hr)}",
                    start_time,
                    {
                        "max_24hr": max_24hr,
                        "sent_24hr": sent_24hr,
                        "remaining": remaining,
                    },
                )
            else:
                return self._warn(
                    "SES send quota",
                    "Quota exhausted for 24-hour period",
                    start_time,
                )

        except ClientError as e:
            return self._fail(
                "SES send quota",
                f"Error: {e.response['Error']['Message']}",
                start_time,
            )
        except Exception as e:
            return self._fail(
                "SES send quota",
                f"Error: {e}",
                start_time,
            )

    async def _check_bedrock(self) -> list[CheckResult]:
        """Check Bedrock model access and invocation.

        Returns:
            List of CheckResult objects for Bedrock checks.
        """
        results: list[CheckResult] = []

        if not self.config or not self._session:
            return results

        llm_config = self.config.get_llm_config()

        # Only check Bedrock if provider is aws_bedrock
        if llm_config.get("provider") != "aws_bedrock":
            start_time = time.perf_counter()
            results.append(
                self._skip(
                    "Bedrock model access",
                    "LLM provider is not AWS Bedrock",
                    start_time,
                )
            )
            results.append(
                self._skip(
                    "Bedrock invoke test",
                    "LLM provider is not AWS Bedrock",
                    start_time,
                )
            )
            return results

        # Check both possible config keys for model ID
        model_id = llm_config.get("bedrock_model_id") or llm_config.get(
            "model", "anthropic.claude-3-sonnet-20240229-v1:0"
        )

        # Check model access
        results.append(await self._check_bedrock_model_access(model_id))

        # Check invoke if model accessible
        if results[-1].status == CheckStatus.PASS:
            results.append(await self._check_bedrock_invoke(model_id))

        return results

    async def _check_bedrock_model_access(self, model_id: str) -> CheckResult:
        """Check if Bedrock model is accessible.

        Args:
            model_id: Bedrock model ID to check.

        Returns:
            CheckResult indicating if model is accessible.
        """
        start_time = time.perf_counter()

        # Cross-region inference models (e.g., us.anthropic.claude-*) don't work
        # with get_foundation_model API. Skip to invoke test for these.
        if model_id.startswith(("us.", "eu.", "ap.")):
            return self._pass(
                "Bedrock model access",
                f"Cross-region model configured: {model_id}",
                start_time,
                {"model_id": model_id, "type": "cross-region"},
            )

        try:
            from botocore.exceptions import ClientError

            bedrock_client = self._session.client("bedrock")

            # Try to get model info
            # Extract base model ID (remove version suffix if present)
            base_model = model_id.split(":")[0] if ":" in model_id else model_id

            response = bedrock_client.get_foundation_model(modelIdentifier=base_model)
            model_name = response.get("modelDetails", {}).get("modelName", model_id)

            return self._pass(
                "Bedrock model access",
                f"Model available: {model_name}",
                start_time,
                {"model_id": model_id, "model_name": model_name},
            )

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "ResourceNotFoundException":
                return self._fail(
                    "Bedrock model access",
                    f"Model not found: {model_id}",
                    start_time,
                )
            elif error_code == "AccessDeniedException":
                return self._fail(
                    "Bedrock model access",
                    f"Access denied to model: {model_id}",
                    start_time,
                )
            return self._fail(
                "Bedrock model access",
                f"Error: {e.response['Error']['Message']}",
                start_time,
            )
        except Exception as e:
            return self._fail(
                "Bedrock model access",
                f"Error: {e}",
                start_time,
            )

    async def _check_bedrock_invoke(self, model_id: str) -> CheckResult:
        """Test Bedrock model invocation with minimal prompt.

        Args:
            model_id: Bedrock model ID to invoke.

        Returns:
            CheckResult indicating if model invocation succeeds.
        """
        start_time = time.perf_counter()

        try:
            import json

            from botocore.exceptions import ClientError

            bedrock_runtime = self._session.client("bedrock-runtime")

            # Minimal test prompt
            body = json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 10,
                    "messages": [
                        {
                            "role": "user",
                            "content": "Reply with only: OK",
                        }
                    ],
                }
            )

            response = bedrock_runtime.invoke_model(
                modelId=model_id,
                body=body,
                contentType="application/json",
                accept="application/json",
            )

            response_body = json.loads(response["body"].read())

            # Check for successful response
            if response_body.get("content"):
                return self._pass(
                    "Bedrock invoke test",
                    "Model responded successfully",
                    start_time,
                    {"model_id": model_id},
                )

            return self._fail(
                "Bedrock invoke test",
                "Empty response from model",
                start_time,
            )

        except ClientError as e:
            return self._fail(
                "Bedrock invoke test",
                f"Invoke failed: {e.response['Error']['Message']}",
                start_time,
            )
        except Exception as e:
            return self._fail(
                "Bedrock invoke test",
                f"Error: {e}",
                start_time,
            )
