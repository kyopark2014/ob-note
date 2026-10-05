#!/usr/bin/env python3
"""Deploy standalone ob-note (own CloudFront / ALB / ECS / S3).

Creates (idempotent):
  - Infra if missing (S3, secrets, IAM roles, ECS cluster, VPC/ALB, CloudFront)
    via ``shared_infra.py`` (``ob-note`` resource naming)
  - Auth: Google OAuth (``google_client_id``) or Cognito User Pool + admin
    (admin password stored in Secrets Manager ``ob-note/cognito-admin-password``)
  - ECR repository
  - ALB target group TG-for-ob-note (port 8502)
  - Listener rule: path /* (+ CloudFront origin header) → ob-note TG
  - ECS task definition + Fargate service on cluster-for-ob-note

``config.json`` may be missing or partial — installer bootstraps it.
When ``google_client_id`` is absent, the installer interactively offers
Google vs Cognito auth.

Usage:
  python installer.py
"""

from __future__ import annotations

import getpass
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

# Legacy AgentCore Harness provisioning is unused by Open Agent
# (replaced by in-process LangGraph). harness_provision.py remains for cleanup.
from s3_files_app_data import (
    APP_DATA_MOUNT_PATH,
    S3FilesAppDataProvisioner,
    apply_app_data_config,
)
from shared_infra import (
    CLUSTER,
    ORIGIN_HEADER_SECRET,
    PROJECT,
    SESSION_SECRET,
    bootstrap_config,
    ensure_infra_stack,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ob-note-installer")

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

SERVICE_NAME = f"service-for-{PROJECT}"
TASK_FAMILY = f"task-for-{PROJECT}"
TG_NAME = f"TG-for-{PROJECT}"
ECR_NAME = f"ecr-for-{PROJECT}"
CONTAINER_PORT = 8502
LOG_GROUP = f"/ecs/app-for-{PROJECT}"
VAULT_AGENT_SECRET = f"{PROJECT}/vault-agent-token"
COGNITO_ADMIN_PASSWORD_SECRET = f"{PROJECT}/cognito-admin-password"
COGNITO_CLIENT_NAME = f"{PROJECT}-web-ui"
COGNITO_ADMIN_USERNAME = "admin"


def load_config() -> dict[str, Any]:
    return bootstrap_config(CONFIG_PATH)


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def clients(region: str):
    return {
        "ecr": boto3.client("ecr", region_name=region),
        "ecs": boto3.client("ecs", region_name=region),
        "elbv2": boto3.client("elbv2", region_name=region),
        "ec2": boto3.client("ec2", region_name=region),
        "logs": boto3.client("logs", region_name=region),
        "s3": boto3.client("s3", region_name=region),
        "sm": boto3.client("secretsmanager", region_name=region),
        "sts": boto3.client("sts", region_name=region),
        "iam": boto3.client("iam"),
        "cognito_idp": boto3.client("cognito-idp", region_name=region),
        "cloudfront": boto3.client("cloudfront", region_name="us-east-1"),
        "acm": boto3.client("acm", region_name="us-east-1"),
    }


def ensure_ecr(ecr) -> str:
    try:
        resp = ecr.describe_repositories(repositoryNames=[ECR_NAME])
        uri = resp["repositories"][0]["repositoryUri"]
        logger.info("ECR exists: %s", uri)
        return uri
    except ClientError as e:
        if e.response["Error"]["Code"] != "RepositoryNotFoundException":
            raise
    resp = ecr.create_repository(
        repositoryName=ECR_NAME,
        imageScanningConfiguration={"scanOnPush": True},
        tags=[{"Key": "Name", "Value": ECR_NAME}],
    )
    uri = resp["repository"]["repositoryUri"]
    logger.info("Created ECR: %s", uri)
    return uri


def docker_login(ecr, repo_uri: str) -> None:
    registry = repo_uri.split("/")[0]
    pw = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    # token is base64 user:pass — docker login expects password via stdin with AWS username
    import base64

    user, token = base64.b64decode(pw).decode("utf-8").split(":", 1)
    proc = subprocess.run(
        ["docker", "login", "--username", user, "--password-stdin", registry],
        input=token,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"docker login failed: {proc.stderr}")


def build_and_push(repo_uri: str, tag: str) -> str:
    image = f"{repo_uri}:{tag}"
    logger.info("Building image %s (linux/arm64)...", image)
    # Prefer buildx push directly
    cmd = [
        "docker",
        "buildx",
        "build",
        "--platform",
        "linux/arm64",
        "-t",
        image,
        "--push",
        str(ROOT),
    ]
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        logger.warning("buildx failed; falling back to docker build + push")
        subprocess.run(
            ["docker", "build", "--platform", "linux/arm64", "-t", image, str(ROOT)],
            check=True,
        )
        subprocess.run(["docker", "push", image], check=True)
    # also tag latest
    latest = f"{repo_uri}:latest"
    subprocess.run(["docker", "buildx", "imagetools", "create", "-t", latest, image], check=False)
    logger.info("Pushed %s", image)
    return image


def ensure_log_group(logs) -> None:
    try:
        logs.create_log_group(logGroupName=LOG_GROUP)
        logger.info("Created log group %s", LOG_GROUP)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
        logger.info("Log group exists: %s", LOG_GROUP)


def get_secret_arn(sm, name: str) -> str:
    return sm.describe_secret(SecretId=name)["ARN"]


def ensure_vault_agent_token(sm) -> str:
    """Create or reuse vault-agent-token; return secret ARN."""
    import secrets as py_secrets

    try:
        return sm.describe_secret(SecretId=VAULT_AGENT_SECRET)["ARN"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    value = py_secrets.token_urlsafe(32)
    resp = sm.create_secret(
        Name=VAULT_AGENT_SECRET,
        SecretString=value,
        Description="HMAC token for AgentCore use-vault → ob-note auth",
        Tags=[
            {"Key": "Name", "Value": VAULT_AGENT_SECRET},
            {"Key": "Project", "Value": PROJECT},
        ],
    )
    logger.info("Created secret %s", VAULT_AGENT_SECRET)
    return resp["ARN"]


def _cognito_password_valid(password: str) -> Optional[str]:
    if len(password) < 8:
        return "Password must be at least 8 characters"
    if not any(c.isupper() for c in password):
        return "Password must include at least one uppercase letter"
    if not any(c.islower() for c in password):
        return "Password must include at least one lowercase letter"
    if not any(c.isdigit() for c in password):
        return "Password must include at least one number"
    return None


def prompt_auth_choice() -> str:
    """Ask operator to choose Google or Cognito when neither is configured."""
    if not sys.stdin.isatty():
        raise RuntimeError(
            "google_client_id is missing and no Cognito pool is configured. "
            "Set google_client_id in config.json, or run installer interactively "
            "to choose Google / Cognito auth."
        )
    logger.info("")
    logger.info("Authentication is not configured (no google_client_id).")
    logger.info("  1) Google OAuth  — provide a Google OAuth client ID")
    logger.info("  2) Cognito       — create User Pool + admin user")
    while True:
        choice = input("Select auth [1=Google / 2=Cognito]: ").strip().lower()
        if choice in {"1", "g", "google"}:
            return "google"
        if choice in {"2", "c", "cognito"}:
            return "cognito"
        logger.warning("  Enter 1 (Google) or 2 (Cognito).")


def prompt_google_client_id() -> str:
    if not sys.stdin.isatty():
        raise RuntimeError(
            "google_client_id must be set in config.json for non-interactive runs."
        )
    logger.info("")
    logger.info("Google OAuth client ID")
    logger.info(
        "  Create an OAuth 2.0 Web client in Google Cloud Console and paste the client ID."
    )
    while True:
        value = input("Enter google_client_id: ").strip()
        if value:
            return value
        logger.warning("  google_client_id cannot be empty.")


def prompt_cognito_admin_password() -> str:
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Cognito admin password must be entered interactively. "
            "Run `python installer.py` in a terminal, or store the password in "
            f"Secrets Manager as `{COGNITO_ADMIN_PASSWORD_SECRET}` first."
        )
    logger.info("")
    logger.info("Cognito admin user registration")
    logger.info("  Username: %s", COGNITO_ADMIN_USERNAME)
    logger.info(
        "  Password policy: min 8 chars, uppercase, lowercase, number "
        "(symbols optional)"
    )
    while True:
        password = getpass.getpass(
            f"Enter password for Cognito admin '{COGNITO_ADMIN_USERNAME}': "
        )
        error = _cognito_password_valid(password)
        if error:
            logger.warning("  %s. Try again.", error)
            continue
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            logger.warning("  Passwords do not match. Try again.")
            continue
        return password


def ensure_cognito_admin_password_secret(sm, password: str) -> str:
    """Store Cognito admin password in Secrets Manager; return secret ARN."""
    try:
        arn = sm.describe_secret(SecretId=COGNITO_ADMIN_PASSWORD_SECRET)["ARN"]
        sm.put_secret_value(
            SecretId=COGNITO_ADMIN_PASSWORD_SECRET,
            SecretString=password,
        )
        logger.info("Updated secret %s", COGNITO_ADMIN_PASSWORD_SECRET)
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    resp = sm.create_secret(
        Name=COGNITO_ADMIN_PASSWORD_SECRET,
        SecretString=password,
        Description=f"Cognito admin password for {PROJECT} ({COGNITO_ADMIN_USERNAME})",
        Tags=[
            {"Key": "Name", "Value": COGNITO_ADMIN_PASSWORD_SECRET},
            {"Key": "Project", "Value": PROJECT},
        ],
    )
    logger.info("Created secret %s", COGNITO_ADMIN_PASSWORD_SECRET)
    return resp["ARN"]


def _read_cognito_admin_password_secret(sm) -> Optional[str]:
    try:
        value = (
            sm.get_secret_value(SecretId=COGNITO_ADMIN_PASSWORD_SECRET).get(
                "SecretString"
            )
            or ""
        ).strip()
        return value or None
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        return None


def _get_or_prompt_cognito_admin_password(sm) -> str:
    existing = _read_cognito_admin_password_secret(sm)
    if existing:
        logger.info(
            "Reusing Cognito admin password from secret %s",
            COGNITO_ADMIN_PASSWORD_SECRET,
        )
        return existing
    password = prompt_cognito_admin_password()
    ensure_cognito_admin_password_secret(sm, password)
    return password


def _find_cognito_user_pool_id(cognito_idp, pool_name: str) -> Optional[str]:
    next_token: Optional[str] = None
    while True:
        kwargs: dict[str, Any] = {"MaxResults": 60}
        if next_token:
            kwargs["NextToken"] = next_token
        response = cognito_idp.list_user_pools(**kwargs)
        for pool in response.get("UserPools") or []:
            if pool.get("Name") == pool_name:
                return str(pool["Id"])
        next_token = response.get("NextToken")
        if not next_token:
            return None


def _cognito_user_pool_exists(cognito_idp, user_pool_id: str) -> bool:
    try:
        cognito_idp.describe_user_pool(UserPoolId=user_pool_id)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return False
        raise


def _resolve_cognito_user_pool_id(
    cognito_idp, cfg: dict[str, Any], pool_name: str
) -> Optional[str]:
    pool_id = (cfg.get("cognito_user_pool_id") or "").strip()
    if pool_id and _cognito_user_pool_exists(cognito_idp, pool_id):
        return pool_id
    return _find_cognito_user_pool_id(cognito_idp, pool_name)


def _find_cognito_client_id(
    cognito_idp, user_pool_id: str, client_name: str
) -> Optional[str]:
    next_token: Optional[str] = None
    while True:
        kwargs: dict[str, Any] = {"UserPoolId": user_pool_id, "MaxResults": 60}
        if next_token:
            kwargs["NextToken"] = next_token
        response = cognito_idp.list_user_pool_clients(**kwargs)
        for client in response.get("UserPoolClients") or []:
            if client.get("ClientName") == client_name:
                return str(client["ClientId"])
        next_token = response.get("NextToken")
        if not next_token:
            return None


def _cognito_admin_exists(cognito_idp, user_pool_id: str, username: str) -> bool:
    try:
        cognito_idp.admin_get_user(UserPoolId=user_pool_id, Username=username)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("UserNotFoundException", "ResourceNotFoundException"):
            return False
        raise


def _create_cognito_admin_user(
    cognito_idp, user_pool_id: str, username: str, password: str
) -> None:
    cognito_idp.admin_create_user(
        UserPoolId=user_pool_id,
        Username=username,
        TemporaryPassword=password,
        MessageAction="SUPPRESS",
    )
    cognito_idp.admin_set_user_password(
        UserPoolId=user_pool_id,
        Username=username,
        Password=password,
        Permanent=True,
    )


def create_cognito_user_pool(
    cognito_idp, sm, cfg: dict[str, Any]
) -> dict[str, str]:
    """Create or reuse Cognito User Pool + app client + admin user."""
    region = str(cfg["region"])
    pool_name = PROJECT
    logger.info("Cognito User Pool for Web UI authentication")

    user_pool_id = _resolve_cognito_user_pool_id(cognito_idp, cfg, pool_name)
    if user_pool_id:
        logger.info("  Reusing Cognito User Pool: %s (name=%s)", user_pool_id, pool_name)
    else:
        response = cognito_idp.create_user_pool(
            PoolName=pool_name,
            Policies={
                "PasswordPolicy": {
                    "MinimumLength": 8,
                    "RequireUppercase": True,
                    "RequireLowercase": True,
                    "RequireNumbers": True,
                    "RequireSymbols": False,
                }
            },
            MfaConfiguration="OFF",
            AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
            Schema=[
                {
                    "Name": "email",
                    "AttributeDataType": "String",
                    "Mutable": True,
                    "Required": False,
                }
            ],
        )
        user_pool_id = response["UserPool"]["Id"]
        logger.info("  Created Cognito User Pool: %s (name=%s)", user_pool_id, pool_name)

    client_id = _find_cognito_client_id(cognito_idp, user_pool_id, COGNITO_CLIENT_NAME)
    if client_id:
        logger.info("  Reusing Cognito App Client: %s", client_id)
    else:
        client_response = cognito_idp.create_user_pool_client(
            UserPoolId=user_pool_id,
            ClientName=COGNITO_CLIENT_NAME,
            GenerateSecret=False,
            ExplicitAuthFlows=[
                "ALLOW_USER_PASSWORD_AUTH",
                "ALLOW_REFRESH_TOKEN_AUTH",
                "ALLOW_USER_SRP_AUTH",
            ],
            PreventUserExistenceErrors="ENABLED",
        )
        client_id = client_response["UserPoolClient"]["ClientId"]
        logger.info("  Created Cognito App Client: %s", client_id)

    if _cognito_admin_exists(cognito_idp, user_pool_id, COGNITO_ADMIN_USERNAME):
        logger.info("  Cognito admin user already exists: %s", COGNITO_ADMIN_USERNAME)
        # Keep password in Secrets Manager if an operator provided one previously;
        # do not prompt again when admin already exists.
        if not _read_cognito_admin_password_secret(sm):
            logger.info(
                "  Secret %s not found (admin already exists — password not re-prompted)",
                COGNITO_ADMIN_PASSWORD_SECRET,
            )
    else:
        password = _get_or_prompt_cognito_admin_password(sm)
        _create_cognito_admin_user(
            cognito_idp, user_pool_id, COGNITO_ADMIN_USERNAME, password
        )
        logger.info("  Created Cognito admin user: %s", COGNITO_ADMIN_USERNAME)

    return {
        "cognito_user_pool_id": user_pool_id,
        "cognito_user_pool_name": pool_name,
        "cognito_client_id": client_id,
        "cognito_client_name": COGNITO_CLIENT_NAME,
        "cognito_admin_username": COGNITO_ADMIN_USERNAME,
        "cognito_region": region,
        "auth_mode": "cognito",
    }


def ensure_auth_config(cfg: dict[str, Any], sm, cognito_idp) -> dict[str, Any]:
    """Ensure Google or Cognito auth is configured before deploy.

    When ``google_client_id`` is missing and Cognito is not already set up,
    prompts the operator to choose Google or Cognito.
    """
    google_id = (cfg.get("google_client_id") or "").strip()
    existing_mode = (cfg.get("auth_mode") or "").strip().lower()
    cognito_pool = (cfg.get("cognito_user_pool_id") or "").strip()

    if google_id:
        cfg["google_client_id"] = google_id
        cfg["auth_mode"] = "google"
        logger.info("Auth: Google OAuth (google_client_id configured)")
        return cfg

    if existing_mode == "cognito" or cognito_pool:
        logger.info("Auth: Cognito (reusing existing configuration)")
        cognito_info = create_cognito_user_pool(cognito_idp, sm, cfg)
        cfg.update(cognito_info)
        return cfg

    choice = prompt_auth_choice()
    if choice == "google":
        cfg["google_client_id"] = prompt_google_client_id()
        cfg["auth_mode"] = "google"
        logger.info("Auth: Google OAuth (client id accepted)")
        return cfg

    cognito_info = create_cognito_user_pool(cognito_idp, sm, cfg)
    cfg.update(cognito_info)
    logger.info("Auth: Cognito (admin password stored in Secrets Manager)")
    return cfg


def ensure_target_group(elbv2, vpc_id: str) -> str:
    """Create or reuse TG-for-ob-note in ``vpc_id`` (recreate if VPC mismatch)."""

    def _create() -> str:
        resp = elbv2.create_target_group(
            Name=TG_NAME,
            Protocol="HTTP",
            Port=CONTAINER_PORT,
            VpcId=vpc_id,
            TargetType="ip",
            HealthCheckProtocol="HTTP",
            HealthCheckPath="/api/health",
            HealthCheckIntervalSeconds=30,
            HealthCheckTimeoutSeconds=5,
            HealthyThresholdCount=2,
            UnhealthyThresholdCount=3,
            Matcher={"HttpCode": "200"},
        )
        arn = resp["TargetGroups"][0]["TargetGroupArn"]
        logger.info("Created target group %s in %s", TG_NAME, vpc_id)
        try:
            elbv2.modify_target_group_attributes(
                TargetGroupArn=arn,
                Attributes=[
                    {"Key": "stickiness.enabled", "Value": "true"},
                    {"Key": "stickiness.type", "Value": "app_cookie"},
                    {
                        "Key": "stickiness.app_cookie.cookie_name",
                        "Value": "agent_user_id",
                    },
                    {
                        "Key": "stickiness.app_cookie.duration_seconds",
                        "Value": "86400",
                    },
                ],
            )
        except ClientError as e:
            logger.warning("Could not enable stickiness: %s", e)
        return arn

    def _detach_and_delete(arn: str) -> None:
        """Detach TG from ECS/listener rules, then delete it."""
        # ECS services cannot change loadBalancers in-place; scale down & delete
        # any service still bound to this TG so delete_target_group can succeed.
        try:
            ecs = boto3.client(
                "ecs",
                region_name=elbv2.meta.region_name,
            )
            try:
                arns = ecs.list_services(cluster=CLUSTER).get("serviceArns") or []
            except ClientError:
                arns = []
            if arns:
                for page_start in range(0, len(arns), 10):
                    batch = arns[page_start : page_start + 10]
                    for svc in ecs.describe_services(
                        cluster=CLUSTER, services=batch
                    ).get("services") or []:
                        if svc.get("status") == "INACTIVE":
                            continue
                        bound = any(
                            (lb.get("targetGroupArn") or "") == arn
                            for lb in (svc.get("loadBalancers") or [])
                        )
                        if not bound:
                            continue
                        name = svc["serviceName"]
                        logger.info(
                            "Detaching ECS service %s from mismatched TG…", name
                        )
                        try:
                            ecs.update_service(
                                cluster=CLUSTER, service=name, desiredCount=0
                            )
                            time.sleep(8)
                            ecs.delete_service(
                                cluster=CLUSTER, service=name, force=True
                            )
                            deadline = time.time() + 180
                            while time.time() < deadline:
                                check = ecs.describe_services(
                                    cluster=CLUSTER, services=[name]
                                ).get("services") or []
                                live = [
                                    s for s in check if s.get("status") != "INACTIVE"
                                ]
                                if not live:
                                    break
                                time.sleep(5)
                        except ClientError as e:
                            logger.warning("Could not delete ECS service %s: %s", name, e)
        except Exception as e:
            logger.warning("ECS detach for TG recreate skipped: %s", e)

        try:
            for lb in elbv2.describe_load_balancers().get("LoadBalancers") or []:
                for listener in elbv2.describe_listeners(
                    LoadBalancerArn=lb["LoadBalancerArn"]
                ).get("Listeners") or []:
                    for rule in elbv2.describe_rules(
                        ListenerArn=listener["ListenerArn"]
                    ).get("Rules") or []:
                        for action in rule.get("Actions") or []:
                            if action.get("TargetGroupArn") != arn:
                                continue
                            try:
                                if rule.get("Priority") == "default":
                                    elbv2.modify_listener(
                                        ListenerArn=listener["ListenerArn"],
                                        DefaultActions=[
                                            {
                                                "Type": "fixed-response",
                                                "FixedResponseConfig": {
                                                    "StatusCode": "404",
                                                    "ContentType": "text/plain",
                                                    "MessageBody": "Not Found",
                                                },
                                            }
                                        ],
                                    )
                                else:
                                    elbv2.delete_rule(RuleArn=rule["RuleArn"])
                                logger.info(
                                    "Detached old TG from rule %s",
                                    rule.get("RuleArn") or listener["ListenerArn"],
                                )
                            except ClientError as e:
                                logger.warning("Could not detach TG from rule: %s", e)
        except ClientError as e:
            logger.warning("Could not scan listeners for old TG: %s", e)

        for _ in range(18):
            try:
                elbv2.delete_target_group(TargetGroupArn=arn)
                logger.info("Deleted mismatched target group %s", arn.rsplit("/", 1)[-1])
                time.sleep(3)
                return
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code == "ResourceInUse":
                    logger.info("TG still in use, waiting…")
                    time.sleep(10)
                    continue
                if code == "TargetGroupNotFound":
                    return
                raise
        raise RuntimeError(f"Timed out deleting mismatched target group {arn}")

    try:
        tg = elbv2.describe_target_groups(Names=[TG_NAME])["TargetGroups"][0]
        arn = tg["TargetGroupArn"]
        existing_vpc = tg.get("VpcId") or ""
        if existing_vpc and existing_vpc != vpc_id:
            logger.warning(
                "Target group %s is in VPC %s but ALB is in %s — recreating",
                TG_NAME,
                existing_vpc,
                vpc_id,
            )
            _detach_and_delete(arn)
            return _create()
        logger.info("Target group exists: %s", TG_NAME)
        try:
            elbv2.modify_target_group(
                TargetGroupArn=arn,
                HealthCheckPath="/api/health",
                Matcher={"HttpCode": "200"},
            )
        except ClientError as e:
            logger.warning("Could not update TG health path: %s", e)
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "TargetGroupNotFound":
            raise
    return _create()


def ensure_listener_rule(
    elbv2,
    listener_arn: str,
    tg_arn: str,
    origin_header: str,
    *,
    require_origin_header: bool = True,
) -> str:
    """Path /* (+ optional origin header) → ob-note TG.

    When ``require_origin_header`` is False (ALB-only / no CloudFront), the rule
    matches path only so browsers can hit the ALB DNS directly.
    """
    rules = elbv2.describe_rules(ListenerArn=listener_arn)["Rules"]
    app_rule = None
    for rule in rules:
        if rule.get("Priority") == "default":
            continue
        conds = rule.get("Conditions") or []
        has_app_path = False
        for c in conds:
            if c.get("Field") != "path-pattern":
                continue
            values = [str(v) for v in (c.get("Values") or [])]
            if any(
                v in {"/*", "/", "/vault", "/vault/*"} or v.startswith("/vault/")
                for v in values
            ):
                has_app_path = True
                break
        if has_app_path:
            app_rule = rule
            break

    desired_priority = 1
    used = {
        int(r["Priority"])
        for r in rules
        if r.get("Priority", "default").isdigit() and r is not app_rule
    }
    while desired_priority in used:
        desired_priority += 1
        if desired_priority > 10:
            raise RuntimeError("No free ALB listener priority for app rule")

    conditions: list[dict[str, Any]] = [
        {
            "Field": "path-pattern",
            "Values": ["/*"],
        },
    ]
    if require_origin_header and origin_header:
        conditions.append(
            {
                "Field": "http-header",
                "HttpHeaderConfig": {
                    "HttpHeaderName": "X-Custom-Header",
                    "Values": [origin_header],
                },
            }
        )
    actions = [{"Type": "forward", "TargetGroupArn": tg_arn}]

    if app_rule:
        elbv2.modify_rule(
            RuleArn=app_rule["RuleArn"],
            Conditions=conditions,
            Actions=actions,
        )
        current = app_rule.get("Priority")
        if str(current) != str(desired_priority):
            try:
                elbv2.set_rule_priorities(
                    RulePriorities=[
                        {"RuleArn": app_rule["RuleArn"], "Priority": desired_priority}
                    ]
                )
                logger.info(
                    "Updated /* listener rule %s priority %s → %s",
                    app_rule["RuleArn"],
                    current,
                    desired_priority,
                )
            except ClientError as e:
                logger.warning(
                    "Could not move /* rule to priority %s (kept %s): %s",
                    desired_priority,
                    current,
                    e,
                )
        else:
            logger.info("Updated existing /* listener rule %s", app_rule["RuleArn"])
        return app_rule["RuleArn"]

    resp = elbv2.create_rule(
        ListenerArn=listener_arn,
        Priority=desired_priority,
        Conditions=conditions,
        Actions=actions,
    )
    arn = resp["Rules"][0]["RuleArn"]
    logger.info("Created listener rule priority=%s → %s", desired_priority, TG_NAME)
    return arn


def register_task_definition(
    ecs,
    image_uri: str,
    cfg: dict[str, Any],
    session_secret_arn: str,
    vault_agent_secret_arn: str,
    s3_files_app_data_info: Optional[dict[str, Any]] = None,
) -> str:
    account = str(cfg["accountId"])
    region = str(cfg["region"])
    exec_role = f"role-ecs-execution-for-{PROJECT}-{region}"
    task_role = f"role-ecs-task-for-{PROJECT}-{region}"
    app_config = {
        "projectName": PROJECT,
        "accountId": account,
        "region": region,
        "s3_bucket": cfg["s3_bucket"],
        "s3_arn": cfg.get("s3_arn", f"arn:aws:s3:::{cfg['s3_bucket']}"),
        "s3_files_vault_mount_path": "/mnt/vault",
        "auth_mode": (cfg.get("auth_mode") or "").strip().lower()
        or ("google" if cfg.get("google_client_id") else "cognito"),
        "google_client_id": cfg.get("google_client_id", ""),
        "sharing_url": cfg.get("sharing_url") or "",
        "open_agent_backend": "langgraph",
    }
    for key in (
        "cognito_user_pool_id",
        "cognito_user_pool_name",
        "cognito_client_id",
        "cognito_client_name",
        "cognito_admin_username",
        "cognito_region",
    ):
        if cfg.get(key):
            app_config[key] = cfg[key]
    if s3_files_app_data_info:
        apply_app_data_config(app_config, s3_files_app_data_info)
    elif cfg.get("s3_files_app_data_access_point_arn"):
        for key in (
            "s3_files_app_data_file_system_id",
            "s3_files_app_data_access_point_arn",
            "s3_files_app_data_mount_path",
        ):
            if cfg.get(key):
                app_config[key] = cfg[key]

    environment = [
        {"name": "APP_CONFIG_JSON", "value": json.dumps(app_config)},
        {"name": "VAULT_S3_ENABLE", "value": "1"},
        {"name": "VAULT_DIR", "value": "/app/data"},
        {"name": "PROJECT_NAME", "value": PROJECT},
    ]
    container: dict[str, Any] = {
        "name": "app",
        "image": image_uri,
        "essential": True,
        "portMappings": [{"containerPort": CONTAINER_PORT, "protocol": "tcp"}],
        "environment": environment,
        "secrets": [
            {"name": "SESSION_SIGNING_KEY", "valueFrom": session_secret_arn},
            {"name": "VAULT_AGENT_TOKEN", "valueFrom": vault_agent_secret_arn},
        ],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": LOG_GROUP,
                "awslogs-region": region,
                "awslogs-stream-prefix": "ecs",
            },
        },
        "healthCheck": {
            "command": [
                "CMD-SHELL",
                f"curl -f http://localhost:{CONTAINER_PORT}/api/health || exit 1",
            ],
            "interval": 30,
            "timeout": 5,
            "retries": 3,
            "startPeriod": 60,
        },
    }

    volumes: list[dict[str, Any]] = []
    info = s3_files_app_data_info or {}
    file_system_id = info.get("file_system_id") or cfg.get(
        "s3_files_app_data_file_system_id"
    )
    access_point_arn = info.get("access_point_arn") or cfg.get(
        "s3_files_app_data_access_point_arn"
    )
    if file_system_id and access_point_arn:
        file_system_arn = (
            info.get("file_system_arn")
            or f"arn:aws:s3files:{region}:{account}:file-system/{file_system_id}"
        )
        app_data_mount = str(
            info.get("mount_path")
            or cfg.get("s3_files_app_data_mount_path")
            or APP_DATA_MOUNT_PATH
        )
        volumes.append(
            {
                "name": "app-data",
                "s3filesVolumeConfiguration": {
                    "fileSystemArn": file_system_arn,
                    "rootDirectory": "/",
                    "accessPointArn": access_point_arn,
                },
            }
        )
        container["mountPoints"] = [
            {
                "sourceVolume": "app-data",
                "containerPath": app_data_mount,
                "readOnly": False,
            }
        ]
        environment.extend(
            [
                {"name": "APP_DATA_MOUNT", "value": app_data_mount},
                {"name": "TASK_DB_MOUNT", "value": app_data_mount},
                {"name": "TASK_DB_PROJECT", "value": PROJECT},
            ]
        )
        mount_prefix = str(info.get("prefix") or cfg.get("s3_files_app_data_prefix") or "/")
        logger.info(
            "  ECS will mount app-data S3 Files at %s (prefix=%s)",
            app_data_mount,
            mount_prefix,
        )

    task_kwargs: dict[str, Any] = {
        "family": TASK_FAMILY,
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "512",
        "memory": "1024",
        "executionRoleArn": f"arn:aws:iam::{account}:role/{exec_role}",
        "taskRoleArn": f"arn:aws:iam::{account}:role/{task_role}",
        "containerDefinitions": [container],
        "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        "tags": [{"key": "Name", "value": TASK_FAMILY}],
    }
    if volumes:
        task_kwargs["volumes"] = volumes

    resp = ecs.register_task_definition(**task_kwargs)
    arn = resp["taskDefinition"]["taskDefinitionArn"]
    logger.info("Registered task definition %s", arn)
    return arn


def ensure_service(
    ecs,
    elbv2,
    task_def_arn: str,
    tg_arn: str,
    subnets: list[str],
    security_groups: list[str],
    *,
    assign_public_ip: str = "DISABLED",
) -> None:
    try:
        desc = ecs.describe_services(cluster=CLUSTER, services=[SERVICE_NAME])
        services = [s for s in desc.get("services", []) if s.get("status") != "INACTIVE"]
    except ClientError:
        services = []

    network = {
        "awsvpcConfiguration": {
            "subnets": subnets,
            "securityGroups": security_groups,
            "assignPublicIp": assign_public_ip,
        }
    }

    def _create() -> None:
        ecs.create_service(
            cluster=CLUSTER,
            serviceName=SERVICE_NAME,
            taskDefinition=task_def_arn,
            desiredCount=1,
            launchType="FARGATE",
            networkConfiguration=network,
            loadBalancers=[
                {
                    "targetGroupArn": tg_arn,
                    "containerName": "app",
                    "containerPort": CONTAINER_PORT,
                }
            ],
            deploymentConfiguration={
                "minimumHealthyPercent": 100,
                "maximumPercent": 200,
            },
            healthCheckGracePeriodSeconds=120,
            tags=[{"key": "Name", "value": SERVICE_NAME}],
        )
        logger.info("Created ECS service %s", SERVICE_NAME)

    if services:
        svc = services[0]
        current_tg = (svc.get("loadBalancers") or [{}])[0].get("targetGroupArn") or ""
        if current_tg and current_tg != tg_arn:
            logger.warning(
                "ECS service TG mismatch (%s → %s) — recreating service",
                current_tg.rsplit("/", 1)[-1],
                tg_arn.rsplit("/", 1)[-1],
            )
            try:
                ecs.update_service(
                    cluster=CLUSTER, service=SERVICE_NAME, desiredCount=0
                )
                time.sleep(8)
                ecs.delete_service(
                    cluster=CLUSTER, service=SERVICE_NAME, force=True
                )
                logger.info("Deleted ECS service %s for TG swap", SERVICE_NAME)
                # Wait until inactive so create_service can reuse the name.
                deadline = time.time() + 180
                while time.time() < deadline:
                    check = ecs.describe_services(
                        cluster=CLUSTER, services=[SERVICE_NAME]
                    ).get("services") or []
                    live = [s for s in check if s.get("status") != "INACTIVE"]
                    if not live:
                        break
                    time.sleep(5)
            except ClientError as e:
                logger.warning("Could not delete service for TG swap: %s", e)
            _create()
            return

        ecs.update_service(
            cluster=CLUSTER,
            service=SERVICE_NAME,
            taskDefinition=task_def_arn,
            desiredCount=1,
            forceNewDeployment=True,
            networkConfiguration=network,
            deploymentConfiguration={
                "minimumHealthyPercent": 100,
                "maximumPercent": 200,
            },
        )
        logger.info("Updated ECS service %s", SERVICE_NAME)
    else:
        _create()


def _ecs_primary_deployment_ready(service: dict[str, Any]) -> bool:
    """Return True when the PRIMARY deployment is serving the desired task count.

    During rolling updates the service-level runningCount can exceed desiredCount
    while the previous deployment drains. Only PRIMARY matters.
    """
    if service.get("status") != "ACTIVE":
        return False

    deployments = service.get("deployments") or []
    primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
    if not primary:
        return False

    desired = primary.get("desiredCount", 0)
    running = primary.get("runningCount", 0)
    pending = primary.get("pendingCount", 0)
    return (
        desired > 0
        and running == desired
        and pending == 0
        and service.get("pendingCount", 0) == 0
    )


def wait_service(
    ecs,
    elbv2,
    tg_arn: str,
    *,
    expected_task_definition_arn: Optional[str] = None,
    timeout: int = 600,
    poll_interval: int = 15,
) -> None:
    """Wait until the PRIMARY ECS deployment is ready (and optionally on the new task def).

    The boto3 ``services_stable`` waiter also blocks on DRAINING deployments while
    ALB target deregistration (default 300s) completes. The service is already
    usable once the PRIMARY deployment reaches the desired task count.
    """
    deadline = time.time() + timeout
    last_log = 0.0

    while time.time() < deadline:
        svc = ecs.describe_services(cluster=CLUSTER, services=[SERVICE_NAME])["services"][0]
        deployments = svc.get("deployments") or []
        primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
        draining = [d for d in deployments if d.get("status") == "DRAINING"]
        desired = (primary or {}).get("desiredCount", 0)
        running = (primary or {}).get("runningCount", 0)
        pending = (primary or {}).get("pendingCount", 0)

        healthy = 0
        try:
            health = elbv2.describe_target_health(TargetGroupArn=tg_arn)
            healthy = sum(
                1
                for t in health.get("TargetHealthDescriptions", [])
                if t.get("TargetHealth", {}).get("State") == "healthy"
            )
        except ClientError as exc:
            logger.debug("  Target health check skipped: %s", exc)

        if _ecs_primary_deployment_ready(svc):
            if expected_task_definition_arn:
                primary_task_def = (primary or {}).get("taskDefinition", "")
                if primary_task_def != expected_task_definition_arn:
                    now = time.time()
                    if now - last_log > 30:
                        logger.info(
                            "  ... waiting for task def roll-out status=%s "
                            "PRIMARY running=%s/%s pending=%s healthy_targets=%s "
                            "current=%s",
                            svc.get("status"),
                            running,
                            desired,
                            pending,
                            healthy,
                            primary_task_def.rsplit("/", 1)[-1],
                        )
                        for event in (svc.get("events") or [])[:2]:
                            logger.info("  event: %s", event.get("message", "")[:160])
                        last_log = now
                    time.sleep(poll_interval)
                    continue
            if healthy >= 1:
                if draining:
                    logger.info(
                        "✓ ECS PRIMARY deployment ready (running=%s/%s); "
                        "%s draining deployment(s) still cleaning up",
                        running,
                        desired,
                        len(draining),
                    )
                else:
                    logger.info("✓ ECS service is stable")
                return

        now = time.time()
        if now - last_log > 30:
            logger.info(
                "  ... service status=%s PRIMARY running=%s/%s pending=%s "
                "healthy_targets=%s failedTasks=%s",
                svc.get("status"),
                running,
                desired,
                pending,
                healthy,
                (primary or {}).get("failedTasks"),
            )
            for event in (svc.get("events") or [])[:2]:
                logger.info("  event: %s", event.get("message", "")[:160])
            last_log = now
        time.sleep(poll_interval)

    svc = ecs.describe_services(cluster=CLUSTER, services=[SERVICE_NAME])["services"][0]
    deployments = svc.get("deployments") or []
    primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
    if _ecs_primary_deployment_ready(svc):
        if expected_task_definition_arn:
            primary_task_def = (primary or {}).get("taskDefinition", "")
            if primary_task_def != expected_task_definition_arn:
                raise TimeoutError(
                    f"Timed out after {timeout}s waiting for ECS service {SERVICE_NAME} "
                    f"to roll out task definition "
                    f"{expected_task_definition_arn.rsplit('/', 1)[-1]} "
                    f"(PRIMARY still on {primary_task_def.rsplit('/', 1)[-1]}). "
                    "Check ECS service events for update_service failures."
                )
        logger.warning(
            "  ECS wait timed out after %ss, but PRIMARY deployment is ready; continuing",
            timeout,
        )
        return

    raise TimeoutError(
        f"Timed out after {timeout}s waiting for ECS service {SERVICE_NAME} "
        f"(running={svc.get('runningCount')}/{svc.get('desiredCount')}, "
        f"pending={svc.get('pendingCount')}, "
        f"primary={(primary or {}).get('runningCount')}/{(primary or {}).get('desiredCount')}, "
        f"failedTasks={(primary or {}).get('failedTasks')}). "
        f"Check ECS task stopped reason / CloudWatch logs ({LOG_GROUP})."
    )


def ensure_sg_ingress(ec2, ecs_sg: str, alb_sg: str) -> None:
    """Allow ALB → ob-note container port."""
    if not ecs_sg or not alb_sg:
        logger.warning("Skipping SG ingress (ecs_sg=%s alb_sg=%s)", ecs_sg, alb_sg)
        return
    try:
        ec2.authorize_security_group_ingress(
            GroupId=ecs_sg,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": CONTAINER_PORT,
                    "ToPort": CONTAINER_PORT,
                    "UserIdGroupPairs": [
                        {
                            "GroupId": alb_sg,
                            "Description": "ALB to ob-note",
                        }
                    ],
                }
            ],
        )
        logger.info("Opened SG ingress %s ← %s :%s", ecs_sg, alb_sg, CONTAINER_PORT)
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise
        logger.info("SG ingress already present for :%s", CONTAINER_PORT)


def _uses_cloudfront(sharing_url: str, alb_dns: str) -> bool:
    """True when traffic is expected via HTTPS/CloudFront (origin header required)."""
    url = (sharing_url or "").strip()
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and alb_dns and host == alb_dns.lower():
        return False
    return parsed.scheme == "https"


def main() -> int:
    if shutil_which("docker") is None:
        logger.error("Docker is required")
        return 1

    cfg = load_config()
    region = str(cfg["region"])
    c = clients(region)
    ident = c["sts"].get_caller_identity()
    logger.info("AWS account=%s arn=%s", ident.get("Account"), ident.get("Arn"))
    if str(ident.get("Account")) != str(cfg.get("accountId")):
        logger.warning(
            "config accountId=%s differs from caller %s — using caller for deploy",
            cfg.get("accountId"),
            ident.get("Account"),
        )
        cfg["accountId"] = str(ident["Account"])

    logger.info("[0/7] Auth configuration (Google or Cognito)")
    cfg = ensure_auth_config(cfg, c["sm"], c["cognito_idp"])
    save_config(cfg)

    logger.info("[0/7] Ensure standalone ob-note infra (S3/ALB/CF/secrets)")
    cfg, network, origin_header = ensure_infra_stack(
        cfg=cfg,
        s3=c["s3"],
        sm=c["sm"],
        iam=c["iam"],
        ecs=c["ecs"],
        elbv2=c["elbv2"],
        ec2=c["ec2"],
        cloudfront=c["cloudfront"],
        acm=c["acm"],
    )
    save_config(cfg)
    bucket = cfg["s3_bucket"]
    sharing_url = str(cfg.get("sharing_url") or "")

    if not origin_header:
        raise RuntimeError(f"Empty origin header secret: {ORIGIN_HEADER_SECRET}")

    ensure_sg_ingress(c["ec2"], network.security_groups[0], network.alb_sg)

    logger.info("[0.5/7] S3 Files app-data storage (ECS /mnt/app-data)")
    ecs_task_role = f"role-ecs-task-for-{PROJECT}-{region}"
    provisioner = S3FilesAppDataProvisioner(
        region=region,
        account_id=str(cfg["accountId"]),
        project_name=PROJECT,
        ec2_client=c["ec2"],
        s3_client=c["s3"],
        iam_client=c["iam"],
    )
    s3_files_app_data_info = provisioner.create_app_data_storage(
        vpc_id=network.vpc_id,
        subnet_ids=list(network.subnets),
        s3_bucket_name=str(bucket),
        ecs_sg_id=network.security_groups[0] if network.security_groups else "",
        ecs_task_role_name=ecs_task_role,
        preferred_file_system_id=str(
            cfg.get("s3_files_app_data_file_system_id") or ""
        ).strip(),
        cleanup_duplicates=True,
    )
    apply_app_data_config(cfg, s3_files_app_data_info)
    save_config(cfg)

    session_arn = get_secret_arn(c["sm"], SESSION_SECRET)
    vault_agent_arn = ensure_vault_agent_token(c["sm"])

    # Open Agent runs LangGraph in-process (vault_* tools). No Harness / S3 skills.
    cfg["open_agent_backend"] = "langgraph"
    cfg.pop("HARNESS_ARN", None)
    cfg.pop("harnessName", None)
    save_config(cfg)
    logger.info("[1/5] Open Agent backend=langgraph (InvokeHarness skipped)")

    logger.info("[2/5] ECR")
    repo_uri = ensure_ecr(c["ecr"])
    docker_login(c["ecr"], repo_uri)

    tag = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    logger.info("[3/5] Build & push image tag=%s", tag)
    image_uri = build_and_push(repo_uri, tag)

    logger.info("[4/5] Target group + listener rule")
    ensure_log_group(c["logs"])
    tg_arn = ensure_target_group(c["elbv2"], network.vpc_id)
    require_header = _uses_cloudfront(sharing_url, network.alb_dns)
    ensure_listener_rule(
        c["elbv2"],
        network.listener_arn,
        tg_arn,
        origin_header,
        require_origin_header=require_header,
    )
    if not require_header:
        logger.info("ALB-only mode: /* listener rule without origin header")

    logger.info("[5/5] Task definition + service")
    task_arn = register_task_definition(
        c["ecs"],
        image_uri,
        cfg,
        session_arn,
        vault_agent_arn,
        s3_files_app_data_info=s3_files_app_data_info,
    )
    ensure_service(
        c["ecs"],
        c["elbv2"],
        task_arn,
        tg_arn,
        network.subnets,
        network.security_groups,
        assign_public_ip=network.assign_public_ip,
    )

    logger.info("Wait for ECS PRIMARY deployment")
    wait_service(
        c["ecs"],
        c["elbv2"],
        tg_arn,
        expected_task_definition_arn=task_arn,
    )

    cfg["latest_image_tag"] = tag
    cfg["ecr_repository_uri"] = repo_uri
    cfg["ecs_service"] = SERVICE_NAME
    cfg["ecs_cluster"] = CLUSTER
    cfg["target_group"] = TG_NAME
    cfg["alb_dns"] = network.alb_dns
    save_config(cfg)

    url = (cfg.get("sharing_url") or "").rstrip("/") or "http://localhost:8502"
    logger.info("Deployed: %s", url)
    if s3_files_app_data_info:
        logger.info(
            "S3 Files app-data: fs=%s ap=%s mount=%s",
            s3_files_app_data_info.get("file_system_id"),
            s3_files_app_data_info.get("access_point_arn"),
            s3_files_app_data_info.get("mount_path"),
        )
    if (cfg.get("auth_mode") or "").strip().lower() == "cognito":
        logger.info(
            "Cognito login: user=%s pool=%s (password in secret %s)",
            cfg.get("cognito_admin_username") or COGNITO_ADMIN_USERNAME,
            cfg.get("cognito_user_pool_id"),
            COGNITO_ADMIN_PASSWORD_SECRET,
        )
    elif not cfg.get("google_client_id"):
        logger.warning(
            "google_client_id is empty — set it in config.json for Google sign-in"
        )
    print(url)
    return 0


def shutil_which(cmd: str) -> Optional[str]:
    from shutil import which

    return which(cmd)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        logger.exception("Deploy failed")
        raise SystemExit(1)
