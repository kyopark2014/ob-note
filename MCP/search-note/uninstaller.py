#!/usr/bin/env python3
"""
Unified uninstallation script for search-note MCP.

Sequentially deletes: AgentCore runtime → ECR repository → IAM role.
Does NOT delete shared vault-agent-token (used by use-vault / skills).

Format follows agent-runtime iam_auth uninstaller; resource names match
ob-note/create_mcp.py (search_note_of_{project}).

Usage:
  cd MCP/search-note && python uninstaller.py
  cd MCP/search-note && python uninstaller.py --yes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, "config.json")
parent_config_path = str(Path(script_dir).resolve().parents[1] / "config.json")

RUNTIME_PREFIX = "search_note"
ROLE_SLUG = "search-note-mcp"
CONFIG_PREFIX = "search_note_mcp"
PARENT_DROP_KEYS = (
    f"{CONFIG_PREFIX}_runtime_arn",
    f"{CONFIG_PREFIX}_url",
    f"{CONFIG_PREFIX}_role",
    f"{CONFIG_PREFIX}_ecr_repository",
    f"{CONFIG_PREFIX}_image_tag",
)
LOCAL_DROP_KEYS = (
    "agent_runtime_arn",
    "agent_runtime_name",
    "agent_runtime_role",
    "search_note_mcp_url",
    "mcp_url",
    "latest_image_tag",
    "ecr_repository",
)


def load_config() -> Optional[dict[str, Any]]:
    config: dict[str, Any] = {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                config = loaded
    except Exception as e:
        print(f"Failed to parse local config.json: {e}")

    parent: dict[str, Any] = {}
    if os.path.isfile(parent_config_path):
        try:
            with open(parent_config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    parent = data
        except Exception as e:
            print(f"Warning: failed to read parent config.json: {e}")

    if parent:
        config.setdefault(
            "projectName",
            parent.get("projectName") or parent.get("project_name") or "ob-note",
        )
        config.setdefault("region", parent.get("region") or "us-west-2")
        if parent.get("accountId"):
            config["accountId"] = parent["accountId"]
        # Prefer parent MCP ARN if local missing
        if not config.get("agent_runtime_arn") and parent.get(
            f"{CONFIG_PREFIX}_runtime_arn"
        ):
            config["agent_runtime_arn"] = parent[f"{CONFIG_PREFIX}_runtime_arn"]
        if not config.get("ecr_repository") and parent.get(
            f"{CONFIG_PREFIX}_ecr_repository"
        ):
            config["ecr_repository"] = parent[f"{CONFIG_PREFIX}_ecr_repository"]
        if not config.get("agent_runtime_role") and parent.get(f"{CONFIG_PREFIX}_role"):
            config["agent_runtime_role"] = parent[f"{CONFIG_PREFIX}_role"]

    if config.get("project_name") and not config.get("projectName"):
        config["projectName"] = config["project_name"]
    config.setdefault("projectName", "ob-note")
    config.setdefault("region", "us-west-2")

    if not config.get("accountId"):
        try:
            sts = boto3.client("sts", region_name=config["region"])
            config["accountId"] = sts.get_caller_identity()["Account"]
        except Exception as e:
            print(f"Error: could not resolve accountId: {e}")
            return None

    return config


def runtime_repository_name(config: dict[str, Any]) -> str:
    project = (config.get("projectName") or "ob-note").strip() or "ob-note"
    return f"{RUNTIME_PREFIX}_of_{project}".replace("-", "_")


def role_name_for(config: dict[str, Any]) -> str:
    project = (config.get("projectName") or "ob-note").strip() or "ob-note"
    region = config.get("region") or "us-west-2"
    # Prefer role name from ARN if present
    role_arn = (config.get("agent_runtime_role") or "").strip()
    if role_arn and ":role/" in role_arn:
        return role_arn.split(":role/")[-1]
    name = f"role-{ROLE_SLUG}-for-{project}-{region}"
    if len(name) > 64:
        name = f"role-sn-mcp-{project[:20]}-{region}"[:64]
    return name


def save_config(config: dict[str, Any]) -> None:
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")


def clear_config_keys() -> None:
    """Remove deploy metadata from local + parent config (keep vault URL settings)."""
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                local = json.load(f)
            if isinstance(local, dict):
                for k in LOCAL_DROP_KEYS:
                    local.pop(k, None)
                save_config(local)
                print(f"✓ Cleared MCP deploy keys from {config_path}")
        except Exception as e:
            print(f"Warning: failed to clean local config: {e}")

    if os.path.isfile(parent_config_path):
        try:
            with open(parent_config_path, "r", encoding="utf-8") as f:
                parent = json.load(f)
            if isinstance(parent, dict):
                changed = False
                for k in PARENT_DROP_KEYS:
                    if k in parent:
                        del parent[k]
                        changed = True
                if changed:
                    with open(parent_config_path, "w", encoding="utf-8") as f:
                        json.dump(parent, f, indent=2, ensure_ascii=False)
                        f.write("\n")
                    print(f"✓ Cleared MCP deploy keys from {parent_config_path}")
        except Exception as e:
            print(f"Warning: failed to clean parent config: {e}")


# ============================================================================
# Agent Runtime Deletion
# ============================================================================


def wait_for_runtime_deletion(
    config: dict[str, Any], runtime_name: str, max_wait_time: int = 600
) -> bool:
    aws_region = config.get("region")
    if not aws_region:
        print("Error: region not found in config.json")
        return False

    print(f"\nWaiting for AgentCore runtime '{runtime_name}' to be deleted...")
    print("Checking every 10 seconds...")

    client = boto3.client("bedrock-agentcore-control", region_name=aws_region)
    start_time = time.time()
    check_count = 0

    while True:
        check_count += 1
        elapsed_time = time.time() - start_time
        try:
            response = client.list_agent_runtimes()
            agent_runtimes = response.get("agentRuntimes", [])
            runtime_exists = any(
                ar.get("agentRuntimeName") == runtime_name for ar in agent_runtimes
            )
            if not runtime_exists:
                print(
                    f"✓ AgentCore runtime '{runtime_name}' has been successfully deleted"
                )
                print(
                    f"  (Checked {check_count} times, elapsed time: {elapsed_time:.1f} seconds)"
                )
                return True
            if elapsed_time >= max_wait_time:
                print(
                    f"\nTimeout: AgentCore runtime '{runtime_name}' still exists "
                    f"after {max_wait_time} seconds"
                )
                return False
            print(
                f"  [{check_count}] Runtime still exists, waiting 10 seconds... "
                f"(elapsed: {elapsed_time:.1f}s)"
            )
            time.sleep(10)
        except Exception as e:
            print(f"Error checking runtime status: {e}")
            return False


def delete_agent_runtime() -> bool:
    print(f"\n{'=' * 60}")
    print("Deleting AgentCore runtime")
    print(f"{'=' * 60}")

    try:
        config = load_config()
        if not config:
            return False

        aws_region = config.get("region")
        if not aws_region:
            print("Error: Missing region in config.json")
            return False

        runtime_name = runtime_repository_name(config)
        agent_runtime_arn = config.get("agent_runtime_arn")

        client = boto3.client("bedrock-agentcore-control", region_name=aws_region)
        deletion_requested = False
        actual_runtime_name = None

        if agent_runtime_arn:
            runtime_id = (
                agent_runtime_arn.split("/")[-1] if "/" in agent_runtime_arn else None
            )
            if runtime_id:
                try:
                    client.delete_agent_runtime(agentRuntimeId=runtime_id)
                    print(f"✓ Agent runtime deletion requested: {agent_runtime_arn}")
                    deletion_requested = True
                    actual_runtime_name = runtime_name
                except ClientError as e:
                    if e.response["Error"]["Code"] == "ResourceNotFoundException":
                        print(
                            f"Agent runtime not found (may already be deleted): "
                            f"{agent_runtime_arn}"
                        )
                    else:
                        print(f"Error deleting agent runtime: {e}")
                        return False

        if not deletion_requested:
            response = client.list_agent_runtimes()
            for agent_runtime in response.get("agentRuntimes", []):
                if agent_runtime.get("agentRuntimeName") == runtime_name:
                    runtime_id = agent_runtime["agentRuntimeId"]
                    actual_runtime_name = agent_runtime["agentRuntimeName"]
                    try:
                        client.delete_agent_runtime(agentRuntimeId=runtime_id)
                        print(
                            f"✓ Agent runtime deletion requested: "
                            f"{agent_runtime['agentRuntimeArn']}"
                        )
                        deletion_requested = True
                        break
                    except ClientError as e:
                        if e.response["Error"]["Code"] == "ResourceNotFoundException":
                            print(
                                f"Agent runtime not found (may already be deleted): "
                                f"{actual_runtime_name}"
                            )
                            return True
                        print(f"Error deleting agent runtime: {e}")
                        return False

            if not deletion_requested:
                print(
                    f"Agent runtime {runtime_name} not found (may already be deleted)"
                )
                return True

        if deletion_requested:
            name_to_check = actual_runtime_name or runtime_name
            return wait_for_runtime_deletion(config, name_to_check)
        return True
    except Exception as e:
        print(f"Error deleting agent runtime: {e}")
        return False


# ============================================================================
# ECR Repository Deletion
# ============================================================================


def delete_ecr_repository() -> bool:
    print(f"\n{'=' * 60}")
    print("Deleting ECR repository")
    print(f"{'=' * 60}")

    try:
        config = load_config()
        if not config:
            return False

        aws_region = config.get("region")
        if not aws_region:
            print("Error: Missing region in config.json")
            return False

        ecr_repository = config.get("ecr_repository") or runtime_repository_name(config)
        print(f"Repository name: {ecr_repository}")

        ecr_client = boto3.client("ecr", region_name=aws_region)
        try:
            ecr_client.describe_repositories(repositoryNames=[ecr_repository])
        except ClientError as e:
            if e.response["Error"]["Code"] == "RepositoryNotFoundException":
                print(
                    f"ECR repository {ecr_repository} not found (may already be deleted)"
                )
                return True
            print(f"Error checking repository: {e}")
            return False

        try:
            response = ecr_client.list_images(repositoryName=ecr_repository)
            image_ids = response.get("imageIds", [])
            if image_ids:
                print(f"Found {len(image_ids)} images in repository. Deleting...")
                ecr_client.batch_delete_image(
                    repositoryName=ecr_repository,
                    imageIds=image_ids,
                )
                print(f"✓ Deleted {len(image_ids)} images from repository")
            else:
                print("No images found in repository")
        except ClientError as e:
            if e.response["Error"]["Code"] != "RepositoryNotFoundException":
                print(f"Warning: Error deleting images: {e}")

        try:
            ecr_client.delete_repository(repositoryName=ecr_repository, force=True)
            print(f"✓ ECR repository deleted: {ecr_repository}")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "RepositoryNotFoundException":
                print(
                    f"ECR repository {ecr_repository} not found (may already be deleted)"
                )
                return True
            print(f"Error deleting repository: {e}")
            return False
    except Exception as e:
        print(f"Error deleting ECR repository: {e}")
        return False


# ============================================================================
# IAM Role Deletion
# ============================================================================


def detach_policy_from_role(role_name: str, policy_arn: str) -> bool:
    try:
        iam_client = boto3.client("iam")
        iam_client.detach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
        print(f"✓ Policy detached successfully: {policy_arn}")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            print(f"Policy not attached to role (may already be detached): {policy_arn}")
            return True
        print(f"Policy detachment failed: {e}")
        return False
    except Exception as e:
        print(f"Policy detachment failed: {e}")
        return False


def delete_iam_role(config: dict[str, Any]) -> bool:
    name = role_name_for(config)
    try:
        iam_client = boto3.client("iam")
        try:
            existing_role = iam_client.get_role(RoleName=name)
            role_arn = existing_role["Role"]["Arn"]

            attached = iam_client.list_attached_role_policies(RoleName=name)
            for policy in attached.get("AttachedPolicies", []):
                detach_policy_from_role(name, policy["PolicyArn"])

            inline = iam_client.list_role_policies(RoleName=name)
            for policy_name in inline.get("PolicyNames", []):
                try:
                    iam_client.delete_role_policy(
                        RoleName=name, PolicyName=policy_name
                    )
                    print(f"✓ Deleted inline policy: {policy_name}")
                except Exception as e:
                    print(f"Warning: Failed to delete inline policy {policy_name}: {e}")

            iam_client.delete_role(RoleName=name)
            print(f"✓ IAM role deleted: {role_arn}")
            return True
        except iam_client.exceptions.NoSuchEntityException:
            print(f"IAM role {name} not found (may already be deleted)")
            return True
    except Exception as e:
        print(f"Role deletion failed: {e}")
        return False


def delete_iam_resources() -> bool:
    print(f"\n{'=' * 60}")
    print("Deleting IAM role")
    print(f"{'=' * 60}")

    try:
        config = load_config()
        if not config:
            return False
        if not config.get("accountId"):
            print("Error: accountId not found in config.json")
            return False

        print("\n1. Deleting IAM role...")
        if not delete_iam_role(config):
            print("Warning: Failed to delete IAM role")

        print(
            "\nNote: shared secret "
            f"`{(config.get('projectName') or 'ob-note')}/vault-agent-token` "
            "is kept (used by use-vault / skills)."
        )
        print("\n✓ IAM resources deletion completed")
        return True
    except Exception as e:
        print(f"Error deleting IAM resources: {e}")
        return False


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="search-note MCP AgentCore Uninstaller")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt and proceed with deletion",
    )
    args = parser.parse_args()

    os.chdir(script_dir)

    print("\n" + "=" * 60)
    print("search-note MCP — AgentCore Runtime Uninstallation")
    print("=" * 60)

    config = load_config()
    if not config:
        print("\nError: Cannot proceed without config")
        return 1

    print("Configuration loaded successfully")
    print(f"  - Project Name: {config.get('projectName')}")
    print(f"  - Region: {config.get('region')}")
    print(f"  - Account ID: {config.get('accountId')}")
    print(f"  - Runtime: {runtime_repository_name(config)}")
    print(f"  - Role: {role_name_for(config)}")

    if not args.yes:
        print("\n" + "=" * 60)
        print("WARNING: This will delete search-note MCP runtime, ECR, and IAM role")
        print("=" * 60)
        response = input("\nAre you sure you want to continue? (yes/no): ")
        if response.lower() != "yes":
            print("Uninstallation cancelled.")
            return 0

    steps = [
        ("Deleting AgentCore runtime", delete_agent_runtime),
        ("Deleting ECR repository", delete_ecr_repository),
        ("Deleting IAM role", delete_iam_resources),
    ]

    for step_name, step_func in steps:
        if not step_func():
            print(f"\nWarning: Error occurred in step '{step_name}'.")
            print("   Continuing with remaining steps...")

    clear_config_keys()

    print("\n" + "=" * 60)
    print("Uninstallation process completed!")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
