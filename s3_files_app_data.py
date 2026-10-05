"""Amazon S3 Files app-data storage for ECS only (agentic-work pattern).

Provisions a dedicated S3 Files filesystem scoped to the bucket root (``/``)
and mounts it on the ECS task at ``/mnt/app-data``. Vault markdown stays on
the S3 API (``vault/``); SQLite (notes.db) is persisted via this mount.

No AgentCore Runtime / ``/mnt/workspace`` session storage is created here.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

APP_DATA_MOUNT_PATH = "/mnt/app-data"
# Empty prefix scopes /mnt/app-data to s3://{bucket}/.
S3_FILES_APP_DATA_PREFIX = ""
LEGACY_S3_FILES_APP_DATA_PREFIX = "app-data/"

logger = logging.getLogger("ob-note-s3files")


class S3FilesAppDataProvisioner:
    """Idempotent S3 Files app-data FS for ECS Fargate."""

    def __init__(
        self,
        *,
        region: str,
        account_id: str,
        project_name: str,
        ec2_client=None,
        s3_client=None,
        s3files_client=None,
        iam_client=None,
    ):
        self.region = region
        self.account_id = str(account_id)
        self.project_name = project_name
        self.ec2 = ec2_client or boto3.client("ec2", region_name=region)
        self.s3 = s3_client or boto3.client("s3", region_name=region)
        self.s3files = s3files_client or boto3.client("s3files", region_name=region)
        self.iam = iam_client or boto3.client("iam")

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _normalize_prefix(prefix: str) -> str:
        if not prefix or prefix == "/":
            return ""
        return prefix if prefix.endswith("/") else f"{prefix}/"

    def _wait_status(
        self,
        describe_fn,
        resource_id_key: str,
        resource_id: str,
        *,
        ready_status: str = "available",
        max_wait_seconds: int = 600,
        poll_seconds: int = 10,
    ) -> None:
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            response = describe_fn(**{resource_id_key: resource_id})
            status = (response.get("status") or "").lower()
            if status == ready_status.lower():
                return
            if status in {"error", "deleted"}:
                message = response.get("statusMessage", "")
                raise RuntimeError(
                    f"S3 Files resource {resource_id} entered status {status}: {message}"
                )
            time.sleep(poll_seconds)
        raise TimeoutError(f"Timed out waiting for S3 Files resource {resource_id}")

    def _ensure_bucket_versioning(self, bucket: str) -> None:
        status = self.s3.get_bucket_versioning(Bucket=bucket).get("Status")
        if status == "Enabled":
            return
        logger.info("  Enabling S3 bucket versioning for S3 Files: %s", bucket)
        self.s3.put_bucket_versioning(
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )

    def _get_or_create_sync_role(self, s3_bucket_arn: str) -> str:
        role_name = f"role-s3files-sync-for-{self.project_name}"
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AllowS3FilesAssumeRole",
                    "Effect": "Allow",
                    "Principal": {"Service": "elasticfilesystem.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "StringEquals": {"aws:SourceAccount": self.account_id},
                        "ArnLike": {
                            "aws:SourceArn": (
                                f"arn:aws:s3files:{self.region}:{self.account_id}:file-system/*"
                            )
                        },
                    },
                }
            ],
        }
        bucket_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:ListBucket",
                        "s3:ListBucketVersions",
                        "s3:GetBucketLocation",
                        "s3:GetBucketVersioning",
                        "s3:AbortMultipartUpload",
                        "s3:ListMultipartUploadParts",
                        "s3:GetObject",
                        "s3:GetObjectVersion",
                        "s3:GetObjectTagging",
                        "s3:GetObjectVersionTagging",
                        "s3:PutObject",
                        "s3:PutObjectTagging",
                        "s3:DeleteObject",
                        "s3:DeleteObjectVersion",
                    ],
                    "Resource": [s3_bucket_arn, f"{s3_bucket_arn}/*"],
                    "Condition": {
                        "StringEquals": {"aws:ResourceAccount": self.account_id}
                    },
                }
            ],
        }
        eventbridge_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "EventBridgeManage",
                    "Effect": "Allow",
                    "Action": [
                        "events:PutRule",
                        "events:PutTargets",
                        "events:DeleteRule",
                        "events:DisableRule",
                        "events:EnableRule",
                        "events:RemoveTargets",
                    ],
                    "Resource": "arn:aws:events:*:*:rule/DO-NOT-DELETE-S3-Files*",
                    "Condition": {
                        "StringEquals": {
                            "events:ManagedBy": "elasticfilesystem.amazonaws.com"
                        }
                    },
                },
                {
                    "Sid": "EventBridgeRead",
                    "Effect": "Allow",
                    "Action": [
                        "events:DescribeRule",
                        "events:ListRules",
                        "events:ListRuleNamesByTarget",
                        "events:ListTargetsByRule",
                    ],
                    "Resource": "arn:aws:events:*:*:rule/*",
                },
            ],
        }

        created = False
        try:
            role = self.iam.get_role(RoleName=role_name)
            role_arn = role["Role"]["Arn"]
            self.iam.update_assume_role_policy(
                RoleName=role_name,
                PolicyDocument=json.dumps(trust_policy),
            )
            logger.info("  Reusing S3 Files sync role: %s", role_arn)
        except self.iam.exceptions.NoSuchEntityException:
            role = self.iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description=f"S3 Files sync role for {self.project_name}",
                Tags=[
                    {"Key": "Name", "Value": role_name},
                    {"Key": "Project", "Value": self.project_name},
                ],
            )
            role_arn = role["Role"]["Arn"]
            created = True
            logger.info("  Created S3 Files sync role: %s", role_arn)

        for policy_name, document in (
            ("s3-bucket-access", bucket_policy),
            ("eventbridge-sync", eventbridge_policy),
        ):
            self.iam.put_role_policy(
                RoleName=role_name,
                PolicyName=policy_name,
                PolicyDocument=json.dumps(document),
            )

        if created:
            wait_seconds = 15
            logger.info("  Waiting %ss for IAM role propagation: %s", wait_seconds, role_name)
            time.sleep(wait_seconds)
        return role_arn

    @staticmethod
    def _file_system_name_tag(item: dict) -> str:
        for tag in item.get("tags") or []:
            if tag.get("key") == "Name" or tag.get("Key") == "Name":
                return tag.get("value") or tag.get("Value") or ""
        return item.get("name") or ""

    def _get_file_system_detail(self, file_system_id: str) -> Optional[dict[str, str]]:
        """Fetch one FS; list APIs often omit ``prefix``, so get is authoritative."""
        if not file_system_id:
            return None
        try:
            item = self.s3files.get_file_system(fileSystemId=file_system_id)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in {
                "ResourceNotFoundException",
                "FileSystemNotFound",
                "NotFound",
                "404",
            }:
                return None
            logger.warning("  get_file_system %s: %s", file_system_id, e)
            return None
        fs_id = item.get("fileSystemId") or file_system_id
        return {
            "file_system_id": fs_id,
            "file_system_arn": item.get("fileSystemArn")
            or f"arn:aws:s3files:{self.region}:{self.account_id}:file-system/{fs_id}",
            "prefix": self._normalize_prefix(item.get("prefix") or ""),
            "bucket": item.get("bucket") or "",
            "name": self._file_system_name_tag(item) or (item.get("name") or ""),
            "status": (item.get("status") or "").lower(),
        }

    def _find_file_system(
        self, s3_bucket_arn: str, prefix: str, name_tag: str
    ) -> Optional[dict[str, str]]:
        want_prefix = self._normalize_prefix(prefix)
        matches: list[dict[str, str]] = []
        paginator = self.s3files.get_paginator("list_file_systems")
        for page in paginator.paginate():
            for item in page.get("fileSystems", []):
                if item.get("bucket") != s3_bucket_arn:
                    continue
                fs_id = item.get("fileSystemId") or ""
                if not fs_id:
                    continue
                # list_file_systems often returns prefix=None — resolve via get.
                item_prefix = item.get("prefix")
                name = self._file_system_name_tag(item) or (item.get("name") or "")
                if item_prefix is None or item_prefix == "":
                    detail = self._get_file_system_detail(fs_id)
                    if not detail:
                        continue
                    item_prefix = detail.get("prefix") or ""
                    name = detail.get("name") or name
                    arn = detail.get("file_system_arn") or ""
                else:
                    item_prefix = self._normalize_prefix(item_prefix)
                    arn = item.get("fileSystemArn") or ""
                if self._normalize_prefix(item_prefix) != want_prefix:
                    continue
                matches.append(
                    {
                        "file_system_id": fs_id,
                        "file_system_arn": arn,
                        "prefix": self._normalize_prefix(item_prefix),
                        "name": name,
                    }
                )
        if not matches:
            return None
        for match in matches:
            if match.get("name") == name_tag:
                return {
                    "file_system_id": match["file_system_id"],
                    "file_system_arn": match["file_system_arn"],
                    "prefix": match["prefix"],
                }
        first = matches[0]
        return {
            "file_system_id": first["file_system_id"],
            "file_system_arn": first["file_system_arn"],
            "prefix": first["prefix"],
        }

    def _get_or_create_file_system(
        self,
        s3_bucket_arn: str,
        role_arn: str,
        *,
        prefix: str,
        name_tag: str,
        preferred_file_system_id: str = "",
    ) -> dict[str, str]:
        want_prefix = self._normalize_prefix(prefix)
        preferred = (preferred_file_system_id or "").strip()
        if preferred:
            detail = self._get_file_system_detail(preferred)
            if (
                detail
                and detail.get("status") == "available"
                and detail.get("bucket") == s3_bucket_arn
                and detail.get("prefix") == want_prefix
            ):
                logger.info(
                    "  Reusing preferred S3 Files file system: %s (prefix=%s)",
                    preferred,
                    detail.get("prefix") or want_prefix,
                )
                return {
                    "file_system_id": detail["file_system_id"],
                    "file_system_arn": detail["file_system_arn"],
                    "prefix": detail.get("prefix") or want_prefix,
                }
            logger.warning(
                "  Preferred FS %s not reusable — falling back to search/create",
                preferred,
            )

        existing = self._find_file_system(s3_bucket_arn, prefix, name_tag)
        if existing and existing.get("file_system_id"):
            logger.info(
                "  Reusing S3 Files file system: %s (prefix=%s)",
                existing["file_system_id"],
                existing.get("prefix") or prefix,
            )
            return existing

        bucket = s3_bucket_arn.removeprefix("arn:aws:s3:::")
        self._ensure_bucket_versioning(bucket)
        normalized = self._normalize_prefix(prefix)
        create_kwargs: dict[str, Any] = {
            "bucket": s3_bucket_arn,
            "roleArn": role_arn,
            "acceptBucketWarning": True,
            "tags": [{"key": "Name", "value": name_tag}],
        }
        # Omit prefix so the file system scopes the entire bucket.
        if normalized:
            create_kwargs["prefix"] = normalized
        resp = self.s3files.create_file_system(**create_kwargs)
        fs_id = resp["fileSystemId"]
        logger.info("  Created S3 Files file system: %s (prefix=%s)", fs_id, normalized)
        self._wait_status(self.s3files.get_file_system, "fileSystemId", fs_id)
        return {
            "file_system_id": fs_id,
            "file_system_arn": resp.get("fileSystemArn", ""),
            "prefix": normalized,
        }

    def cleanup_duplicate_file_systems(
        self,
        *,
        keep_file_system_id: str,
        s3_bucket_arn: str,
        prefix: str,
        name_tag: str,
    ) -> list[str]:
        """Delete same-bucket/prefix/name FS duplicates, keeping ``keep_file_system_id``."""
        keep = (keep_file_system_id or "").strip()
        if not keep:
            return []
        want_prefix = self._normalize_prefix(prefix)
        deleted: list[str] = []
        paginator = self.s3files.get_paginator("list_file_systems")
        candidates: list[str] = []
        for page in paginator.paginate():
            for item in page.get("fileSystems", []):
                if item.get("bucket") != s3_bucket_arn:
                    continue
                fs_id = item.get("fileSystemId") or ""
                if not fs_id or fs_id == keep:
                    continue
                name = self._file_system_name_tag(item) or (item.get("name") or "")
                item_prefix = item.get("prefix")
                if item_prefix is None or item_prefix == "":
                    detail = self._get_file_system_detail(fs_id)
                    if not detail:
                        continue
                    item_prefix = detail.get("prefix") or ""
                    name = detail.get("name") or name
                if name and name != name_tag:
                    continue
                if (
                    item_prefix
                    and self._normalize_prefix(item_prefix) != want_prefix
                ):
                    continue
                # Same name tag (or empty name with matching prefix) → orphan duplicate
                if name == name_tag or (
                    not name and self._normalize_prefix(item_prefix) == want_prefix
                ):
                    candidates.append(fs_id)

        for fs_id in candidates:
            logger.info("  Cleaning duplicate S3 Files FS %s …", fs_id)
            try:
                ap_paginator = self.s3files.get_paginator("list_access_points")
                for page in ap_paginator.paginate(fileSystemId=fs_id):
                    for ap in page.get("accessPoints") or []:
                        ap_id = ap.get("accessPointId")
                        if not ap_id:
                            continue
                        try:
                            self.s3files.delete_access_point(accessPointId=ap_id)
                            logger.info("    Deleted access point %s", ap_id)
                        except ClientError as e:
                            logger.warning("    delete_access_point %s: %s", ap_id, e)
                mt_paginator = self.s3files.get_paginator("list_mount_targets")
                mt_ids: list[str] = []
                for page in mt_paginator.paginate(fileSystemId=fs_id):
                    for mt in page.get("mountTargets") or []:
                        mt_id = mt.get("mountTargetId")
                        if not mt_id:
                            continue
                        mt_ids.append(mt_id)
                        try:
                            self.s3files.delete_mount_target(mountTargetId=mt_id)
                            logger.info("    Deleted mount target %s", mt_id)
                        except ClientError as e:
                            logger.warning("    delete_mount_target %s: %s", mt_id, e)
                # Mount targets must finish deleting before the FS can be removed.
                deadline = time.time() + 180
                while time.time() < deadline:
                    remaining = []
                    for page in mt_paginator.paginate(fileSystemId=fs_id):
                        for mt in page.get("mountTargets") or []:
                            mid = mt.get("mountTargetId")
                            if mid:
                                remaining.append(mid)
                    if not remaining:
                        break
                    time.sleep(5)
                else:
                    logger.warning(
                        "  Mount targets still present on %s after wait — skip FS delete",
                        fs_id,
                    )
                    continue
                time.sleep(2)
                self.s3files.delete_file_system(fileSystemId=fs_id, forceDelete=True)
                logger.info("  ✓ Deleted duplicate file system %s", fs_id)
                deleted.append(fs_id)
            except ClientError as e:
                logger.warning("  Could not delete duplicate FS %s: %s", fs_id, e)
        if deleted:
            logger.info(
                "  Removed %d duplicate S3 Files FS(s); kept %s",
                len(deleted),
                keep,
            )
        return deleted

    def _ensure_nfs_access(self, client_sg_id: str, mount_sg_id: str) -> None:
        if not client_sg_id or not mount_sg_id:
            return
        try:
            self.ec2.authorize_security_group_egress(
                GroupId=client_sg_id,
                IpPermissions=[
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 2049,
                        "ToPort": 2049,
                        "UserIdGroupPairs": [{"GroupId": mount_sg_id}],
                    }
                ],
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                logger.warning("  NFS egress on %s: %s", client_sg_id, e)

        try:
            self.ec2.authorize_security_group_ingress(
                GroupId=mount_sg_id,
                IpPermissions=[
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 2049,
                        "ToPort": 2049,
                        "UserIdGroupPairs": [{"GroupId": client_sg_id}],
                    }
                ],
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                logger.warning("  NFS ingress on %s: %s", mount_sg_id, e)

    def _get_or_create_mount_sg(self, vpc_id: str, client_sg_ids: list[str]) -> str:
        group_name = f"s3files-mount-sg-for-{self.project_name}"
        unique = []
        for sg_id in client_sg_ids:
            if sg_id and sg_id not in unique:
                unique.append(sg_id)

        try:
            resp = self.ec2.create_security_group(
                GroupName=group_name,
                Description=f"S3 Files mount SG for {self.project_name}",
                VpcId=vpc_id,
                TagSpecifications=[
                    {
                        "ResourceType": "security-group",
                        "Tags": [
                            {"Key": "Name", "Value": group_name},
                            {"Key": "Project", "Value": self.project_name},
                        ],
                    }
                ],
            )
            sg_id = resp["GroupId"]
            logger.info("  Created mount security group: %s", sg_id)
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidGroup.Duplicate":
                raise
            found = self.ec2.describe_security_groups(
                Filters=[
                    {"Name": "group-name", "Values": [group_name]},
                    {"Name": "vpc-id", "Values": [vpc_id]},
                ]
            )["SecurityGroups"]
            if not found:
                raise
            sg_id = found[0]["GroupId"]
            logger.info("  Reusing mount security group: %s", sg_id)

        if unique:
            try:
                self.ec2.authorize_security_group_ingress(
                    GroupId=sg_id,
                    IpPermissions=[
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 2049,
                            "ToPort": 2049,
                            "UserIdGroupPairs": [{"GroupId": cid}],
                        }
                        for cid in unique
                    ],
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                    logger.warning("  mount SG ingress: %s", e)

        for client_sg_id in unique:
            self._ensure_nfs_access(client_sg_id, sg_id)
        return sg_id

    def _ensure_mount_targets(
        self,
        file_system_id: str,
        subnet_ids: list[str],
        security_group_ids: list[str],
    ) -> None:
        existing: set[str] = set()
        paginator = self.s3files.get_paginator("list_mount_targets")
        for page in paginator.paginate(fileSystemId=file_system_id):
            for item in page.get("mountTargets", []):
                if item.get("subnetId"):
                    existing.add(item["subnetId"])

        for subnet_id in subnet_ids:
            if subnet_id in existing:
                logger.info("  Reusing mount target in %s", subnet_id)
                continue
            resp = self.s3files.create_mount_target(
                fileSystemId=file_system_id,
                subnetId=subnet_id,
                securityGroups=security_group_ids,
            )
            mt_id = resp.get("mountTargetId", subnet_id)
            logger.info("  Created mount target %s in %s", mt_id, subnet_id)
            self._wait_status(self.s3files.get_mount_target, "mountTargetId", mt_id)

    @staticmethod
    def _access_point_name_tag(item: dict) -> str:
        for tag in item.get("tags") or []:
            if tag.get("key") == "Name" or tag.get("Key") == "Name":
                return tag.get("value") or tag.get("Value") or ""
        return item.get("name") or ""

    def _get_or_create_access_point(self, file_system_id: str, *, name_tag: str) -> str:
        aps: list[dict] = []
        paginator = self.s3files.get_paginator("list_access_points")
        for page in paginator.paginate(fileSystemId=file_system_id):
            aps.extend(page.get("accessPoints") or [])

        for item in aps:
            arn = item.get("accessPointArn")
            if not arn:
                continue
            if self._access_point_name_tag(item) == name_tag:
                logger.info("  Reusing S3 Files access point: %s", arn)
                return arn
        if len(aps) == 1:
            arn = aps[0].get("accessPointArn")
            if arn:
                logger.info("  Reusing legacy S3 Files access point: %s", arn)
                return arn

        resp = self.s3files.create_access_point(
            fileSystemId=file_system_id,
            posixUser={"uid": 0, "gid": 0},
            rootDirectory={
                "path": "/",
                "creationPermissions": {
                    "ownerUid": 0,
                    "ownerGid": 0,
                    "permissions": "0777",
                },
            },
            tags=[{"key": "Name", "value": name_tag}],
        )
        arn = resp["accessPointArn"]
        logger.info("  Created S3 Files access point: %s", arn)
        self._wait_status(
            self.s3files.get_access_point, "accessPointId", resp["accessPointId"]
        )
        return arn

    def _put_file_system_policy(
        self,
        file_system_id: str,
        access_point_arn: str,
        client_role_arns: list[str],
    ) -> None:
        principals = [a for a in client_role_arns if a]
        if not principals:
            return
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "AWS": principals if len(principals) > 1 else principals[0]
                    },
                    "Action": [
                        "s3files:ClientMount",
                        "s3files:ClientWrite",
                        "s3files:ClientRootAccess",
                    ],
                    "Condition": {
                        "StringEquals": {
                            "s3files:AccessPointArn": access_point_arn,
                        }
                    },
                }
            ],
        }
        try:
            self.s3files.put_file_system_policy(
                fileSystemId=file_system_id,
                policy=json.dumps(policy),
            )
            logger.info("  Applied S3 Files app-data FS policy (ECS only)")
        except ClientError as e:
            logger.warning("  Could not apply S3 Files FS policy: %s", e)

    def attach_ecs_task_policy(
        self,
        ecs_task_role_name: str,
        file_system_id: str,
        access_point_arn: str,
    ) -> None:
        if not ecs_task_role_name or not file_system_id or not access_point_arn:
            return
        fs_arn = (
            f"arn:aws:s3files:{self.region}:{self.account_id}:file-system/{file_system_id}"
        )
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "S3FilesAppDataClientAccess",
                    "Effect": "Allow",
                    "Action": [
                        "s3files:ClientMount",
                        "s3files:ClientWrite",
                        "s3files:ClientRootAccess",
                    ],
                    "Resource": fs_arn,
                    "Condition": {
                        "ArnEquals": {
                            "s3files:AccessPointArn": access_point_arn,
                        }
                    },
                },
                {
                    "Sid": "S3FilesAppDataGetAccessPoint",
                    "Effect": "Allow",
                    "Action": ["s3files:GetAccessPoint"],
                    "Resource": access_point_arn,
                },
                {
                    "Sid": "S3FilesAppDataListMountTargets",
                    "Effect": "Allow",
                    "Action": ["s3files:ListMountTargets"],
                    "Resource": fs_arn,
                },
            ],
        }
        self.iam.put_role_policy(
            RoleName=ecs_task_role_name,
            PolicyName=f"s3files-ecs-task-policy-for-{self.project_name}",
            PolicyDocument=json.dumps(policy),
        )
        logger.info("  ✓ Attached app-data S3 Files policy to %s", ecs_task_role_name)

    def prepare_for_ecs(
        self,
        app_data_info: dict[str, Any],
        *,
        ecs_sg_id: str,
        ecs_task_role_name: str,
    ) -> None:
        file_system_id = str(app_data_info.get("file_system_id") or "")
        access_point_arn = str(app_data_info.get("access_point_arn") or "")
        if not file_system_id or not access_point_arn:
            logger.warning("  Skipping S3 Files ECS prep: missing app-data FS/AP")
            return

        mount_sg_id = str(app_data_info.get("mount_sg_id") or "")
        if ecs_sg_id and mount_sg_id:
            self._ensure_nfs_access(ecs_sg_id, mount_sg_id)

        if ecs_task_role_name:
            role_arn = f"arn:aws:iam::{self.account_id}:role/{ecs_task_role_name}"
            self._put_file_system_policy(file_system_id, access_point_arn, [role_arn])
            self.attach_ecs_task_policy(
                ecs_task_role_name, file_system_id, access_point_arn
            )

    def create_app_data_storage(
        self,
        *,
        vpc_id: str,
        subnet_ids: list[str],
        s3_bucket_name: str,
        ecs_sg_id: str = "",
        ecs_task_role_name: str = "",
        preferred_file_system_id: str = "",
        cleanup_duplicates: bool = True,
    ) -> dict[str, Any]:
        """Provision or reuse bucket-root S3 Files FS + mount targets for ECS."""
        logger.info(
            "Ensuring S3 Files app-data storage (ECS → %s, prefix=%s)",
            APP_DATA_MOUNT_PATH,
            S3_FILES_APP_DATA_PREFIX or "/",
        )
        if not subnet_ids:
            raise RuntimeError("At least one subnet is required for S3 Files mount targets")

        s3_bucket_arn = f"arn:aws:s3:::{s3_bucket_name}"
        name_tag = f"s3files-app-data-for-{self.project_name}"
        if S3_FILES_APP_DATA_PREFIX:
            try:
                self.s3.put_object(
                    Bucket=s3_bucket_name, Key=S3_FILES_APP_DATA_PREFIX, Body=b""
                )
            except ClientError as e:
                logger.warning("  app-data prefix marker: %s", e)
        else:
            self._relocate_legacy_prefix(s3_bucket_name, LEGACY_S3_FILES_APP_DATA_PREFIX)

        sync_role_arn = self._get_or_create_sync_role(s3_bucket_arn)
        file_system = self._get_or_create_file_system(
            s3_bucket_arn,
            sync_role_arn,
            prefix=S3_FILES_APP_DATA_PREFIX,
            name_tag=name_tag,
            preferred_file_system_id=preferred_file_system_id,
        )
        file_system_id = file_system["file_system_id"]

        if cleanup_duplicates:
            self.cleanup_duplicate_file_systems(
                keep_file_system_id=file_system_id,
                s3_bucket_arn=s3_bucket_arn,
                prefix=S3_FILES_APP_DATA_PREFIX,
                name_tag=name_tag,
            )

        mount_sg_id = self._get_or_create_mount_sg(
            vpc_id, [ecs_sg_id] if ecs_sg_id else []
        )
        self._ensure_mount_targets(file_system_id, list(subnet_ids), [mount_sg_id])
        access_point_arn = self._get_or_create_access_point(
            file_system_id,
            name_tag=f"s3files-ap-app-data-for-{self.project_name}",
        )

        info: dict[str, Any] = {
            "file_system_id": file_system_id,
            "file_system_arn": file_system.get("file_system_arn")
            or f"arn:aws:s3files:{self.region}:{self.account_id}:file-system/{file_system_id}",
            "access_point_arn": access_point_arn,
            "mount_path": APP_DATA_MOUNT_PATH,
            "prefix": S3_FILES_APP_DATA_PREFIX or "/",
            "mount_sg_id": mount_sg_id,
            "subnets": list(subnet_ids),
        }

        if ecs_task_role_name:
            self.prepare_for_ecs(
                info,
                ecs_sg_id=ecs_sg_id,
                ecs_task_role_name=ecs_task_role_name,
            )

        logger.info("✓ S3 Files app-data storage ready")
        logger.info("  File system: %s", file_system_id)
        logger.info("  Access point: %s", access_point_arn)
        logger.info("  Mount path: %s", APP_DATA_MOUNT_PATH)
        logger.info("  Prefix: %s", S3_FILES_APP_DATA_PREFIX or "/")
        return info

    def _relocate_legacy_prefix(self, bucket: str, src_prefix: str) -> int:
        """Copy ``{src_prefix}{path}`` to ``{path}`` when the destination is missing.

        Source keys stay in place. Existing bucket-root objects are not overwritten,
        so ``vault/`` is left as-is when ``app-data/vault/`` also exists.
        """
        if not src_prefix:
            return 0
        copied = 0
        try:
            paginator = self.s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=src_prefix):
                for obj in page.get("Contents") or []:
                    src_key = obj.get("Key") or ""
                    if not src_key.startswith(src_prefix) or src_key.endswith("/"):
                        continue
                    dst_key = src_key[len(src_prefix) :]
                    if not dst_key or dst_key == "vault" or dst_key.startswith("vault/"):
                        continue
                    try:
                        self.s3.head_object(Bucket=bucket, Key=dst_key)
                        continue
                    except ClientError as e:
                        code = e.response.get("Error", {}).get("Code", "")
                        if code not in {"404", "NoSuchKey", "NotFound"}:
                            logger.warning("  head %s: %s", dst_key, e)
                            continue
                    self.s3.copy_object(
                        Bucket=bucket,
                        Key=dst_key,
                        CopySource={"Bucket": bucket, "Key": src_key},
                    )
                    copied += 1
        except ClientError as e:
            logger.warning("  legacy prefix relocate skipped: %s", e)
            return copied
        if copied:
            logger.info(
                "  Copied %s object(s) from %s to bucket root", copied, src_prefix
            )
        return copied


def apply_app_data_config(
    cfg: dict[str, Any], app_data_info: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """Write S3 Files app-data keys into config.json."""
    if not app_data_info:
        return cfg
    cfg["s3_files_app_data_file_system_id"] = app_data_info.get("file_system_id", "")
    cfg["s3_files_app_data_access_point_arn"] = app_data_info.get(
        "access_point_arn", ""
    )
    cfg["s3_files_app_data_mount_path"] = app_data_info.get(
        "mount_path", APP_DATA_MOUNT_PATH
    )
    cfg["s3_files_app_data_prefix"] = app_data_info.get("prefix") or "/"
    return cfg


def delete_app_data_storage(
    *,
    region: str,
    account_id: str,
    project_name: str,
    cfg: dict[str, Any],
    s3files_client=None,
    iam_client=None,
) -> None:
    """Delete the app-data S3 Files filesystem and sync role (best-effort)."""
    s3files = s3files_client or boto3.client("s3files", region_name=region)
    iam = iam_client or boto3.client("iam")

    fs_ids: list[str] = []
    configured = str(cfg.get("s3_files_app_data_file_system_id") or "").strip()
    if configured:
        fs_ids.append(configured)

    name_tag = f"s3files-app-data-for-{project_name}"
    try:
        paginator = s3files.get_paginator("list_file_systems")
        for page in paginator.paginate():
            for item in page.get("fileSystems", []):
                fs_id = item.get("fileSystemId") or ""
                if not fs_id or fs_id in fs_ids:
                    continue
                tags = item.get("tags") or []
                for tag in tags:
                    key = tag.get("key") or tag.get("Key")
                    val = tag.get("value") or tag.get("Value")
                    if key == "Name" and val == name_tag:
                        fs_ids.append(fs_id)
                        break
    except ClientError as e:
        logger.warning("  list_file_systems: %s", e)

    for fs_id in fs_ids:
        logger.info("  Deleting S3 Files FS %s …", fs_id)
        try:
            # Delete access points first
            paginator = s3files.get_paginator("list_access_points")
            for page in paginator.paginate(fileSystemId=fs_id):
                for ap in page.get("accessPoints") or []:
                    ap_id = ap.get("accessPointId")
                    if not ap_id:
                        continue
                    try:
                        s3files.delete_access_point(accessPointId=ap_id)
                        logger.info("    Deleted access point %s", ap_id)
                    except ClientError as e:
                        logger.warning("    delete_access_point %s: %s", ap_id, e)
            # Mount targets
            mt_paginator = s3files.get_paginator("list_mount_targets")
            for page in mt_paginator.paginate(fileSystemId=fs_id):
                for mt in page.get("mountTargets") or []:
                    mt_id = mt.get("mountTargetId")
                    if not mt_id:
                        continue
                    try:
                        s3files.delete_mount_target(mountTargetId=mt_id)
                        logger.info("    Deleted mount target %s", mt_id)
                    except ClientError as e:
                        logger.warning("    delete_mount_target %s: %s", mt_id, e)
            time.sleep(5)
            s3files.delete_file_system(fileSystemId=fs_id, forceDelete=True)
            logger.info("  ✓ Deleted file system %s", fs_id)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code not in {
                "ResourceNotFoundException",
                "FileSystemNotFound",
                "NotFound",
            }:
                logger.warning("  Could not delete FS %s: %s", fs_id, e)

    role_name = f"role-s3files-sync-for-{project_name}"
    try:
        for pname in ("s3-bucket-access", "eventbridge-sync"):
            try:
                iam.delete_role_policy(RoleName=role_name, PolicyName=pname)
            except ClientError:
                pass
        iam.delete_role(RoleName=role_name)
        logger.info("  ✓ Deleted sync role %s", role_name)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "NoSuchEntity":
            logger.warning("  Could not delete sync role: %s", e)

    # Drop config keys
    for key in (
        "s3_files_app_data_file_system_id",
        "s3_files_app_data_access_point_arn",
        "s3_files_app_data_mount_path",
    ):
        cfg.pop(key, None)
