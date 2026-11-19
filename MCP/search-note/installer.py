#!/usr/bin/env python3
"""
Unified installation script for search-note MCP (AgentCore Runtime, IAM auth).

Sequentially: vault-agent-token → IAM role → Docker/ECR → AgentCore runtime
(+ resource policy). Format follows agent-runtime korea-weather installer;
deploy semantics match ob-note/create_mcp.py (search-note target).

Usage:
  cd MCP/search-note && python installer.py
  # or from ob-note root:
  python installer.py --search-note-only
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
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("search-note-installer")

script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, "config.json")
parent_config_path = str(Path(script_dir).resolve().parents[1] / "config.json")

MCP_DESCRIPTION = "ob-note search-note Streamable HTTP MCP (Notes graph query)"
RUNTIME_PREFIX = "search_note"
ROLE_SLUG = "search-note-mcp"
CONFIG_PREFIX = "search_note_mcp"


def load_config() -> dict[str, Any]:
    """Load package config.json; bootstrap from parent ob-note/config.json if needed."""
    config: dict[str, Any] = {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                config = loaded
    except Exception as e:
        print(f"Failed to parse local config.json: {e}")
        config = {}

    parent: dict[str, Any] = {}
    if os.path.isfile(parent_config_path):
        try:
            with open(parent_config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    parent = data
        except Exception as e:
            print(f"Warning: failed to read parent config.json: {e}")

    # Prefer parent for AWS identity / vault URL when local is incomplete
    if parent:
        config.setdefault(
            "projectName",
            parent.get("projectName") or parent.get("project_name") or "ob-note",
        )
        config.setdefault("region", parent.get("region") or "us-west-2")
        if parent.get("accountId"):
            config["accountId"] = parent["accountId"]
        sharing = (
            (parent.get("sharing_url") or "").strip()
            or (
                f"https://{(parent.get('custom_domain') or 'vault.my-agentic-ai.click').strip()}"
            )
        )
        config.setdefault("sharing_url", sharing)
        config.setdefault("ob_docs_url", sharing)
        if parent.get("s3_bucket"):
            config.setdefault("s3_bucket", parent["s3_bucket"])

    # Local aliases used by vault_client
    if config.get("project_name") and not config.get("projectName"):
        config["projectName"] = config["project_name"]
    if config.get("projectName") and not config.get("project_name"):
        config["project_name"] = config["projectName"]

    if not config.get("region") or not config.get("accountId"):
        session = boto3.Session()
        config.setdefault("region", session.region_name or "us-west-2")
        try:
            sts = boto3.client("sts", region_name=config["region"])
            config["accountId"] = sts.get_caller_identity()["Account"]
        except Exception as e:
            print(f"Warning: could not resolve accountId: {e}")

    config.setdefault("projectName", "ob-note")
    config.setdefault("project_name", config["projectName"])
    if not config.get("sharing_url") and not config.get("ob_docs_url"):
        config["sharing_url"] = "https://vault.my-agentic-ai.click"
        config["ob_docs_url"] = config["sharing_url"]
    elif not config.get("ob_docs_url"):
        config["ob_docs_url"] = config.get("sharing_url")
    elif not config.get("sharing_url"):
        config["sharing_url"] = config.get("ob_docs_url")

    save_config_file(config)
    return config


def save_config_file(config: dict[str, Any]) -> None:
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")


def update_config(key: str, value: Any) -> bool:
    try:
        config = load_config()
        config[key] = value
        save_config_file(config)
        return True
    except Exception as e:
        print(f"Error updating config: {e}")
        return False


def sync_parent_config(config: dict[str, Any]) -> None:
    """Mirror search-note MCP keys into ob-note/config.json (create_mcp parity)."""
    if not os.path.isfile(parent_config_path):
        print(f"Parent config not found (skip sync): {parent_config_path}")
        return
    try:
        with open(parent_config_path, "r", encoding="utf-8") as f:
            parent = json.load(f)
        if not isinstance(parent, dict):
            return
        parent[f"{CONFIG_PREFIX}_runtime_arn"] = config.get("agent_runtime_arn")
        parent[f"{CONFIG_PREFIX}_url"] = config.get("search_note_mcp_url") or config.get(
            "mcp_url"
        )
        parent[f"{CONFIG_PREFIX}_role"] = config.get("agent_runtime_role")
        parent[f"{CONFIG_PREFIX}_ecr_repository"] = config.get("ecr_repository")
        parent[f"{CONFIG_PREFIX}_image_tag"] = config.get("latest_image_tag")
        with open(parent_config_path, "w", encoding="utf-8") as f:
            json.dump(parent, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"✓ Synced MCP keys to {parent_config_path}")
    except Exception as e:
        print(f"Warning: failed to sync parent config: {e}")


def mcp_runtime_url(agent_runtime_arn: str, region: str) -> str:
    encoded = agent_runtime_arn.replace(":", "%3A").replace("/", "%2F")
    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{encoded}/invocations?qualifier=DEFAULT"
    )


def runtime_repository_name(config: dict[str, Any]) -> str:
    """ECR / Agent Runtime name: search_note_of_{project} (create_mcp naming)."""
    project = (config.get("projectName") or "ob-note").strip() or "ob-note"
    return f"{RUNTIME_PREFIX}_of_{project}".replace("-", "_")


def role_name_for(config: dict[str, Any]) -> str:
    project = (config.get("projectName") or "ob-note").strip() or "ob-note"
    region = config.get("region") or "us-west-2"
    name = f"role-{ROLE_SLUG}-for-{project}-{region}"
    if len(name) > 64:
        name = f"role-sn-mcp-{project[:20]}-{region}"[:64]
    return name


def ob_docs_url(config: dict[str, Any]) -> str:
    return (
        (config.get("ob_docs_url") or config.get("sharing_url") or "")
        .strip()
        .rstrip("/")
        or "https://vault.my-agentic-ai.click"
    )


# ============================================================================
# Vault agent token + IAM
# ============================================================================


def ensure_vault_agent_token(config: dict[str, Any]) -> Optional[str]:
    """Create or reuse {project}/vault-agent-token; return secret ARN."""
    project = config.get("projectName") or "ob-note"
    region = config["region"]
    secret_id = f"{project}/vault-agent-token"
    sm = boto3.client("secretsmanager", region_name=region)
    try:
        arn = sm.describe_secret(SecretId=secret_id)["ARN"]
        print(f"✓ Vault agent token exists: {secret_id}")
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            print(f"Error checking vault-agent-token: {e}")
            return None
    try:
        value = py_secrets.token_urlsafe(32)
        resp = sm.create_secret(
            Name=secret_id,
            SecretString=value,
            Description="HMAC token for MCP runtimes → ob-note vault API auth",
            Tags=[
                {"Key": "Name", "Value": secret_id},
                {"Key": "Project", "Value": project},
            ],
        )
        print(f"✓ Created secret {secret_id}")
        return resp["ARN"]
    except Exception as e:
        print(f"Failed to create vault-agent-token: {e}")
        return None


def create_trust_policy_for_bedrock(config: dict[str, Any]) -> dict[str, Any]:
    account_id = config["accountId"]
    return {
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


def inline_policy_document(config: dict[str, Any]) -> dict[str, Any]:
    region = config["region"]
    account_id = config["accountId"]
    project = config.get("projectName") or "ob-note"
    s3_bucket = (config.get("s3_bucket") or "").strip()
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
    return {"Version": "2012-10-17", "Statement": statements}


def create_bedrock_agentcore_role(config: dict[str, Any]) -> Optional[str]:
    """Create/update execution role with inline policy (create_mcp parity)."""
    iam = boto3.client("iam")
    name = role_name_for(config)
    trust = create_trust_policy_for_bedrock(config)
    policy = inline_policy_document(config)
    policy_name = f"{ROLE_SLUG}-inline-for-{config.get('projectName') or 'ob-note'}"[:128]

    try:
        try:
            existing = iam.get_role(RoleName=name)
            role_arn = existing["Role"]["Arn"]
            print(f"Existing role found: {role_arn}")
            iam.update_assume_role_policy(
                RoleName=name,
                PolicyDocument=json.dumps(trust),
            )
            print("✓ Trust policy updated")
        except iam.exceptions.NoSuchEntityException:
            resp = iam.create_role(
                RoleName=name,
                AssumeRolePolicyDocument=json.dumps(trust),
                Description="Execution role for search-note MCP AgentCore Runtime",
            )
            role_arn = resp["Role"]["Arn"]
            print(f"✓ New role created: {role_arn}")

        iam.put_role_policy(
            RoleName=name,
            PolicyName=policy_name,
            PolicyDocument=json.dumps(policy),
        )
        print(f"✓ Inline policy attached: {policy_name}")
        return role_arn
    except Exception as e:
        print(f"Role creation failed: {e}")
        return None


def create_iam_policies() -> bool:
    print(f"\n{'=' * 60}")
    print("Creating IAM policies and roles (+ vault-agent-token)")
    print(f"{'=' * 60}")

    try:
        config = load_config()

        print("\n1. Ensuring vault-agent-token secret...")
        if not ensure_vault_agent_token(config):
            return False

        print("\n2. Creating Bedrock AgentCore role...")
        role_arn = create_bedrock_agentcore_role(config)
        if not role_arn:
            return False

        print("\n3. Updating configuration...")
        update_config("agent_runtime_role", role_arn)
        print(f"✓ agent_runtime_role = {role_arn}")

        print("\nWaiting 12s for IAM role propagation…")
        time.sleep(12)

        print("\n✓ IAM policies and roles creation completed")
        return True
    except Exception as e:
        print(f"Error creating IAM policies: {e}")
        return False


# ============================================================================
# Docker Build and ECR Push
# ============================================================================


def check_aws_cli() -> bool:
    if not shutil.which("aws"):
        print("Error: AWS CLI is not installed")
        return False
    return True


def check_aws_credentials() -> bool:
    try:
        sts = boto3.client("sts")
        sts.get_caller_identity()
        return True
    except NoCredentialsError:
        print("Error: AWS credentials are not configured properly")
        return False
    except Exception as e:
        print(f"Error: Failed to verify AWS credentials: {e}")
        return False


def ensure_ecr_repository(ecr_client, repository_name: str) -> bool:
    try:
        ecr_client.describe_repositories(repositoryNames=[repository_name])
        print(f"Repository {repository_name} exists.")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] != "RepositoryNotFoundException":
            print(f"Error: Failed to check repository: {e}")
            return False
        print(f"Repository {repository_name} does not exist. Creating it...")
        try:
            ecr_client.create_repository(repositoryName=repository_name)
            print(f"Repository {repository_name} created successfully.")
            return True
        except Exception as create_error:
            print(f"Error: Failed to create repository: {create_error}")
            return False


def docker_login(account_id: str, region: str) -> bool:
    ecr_registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"
    try:
        ecr_client = boto3.client("ecr", region_name=region)
        token = ecr_client.get_authorization_token()["authorizationData"][0][
            "authorizationToken"
        ]
        username, password = base64.b64decode(token).decode("utf-8").split(":")
        process = subprocess.Popen(
            ["docker", "login", "--username", username, "--password-stdin", ecr_registry],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _, stderr = process.communicate(input=password)
        if process.returncode != 0:
            print(f"Error: Docker login failed: {stderr}")
            return False
        return True
    except Exception as e:
        print(f"Error: Failed to login to ECR: {e}")
        return False


def run_docker_command(command: list[str], description: str) -> bool:
    print(f"===== {description} =====")
    try:
        subprocess.run(command, check=True, text=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error: {description} failed: {e}")
        return False
    except Exception as e:
        print(f"Error: {description} failed: {e}")
        return False


def push_to_ecr() -> bool:
    print(f"\n{'=' * 60}")
    print("Building Docker image and pushing to ECR")
    print(f"{'=' * 60}")

    try:
        if not shutil.which("docker"):
            print("Error: docker is required")
            return False

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        config = load_config()
        aws_account_id = config.get("accountId")
        aws_region = config.get("region")
        project_name = config.get("projectName")

        if not all([aws_account_id, aws_region, project_name]):
            print("Error: Missing required configuration in config.json")
            print("Required: accountId, region, projectName")
            return False

        ecr_repository = runtime_repository_name(config)
        image_tag = timestamp
        ecr_uri = (
            f"{aws_account_id}.dkr.ecr.{aws_region}.amazonaws.com/"
            f"{ecr_repository}:{image_tag}"
        )

        print("===== Checking AWS Configuration =====")
        print(f"AWS Account ID: {aws_account_id}")
        print(f"AWS Region: {aws_region}")
        print(f"ECR Repository: {ecr_repository}")
        print(f"ECR URI: {ecr_uri}")

        if not check_aws_cli():
            return False
        print("===== Checking AWS Credentials =====")
        if not check_aws_credentials():
            return False

        print("===== Checking ECR Repository =====")
        ecr_client = boto3.client("ecr", region_name=aws_region)
        if not ensure_ecr_repository(ecr_client, ecr_repository):
            return False

        print("===== AWS ECR Login =====")
        if not docker_login(aws_account_id, aws_region):
            return False

        if not run_docker_command(
            [
                "docker",
                "build",
                "--no-cache",
                "--platform",
                "linux/arm64",
                "--provenance=false",
                "--sbom=false",
                "-t",
                f"{ecr_repository}:{image_tag}",
                ".",
            ],
            "Building Docker Image",
        ):
            return False

        if not run_docker_command(
            ["docker", "tag", f"{ecr_repository}:{image_tag}", ecr_uri],
            "Tagging for ECR Repository",
        ):
            return False

        if not run_docker_command(
            ["docker", "push", ecr_uri],
            "Pushing Image to ECR Repository",
        ):
            return False

        print("===== Complete =====")
        print("Image has been successfully built and pushed to ECR.")
        print(f"Image URI: {ecr_uri}")

        update_config("latest_image_tag", image_tag)
        update_config("ecr_repository", ecr_repository)
        return True
    except Exception as e:
        print(f"Error building and pushing Docker image: {e}")
        return False


# ============================================================================
# Agent Runtime Creation/Update
# ============================================================================


def get_latest_image_tag(config: dict[str, Any]) -> Optional[str]:
    try:
        aws_region = config["region"]
        repository_name = runtime_repository_name(config)
        ecr_client = boto3.client("ecr", region_name=aws_region)
        response = ecr_client.describe_images(repositoryName=repository_name)
        images = response["imageDetails"]
        if not images:
            print(f"Error: No images found in repository {repository_name}")
            return None
        images_sorted = sorted(images, key=lambda x: x["imagePushedAt"], reverse=True)
        latest_image = images_sorted[0]
        if "imageTags" not in latest_image or not latest_image["imageTags"]:
            print("Error: Latest image has no tags")
            return None
        image_tag = latest_image["imageTags"][0]
        print(f"Latest image tag: {image_tag}")
        return image_tag
    except Exception as e:
        print(f"Error getting latest image tag: {e}")
        return None


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


def put_runtime_resource_policy(
    control,
    *,
    agent_runtime_arn: str,
    account_id: str,
) -> None:
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
        print("✓ Resource policy set on MCP Runtime")
    except ClientError as e:
        print(f"Warning: Could not put resource policy: {e}")


def create_agent_runtime_func(
    config: dict[str, Any],
    repository_name: str,
    runtime_name: str,
    image_tag: str,
) -> Optional[str]:
    aws_region = config["region"]
    account_id = config["accountId"]
    agent_runtime_role = config.get("agent_runtime_role")
    if not agent_runtime_role:
        print("Error: agent_runtime_role not found in config.json")
        return None

    env_vars = {
        "AWS_REGION": aws_region,
        "AWS_DEFAULT_REGION": aws_region,
        "PROJECT_NAME": config.get("projectName") or "ob-note",
        "OB_DOCS_URL": ob_docs_url(config),
        "SHARING_URL": ob_docs_url(config),
    }

    try:
        client = boto3.client("bedrock-agentcore-control", region_name=aws_region)
        response = client.create_agent_runtime(
            agentRuntimeName=runtime_name,
            description=MCP_DESCRIPTION,
            agentRuntimeArtifact={
                "containerConfiguration": {
                    "containerUri": (
                        f"{account_id}.dkr.ecr.{aws_region}.amazonaws.com/"
                        f"{repository_name}:{image_tag}"
                    )
                }
            },
            networkConfiguration={"networkMode": "PUBLIC"},
            roleArn=agent_runtime_role,
            protocolConfiguration={"serverProtocol": "MCP"},
            environmentVariables=env_vars,
            platformVersion="V2",
        )
        print(f"✓ Agent runtime created: {response['agentRuntimeArn']}")
        return response["agentRuntimeArn"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConflictException":
            print(f"Agent runtime {runtime_name} already exists")
            return None
        print(f"Error creating agent runtime: {e}")
        return None
    except Exception as e:
        print(f"Error creating agent runtime: {e}")
        return None


def update_agent_runtime_func(
    config: dict[str, Any],
    repository_name: str,
    agent_runtime_id: str,
    image_tag: str,
) -> Optional[str]:
    aws_region = config["region"]
    account_id = config["accountId"]
    agent_runtime_role = config.get("agent_runtime_role")
    if not agent_runtime_role:
        print("Error: agent_runtime_role not found in config.json")
        return None

    env_vars = {
        "AWS_REGION": aws_region,
        "AWS_DEFAULT_REGION": aws_region,
        "PROJECT_NAME": config.get("projectName") or "ob-note",
        "OB_DOCS_URL": ob_docs_url(config),
        "SHARING_URL": ob_docs_url(config),
    }

    try:
        client = boto3.client("bedrock-agentcore-control", region_name=aws_region)
        response = client.update_agent_runtime(
            agentRuntimeId=agent_runtime_id,
            description=MCP_DESCRIPTION,
            agentRuntimeArtifact={
                "containerConfiguration": {
                    "containerUri": (
                        f"{account_id}.dkr.ecr.{aws_region}.amazonaws.com/"
                        f"{repository_name}:{image_tag}"
                    )
                }
            },
            roleArn=agent_runtime_role,
            networkConfiguration={"networkMode": "PUBLIC"},
            protocolConfiguration={"serverProtocol": "MCP"},
            environmentVariables=env_vars,
            platformVersion="V2",
        )
        print(f"✓ Agent runtime updated: {response['agentRuntimeArn']}")
        return response["agentRuntimeArn"]
    except Exception as e:
        print(f"Error updating agent runtime: {e}")
        return None


def create_agent_runtime() -> bool:
    print(f"\n{'=' * 60}")
    print("Creating/updating AgentCore runtime")
    print(f"{'=' * 60}")

    try:
        config = load_config()
        aws_region = config["region"]
        repository_name = runtime_repository_name(config)
        runtime_name = repository_name

        print(f"\n1. Repository name: {repository_name}")
        print(f"   Agent runtime name: {runtime_name}")

        image_tag = config.get("latest_image_tag") or get_latest_image_tag(config)
        if not image_tag:
            print("Error: Could not get latest image tag")
            return False
        print(f"Using image tag: {image_tag}")

        client = boto3.client("bedrock-agentcore-control", region_name=aws_region)
        existing = find_agent_runtime_by_name(client, runtime_name)

        print("\n2. Creating/updating Agent Runtime...")
        if existing:
            print(f"Updating agent runtime: {runtime_name}")
            agent_runtime_arn = update_agent_runtime_func(
                config, repository_name, existing["agentRuntimeId"], image_tag
            )
        else:
            print(f"Creating agent runtime: {runtime_name}")
            agent_runtime_arn = create_agent_runtime_func(
                config, repository_name, runtime_name, image_tag
            )

        if not agent_runtime_arn:
            print("Error: Failed to create/update agent runtime")
            return False

        url = mcp_runtime_url(agent_runtime_arn, aws_region)
        put_runtime_resource_policy(
            client,
            agent_runtime_arn=agent_runtime_arn,
            account_id=str(config["accountId"]),
        )

        update_config("agent_runtime_arn", agent_runtime_arn)
        update_config("agent_runtime_name", runtime_name)
        update_config("search_note_mcp_url", url)
        update_config("mcp_url", url)
        update_config("latest_image_tag", image_tag)
        update_config("ecr_repository", repository_name)

        config = load_config()
        sync_parent_config(config)

        print(f"\nMCP URL: {url}")
        print("\n✓ Agent runtime creation/update completed")
        return True
    except Exception as e:
        print(f"Error creating/updating agent runtime: {e}")
        return False


# ============================================================================
# Main
# ============================================================================


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Deploy search-note MCP as AgentCore Runtime (IAM auth)"
    )
    parser.parse_args(argv)

    os.chdir(script_dir)

    print("\n" + "=" * 60)
    print("search-note MCP — AgentCore Runtime Installation")
    print("=" * 60)

    config = load_config()
    print("Configuration file loaded successfully")
    print(f"  - Project Name: {config.get('projectName')}")
    print(f"  - Region: {config.get('region')}")
    print(f"  - Account ID: {config.get('accountId')}")
    print(f"  - OB_DOCS_URL: {ob_docs_url(config)}")

    steps = [
        ("Creating IAM policies and roles", create_iam_policies),
        ("Building Docker image and pushing to ECR", push_to_ecr),
        ("Creating/updating AgentCore runtime", create_agent_runtime),
    ]

    for step_name, step_func in steps:
        if not step_func():
            print(f"\nInstallation failed: Error occurred in step '{step_name}'.")
            print("   Previous steps completed, but installation was aborted.")
            return 1

    print("\n" + "=" * 60)
    print("All installation steps completed successfully!")
    print("=" * 60)

    config = load_config()
    role_arn = config.get("agent_runtime_role")
    arn = config.get("agent_runtime_arn")
    url = config.get("search_note_mcp_url") or config.get("mcp_url")

    if role_arn:
        print(f"\nCreated AgentCore Runtime Role ARN: {role_arn}")
    if arn:
        print(f"Created AgentCore Runtime ARN: {arn}")
    if url:
        print(f"MCP URL: {url}")

    if role_arn and arn:
        print("\nInstallation complete!")
        return 0
    print("\nInstallation completed with warnings!")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"Installation failed: {e}")
        raise SystemExit(1) from e
