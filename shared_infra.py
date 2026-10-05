#!/usr/bin/env python3
"""Standalone infrastructure helpers for ob-note.

Creates (idempotent) project-scoped ALB / ECS cluster / S3 / Secrets / CloudFront.
Does not share or merge config with agentic-work.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("ob-note-infra")

ROOT = Path(__file__).resolve().parent

PROJECT = "ob-note"
DEFAULT_REGION = "us-west-2"
DEFAULT_CUSTOM_DOMAIN = "vault.my-agentic-ai.click"

CLUSTER = f"cluster-for-{PROJECT}"
ALB_NAME = f"alb-for-{PROJECT}"
VPC_NAME = f"vpc-for-{PROJECT}"
ALB_SG_NAME = f"alb-sg-for-{PROJECT}"
ECS_SG_NAME = f"ecs-sg-for-{PROJECT}"
ORIGIN_HEADER_SECRET = f"{PROJECT}/cloudfront-alb-origin-header"
SESSION_SECRET = f"{PROJECT}/session-signing-key"
CUSTOM_HEADER_NAME = "X-Custom-Header"
CF_COMMENT = f"CloudFront-for-{PROJECT}"
CF_CACHE_POLICY_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
CF_ORIGIN_REQUEST_ALL_VIEWER = "216adef6-5c7f-47e4-b989-5492eafa07d3"


@dataclass
class NetworkInfo:
    vpc_id: str
    subnets: list[str]
    security_groups: list[str]
    alb_arn: str
    alb_dns: str
    alb_sg: str
    listener_arn: str
    assign_public_ip: str  # "ENABLED" | "DISABLED"


def _sts_identity(region: str) -> tuple[str, str]:
    sts = boto3.client("sts", region_name=region)
    ident = sts.get_caller_identity()
    return str(ident["Account"]), str(ident.get("Arn") or "")


def default_bucket_name(account: str, region: str) -> str:
    return f"storage-for-{PROJECT}-{account}-{region}"


def load_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}


def bootstrap_config(config_path: Path) -> dict[str, Any]:
    """Load or create config.json with ob-note defaults (no external merge)."""
    cfg = load_json_if_exists(config_path)

    region = str(cfg.get("region") or DEFAULT_REGION).strip() or DEFAULT_REGION
    account = str(cfg.get("accountId") or "").strip()
    if not account:
        account, _ = _sts_identity(region)

    cfg["projectName"] = PROJECT
    cfg.pop("sharedProjectName", None)
    cfg.pop("agentic_work_url", None)
    cfg["region"] = region
    cfg["accountId"] = account
    cfg.setdefault("s3_files_vault_mount_path", "/mnt/vault")
    cfg.setdefault("s3_files_app_data_mount_path", "/mnt/app-data")
    cfg.setdefault("custom_domain", DEFAULT_CUSTOM_DOMAIN)

    # Migrate away from agentic-work bucket naming if still present.
    bucket = str(cfg.get("s3_bucket") or "").strip()
    if not bucket or "agentic-work" in bucket:
        cfg["s3_bucket"] = default_bucket_name(account, region)
        logger.info("config s3_bucket → %s", cfg["s3_bucket"])
    cfg["s3_arn"] = f"arn:aws:s3:::{cfg['s3_bucket']}"

    config_path.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    logger.info("Wrote bootstrap config → %s", config_path)
    return cfg


def ensure_s3_bucket(s3, bucket: str, region: str) -> str:
    """Create project storage bucket if missing."""
    try:
        s3.head_bucket(Bucket=bucket)
        logger.info("S3 bucket exists: %s", bucket)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code not in {"404", "NoSuchBucket", "NotFound", "403", "AccessDenied"}:
            raise
        try:
            logger.info("Creating S3 bucket %s …", bucket)
            if region == "us-east-1":
                s3.create_bucket(Bucket=bucket)
            else:
                s3.create_bucket(
                    Bucket=bucket,
                    CreateBucketConfiguration={"LocationConstraint": region},
                )
        except ClientError as create_err:
            err = create_err.response.get("Error", {}).get("Code", "")
            if err not in {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}:
                raise
            logger.info("S3 bucket already owned: %s", bucket)

    try:
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
    except ClientError as e:
        logger.warning("public access block: %s", e)

    try:
        s3.put_bucket_cors(
            Bucket=bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedHeaders": ["*"],
                        "AllowedMethods": ["GET", "POST", "PUT"],
                        "AllowedOrigins": ["*"],
                    }
                ]
            },
        )
    except ClientError as e:
        logger.warning("bucket CORS: %s", e)

    try:
        s3.put_bucket_versioning(
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
    except ClientError as e:
        logger.warning("bucket versioning: %s", e)

    try:
        s3.put_object(Bucket=bucket, Key="vault/", Body=b"")
    except ClientError as e:
        logger.warning("vault/ prefix: %s", e)

    return bucket


def ensure_origin_header_secret(sm) -> str:
    """CloudFront→ALB origin header."""
    try:
        current = (
            sm.get_secret_value(SecretId=ORIGIN_HEADER_SECRET).get("SecretString") or ""
        ).strip()
        if current:
            logger.info("Reusing origin header secret %s", ORIGIN_HEADER_SECRET)
            return current
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    value = secrets.token_urlsafe(32)
    try:
        sm.create_secret(
            Name=ORIGIN_HEADER_SECRET,
            Description=(
                f"CloudFront to ALB origin verification header ({CUSTOM_HEADER_NAME})"
            ),
            SecretString=value,
            Tags=[
                {"Key": "Name", "Value": ORIGIN_HEADER_SECRET},
                {"Key": "Project", "Value": PROJECT},
            ],
        )
        logger.info("Created origin header secret %s", ORIGIN_HEADER_SECRET)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceExistsException":
            raise
        value = (
            sm.get_secret_value(SecretId=ORIGIN_HEADER_SECRET).get("SecretString") or ""
        ).strip()
    return value


def ensure_session_signing_key(sm) -> str:
    """HMAC session cookie key for ob-note."""
    try:
        current = (
            sm.get_secret_value(SecretId=SESSION_SECRET).get("SecretString") or ""
        ).strip()
        if current:
            logger.info("Reusing session signing key %s", SESSION_SECRET)
            return current
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    value = secrets.token_urlsafe(32)
    try:
        sm.create_secret(
            Name=SESSION_SECRET,
            Description=f"HMAC signing key for {PROJECT} session cookies",
            SecretString=value,
            Tags=[
                {"Key": "Name", "Value": SESSION_SECRET},
                {"Key": "Project", "Value": PROJECT},
            ],
        )
        logger.info("Created session signing key %s", SESSION_SECRET)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceExistsException":
            raise
        value = (
            sm.get_secret_value(SecretId=SESSION_SECRET).get("SecretString") or ""
        ).strip()
    return value


def _put_role_policy(iam, role_name: str, policy_name: str, document: dict[str, Any]) -> None:
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=policy_name,
        PolicyDocument=json.dumps(document),
    )


def ensure_ecs_roles(iam, account: str, region: str, bucket: str) -> dict[str, str]:
    """Create ECS task/execution roles (if missing)."""
    task_role = f"role-ecs-task-for-{PROJECT}-{region}"
    exec_role = f"role-ecs-execution-for-{PROJECT}-{region}"
    assume = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    def _ensure_role(name: str, managed: Optional[list[str]] = None) -> str:
        try:
            arn = iam.get_role(RoleName=name)["Role"]["Arn"]
            logger.info("IAM role exists: %s", name)
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise
            arn = iam.create_role(
                RoleName=name,
                AssumeRolePolicyDocument=json.dumps(assume),
                Description=f"ECS role for {PROJECT}",
                Tags=[
                    {"Key": "Name", "Value": name},
                    {"Key": "Project", "Value": PROJECT},
                ],
            )["Role"]["Arn"]
            logger.info("Created IAM role %s", name)
            time.sleep(8)
        for policy_arn in managed or []:
            try:
                iam.attach_role_policy(RoleName=name, PolicyArn=policy_arn)
            except ClientError as e:
                logger.warning("attach %s: %s", policy_arn, e)
        return arn

    task_arn = _ensure_role(task_role)
    exec_arn = _ensure_role(
        exec_role,
        managed=["arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"],
    )

    bucket_arn = f"arn:aws:s3:::{bucket}"
    _put_role_policy(
        iam,
        task_role,
        f"ecs-task-s3-vault-for-{PROJECT}",
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "VaultBucketList",
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                    "Resource": [bucket_arn],
                },
                {
                    "Sid": "VaultObjectRW",
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                    "Resource": [f"{bucket_arn}/*"],
                },
            ],
        },
    )

    secret_arns = [f"arn:aws:secretsmanager:{region}:{account}:secret:{PROJECT}/*"]
    secrets_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadProjectSecrets",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": secret_arns,
            }
        ],
    }
    _put_role_policy(iam, exec_role, f"ecs-execution-secrets-for-{PROJECT}", secrets_doc)
    _put_role_policy(iam, task_role, f"ecs-task-secrets-for-{PROJECT}", secrets_doc)

    _put_role_policy(
        iam,
        task_role,
        f"ecs-task-cognito-policy-for-{PROJECT}",
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "CognitoUserPasswordAuth",
                    "Effect": "Allow",
                    "Action": [
                        "cognito-idp:InitiateAuth",
                        "cognito-idp:RespondToAuthChallenge",
                        "cognito-idp:GetUser",
                        "cognito-idp:DescribeUserPool",
                        "cognito-idp:DescribeUserPoolClient",
                    ],
                    "Resource": ["*"],
                }
            ],
        },
    )

    # Documents Foundation Model Parser calls Bedrock InvokeModel directly
    # from the ECS task (not via AgentCore Harness).
    _put_role_policy(
        iam,
        task_role,
        f"ecs-task-bedrock-policy-for-{PROJECT}",
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "InvokeBedrockModels",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                        "bedrock:GetInferenceProfile",
                        "bedrock:GetFoundationModel",
                    ],
                    "Resource": [
                        "arn:aws:bedrock:*::foundation-model/*",
                        f"arn:aws:bedrock:{region}:{account}:inference-profile/*",
                        f"arn:aws:bedrock:*:{account}:inference-profile/*",
                    ],
                }
            ],
        },
    )

    return {"task_role_arn": task_arn, "execution_role_arn": exec_arn}


def ensure_ecs_cluster(ecs) -> str:
    try:
        desc = ecs.describe_clusters(clusters=[CLUSTER])["clusters"]
        if desc and desc[0].get("status") == "ACTIVE":
            logger.info("ECS cluster exists: %s", CLUSTER)
            return CLUSTER
    except ClientError:
        pass
    ecs.create_cluster(
        clusterName=CLUSTER,
        capacityProviders=["FARGATE", "FARGATE_SPOT"],
        defaultCapacityProviderStrategy=[{"capacityProvider": "FARGATE", "weight": 1}],
        tags=[{"key": "Name", "value": CLUSTER}, {"key": "Project", "value": PROJECT}],
    )
    logger.info("Created ECS cluster %s", CLUSTER)
    return CLUSTER


def _classify_subnets(ec2, subnet_ids: list[str]) -> tuple[list[str], list[str]]:
    if not subnet_ids:
        return [], []
    rts = ec2.describe_route_tables(
        Filters=[{"Name": "association.subnet-id", "Values": subnet_ids}]
    )["RouteTables"]
    public_ids: set[str] = set()
    for rt in rts:
        has_igw = any(
            (r.get("GatewayId") or "").startswith("igw-") for r in rt.get("Routes", [])
        )
        if not has_igw:
            continue
        for assoc in rt.get("Associations", []):
            sid = assoc.get("SubnetId")
            if sid:
                public_ids.add(sid)
    detail = ec2.describe_subnets(SubnetIds=subnet_ids)["Subnets"]
    for sn in detail:
        if sn.get("MapPublicIpOnLaunch"):
            public_ids.add(sn["SubnetId"])
    public = [s for s in subnet_ids if s in public_ids]
    private = [s for s in subnet_ids if s not in public_ids]
    return public, private


def _find_named_sg(ec2, vpc_id: str, name_substr: str) -> Optional[str]:
    sgs = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])[
        "SecurityGroups"
    ]
    for sg in sgs:
        for tag in sg.get("Tags") or []:
            if tag.get("Key") == "Name" and name_substr in str(tag.get("Value") or ""):
                return sg["GroupId"]
        if name_substr in (sg.get("GroupName") or ""):
            return sg["GroupId"]
    return None


def _create_minimal_network(ec2, elbv2, region: str) -> NetworkInfo:
    """Create a minimal VPC+ALB for Fargate (public subnets)."""
    logger.info("Creating network (%s / %s) …", VPC_NAME, ALB_NAME)
    azs = [
        a["ZoneName"]
        for a in ec2.describe_availability_zones(
            Filters=[{"Name": "region-name", "Values": [region]}]
        )["AvailabilityZones"]
        if a.get("State") == "available"
    ][:2]
    if len(azs) < 2:
        raise RuntimeError(f"Need ≥2 AZs in {region} to create ALB")

    vpc_id = ec2.create_vpc(CidrBlock="10.92.0.0/16")["Vpc"]["VpcId"]
    ec2.create_tags(
        Resources=[vpc_id],
        Tags=[
            {"Key": "Name", "Value": VPC_NAME},
            {"Key": "Project", "Value": PROJECT},
        ],
    )
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

    igw_id = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    ec2.create_tags(
        Resources=[igw_id],
        Tags=[{"Key": "Name", "Value": f"igw-for-{PROJECT}"}],
    )

    public_subnets: list[str] = []
    for i, az in enumerate(azs):
        sid = ec2.create_subnet(
            VpcId=vpc_id,
            CidrBlock=f"10.92.{i}.0/24",
            AvailabilityZone=az,
        )["Subnet"]["SubnetId"]
        ec2.create_tags(
            Resources=[sid],
            Tags=[
                {"Key": "Name", "Value": f"public-subnet-{i}-for-{PROJECT}"},
                {"Key": "Project", "Value": PROJECT},
            ],
        )
        ec2.modify_subnet_attribute(SubnetId=sid, MapPublicIpOnLaunch={"Value": True})
        public_subnets.append(sid)

    rt = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
    ec2.create_tags(
        Resources=[rt], Tags=[{"Key": "Name", "Value": f"public-rt-for-{PROJECT}"}]
    )
    ec2.create_route(RouteTableId=rt, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
    for sid in public_subnets:
        ec2.associate_route_table(RouteTableId=rt, SubnetId=sid)

    alb_sg = ec2.create_security_group(
        GroupName=ALB_SG_NAME,
        Description=f"ALB SG for {PROJECT}",
        VpcId=vpc_id,
        TagSpecifications=[
            {
                "ResourceType": "security-group",
                "Tags": [
                    {"Key": "Name", "Value": ALB_SG_NAME},
                    {"Key": "Project", "Value": PROJECT},
                ],
            }
        ],
    )["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=alb_sg,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 80,
                "ToPort": 80,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTP"}],
            }
        ],
    )

    ecs_sg = ec2.create_security_group(
        GroupName=ECS_SG_NAME,
        Description=f"ECS SG for {PROJECT}",
        VpcId=vpc_id,
        TagSpecifications=[
            {
                "ResourceType": "security-group",
                "Tags": [
                    {"Key": "Name", "Value": ECS_SG_NAME},
                    {"Key": "Project", "Value": PROJECT},
                ],
            }
        ],
    )["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=ecs_sg,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 8502,
                "ToPort": 8502,
                "UserIdGroupPairs": [{"GroupId": alb_sg, "Description": "ALB to ECS"}],
            }
        ],
    )

    alb = elbv2.create_load_balancer(
        Name=ALB_NAME,
        Subnets=public_subnets,
        SecurityGroups=[alb_sg],
        Scheme="internet-facing",
        Type="application",
        IpAddressType="ipv4",
        Tags=[
            {"Key": "Name", "Value": ALB_NAME},
            {"Key": "Project", "Value": PROJECT},
        ],
    )["LoadBalancers"][0]

    listener = elbv2.create_listener(
        LoadBalancerArn=alb["LoadBalancerArn"],
        Protocol="HTTP",
        Port=80,
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
    )["Listeners"][0]

    logger.info("Created ALB %s (%s)", ALB_NAME, alb["DNSName"])
    return NetworkInfo(
        vpc_id=vpc_id,
        subnets=public_subnets,
        security_groups=[ecs_sg],
        alb_arn=alb["LoadBalancerArn"],
        alb_dns=alb["DNSName"],
        alb_sg=alb_sg,
        listener_arn=listener["ListenerArn"],
        assign_public_ip="ENABLED",
    )


def discover_or_create_network(ecs, elbv2, ec2, region: str) -> NetworkInfo:
    """Reuse existing ob-note ALB/ECS network, or create a minimal stack."""
    try:
        services = ecs.describe_services(
            cluster=CLUSTER, services=[f"service-for-{PROJECT}"]
        ).get("services") or []
        svc = next((s for s in services if s.get("status") != "INACTIVE"), None)
        if svc and svc.get("networkConfiguration"):
            net = svc["networkConfiguration"]["awsvpcConfiguration"]
            subnets = list(net.get("subnets") or [])
            sgs = list(net.get("securityGroups") or [])
            tg = (svc.get("loadBalancers") or [{}])[0].get("targetGroupArn")
            vpc_id = None
            if tg:
                vpc_id = elbv2.describe_target_groups(TargetGroupArns=[tg])[
                    "TargetGroups"
                ][0]["VpcId"]
            alb = elbv2.describe_load_balancers(Names=[ALB_NAME])["LoadBalancers"][0]
            listener = elbv2.describe_listeners(LoadBalancerArn=alb["LoadBalancerArn"])[
                "Listeners"
            ][0]
            logger.info("Using network from ECS service-for-%s", PROJECT)
            return NetworkInfo(
                vpc_id=vpc_id or alb["VpcId"],
                subnets=subnets,
                security_groups=sgs,
                alb_arn=alb["LoadBalancerArn"],
                alb_dns=alb["DNSName"],
                alb_sg=(alb.get("SecurityGroups") or [""])[0],
                listener_arn=listener["ListenerArn"],
                assign_public_ip=net.get("assignPublicIp") or "DISABLED",
            )
    except ClientError as e:
        logger.info(
            "No ECS service yet (%s)",
            e.response.get("Error", {}).get("Code"),
        )

    try:
        alb = elbv2.describe_load_balancers(Names=[ALB_NAME])["LoadBalancers"][0]
        vpc_id = alb["VpcId"]
        listener = elbv2.describe_listeners(LoadBalancerArn=alb["LoadBalancerArn"])[
            "Listeners"
        ][0]
        all_subnets = [
            s["SubnetId"]
            for s in ec2.describe_subnets(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            )["Subnets"]
        ]
        public, private = _classify_subnets(ec2, all_subnets)
        ecs_sg = _find_named_sg(ec2, vpc_id, ECS_SG_NAME) or _find_named_sg(
            ec2, vpc_id, "ecs-sg-for-"
        )
        if not ecs_sg:
            raise RuntimeError(f"No ECS security group in VPC {vpc_id}")
        if private:
            subnets, assign = private, "DISABLED"
        elif public:
            subnets, assign = public, "ENABLED"
        else:
            raise RuntimeError(f"No usable subnets in VPC {vpc_id}")
        logger.info("Using existing ALB %s (assignPublicIp=%s)", ALB_NAME, assign)
        return NetworkInfo(
            vpc_id=vpc_id,
            subnets=subnets[:4],
            security_groups=[ecs_sg],
            alb_arn=alb["LoadBalancerArn"],
            alb_dns=alb["DNSName"],
            alb_sg=(alb.get("SecurityGroups") or [""])[0],
            listener_arn=listener["ListenerArn"],
            assign_public_ip=assign,
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "LoadBalancerNotFound":
            logger.warning("ALB lookup failed: %s", e)

    ensure_ecs_cluster(ecs)
    return _create_minimal_network(ec2, elbv2, region)


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


def _log_acm_validation_records(acm, certificate_arn: str) -> list[dict[str, str]]:
    """Print DNS validation CNAMEs; return list of {Name, Type, Value}."""
    desc = acm.describe_certificate(CertificateArn=certificate_arn)["Certificate"]
    records: list[dict[str, str]] = []
    for opt in desc.get("DomainValidationOptions") or []:
        rr = opt.get("ResourceRecord") or {}
        name = (rr.get("Name") or "").strip()
        value = (rr.get("Value") or "").strip()
        rtype = (rr.get("Type") or "CNAME").strip()
        if name and value:
            records.append({"Name": name, "Type": rtype, "Value": value})
            logger.info(
                "ACM DNS validation → add %s %s → %s",
                rtype,
                name.rstrip("."),
                value.rstrip("."),
            )
    return records


def ensure_acm_certificate(
    acm,
    domain: str,
    *,
    wait_seconds: int = 120,
) -> tuple[str, str, list[dict[str, str]]]:
    """Find or request an ACM cert (us-east-1) for ``domain``.

    Returns ``(certificate_arn, status, validation_records)``.
    Status is typically ``ISSUED`` or ``PENDING_VALIDATION``.
    """
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain:
        raise ValueError("custom domain is empty")

    # Prefer an already-issued cert that covers this exact domain.
    for status_filter in ("ISSUED", "PENDING_VALIDATION"):
        try:
            paginator = acm.get_paginator("list_certificates")
            for page in paginator.paginate(CertificateStatuses=[status_filter]):
                for summary in page.get("CertificateSummaryList") or []:
                    arn = summary["CertificateArn"]
                    detail = acm.describe_certificate(CertificateArn=arn)["Certificate"]
                    names = {
                        (detail.get("DomainName") or "").lower().rstrip("."),
                        *[
                            (n or "").lower().rstrip(".")
                            for n in (detail.get("SubjectAlternativeNames") or [])
                        ],
                    }
                    parent = ".".join(domain.split(".")[1:])
                    covers = domain in names or (parent and f"*.{parent}" in names)
                    if covers:
                        st = detail.get("Status") or status_filter
                        logger.info(
                            "Reusing ACM certificate %s (%s) for %s",
                            arn,
                            st,
                            domain,
                        )
                        return arn, st, _log_acm_validation_records(acm, arn)
        except ClientError as e:
            logger.warning("list_certificates(%s): %s", status_filter, e)

    logger.info("Requesting ACM certificate for %s …", domain)
    resp = acm.request_certificate(
        DomainName=domain,
        ValidationMethod="DNS",
        SubjectAlternativeNames=[domain],
        Tags=[
            {"Key": "Name", "Value": domain},
            {"Key": "Project", "Value": PROJECT},
        ],
    )
    arn = resp["CertificateArn"]
    # Validation options appear shortly after request.
    deadline = time.time() + 60
    while time.time() < deadline:
        records = _log_acm_validation_records(acm, arn)
        if records:
            break
        time.sleep(3)
    else:
        records = _log_acm_validation_records(acm, arn)

    status = "PENDING_VALIDATION"
    if wait_seconds > 0:
        logger.info(
            "Waiting up to %ss for ACM certificate to become ISSUED …", wait_seconds
        )
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            detail = acm.describe_certificate(CertificateArn=arn)["Certificate"]
            status = detail.get("Status") or status
            if status == "ISSUED":
                logger.info("ACM certificate ISSUED: %s", arn)
                return arn, status, records
            if status in {"FAILED", "VALIDATION_TIMED_OUT", "REVOKED"}:
                raise RuntimeError(f"ACM certificate {arn} status={status}")
            time.sleep(10)
        logger.warning(
            "ACM still %s after %ss — attach alias after DNS validation completes",
            status,
            wait_seconds,
        )
    return arn, status, records


def ensure_route53_records(
    *,
    domain: str,
    cloudfront_domain: str,
    validation_records: list[dict[str, str]] | None = None,
    profile: str = "stock",
    hosted_zone_id: str = "",
) -> Optional[str]:
    """Upsert ACM validation CNAMEs + A/AAAA alias in Route53 (often stock account).

    Domain zone ``my-agentic-ai.click`` lives in profile ``stock`` while
    CloudFront/ACM live in the infra account — same pattern as cowork.
    Returns hosted zone id when successful.
    """
    domain = (domain or "").strip().lower().rstrip(".")
    cf_dns = (cloudfront_domain or "").strip().rstrip(".")
    if not domain:
        return None

    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        r53 = session.client("route53")
    except Exception as e:
        logger.warning("Route53 session (profile=%s) unavailable: %s", profile, e)
        return None

    zone_id = (hosted_zone_id or "").strip()
    if not zone_id:
        parent = ".".join(domain.split(".")[1:]) or domain
        try:
            zones = r53.list_hosted_zones().get("HostedZones") or []
            for z in zones:
                name = (z.get("Name") or "").rstrip(".").lower()
                if name == parent or name == domain:
                    zone_id = (z.get("Id") or "").rsplit("/", 1)[-1]
                    break
        except ClientError as e:
            logger.warning("list_hosted_zones: %s", e)
            return None
    if not zone_id:
        logger.warning(
            "No Route53 hosted zone for %s (profile=%s) — add DNS manually",
            domain,
            profile,
        )
        return None

    changes: list[dict[str, Any]] = []
    for rr in validation_records or []:
        name = (rr.get("Name") or "").strip()
        value = (rr.get("Value") or "").strip()
        rtype = (rr.get("Type") or "CNAME").strip() or "CNAME"
        if not name or not value:
            continue
        changes.append(
            {
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": name,
                    "Type": rtype,
                    "TTL": 300,
                    "ResourceRecords": [{"Value": value}],
                },
            }
        )

    if cf_dns:
        # CloudFront alias target hosted zone id is global.
        cf_zone = "Z2FDTNDATAQYW2"
        target = cf_dns if cf_dns.endswith(".") else f"{cf_dns}."
        for rtype in ("A", "AAAA"):
            changes.append(
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": f"{domain}.",
                        "Type": rtype,
                        "AliasTarget": {
                            "HostedZoneId": cf_zone,
                            "DNSName": target,
                            "EvaluateTargetHealth": False,
                        },
                    },
                }
            )

    if not changes:
        return zone_id

    try:
        r53.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={
                "Comment": f"{PROJECT} custom domain {domain}",
                "Changes": changes,
            },
        )
        logger.info(
            "Upserted %d Route53 record(s) in zone %s (profile=%s) for %s",
            len(changes),
            zone_id,
            profile,
            domain,
        )
    except ClientError as e:
        logger.warning("Route53 change_resource_record_sets failed: %s", e)
        return None
    return zone_id


def _apply_custom_domain(
    cfg: dict[str, Any],
    *,
    custom_domain: str,
    certificate_arn: str,
    certificate_status: str,
    cloudfront_domain: str,
) -> str:
    """Return public sharing_url; prefer custom domain when cert is ready."""
    domain = (custom_domain or "").strip().lower().rstrip(".")
    if domain and certificate_status == "ISSUED" and certificate_arn:
        url = f"https://{domain}"
        logger.info("Custom domain ready: %s → %s", domain, cloudfront_domain)
        return url
    if domain:
        logger.warning(
            "custom_domain=%s but ACM status=%s — using CloudFront domain until ISSUED. "
            "After validating the ACM CNAME, re-run installer (or attach alias).",
            domain,
            certificate_status,
        )
    return f"https://{cloudfront_domain}"


def ensure_cloudfront(
    cloudfront,
    *,
    alb_dns: str,
    origin_header: str,
    custom_domain: str = "",
    certificate_arn: str = "",
) -> dict[str, str]:
    """Create or reuse CloudFront distribution with ALB origin (+ optional alias)."""
    origin_id = f"alb-{PROJECT}"
    alias = (custom_domain or "").strip().lower().rstrip(".")
    use_alias = bool(alias and certificate_arn)

    def _viewer_cert() -> dict[str, Any]:
        if use_alias:
            return {
                "CloudFrontDefaultCertificate": False,
                "ACMCertificateArn": certificate_arn,
                "SSLSupportMethod": "sni-only",
                "MinimumProtocolVersion": "TLSv1.2_2021",
            }
        return {"CloudFrontDefaultCertificate": True}

    def _aliases() -> dict[str, Any]:
        if use_alias:
            return {"Quantity": 1, "Items": [alias]}
        return {"Quantity": 0}

    existing = _find_cloudfront(cloudfront)
    if existing:
        dist_id = existing["Id"]
        domain = existing["DomainName"]
        logger.info("Reusing CloudFront %s (%s)", domain, dist_id)
        try:
            cfg_resp = cloudfront.get_distribution_config(Id=dist_id)
            etag = cfg_resp["ETag"]
            cfg = cfg_resp["DistributionConfig"]
            updated = False
            for origin in cfg.get("Origins", {}).get("Items") or []:
                if origin.get("Id") != origin_id and origin.get("DomainName") != alb_dns:
                    continue
                if origin.get("DomainName") != alb_dns:
                    origin["DomainName"] = alb_dns
                    updated = True
                headers = origin.setdefault("CustomHeaders", {"Quantity": 0, "Items": []})
                items = headers.get("Items") or []
                found = False
                for h in items:
                    if h.get("HeaderName") == CUSTOM_HEADER_NAME:
                        if h.get("HeaderValue") != origin_header:
                            h["HeaderValue"] = origin_header
                            updated = True
                        found = True
                        break
                if not found:
                    items.append(
                        {
                            "HeaderName": CUSTOM_HEADER_NAME,
                            "HeaderValue": origin_header,
                        }
                    )
                    updated = True
                headers["Items"] = items
                headers["Quantity"] = len(items)

            desired_aliases = _aliases()
            if cfg.get("Aliases") != desired_aliases:
                cfg["Aliases"] = desired_aliases
                updated = True
            desired_viewer = _viewer_cert()
            current_viewer = cfg.get("ViewerCertificate") or {}
            if use_alias:
                if (
                    current_viewer.get("ACMCertificateArn") != certificate_arn
                    or current_viewer.get("CloudFrontDefaultCertificate")
                ):
                    cfg["ViewerCertificate"] = desired_viewer
                    updated = True
            # Keep existing ACM alias cert if already set and we are not attaching yet.

            if updated:
                cloudfront.update_distribution(
                    Id=dist_id, IfMatch=etag, DistributionConfig=cfg
                )
                logger.info("Updated CloudFront origin / alias / certificate")
        except ClientError as e:
            logger.warning("Could not refresh CloudFront: %s", e)
        public_url = f"https://{alias}" if use_alias else f"https://{domain}"
        return {
            "id": dist_id,
            "domain": domain,
            "url": public_url,
            "alias": alias if use_alias else "",
        }

    config = {
        "CallerReference": f"{PROJECT}-ui-{int(time.time())}",
        "Comment": CF_COMMENT,
        "Enabled": True,
        "DefaultRootObject": "",
        "Aliases": _aliases(),
        "Origins": {
            "Quantity": 1,
            "Items": [
                {
                    "Id": origin_id,
                    "DomainName": alb_dns,
                    "OriginPath": "",
                    "CustomHeaders": {
                        "Quantity": 1,
                        "Items": [
                            {
                                "HeaderName": CUSTOM_HEADER_NAME,
                                "HeaderValue": origin_header,
                            }
                        ],
                    },
                    "CustomOriginConfig": {
                        "HTTPPort": 80,
                        "HTTPSPort": 443,
                        "OriginProtocolPolicy": "http-only",
                        "OriginSslProtocols": {
                            "Quantity": 1,
                            "Items": ["TLSv1.2"],
                        },
                        "OriginReadTimeout": 60,
                        "OriginKeepaliveTimeout": 60,
                    },
                }
            ],
        },
        "DefaultCacheBehavior": {
            "TargetOriginId": origin_id,
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {
                "Quantity": 7,
                "Items": [
                    "GET",
                    "HEAD",
                    "OPTIONS",
                    "PUT",
                    "POST",
                    "PATCH",
                    "DELETE",
                ],
                "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
            },
            "Compress": True,
            "CachePolicyId": CF_CACHE_POLICY_DISABLED,
            "OriginRequestPolicyId": CF_ORIGIN_REQUEST_ALL_VIEWER,
        },
        "PriceClass": "PriceClass_200",
        "ViewerCertificate": _viewer_cert(),
        "HttpVersion": "http2",
        "IsIPV6Enabled": True,
    }
    resp = cloudfront.create_distribution(DistributionConfig=config)
    dist = resp["Distribution"]
    domain = dist["DomainName"]
    logger.info("Created CloudFront %s (%s)", domain, dist["Id"])
    public_url = f"https://{alias}" if use_alias else f"https://{domain}"
    return {
        "id": dist["Id"],
        "domain": domain,
        "url": public_url,
        "alias": alias if use_alias else "",
    }


def ensure_infra_stack(
    *,
    cfg: dict[str, Any],
    s3,
    sm,
    iam,
    ecs,
    elbv2,
    ec2,
    cloudfront,
    acm=None,
) -> tuple[dict[str, Any], NetworkInfo, str]:
    """Ensure config + AWS resources. Returns (cfg, network, origin_header)."""
    region = str(cfg["region"])
    account = str(cfg["accountId"])
    bucket = str(cfg["s3_bucket"])
    custom_domain = (
        str(cfg.get("custom_domain") or DEFAULT_CUSTOM_DOMAIN).strip().lower().rstrip(".")
    )
    cfg["custom_domain"] = custom_domain

    logger.info("[infra] S3 bucket")
    ensure_s3_bucket(s3, bucket, region)
    cfg["s3_arn"] = f"arn:aws:s3:::{bucket}"

    logger.info("[infra] Secrets (origin header + session signing key)")
    origin_header = ensure_origin_header_secret(sm)
    ensure_session_signing_key(sm)

    logger.info("[infra] ECS IAM roles")
    ensure_ecs_roles(iam, account, region, bucket)

    logger.info("[infra] ECS cluster")
    ensure_ecs_cluster(ecs)

    logger.info("[infra] Network (discover or create)")
    network = discover_or_create_network(ecs, elbv2, ec2, region)

    if acm is None:
        acm = boto3.client("acm", region_name="us-east-1")

    logger.info("[infra] ACM certificate for %s", custom_domain)
    cert_arn, cert_status, validation = ensure_acm_certificate(
        acm, custom_domain, wait_seconds=90
    )
    cfg["acm_certificate_arn"] = cert_arn
    cfg["acm_certificate_status"] = cert_status

    logger.info("[infra] CloudFront (ALB origin + custom domain)")
    attach_alias = cert_status == "ISSUED"
    cf = ensure_cloudfront(
        cloudfront,
        alb_dns=network.alb_dns,
        origin_header=origin_header,
        custom_domain=custom_domain if attach_alias else "",
        certificate_arn=cert_arn if attach_alias else "",
    )
    cfg["cloudfront_id"] = cf["id"]
    cfg["cloudfront_domain"] = cf["domain"]

    # DNS often lives in a separate account (AWS_PROFILE=stock).
    r53_profile = str(cfg.get("route53_profile") or "stock").strip() or "stock"
    zone = ensure_route53_records(
        domain=custom_domain,
        cloudfront_domain=cf["domain"],
        validation_records=validation if cert_status != "ISSUED" else None,
        profile=r53_profile,
        hosted_zone_id=str(cfg.get("route53_hosted_zone_id") or ""),
    )
    if zone:
        cfg["route53_hosted_zone_id"] = zone
        cfg["route53_profile"] = r53_profile
        if cert_status != "ISSUED":
            logger.info("[infra] Re-checking ACM after Route53 validation records…")
            cert_arn, cert_status, _ = ensure_acm_certificate(
                acm, custom_domain, wait_seconds=180
            )
            cfg["acm_certificate_arn"] = cert_arn
            cfg["acm_certificate_status"] = cert_status
            if cert_status == "ISSUED":
                cf = ensure_cloudfront(
                    cloudfront,
                    alb_dns=network.alb_dns,
                    origin_header=origin_header,
                    custom_domain=custom_domain,
                    certificate_arn=cert_arn,
                )
                cfg["cloudfront_id"] = cf["id"]
                cfg["cloudfront_domain"] = cf["domain"]
                ensure_route53_records(
                    domain=custom_domain,
                    cloudfront_domain=cf["domain"],
                    validation_records=None,
                    profile=r53_profile,
                    hosted_zone_id=zone,
                )

    cfg["sharing_url"] = _apply_custom_domain(
        cfg,
        custom_domain=custom_domain,
        certificate_arn=cert_arn,
        certificate_status=cert_status,
        cloudfront_domain=cf["domain"],
    )
    logger.info("sharing_url → %s", cfg["sharing_url"])
    if custom_domain:
        logger.info(
            "DNS: %s → %s (Route53 profile=%s)",
            custom_domain,
            cf["domain"],
            r53_profile,
        )

    return cfg, network, origin_header


# Backward-compatible alias
ensure_shared_stack = ensure_infra_stack
