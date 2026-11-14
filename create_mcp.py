#!/usr/bin/env python3
"""Deploy MCP/use-vault as an AgentCore Runtime (MCP protocol, IAM auth).

Unlike harness-work (Gateway + Runtime target), this script only creates the
Runtime MCP endpoint. Other apps connect with SigV4 to the Runtime URL.

Usage:
  python create_mcp.py
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets as py_secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ob-note-create-mcp")

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
MCP_DIR = ROOT / "MCP" / "use-vault"
MCP_CONFIG_PATH = MCP_DIR / "config.json"
VAULT_AGENT_SECRET = "ob-note/vault-agent-token"


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        raise SystemExit(f"Missing {CONFIG_PATH} — run installer.py first")
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("config.json must be an object")
    return data


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _project_name(cfg: dict[str, Any]) -> str:
    return (cfg.get("projectName") or "ob-note").strip() or "ob-note"


def _region(cfg: dict[str, Any]) -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or cfg.get("region")
        or "us-west-2"
    )


def _sharing_url(cfg: dict[str, Any]) -> str:
    return (
        (cfg.get("sharing_url") or "").strip()
        or (
            f"https://{(cfg.get('custom_domain') or 'vault.my-agentic-ai.click').strip()}"
        )
    ).rstrip("/")


def runtime_name(project: str) -> str:
    """ECR / Agent Runtime name: use_vault_of_{project} (hyphens → underscores)."""
    return f"use_vault_of_{project}".replace("-", "_")


def mcp_runtime_url(agent_runtime_arn: str, region: str) -> str:
    """Streamable-HTTP MCP endpoint for an IAM-auth AgentCore Runtime."""
    encoded = agent_runtime_arn.replace(":", "%3A").replace("/", "%2F")
    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{encoded}/invocations?qualifier=DEFAULT"
    )


def ensure_vault_agent_token(sm, project: str) -> str:
    """Create or reuse vault-agent-token; return secret ARN."""
    secret_id = f"{project}/vault-agent-token"
    try:
        return sm.describe_secret(SecretId=secret_id)["ARN"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    value = py_secrets.token_urlsafe(32)
    resp = sm.create_secret(
        Name=secret_id,
        SecretString=value,
        Description="HMAC token for use-vault MCP → ob-note vault API auth",
        Tags=[
            {"Key": "Name", "Value": secret_id},
            {"Key": "Project", "Value": project},
        ],
    )
    logger.info("Created secret %s", secret_id)
    return resp["ARN"]


def create_iam_role(
    iam,
    role_name: str,
    assume_role_policy: dict,
    description: str,
) -> tuple[str, bool]:
    try:
        response = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            Description=description,
        )
        logger.info("Created IAM role %s", role_name)
        return response["Role"]["Arn"], True
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        logger.info("IAM role exists: %s", role_name)
        response = iam.get_role(RoleName=role_name)
        iam.update_assume_role_policy(
            RoleName=role_name,
            PolicyDocument=json.dumps(assume_role_policy),
        )
        return response["Role"]["Arn"], False


def attach_inline_policy(iam, role_name: str, policy_name: str, policy: dict) -> None:
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=policy_name[:128],
        PolicyDocument=json.dumps(policy),
    )


def create_use_vault_mcp_role(
    iam,
    *,
    account_id: str,
    region: str,
    project: str,
    s3_bucket: str,
) -> str:
    role_name = f"role-use-vault-mcp-for-{project}-{region}"
    if len(role_name) > 64:
        role_name = f"role-uv-mcp-{project[:20]}-{region}"[:64]

    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            },
            {
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                "Action": "sts:AssumeRole",
            },
        ],
    }
    role_arn, _ = create_iam_role(
        iam,
        role_name,
        assume_role_policy,
        description="Execution role for use-vault MCP AgentCore Runtime",
    )

    secret_arn = (
        f"arn:aws:secretsmanager:{region}:{account_id}:secret:{project}/vault-agent-token*"
    )
    statements: list[dict[str, Any]] = [
        {
            "Sid": "SecretsVaultAgentToken",
            "Effect": "Allow",
            "Action": [
                "secretsmanager:GetSecretValue",
                "secretsmanager:DescribeSecret",
            ],
            "Resource": [secret_arn],
        },
        {
            "Sid": "EcrPull",
            "Effect": "Allow",
            "Action": [
                "ecr:GetAuthorizationToken",
                "ecr:BatchGetImage",
                "ecr:GetDownloadUrlForLayer",
                "ecr:BatchCheckLayerAvailability",
                "ecr:DescribeImages",
                "ecr:DescribeRepositories",
            ],
            "Resource": ["*"],
        },
        {
            "Sid": "CloudWatchLogs",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "logs:DescribeLogGroups",
                "logs:DescribeLogStreams",
            ],
            "Resource": [
                f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/*",
                f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/*:log-stream:*",
            ],
        },
        {
            "Sid": "CloudWatchMetrics",
            "Effect": "Allow",
            "Action": [
                "cloudwatch:PutMetricData",
                "xray:PutTraceSegments",
                "xray:PutTelemetryRecords",
            ],
            "Resource": ["*"],
        },
    ]
    if s3_bucket:
        statements.append(
            {
                "Sid": "OptionalConfigRead",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [
                    f"arn:aws:s3:::{s3_bucket}",
                    f"arn:aws:s3:::{s3_bucket}/skills/*",
                ],
            }
        )

    attach_inline_policy(
        iam,
        role_name,
        f"use-vault-mcp-inline-for-{project}"[:128],
        {"Version": "2012-10-17", "Statement": statements},
    )
    logger.info("use-vault MCP Runtime role ready: %s", role_arn)
    return role_arn


def ensure_ecr_repository(ecr, repository_name: str) -> None:
    try:
        ecr.describe_repositories(repositoryNames=[repository_name])
        logger.info("ECR repository exists: %s", repository_name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "RepositoryNotFoundException":
            raise
        logger.info("Creating ECR repository: %s", repository_name)
        ecr.create_repository(repositoryName=repository_name)


def docker_ecr_login(ecr, account_id: str, region: str) -> None:
    token = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    username, password = base64.b64decode(token).decode("utf-8").split(":")
    registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"
    process = subprocess.Popen(
        ["docker", "login", "--username", username, "--password-stdin", registry],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _, stderr = process.communicate(input=password)
    if process.returncode != 0:
        raise RuntimeError(f"Docker ECR login failed: {stderr}")


def run_docker(cmd: list[str], description: str) -> None:
    logger.info("%s: %s", description, " ".join(cmd))
    subprocess.run(cmd, check=True)


def push_use_vault_mcp_image(
    *,
    account_id: str,
    region: str,
    project: str,
) -> tuple[str, str]:
    if not shutil.which("docker"):
        raise RuntimeError("docker is required to build the use-vault MCP image")
    if not MCP_DIR.is_dir():
        raise RuntimeError(f"MCP directory not found: {MCP_DIR}")

    ecr = boto3.client("ecr", region_name=region)
    repository = runtime_name(project)
    image_tag = datetime.now().strftime("%Y%m%d%H%M%S")
    local_tag = f"{repository}:{image_tag}"
    ecr_uri = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repository}:{image_tag}"

    ensure_ecr_repository(ecr, repository)
    docker_ecr_login(ecr, account_id, region)
    run_docker(
        [
            "docker",
            "build",
            "--no-cache",
            "--platform",
            "linux/arm64",
            "--provenance=false",
            "--sbom=false",
            "-t",
            local_tag,
            str(MCP_DIR),
        ],
        "Building Docker image",
    )
    run_docker(["docker", "tag", local_tag, ecr_uri], "Tagging for ECR")
    run_docker(["docker", "push", ecr_uri], "Pushing to ECR")
    logger.info("Pushed use-vault MCP image: %s", ecr_uri)
    return repository, image_tag


def find_agent_runtime_by_name(control, name: str) -> Optional[dict]:
    next_token = None
    while True:
        kwargs: dict[str, Any] = {}
        if next_token:
            kwargs["nextToken"] = next_token
        response = control.list_agent_runtimes(**kwargs)
        for item in response.get("agentRuntimes", []):
            if item.get("agentRuntimeName") == name:
                return item
        next_token = response.get("nextToken")
        if not next_token:
            return None


def create_or_update_use_vault_mcp_runtime(
    control,
    *,
    account_id: str,
    region: str,
    project: str,
    role_arn: str,
    repository: str,
    image_tag: str,
    ob_docs_url: str,
) -> dict[str, str]:
    name = repository
    container_uri = (
        f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repository}:{image_tag}"
    )
    env_vars = {
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
        "PROJECT_NAME": project,
        "OB_DOCS_URL": ob_docs_url.rstrip("/"),
        "SHARING_URL": ob_docs_url.rstrip("/"),
    }

    existing = find_agent_runtime_by_name(control, name)
    if existing:
        runtime_id = existing["agentRuntimeId"]
        logger.info("Updating existing runtime: %s (%s)", name, runtime_id)
        response = control.update_agent_runtime(
            agentRuntimeId=runtime_id,
            description="ob-note use-vault Streamable HTTP MCP",
            agentRuntimeArtifact={
                "containerConfiguration": {"containerUri": container_uri}
            },
            roleArn=role_arn,
            networkConfiguration={"networkMode": "PUBLIC"},
            protocolConfiguration={"serverProtocol": "MCP"},
            environmentVariables=env_vars,
            platformVersion="V2",
        )
        agent_runtime_arn = response["agentRuntimeArn"]
    else:
        logger.info("Creating runtime: %s", name)
        response = control.create_agent_runtime(
            agentRuntimeName=name,
            description="ob-note use-vault Streamable HTTP MCP",
            agentRuntimeArtifact={
                "containerConfiguration": {"containerUri": container_uri}
            },
            networkConfiguration={"networkMode": "PUBLIC"},
            roleArn=role_arn,
            protocolConfiguration={"serverProtocol": "MCP"},
            environmentVariables=env_vars,
            platformVersion="V2",
        )
        agent_runtime_arn = response["agentRuntimeArn"]

    url = mcp_runtime_url(agent_runtime_arn, region)
    logger.info("use-vault MCP Runtime: %s", agent_runtime_arn)
    logger.info("MCP URL: %s", url)
    return {
        "agent_runtime_arn": agent_runtime_arn,
        "use_vault_mcp_url": url,
        "ecr_repository": repository,
        "latest_image_tag": image_tag,
        "agent_runtime_role": role_arn,
    }


def put_runtime_resource_policy(
    control,
    *,
    agent_runtime_arn: str,
    account_id: str,
) -> None:
    """Allow same-account principals to InvokeAgentRuntime (no Gateway)."""
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowInvokeFromAccount",
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                "Action": [
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                "Resource": agent_runtime_arn,
            }
        ],
    }
    try:
        control.put_resource_policy(
            resourceArn=agent_runtime_arn,
            policy=json.dumps(policy),
        )
        logger.info("Resource policy set on MCP Runtime")
    except ClientError as e:
        logger.warning("Could not put resource policy on MCP Runtime: %s", e)


def sync_mcp_package_config(cfg: dict[str, Any], ob_docs_url: str) -> None:
    """Keep MCP/use-vault/config.json aligned with app config."""
    payload = {
        "s3_bucket": cfg.get("s3_bucket") or "",
        "sharing_url": ob_docs_url,
        "ob_docs_url": ob_docs_url,
        "region": _region(cfg),
        "project_name": _project_name(cfg),
    }
    MCP_CONFIG_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("Updated %s", MCP_CONFIG_PATH)


def wait_role_propagated(seconds: int = 12) -> None:
    logger.info("Waiting %ss for IAM role propagation…", seconds)
    time.sleep(seconds)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Deploy MCP/use-vault as AgentCore Runtime (MCP protocol, IAM). "
            "No Gateway — other apps connect with SigV4 to use_vault_mcp_url."
        )
    )
    parser.parse_args(argv)

    cfg = load_config()
    project = _project_name(cfg)
    region = _region(cfg)
    ob_docs_url = _sharing_url(cfg)
    s3_bucket = (cfg.get("s3_bucket") or "").strip()

    try:
        sts = boto3.client("sts", region_name=region)
        account_id = str(sts.get_caller_identity()["Account"])
    except NoCredentialsError:
        logger.error("AWS credentials are not configured")
        return 1

    if str(cfg.get("accountId") or "") != account_id:
        cfg["accountId"] = account_id
    cfg["region"] = region

    iam = boto3.client("iam")
    sm = boto3.client("secretsmanager", region_name=region)
    control = boto3.client("bedrock-agentcore-control", region_name=region)

    logger.info("[1/5] Ensure vault-agent-token secret (%s)", f"{project}/vault-agent-token")
    secret_arn = ensure_vault_agent_token(sm, project)
    logger.info("  secret ARN: %s", secret_arn)

    logger.info("[2/5] Sync MCP package config + IAM role")
    sync_mcp_package_config(cfg, ob_docs_url)
    role_arn = create_use_vault_mcp_role(
        iam,
        account_id=account_id,
        region=region,
        project=project,
        s3_bucket=s3_bucket,
    )
    wait_role_propagated()

    logger.info("[3/5] Build & push Docker image from %s", MCP_DIR)
    repository, image_tag = push_use_vault_mcp_image(
        account_id=account_id,
        region=region,
        project=project,
    )

    logger.info("[4/5] Create/update AgentCore Runtime (MCP protocol)")
    mcp_info = create_or_update_use_vault_mcp_runtime(
        control,
        account_id=account_id,
        region=region,
        project=project,
        role_arn=role_arn,
        repository=repository,
        image_tag=image_tag,
        ob_docs_url=ob_docs_url,
    )

    logger.info("[5/5] Resource policy (account invoke) + save config")
    put_runtime_resource_policy(
        control,
        agent_runtime_arn=mcp_info["agent_runtime_arn"],
        account_id=account_id,
    )

    cfg["use_vault_mcp_runtime_arn"] = mcp_info["agent_runtime_arn"]
    cfg["use_vault_mcp_url"] = mcp_info["use_vault_mcp_url"]
    cfg["use_vault_mcp_role"] = mcp_info["agent_runtime_role"]
    cfg["use_vault_mcp_ecr_repository"] = mcp_info["ecr_repository"]
    cfg["use_vault_mcp_image_tag"] = mcp_info["latest_image_tag"]
    save_config(cfg)

    logger.info("Done.")
    logger.info("  runtime_arn = %s", cfg["use_vault_mcp_runtime_arn"])
    logger.info("  mcp_url     = %s", cfg["use_vault_mcp_url"])
    logger.info(
        "Other apps: use mcp.json with type=streamable_http, "
        "auth_type=aws_sigv4, auth_service=bedrock-agentcore "
        "(see README ### Vault MCP)."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as e:
        logger.error("Command failed: %s", e)
        raise SystemExit(1) from e
    except Exception as e:
        logger.error("%s", e)
        raise SystemExit(1) from e
