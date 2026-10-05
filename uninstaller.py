#!/usr/bin/env python3
"""Remove infrastructure created by ob-note/installer.py.

Deletes the full standalone stack:
  - ECS service ``service-for-ob-note``
  - Task definitions ``task-for-ob-note``
  - Target group ``TG-for-ob-note``
  - ALB listener rules for ``/vault*``
  - ECR ``ecr-for-ob-note``
  - Log group ``/ecs/app-for-ob-note``
  - Secrets ``ob-note/vault-agent-token``, origin header, session signing key
  - CloudFront ``CloudFront-for-ob-note``
  - ALB / VPC / ECS cluster / S3 bucket / IAM roles

Usage:
  python uninstaller.py
  python uninstaller.py --yes
  python uninstaller.py --yes --keep-s3   # keep bucket (empty vault/ only if --purge-vault-prefix)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

from shared_infra import (
    ALB_NAME,
    CF_COMMENT,
    CLUSTER,
    DEFAULT_REGION,
    ORIGIN_HEADER_SECRET,
    PROJECT,
    SESSION_SECRET,
    VPC_NAME,
    default_bucket_name,
    load_json_if_exists,
)
from s3_files_app_data import delete_app_data_storage


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

SERVICE_NAME = f"service-for-{PROJECT}"
TASK_FAMILY = f"task-for-{PROJECT}"
TG_NAME = f"TG-for-{PROJECT}"
ECR_NAME = f"ecr-for-{PROJECT}"
LOG_GROUP = f"/ecs/app-for-{PROJECT}"
VAULT_AGENT_SECRET = f"{PROJECT}/vault-agent-token"
COGNITO_ADMIN_PASSWORD_SECRET = f"{PROJECT}/cognito-admin-password"


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("ob-note-uninstaller")


logger = setup_logging()


def load_cfg() -> dict[str, Any]:
    return load_json_if_exists(CONFIG_PATH)


def clients(region: str) -> dict[str, Any]:
    return {
        "ecs": boto3.client("ecs", region_name=region),
        "elbv2": boto3.client("elbv2", region_name=region),
        "ec2": boto3.client("ec2", region_name=region),
        "ecr": boto3.client("ecr", region_name=region),
        "logs": boto3.client("logs", region_name=region),
        "s3": boto3.client("s3", region_name=region),
        "sm": boto3.client("secretsmanager", region_name=region),
        "iam": boto3.client("iam"),
        "sts": boto3.client("sts", region_name=region),
        "cognito_idp": boto3.client("cognito-idp", region_name=region),
        "cloudfront": boto3.client("cloudfront", region_name="us-east-1"),
    }


def delete_ob_note_ecs_service(ecs) -> None:
    logger.info("[1/9] Deleting ECS service %s", SERVICE_NAME)
    try:
        services = ecs.describe_services(cluster=CLUSTER, services=[SERVICE_NAME]).get(
            "services"
        ) or []
        svc = next((s for s in services if s.get("status") != "INACTIVE"), None)
        if not svc:
            logger.info("  Service not found / inactive")
            return
        ecs.update_service(cluster=CLUSTER, service=SERVICE_NAME, desiredCount=0)
        logger.info("  Scaled to 0")
        time.sleep(8)
        ecs.delete_service(cluster=CLUSTER, service=SERVICE_NAME, force=True)
        logger.info("  ✓ Deleted service %s", SERVICE_NAME)
        time.sleep(10)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code not in {"ClusterNotFoundException", "ServiceNotFoundException"}:
            logger.warning("  Could not delete service: %s", e)


def deregister_task_definitions(ecs) -> None:
    logger.info("[2/9] Deregistering task definitions %s*", TASK_FAMILY)
    try:
        paginator = ecs.get_paginator("list_task_definitions")
        for page in paginator.paginate(familyPrefix=TASK_FAMILY, sort="DESC"):
            for arn in page.get("taskDefinitionArns") or []:
                try:
                    ecs.deregister_task_definition(taskDefinition=arn)
                    logger.info("  ✓ Deregistered %s", arn.rsplit("/", 1)[-1])
                except ClientError as e:
                    logger.warning("  Could not deregister %s: %s", arn, e)
    except ClientError as e:
        logger.warning("  list_task_definitions: %s", e)


def delete_vault_listener_rules(elbv2) -> None:
    logger.info("[3/9] Deleting ALB app listener rules (/* and legacy /vault*)")
    try:
        alb = elbv2.describe_load_balancers(Names=[ALB_NAME])["LoadBalancers"][0]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "LoadBalancerNotFound":
            logger.info("  ALB %s not found", ALB_NAME)
            return
        raise

    listeners = elbv2.describe_listeners(LoadBalancerArn=alb["LoadBalancerArn"]).get(
        "Listeners"
    ) or []
    deleted = 0
    for listener in listeners:
        rules = elbv2.describe_rules(ListenerArn=listener["ListenerArn"]).get("Rules") or []
        for rule in rules:
            if rule.get("Priority") == "default":
                continue
            has_app = False
            for cond in rule.get("Conditions") or []:
                if cond.get("Field") != "path-pattern":
                    continue
                for v in cond.get("Values") or []:
                    s = str(v)
                    if (
                        s in {"/*", "/", "/vault", "/vault/*"}
                        or s.startswith("/vault/")
                    ):
                        has_app = True
                        break
            if not has_app:
                continue
            try:
                elbv2.delete_rule(RuleArn=rule["RuleArn"])
                deleted += 1
                logger.info("  ✓ Deleted rule %s (priority=%s)", rule["RuleArn"], rule.get("Priority"))
            except ClientError as e:
                logger.warning("  Could not delete rule: %s", e)
    if not deleted:
        logger.info("  No app path rules found")


def delete_ob_note_target_group(elbv2) -> None:
    logger.info("[4/9] Deleting target group %s", TG_NAME)
    try:
        tgs = elbv2.describe_target_groups(Names=[TG_NAME])["TargetGroups"]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "TargetGroupNotFound":
            logger.info("  Target group not found")
            return
        raise
    for tg in tgs:
        arn = tg["TargetGroupArn"]
        for _ in range(12):
            try:
                elbv2.delete_target_group(TargetGroupArn=arn)
                logger.info("  ✓ Deleted %s", TG_NAME)
                return
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "ResourceInUse":
                    logger.warning("  Could not delete TG: %s", e)
                    return
                logger.info("  TG still in use, waiting…")
                time.sleep(10)
        logger.warning("  Timed out waiting to delete %s", TG_NAME)


def delete_ecr_and_logs(ecr, logs) -> None:
    logger.info("[5/9] Deleting ECR %s + log group %s", ECR_NAME, LOG_GROUP)
    try:
        ecr.delete_repository(repositoryName=ECR_NAME, force=True)
        logger.info("  ✓ Deleted ECR %s", ECR_NAME)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "RepositoryNotFoundException":
            logger.warning("  ECR: %s", e)
        else:
            logger.info("  ECR not found")

    try:
        logs.delete_log_group(logGroupName=LOG_GROUP)
        logger.info("  ✓ Deleted log group %s", LOG_GROUP)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            logger.warning("  Log group: %s", e)
        else:
            logger.info("  Log group not found")


def delete_secrets(sm) -> None:
    logger.info("[6/9] Deleting secrets")
    for name in (
        VAULT_AGENT_SECRET,
        ORIGIN_HEADER_SECRET,
        SESSION_SECRET,
        COGNITO_ADMIN_PASSWORD_SECRET,
    ):
        try:
            sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
            logger.info("  ✓ Deleted %s", name)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                logger.warning("  Secret %s: %s", name, e)
            else:
                logger.info("  Secret not found: %s", name)


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


def delete_cognito_user_pool(cognito_idp, cfg: dict[str, Any]) -> None:
    logger.info("[6.5/9] Deleting Cognito User Pool")
    pool_name = PROJECT
    user_pool_id = (cfg.get("cognito_user_pool_id") or "").strip() or None
    if not user_pool_id:
        try:
            user_pool_id = _find_cognito_user_pool_id(cognito_idp, pool_name)
        except ClientError as e:
            logger.warning("  Could not list Cognito User Pools: %s", e)
            return
    if not user_pool_id:
        logger.info("  Cognito User Pool not found (name=%s)", pool_name)
        return
    try:
        clients_resp = cognito_idp.list_user_pool_clients(
            UserPoolId=user_pool_id, MaxResults=60
        )
        for client in clients_resp.get("UserPoolClients") or []:
            client_id = client["ClientId"]
            try:
                cognito_idp.delete_user_pool_client(
                    UserPoolId=user_pool_id, ClientId=client_id
                )
                logger.info("  ✓ Deleted Cognito App Client: %s", client_id)
            except ClientError as e:
                logger.warning("  Could not delete Cognito App Client %s: %s", client_id, e)
        cognito_idp.delete_user_pool(UserPoolId=user_pool_id)
        logger.info("  ✓ Deleted Cognito User Pool: %s (name=%s)", user_pool_id, pool_name)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            logger.info("  Cognito User Pool already deleted: %s", user_pool_id)
        else:
            logger.warning("  Could not delete Cognito User Pool: %s", e)


def empty_s3_prefix(s3, bucket: str, prefix: str) -> int:
    """Delete all object versions under prefix. Returns deleted count."""
    deleted = 0
    paginator = s3.get_paginator("list_object_versions")
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            to_delete: list[dict[str, str]] = []
            for obj in page.get("Versions") or []:
                to_delete.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
            for obj in page.get("DeleteMarkers") or []:
                to_delete.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
            for i in range(0, len(to_delete), 1000):
                batch = to_delete[i : i + 1000]
                if not batch:
                    continue
                s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
                deleted += len(batch)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"NoSuchBucket", "404"}:
            return 0
        if code in {"InvalidArgument", "NotImplemented"}:
            paginator2 = s3.get_paginator("list_objects_v2")
            for page in paginator2.paginate(Bucket=bucket, Prefix=prefix):
                keys = [{"Key": o["Key"]} for o in page.get("Contents") or []]
                for i in range(0, len(keys), 1000):
                    batch = keys[i : i + 1000]
                    if batch:
                        s3.delete_objects(
                            Bucket=bucket, Delete={"Objects": batch, "Quiet": True}
                        )
                        deleted += len(batch)
            return deleted
        raise
    return deleted


def empty_per_user_vaults(s3, bucket: str) -> int:
    """Delete ``{user}/vault/`` objects. Skips the legacy ``vault/`` prefix."""
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Delimiter="/"):
        for common in page.get("CommonPrefixes") or []:
            top = common.get("Prefix") or ""
            if not top or top == "vault/":
                continue
            deleted += empty_s3_prefix(s3, bucket, f"{top}vault/")
    return deleted


def delete_s3_bucket_fully(s3, bucket: str) -> None:
    logger.info("  Emptying and deleting bucket %s …", bucket)
    try:
        n = empty_s3_prefix(s3, bucket, "")
        logger.info("  Removed %d object versions", n)
        s3.delete_bucket(Bucket=bucket)
        logger.info("  ✓ Deleted bucket %s", bucket)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in {"NoSuchBucket", "404"}:
            logger.warning("  Bucket delete: %s", e)


def delete_iam_role(iam, role_name: str) -> None:
    try:
        for p in iam.list_attached_role_policies(RoleName=role_name).get(
            "AttachedPolicies"
        ) or []:
            iam.detach_role_policy(RoleName=role_name, PolicyArn=p["PolicyArn"])
        for p in iam.list_role_policies(RoleName=role_name).get("PolicyNames") or []:
            iam.delete_role_policy(RoleName=role_name, PolicyName=p)
        iam.delete_role(RoleName=role_name)
        logger.info("  ✓ Deleted IAM role %s", role_name)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "NoSuchEntity":
            logger.warning("  IAM role %s: %s", role_name, e)


def delete_alb_fully(elbv2) -> Optional[str]:
    """Delete ALB + listeners. Returns VPC id if known."""
    vpc_id: Optional[str] = None
    try:
        alb = elbv2.describe_load_balancers(Names=[ALB_NAME])["LoadBalancers"][0]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "LoadBalancerNotFound":
            logger.info("  ALB %s not found", ALB_NAME)
            return None
        raise
    vpc_id = alb.get("VpcId")
    alb_arn = alb["LoadBalancerArn"]
    for listener in elbv2.describe_listeners(LoadBalancerArn=alb_arn).get("Listeners") or []:
        try:
            elbv2.delete_listener(ListenerArn=listener["ListenerArn"])
            logger.info("  ✓ Deleted listener %s", listener["ListenerArn"])
        except ClientError as e:
            logger.warning("  Listener: %s", e)
    elbv2.delete_load_balancer(LoadBalancerArn=alb_arn)
    logger.info("  ✓ Deleted ALB %s", ALB_NAME)
    time.sleep(20)
    return vpc_id


def delete_vpc_by_id(ec2, vpc_id: str) -> None:
    """Best-effort delete of the project VPC."""
    logger.info("  Cleaning VPC %s …", vpc_id)
    try:
        for eni in ec2.describe_network_interfaces(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("NetworkInterfaces") or []:
            try:
                ec2.delete_network_interface(NetworkInterfaceId=eni["NetworkInterfaceId"])
            except ClientError:
                pass

        for sg in ec2.describe_security_groups(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("SecurityGroups") or []:
            if sg.get("GroupName") == "default":
                continue
            try:
                ec2.delete_security_group(GroupId=sg["GroupId"])
                logger.info("  ✓ Deleted SG %s", sg.get("GroupName"))
            except ClientError as e:
                logger.warning("  SG %s: %s", sg.get("GroupId"), e)

        for subnet in ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("Subnets") or []:
            try:
                ec2.delete_subnet(SubnetId=subnet["SubnetId"])
            except ClientError as e:
                logger.warning("  Subnet: %s", e)

        for rt in ec2.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("RouteTables") or []:
            main = any(a.get("Main") for a in rt.get("Associations") or [])
            if main:
                continue
            try:
                ec2.delete_route_table(RouteTableId=rt["RouteTableId"])
            except ClientError as e:
                logger.warning("  Route table: %s", e)

        for igw in ec2.describe_internet_gateways(
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
        ).get("InternetGateways") or []:
            try:
                ec2.detach_internet_gateway(
                    InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id
                )
                ec2.delete_internet_gateway(InternetGatewayId=igw["InternetGatewayId"])
            except ClientError as e:
                logger.warning("  IGW: %s", e)

        ec2.delete_vpc(VpcId=vpc_id)
        logger.info("  ✓ Deleted VPC %s", vpc_id)
    except ClientError as e:
        logger.warning("  VPC %s: %s", vpc_id, e)


def find_vpc_id(ec2) -> Optional[str]:
    vpcs = ec2.describe_vpcs(
        Filters=[{"Name": "tag:Name", "Values": [VPC_NAME]}]
    ).get("Vpcs") or []
    if vpcs:
        return vpcs[0]["VpcId"]
    return None


def _find_cloudfront(cloudfront) -> Optional[dict[str, Any]]:
    marker = None
    while True:
        kwargs: dict[str, Any] = {}
        if marker:
            kwargs["Marker"] = marker
        resp = cloudfront.list_distributions(**kwargs)
        listing = resp.get("DistributionList") or {}
        for item in listing.get("Items") or []:
            if item.get("Comment") == CF_COMMENT:
                return item
        if not listing.get("IsTruncated"):
            return None
        marker = listing.get("NextMarker")


def delete_cloudfront(cloudfront) -> None:
    logger.info("[7/9] Disabling/deleting CloudFront %s", CF_COMMENT)
    dist = _find_cloudfront(cloudfront)
    if not dist:
        logger.info("  CloudFront not found")
        return
    dist_id = dist["Id"]
    try:
        cfg_resp = cloudfront.get_distribution_config(Id=dist_id)
        cfg = cfg_resp["DistributionConfig"]
        if cfg.get("Enabled"):
            cfg["Enabled"] = False
            cloudfront.update_distribution(
                Id=dist_id, IfMatch=cfg_resp["ETag"], DistributionConfig=cfg
            )
            logger.info("  Disabled %s — waiting for Deployed…", dist_id)
            deadline = time.time() + 1800
            while time.time() < deadline:
                d = cloudfront.get_distribution(Id=dist_id)["Distribution"]
                if d.get("Status") == "Deployed" and not d.get("DistributionConfig", {}).get(
                    "Enabled"
                ):
                    break
                time.sleep(20)
        etag = cloudfront.get_distribution_config(Id=dist_id)["ETag"]
        cloudfront.delete_distribution(Id=dist_id, IfMatch=etag)
        logger.info("  ✓ Deleted CloudFront %s", dist_id)
    except ClientError as e:
        logger.warning("  CloudFront: %s", e)


def delete_stack(
    *,
    ecs,
    elbv2,
    ec2,
    s3,
    iam,
    region: str,
    account: str,
    bucket: str,
    keep_s3: bool,
    purge_vault_prefix: bool,
) -> None:
    logger.info("[8/9] Deleting network / cluster / IAM / S3")

    try:
        ecs.delete_cluster(cluster=CLUSTER)
        logger.info("  ✓ Deleted cluster %s", CLUSTER)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ClusterNotFoundException":
            logger.warning("  Cluster: %s", e)

    vpc_id = delete_alb_fully(elbv2) or find_vpc_id(ec2)
    if vpc_id:
        try:
            for tg in elbv2.describe_target_groups().get("TargetGroups") or []:
                if tg.get("VpcId") == vpc_id and PROJECT in tg.get("TargetGroupName", ""):
                    try:
                        elbv2.delete_target_group(TargetGroupArn=tg["TargetGroupArn"])
                    except ClientError:
                        pass
        except ClientError:
            pass
        time.sleep(5)
        delete_vpc_by_id(ec2, vpc_id)

    delete_iam_role(iam, f"role-ecs-task-for-{PROJECT}-{region}")
    delete_iam_role(iam, f"role-ecs-execution-for-{PROJECT}-{region}")
    delete_iam_role(iam, f"role-harness-for-{PROJECT}-{region}")

    target = bucket or default_bucket_name(account, region)
    if keep_s3:
        if purge_vault_prefix:
            try:
                n = empty_s3_prefix(s3, target, "vault/")
                n += empty_per_user_vaults(s3, target)
                logger.info("  ✓ Purged %d vault object versions (bucket kept)", n)
            except ClientError as e:
                logger.warning("  vault/ purge: %s", e)
        else:
            logger.info("  Keeping S3 bucket %s", target)
    else:
        delete_s3_bucket_fully(s3, target)


def clean_local_config(cfg: dict[str, Any]) -> None:
    logger.info("[9/9] Cleaning deploy metadata in config.json")
    if not CONFIG_PATH.is_file():
        logger.info("  No config.json")
        return
    drop = {
        "latest_image_tag",
        "ecr_repository_uri",
        "ecs_service",
        "ecs_cluster",
        "target_group",
        "alb_dns",
        "cloudfront_id",
        "cloudfront_domain",
        "HARNESS_ARN",
        "harnessName",
        "sharedProjectName",
        "agentic_work_url",
        "s3_files_app_data_file_system_id",
        "s3_files_app_data_access_point_arn",
        "s3_files_app_data_mount_path",
    }
    changed = False
    for k in drop:
        if k in cfg:
            del cfg[k]
            changed = True
    if changed:
        CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        logger.info("  ✓ Removed deploy keys from config.json")
    else:
        logger.info("  Nothing to clean")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Uninstall standalone ob-note AWS resources"
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )
    parser.add_argument(
        "--keep-s3",
        action="store_true",
        help="Do not delete the S3 bucket",
    )
    parser.add_argument(
        "--purge-vault-prefix",
        action="store_true",
        help="With --keep-s3, also delete vault/_public/ and {user}/vault/ objects",
    )
    args = parser.parse_args()

    cfg = load_cfg()
    region = str(cfg.get("region") or DEFAULT_REGION)
    c = clients(region)
    account = str(cfg.get("accountId") or c["sts"].get_caller_identity()["Account"])
    bucket = str(cfg.get("s3_bucket") or default_bucket_name(account, region))

    logger.info("=" * 60)
    logger.info("ob-note Infrastructure Cleanup")
    logger.info("=" * 60)
    logger.info("Project: %s", PROJECT)
    logger.info("Region:  %s", region)
    logger.info("Account: %s", account)
    logger.info("Bucket:  %s", bucket)
    logger.info("=" * 60)

    if not args.yes:
        logger.info("")
        logger.info(
            "Will delete: ECS, TG, /vault rules, ECR, logs, secrets, Cognito, "
            "CloudFront, ALB/VPC/cluster/IAM%s",
            "" if args.keep_s3 else ", S3 bucket",
        )
        if args.keep_s3 and args.purge_vault_prefix:
            logger.info("Also purge s3://%s/vault/ and s3://%s/{user}/vault/", bucket, bucket)
        response = input("\nAre you sure you want to continue? (yes/no): ")
        if response.lower() != "yes":
            logger.info("Uninstallation cancelled.")
            return 0

    start = time.time()
    try:
        delete_ob_note_ecs_service(c["ecs"])
        deregister_task_definitions(c["ecs"])
        delete_vault_listener_rules(c["elbv2"])
        delete_ob_note_target_group(c["elbv2"])
        delete_ecr_and_logs(c["ecr"], c["logs"])
        delete_secrets(c["sm"])
        delete_cognito_user_pool(c["cognito_idp"], cfg)
        delete_cloudfront(c["cloudfront"])
        logger.info("Deleting S3 Files app-data storage")
        delete_app_data_storage(
            region=region,
            account_id=account,
            project_name=PROJECT,
            cfg=cfg,
        )
        delete_stack(
            ecs=c["ecs"],
            elbv2=c["elbv2"],
            ec2=c["ec2"],
            s3=c["s3"],
            iam=c["iam"],
            region=region,
            account=account,
            bucket=bucket,
            keep_s3=args.keep_s3,
            purge_vault_prefix=args.purge_vault_prefix,
        )
        clean_local_config(cfg)

        elapsed = time.time() - start
        logger.info("")
        logger.info("=" * 60)
        logger.info("Cleanup completed successfully")
        logger.info("Total time: %.2f minutes", elapsed / 60)
        logger.info("=" * 60)
        return 0
    except Exception as e:
        logger.exception("Cleanup failed: %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
