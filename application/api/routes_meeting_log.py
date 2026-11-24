"""Meeting-log proxy — authenticated bridge to the meeting-log Lambda.

The Lambda has no public endpoint. Only this backend (ECS task role) may invoke
it, and the user id is taken from the verified session, never from the client.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from botocore.config import Config
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from application import utils
from application.api.routes_auth import require_user_id

logger = logging.getLogger("routes_meeting_log")

router = APIRouter(prefix="/api/meeting-log", tags=["meeting-log"])

LAMBDA_NAME = os.environ.get("MEETING_LOG_LAMBDA", "lambda-api-meeting-log")
_client = None


def _lambda():
    global _client
    if _client is None:
        region = str(utils.load_config().get("region") or "us-west-2")
        # Batch-job COMPLETED responses include full speaker-labeled transcripts
        # for long meetings; allow the Lambda's 60s ceiling to finish.
        _client = boto3.client(
            "lambda",
            region_name=region,
            config=Config(read_timeout=70, retries={"max_attempts": 2}),
        )
    return _client


def _invoke(method: str, path: str, user_id: str, *, query: dict | None = None, body: Any = None) -> dict:
    event = {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
        "queryStringParameters": query or {},
        "body": json.dumps(body) if body is not None else "",
        "obUserId": user_id,
    }
    try:
        resp = _lambda().invoke(
            FunctionName=LAMBDA_NAME,
            Payload=json.dumps(event).encode("utf-8"),
        )
        out = json.loads(resp["Payload"].read() or b"{}")
    except Exception as e:
        logger.exception("meeting-log invoke failed")
        raise HTTPException(status_code=502, detail="meeting-log backend unavailable") from e
    if resp.get("FunctionError") or "statusCode" not in out:
        logger.error("meeting-log lambda error: %s", out)
        raise HTTPException(status_code=502, detail="meeting-log backend error")
    status = int(out["statusCode"])
    try:
        data = json.loads(out.get("body") or "{}")
    except json.JSONDecodeError:
        data = {}
    if status >= 400:
        raise HTTPException(status_code=status, detail=data.get("error") or data)
    return data


class BatchBody(BaseModel):
    key: str = Field(..., min_length=1, max_length=1024)


@router.get("/stream-url")
def stream_url(request: Request) -> dict:
    return _invoke("GET", "/stream-url", require_user_id(request))


@router.get("/upload-url")
def upload_url(request: Request, size: int = Query(..., ge=1)) -> dict:
    return _invoke("GET", "/upload-url", require_user_id(request), query={"size": str(size)})


@router.post("/batch-transcribe")
def batch_transcribe(request: Request, body: BatchBody) -> dict:
    return _invoke("POST", "/batch-transcribe", require_user_id(request), body={"key": body.key})


@router.get("/batch-jobs")
def batch_jobs(request: Request, limit: int = Query(20, ge=1, le=50)) -> dict:
    return _invoke(
        "GET",
        "/batch-jobs",
        require_user_id(request),
        query={"limit": str(limit)},
    )


@router.get("/batch-job/{job_name}")
def batch_job(request: Request, job_name: str) -> dict:
    return _invoke("GET", f"/batch-job/{job_name}", require_user_id(request))
