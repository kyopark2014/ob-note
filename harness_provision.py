"""Provision AgentCore Harness for ob-note Open Agent.

Default tools: websearch (Exa) + code interpreter (harness-work style).
Default skill: ``use-vault`` (S3). Scripts run via code interpreter; VAULT_WRITE
markers remain as a fast path for selected-note edits.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("ob-note-harness")

ROOT = Path(__file__).resolve().parent
# Single source: MCP Skills over MCP bundle (also uploaded for Open Agent harness).
SKILLS_DIR = ROOT / "MCP" / "use-vault" / "skills"
SKILLS_S3_PREFIX = "skills"
USE_VAULT_SKILL = "use-vault"

DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-6"
HARNESS_TIMEOUT_SECONDS = 600
HARNESS_MAX_ITERATIONS = 40
HARNESS_MAX_TOKENS = 50000

_HARNESS_NAME_API_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,39}$")

WEBSEARCH_TOOL: dict[str, Any] = {
    "type": "remote_mcp",
    "name": "exa",
    "config": {"remoteMcp": {"url": "https://mcp.exa.ai/mcp"}},
}

CODE_INTERPRETER_TOOL: dict[str, Any] = {
    "type": "agentcore_code_interpreter",
    "name": "code",
    "config": {"agentCoreCodeInterpreter": {}},
}

DEFAULT_HARNESS_TOOLS: list[dict[str, Any]] = [
    WEBSEARCH_TOOL,
    CODE_INTERPRETER_TOOL,
]

_SYSTEM_PROMPT_TEMPLATE = """당신은 ob-note vault의 마크다운 노트를 도와주는 에디터 에이전트입니다.
한국어로 답변하세요. 모르는 내용은 추측하지 마세요.

## 역할
- 선택/첨부 노트는 **본문 전체가 프롬프트에 없습니다.** path · s3 · **url(presigned)** 만 전달됩니다.
- 내용이 필요하면 code interpreter에서 `urllib.request.urlopen(url)`로 url을 읽어 사용하세요.
- vault 규칙이 필요하면 **use-vault** skill을 로드하세요.
- 최신 정보·사실 확인이 필요하면 **websearch(exa)** MCP 도구로 검색하세요.
- **skill 스크립트 절대 경로(`/home/.agents/...`)는 CI에 없을 수 있으니 실행하지 마세요.**

## 노트 수정 (필수 형식)
노트를 저장·덮어쓸 때는 응답에 아래 마커 블록을 **그대로** 포함하세요.
서버가 마커를 파싱해 vault에 저장합니다.

<<<VAULT_WRITE path/to/note.md>>>
# 제목

본문 전체 (YAML frontmatter 금지)
<<<END_VAULT_WRITE>>>

- path는 vault 상대경로입니다. 선택된 노트가 있으면 그 경로만 수정하세요.
- 마커 밖의 텍스트로 사용자에게 변경 요약을 한국어로 알려 주세요.
- 읽기만 할 때는 마커를 넣지 마세요.

## 규칙
- skill: use-vault · 도구: websearch(exa) + code interpreter
- 첨부는 url로 읽고, 저장은 VAULT_WRITE 마커를 사용하세요
- 모르는 내용은 추측하지 마세요.
"""


def build_system_prompt(s3_bucket: str = "") -> str:
    del s3_bucket  # kept for call-site compatibility
    return _SYSTEM_PROMPT_TEMPLATE


# Backward-compatible alias for callers that still import BASE_SYSTEM_PROMPT.
BASE_SYSTEM_PROMPT = build_system_prompt()


def write_use_vault_skill_config(
    *,
    s3_bucket: str,
    sharing_url: str = "",
    region: str = "us-west-2",
    project: str = "ob-note",
) -> Path:
    """Write MCP/use-vault/skills/use-vault/config.json (optional sidecar)."""
    skill_dir = SKILLS_DIR / USE_VAULT_SKILL
    skill_dir.mkdir(parents=True, exist_ok=True)
    url = (sharing_url or "").rstrip("/")
    payload = {
        "s3_bucket": (s3_bucket or "").strip(),
        "sharing_url": url,
        "ob_docs_url": url,
        "region": (region or "us-west-2").strip() or "us-west-2",
        "project_name": (project or "ob-note").strip() or "ob-note",
    }
    path = skill_dir / "config.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("Wrote %s", path)
    return path


def default_harness_tools() -> list[dict[str, Any]]:
    return json.loads(json.dumps(DEFAULT_HARNESS_TOOLS))


def build_use_vault_skills(s3_bucket: str) -> list[dict[str, Any]]:
    bucket = (s3_bucket or "").strip()
    if not bucket:
        return [{"path": f"skills/{USE_VAULT_SKILL}"}]
    return [{"s3": {"uri": f"s3://{bucket}/{SKILLS_S3_PREFIX}/{USE_VAULT_SKILL}/"}}]


def default_harness_skills(s3_bucket: str = "") -> list[dict[str, Any]]:
    return build_use_vault_skills(s3_bucket)


def upload_skills_to_s3(
    s3_bucket: str,
    *,
    sharing_url: str = "",
    region: str = "us-west-2",
    project: str = "ob-note",
) -> int:
    """Upload MCP/use-vault/skills/ to s3://{bucket}/skills/."""
    bucket = (s3_bucket or "").strip()
    if not bucket:
        raise ValueError("s3_bucket is required to upload skills")
    if not SKILLS_DIR.is_dir():
        logger.warning("Skills directory not found: %s", SKILLS_DIR)
        return 0

    write_use_vault_skill_config(
        s3_bucket=bucket,
        sharing_url=sharing_url,
        region=region,
        project=project,
    )

    s3 = boto3.client("s3")
    uploaded = 0
    failed = 0
    for root, dirs, files in os.walk(SKILLS_DIR):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", ".git", "node_modules"}]
        for filename in files:
            local_path = Path(root) / filename
            rel = local_path.relative_to(SKILLS_DIR).as_posix()
            if rel.endswith(".pyc") or "/__pycache__/" in f"/{rel}/":
                continue
            key = f"{SKILLS_S3_PREFIX}/{rel}"
            content_type, _ = mimetypes.guess_type(str(local_path))
            extra: dict[str, Any] = {}
            if content_type:
                extra["ExtraArgs"] = {"ContentType": content_type}
            try:
                s3.upload_file(str(local_path), bucket, key, **extra)
                uploaded += 1
            except ClientError as e:
                failed += 1
                logger.error("Failed to upload %s: %s", rel, e)
    if failed:
        raise RuntimeError(f"Skills upload incomplete: {uploaded} ok, {failed} failed")
    logger.info(
        "Uploaded %s skill file(s) to s3://%s/%s/",
        uploaded,
        bucket,
        SKILLS_S3_PREFIX,
    )
    return uploaded


def harness_name_for_api(project_name: str) -> str:
    normalized = (project_name or "ob_note").replace("-", "_")
    if not _HARNESS_NAME_API_RE.match(normalized):
        raise ValueError(
            "CreateHarness harnessName must match [a-zA-Z][a-zA-Z0-9_]{0,39} "
            f"(got {normalized!r} from projectName={project_name!r})"
        )
    return normalized


def get_max_output_tokens(model_id: str = "") -> int:
    """Match agentic-work runtime ``get_max_output_tokens``."""
    mid = (model_id or "").lower()
    if "fable" in mid:
        return 128000
    if "claude-sonnet-5" in mid or "claude-5-sonnet" in mid:
        return 128000
    if "claude-opus-5" in mid or "claude-5-opus" in mid:
        return 128000
    if "claude-opus-4-7" in mid or "claude-opus-4-6" in mid:
        return 128000
    if "claude-opus-4-5" in mid:
        return 64000
    if "claude-opus-4" in mid or "claude-4-opus" in mid:
        return 32000
    if "claude-sonnet-4" in mid or "claude-4-sonnet" in mid or "claude-haiku-4" in mid:
        return 64000
    return 8192


def _control_client(region: str):
    return boto3.client("bedrock-agentcore-control", region_name=region)


def _iam_client():
    return boto3.client("iam")


def _harness_env_vars(
    *,
    region: str,
    s3_bucket: str,
    sharing_url: str,
    project_secret_prefix: str,
) -> dict[str, str]:
    env = {
        "LOG_LEVEL": "info",
        "BEDROCK_REGION": region,
        "PROJECT_NAME": project_secret_prefix or "ob-note",
    }
    if s3_bucket:
        env["S3_BUCKET"] = s3_bucket
    url = (sharing_url or "").rstrip("/")
    if url:
        env["SHARING_URL"] = url
        env["OB_DOCS_URL"] = url
    return env


def create_harness_execution_role(
    account: str,
    region: str,
    project: str,
    *,
    s3_bucket: str = "",
    project_secret_prefix: str = "ob-note",
) -> str:
    """IAM role assumed by AgentCore for the harness (PUBLIC)."""
    role_name = f"role-harness-for-{project}-{region}"
    if len(role_name) > 64:
        raise ValueError(f"IAM RoleName exceeds 64 characters: {role_name!r}")

    iam = _iam_client()
    assume = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowAgentCoreAssumeHarness",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    role_created = False
    try:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        logger.info("Harness execution role exists: %s", role_name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        role_created = True
        role_arn = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume),
            Description=f"AgentCore harness execution role for {project}",
            Tags=[
                {"Key": "Name", "Value": role_name},
                {"Key": "Project", "Value": project},
            ],
        )["Role"]["Arn"]
        logger.info("Created harness execution role %s", role_name)

    secret_prefix = (project_secret_prefix or project or "ob-note").strip() or "ob-note"
    statements: list[dict[str, Any]] = [
        {
            "Sid": "BedrockModelInvocation",
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
            ],
        },
        {
            # Kimi K3 Chat Completions (apiFormat=chat_completions) via
            # bedrock-runtime …/openai/v1
            "Sid": "BedrockRuntimeBearerToken",
            "Effect": "Allow",
            "Action": ["bedrock:CallWithBearerToken"],
            "Resource": ["*"],
        },
        {
            "Sid": "AgentCoreAccess",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:*"],
            "Resource": ["*"],
        },
        {
            "Sid": "CloudWatchLogsAgentCore",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "logs:DescribeLogStreams",
            ],
            "Resource": [
                f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/*",
            ],
        },
        {
            "Sid": "EcrManagedImagePull",
            "Effect": "Allow",
            "Action": [
                "ecr:BatchGetImage",
                "ecr:GetDownloadUrlForLayer",
                "ecr:BatchCheckLayerAvailability",
            ],
            "Resource": [f"arn:aws:ecr:{region}:*:repository/harness-*"],
        },
        {
            "Sid": "EcrManagedImageToken",
            "Effect": "Allow",
            "Action": ["ecr:GetAuthorizationToken"],
            "Resource": ["*"],
        },
        {
            "Sid": "VaultAgentToken",
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": [
                f"arn:aws:secretsmanager:{region}:{account}:secret:{secret_prefix}/vault-agent-token*"
            ],
        },
    ]
    if s3_bucket:
        statements.extend(
            [
                {
                    "Sid": "SkillsS3List",
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                    "Resource": [f"arn:aws:s3:::{s3_bucket}"],
                    "Condition": {
                        "StringLike": {"s3:prefix": [f"{SKILLS_S3_PREFIX}/*"]}
                    },
                },
                {
                    "Sid": "SkillsS3Get",
                    "Effect": "Allow",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{s3_bucket}/{SKILLS_S3_PREFIX}/*"],
                },
            ]
        )

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=f"harness-exec-inline-for-{project}",
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements}),
    )
    if role_created:
        logger.info("Waiting 20s for IAM role propagation before CreateHarness...")
        time.sleep(20)
    return role_arn


def ensure_ecs_invoke_harness(
    iam,
    *,
    project: str,
    region: str,
    account: str,
    harness_arn: str,
) -> None:
    """Allow the ECS task role to call InvokeHarness."""
    task_role = f"role-ecs-task-for-{project}-{region}"
    policy_name = f"ecs-task-invoke-harness-for-{project}"
    document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeObDocsHarness",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:InvokeHarness",
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                    "bedrock-agentcore:GetHarness",
                ],
                "Resource": [
                    harness_arn,
                    f"{harness_arn}/*",
                    f"arn:aws:bedrock-agentcore:{region}:{account}:harness/*",
                    f"arn:aws:bedrock-agentcore:{region}:{account}:harness/*/*",
                ],
            },
            {
                "Sid": "ListHarnesses",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:ListHarnesses",
                    "bedrock-agentcore-control:ListHarnesses",
                    "bedrock-agentcore-control:GetHarness",
                ],
                "Resource": ["*"],
            },
        ],
    }
    try:
        iam.put_role_policy(
            RoleName=task_role,
            PolicyName=policy_name,
            PolicyDocument=json.dumps(document),
        )
        logger.info("Attached InvokeHarness policy to %s", task_role)
    except ClientError as e:
        logger.warning("Could not attach InvokeHarness policy to %s: %s", task_role, e)


def _paginate_list_harnesses(control) -> list[dict]:
    items: list[dict] = []
    token = None
    while True:
        kw: dict[str, Any] = {"maxResults": 50}
        if token:
            kw["nextToken"] = token
        resp = control.list_harnesses(**kw)
        items.extend(resp.get("harnesses") or [])
        token = resp.get("nextToken")
        if not token:
            break
    return items


def find_harness_by_api_name(control, harness_api_name: str) -> Optional[dict]:
    for h in _paginate_list_harnesses(control):
        if h.get("harnessName") == harness_api_name:
            return h
    return None


def wait_for_harness_ready(control, harness_id: str, timeout_seconds: int = 300) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        h = control.get_harness(harnessId=harness_id)["harness"]
        status = h["status"]
        if status == "READY":
            arn = h["arn"]
            logger.info("Harness ready: %s", arn)
            return arn
        if status in (
            "FAILED",
            "CREATE_FAILED",
            "UPDATE_FAILED",
            "DELETING",
            "DELETE_UNSUCCESSFUL",
            "DELETE_FAILED",
        ):
            reason = h.get("failureReason") or h.get("statusReason") or ""
            raise RuntimeError(
                f"Harness {harness_id} status={status}"
                + (f" — {reason}" if reason else "")
            )
        logger.info("  Waiting for harness %s status=%s", harness_id, status)
        time.sleep(5)
    raise TimeoutError(f"Harness {harness_id} not READY within {timeout_seconds}s")


def update_harness_safe(control, harness_id: str, *, timeout_seconds: int = 600, **kwargs) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        remaining = max(30, int(deadline - time.time()))
        wait_for_harness_ready(control, harness_id, timeout_seconds=remaining)
        try:
            control.update_harness(harnessId=harness_id, **kwargs)
            return
        except ClientError as e:
            code = (e.response.get("Error") or {}).get("Code", "")
            msg = (e.response.get("Error") or {}).get("Message", "")
            if code != "ConflictException" and "while it is UPDATING" not in msg:
                raise
            logger.warning("UpdateHarness conflict; retrying...")
            time.sleep(8)
    raise TimeoutError(f"Timed out updating harness {harness_id}")


def ensure_harness_system_prompt(
    control, harness_id: str, *, s3_bucket: str = ""
) -> None:
    desired = [{"text": build_system_prompt(s3_bucket)}]
    h = control.get_harness(harnessId=harness_id)["harness"]
    if (h.get("systemPrompt") or []) == desired:
        logger.info("  Harness systemPrompt already up to date")
        return
    logger.info("  Updating harness systemPrompt")
    update_harness_safe(control, harness_id, systemPrompt=desired)


def ensure_harness_tools_and_skills(
    control,
    harness_id: str,
    *,
    s3_bucket: str,
) -> None:
    """Keep tools = websearch + code; skills = use-vault."""
    desired_tools = default_harness_tools()
    desired_skills = build_use_vault_skills(s3_bucket)
    h = control.get_harness(harnessId=harness_id)["harness"]
    tools = h.get("tools") or []
    skills = h.get("skills") or []

    desired_tool_names = {
        t.get("name") for t in desired_tools if isinstance(t, dict) and t.get("name")
    }
    current_tool_names = {
        t.get("name") for t in tools if isinstance(t, dict) and t.get("name")
    }
    desired_uris = {
        ((s.get("s3") or {}).get("uri") or "").rstrip("/")
        for s in desired_skills
        if isinstance(s, dict)
    }
    current_uris = {
        ((s.get("s3") or {}).get("uri") or "").rstrip("/")
        for s in skills
        if isinstance(s, dict)
    }
    tools_ok = current_tool_names == desired_tool_names
    skills_ok = bool(desired_uris) and desired_uris.issubset(current_uris) and len(skills) == len(
        desired_skills
    )
    if tools_ok and skills_ok:
        logger.info("  Harness tools/skills already exa + code + use-vault")
        return
    logger.info("  Updating harness tools/skills → exa + code + use-vault")
    update_harness_safe(
        control,
        harness_id,
        tools=desired_tools,
        skills=desired_skills,
    )


def ensure_harness_environment_variables(
    control,
    harness_id: str,
    *,
    region: str,
    s3_bucket: str,
    sharing_url: str,
    project_secret_prefix: str,
) -> None:
    desired = _harness_env_vars(
        region=region,
        s3_bucket=s3_bucket,
        sharing_url=sharing_url,
        project_secret_prefix=project_secret_prefix,
    )
    reserved = {"AWS_REGION", "AWS_DEFAULT_REGION"}
    h = control.get_harness(harnessId=harness_id)["harness"]
    current = h.get("environmentVariables") or {}
    if not isinstance(current, dict):
        current = {}
    merged = {k: v for k, v in current.items() if k not in reserved}
    merged.update(desired)
    if merged == current:
        logger.info("  Harness environmentVariables already up to date")
        return
    logger.info("  Updating harness environmentVariables")
    update_harness_safe(control, harness_id, environmentVariables=merged)


def ensure_harness_memory_disabled(control, harness_id: str) -> None:
    h = control.get_harness(harnessId=harness_id)["harness"]
    memory = h.get("memory") or {}
    if isinstance(memory, dict) and "disabled" in memory:
        logger.info("  Harness memory already disabled")
        return
    logger.info("  Disabling harness memory")
    update_harness_safe(control, harness_id, memory={"optionalValue": {"disabled": {}}})


def create_or_get_harness(
    *,
    account: str,
    region: str,
    project: str,
    execution_role_arn: str,
    s3_bucket: str = "",
    sharing_url: str = "",
    project_secret_prefix: str = "ob-note",
) -> dict[str, str]:
    """Create PUBLIC harness with websearch + code interpreter + use-vault."""
    control = _control_client(region)
    harness_api_name = harness_name_for_api(project)
    logger.info("Creating/reusing harness %s", harness_api_name)

    model_id = DEFAULT_MODEL_ID
    system_prompt = [{"text": build_system_prompt(s3_bucket)}]
    tools = default_harness_tools()
    skills = build_use_vault_skills(s3_bucket)
    env_vars = _harness_env_vars(
        region=region,
        s3_bucket=s3_bucket,
        sharing_url=sharing_url,
        project_secret_prefix=project_secret_prefix,
    )
    environment = {
        "agentCoreRuntimeEnvironment": {
            "networkConfiguration": {"networkMode": "PUBLIC"},
        }
    }

    existing = find_harness_by_api_name(control, harness_api_name)
    harness_id = ""
    if existing:
        harness_id = existing["harnessId"]
        try:
            status = control.get_harness(harnessId=harness_id)["harness"].get("status")
        except ClientError:
            status = None
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "FAILED", "DELETE_FAILED"):
            logger.warning(
                "Harness %s is %s; deleting to recreate", harness_api_name, status
            )
            try:
                control.delete_harness(
                    harnessId=harness_id,
                    clientToken=str(uuid.uuid4()),
                )
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                    raise
            deadline = time.time() + 600
            while time.time() < deadline:
                try:
                    control.get_harness(harnessId=harness_id)
                    time.sleep(5)
                except ClientError as e:
                    if (
                        e.response.get("Error", {}).get("Code")
                        == "ResourceNotFoundException"
                    ):
                        break
                    raise
            else:
                raise TimeoutError(f"Timed out deleting failed harness {harness_id}")
            existing = None
            harness_id = ""
        else:
            logger.info(
                "Harness %s exists (id=%s); skipping CreateHarness",
                harness_api_name,
                harness_id,
            )

    if not existing:
        try:
            response = control.create_harness(
                harnessName=harness_api_name,
                executionRoleArn=execution_role_arn,
                model={
                    "bedrockModelConfig": {
                        "modelId": model_id,
                        "maxTokens": get_max_output_tokens(model_id),
                    }
                },
                systemPrompt=system_prompt,
                tools=tools,
                skills=skills,
                memory={"disabled": {}},
                truncation={
                    "strategy": "sliding_window",
                    "config": {"slidingWindow": {"messagesCount": 50}},
                },
                maxIterations=HARNESS_MAX_ITERATIONS,
                maxTokens=HARNESS_MAX_TOKENS,
                timeoutSeconds=HARNESS_TIMEOUT_SECONDS,
                environment=environment,
                environmentVariables=env_vars,
                tags={"Project": project, "Env": "dev"},
            )
            harness_id = response["harness"]["harnessId"]
            logger.info("Harness created: %s", harness_id)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConflictException":
                raise
            rerun = find_harness_by_api_name(control, harness_api_name)
            if not rerun:
                raise
            harness_id = rerun["harnessId"]
            logger.info("CreateHarness conflict; using existing %s", harness_id)

    ensure_harness_memory_disabled(control, harness_id)
    ensure_harness_system_prompt(control, harness_id, s3_bucket=s3_bucket)
    ensure_harness_tools_and_skills(control, harness_id, s3_bucket=s3_bucket)
    ensure_harness_environment_variables(
        control,
        harness_id,
        region=region,
        s3_bucket=s3_bucket,
        sharing_url=sharing_url,
        project_secret_prefix=project_secret_prefix,
    )
    harness_arn = wait_for_harness_ready(control, harness_id)
    return {
        "harness_id": harness_id,
        "harness_arn": harness_arn,
        "harness_name": harness_api_name,
    }
