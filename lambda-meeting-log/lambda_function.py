#!/usr/bin/env python3
"""
meeting-log API.

Invoked ONLY directly (IAM lambda:InvokeFunction) by the ob-note backend, which
authenticates the user and passes the verified id as ``event["obUserId"]``.
Events without it (e.g. from API Gateway) are rejected with 401.

GET  /health
GET  /stream-url          — Transcribe Streaming WebSocket (real-time)
GET  /upload-url          — S3 presigned PUT for full WAV
POST /batch-transcribe    — StartTranscriptionJob on uploaded WAV
GET  /batch-job/{name}    — poll job + speaker-labeled transcript
GET  /batch-jobs          — recent jobs for this user (recover UX)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config as BotoConfig

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SAMPLE_RATE = os.environ.get("TRANSCRIBE_SAMPLE_RATE", "16000")
MEDIA_ENCODING = os.environ.get("TRANSCRIBE_MEDIA_ENCODING", "pcm")
LANGUAGE_OPTIONS = os.environ.get("TRANSCRIBE_LANGUAGE_OPTIONS", "ko-KR,en-US")
PREFERRED_LANGUAGE = os.environ.get("TRANSCRIBE_PREFERRED_LANGUAGE", "ko-KR")
BUCKET_NAME = os.environ.get("BUCKET_NAME", "")
# Objects land at ``{userId}/meeting-log/incoming/{uuid}.wav``.
AUDIO_SUFFIX = "meeting-log/incoming"
PRESIGN_EXPIRES = "299"
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(400 * 1024 * 1024)))
SPEAKERS = ["A", "B", "C", "D"]
MAX_SPEAKERS = 6

_s3 = None
_transcribe = None


def _region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )


def s3():
    global _s3
    if _s3 is None:
        region = _region()
        # Use the regional endpoint. The global s3.amazonaws.com host 307-redirects
        # newly created buckets; that redirect has no CORS headers, so the browser
        # reports TypeError: Failed to fetch.
        _s3 = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
            ),
        )
    return _s3


def transcribe():
    global _transcribe
    if _transcribe is None:
        _transcribe = boto3.client("transcribe", region_name=_region())
    return _transcribe


def language_options_list() -> List[str]:
    return [part.strip() for part in LANGUAGE_OPTIONS.split(",") if part.strip()]


def _response(status: int, body: Any, *, cors: bool = False) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if cors:
        headers.update(
            {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                "Access-Control-Allow-Headers": "content-type,authorization",
            }
        )
    return {
        "statusCode": status,
        "headers": headers,
        "body": body if isinstance(body, str) else json.dumps(body, ensure_ascii=False),
    }


def _path(event: Dict[str, Any]) -> str:
    return str(event.get("rawPath") or event.get("path") or "/")


def _method(event: Dict[str, Any]) -> str:
    http = (event.get("requestContext") or {}).get("http") or {}
    return str(http.get("method") or event.get("httpMethod") or "GET").upper()


def _auth_user(event: Dict[str, Any]) -> str:
    """User id verified by ob-note. Never read from query string or body."""
    uid = event.get("obUserId")
    return uid.strip() if isinstance(uid, str) else ""


def _query(event: Dict[str, Any], name: str) -> str:
    params = event.get("queryStringParameters") or {}
    if not isinstance(params, dict):
        return ""
    return str(params.get(name) or "").strip()


def _sanitize_user_segment(user_id: str) -> str:
    """Single path segment. Matches ob-note ``sanitize_user_path_segment``."""
    segment = (
        (user_id or "")
        .strip()
        .replace("/", "_")
        .replace("\\", "_")
        .replace("..", "_")
    )
    if not segment or segment in {".", ".."}:
        raise ValueError("userId is required")
    if segment in {"_public", "_legacy"} or segment.startswith("_"):
        segment = "u_" + segment.lstrip("_")
    if not segment:
        raise ValueError("userId is required")
    return segment


def _user_tag(user_id: str) -> str:
    """Short stable hash embedded in job names to bind a job to its owner."""
    return hashlib.sha256(_sanitize_user_segment(user_id).encode("utf-8")).hexdigest()[:16]


def _user_audio_prefix(user_id: str) -> str:
    return f"{_sanitize_user_segment(user_id)}/{AUDIO_SUFFIX}"


def _parse_body(event: Dict[str, Any]) -> Dict[str, Any]:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8", errors="replace")
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if not raw:
        return {}
    return json.loads(raw)


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signature_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    k_date = _sign(("AWS4" + secret).encode("utf-8"), datestamp)
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


def _quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="-_.~")


def map_speaker(raw: Any) -> str:
    digits = str(raw or "").replace("spk_", "")
    try:
        n = int(digits)
    except ValueError:
        return "A"
    if n < 0:
        return "A"
    return SPEAKERS[min(n, len(SPEAKERS) - 1)]


def presign_transcribe_websocket_url(region: Optional[str] = None) -> Dict[str, Any]:
    region = region or _region()
    creds = boto3.Session().get_credentials()
    if creds is None:
        raise RuntimeError("No AWS credentials available")
    frozen = creds.get_frozen_credentials()

    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    service = "transcribe"
    host = f"transcribestreaming.{region}.amazonaws.com:8443"
    canonical_uri = "/stream-transcription-websocket"
    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{datestamp}/{region}/{service}/aws4_request"

    query = {
        "X-Amz-Algorithm": algorithm,
        "X-Amz-Credential": f"{frozen.access_key}/{credential_scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": PRESIGN_EXPIRES,
        "X-Amz-SignedHeaders": "host",
        "identify-multiple-languages": "true",
        "language-options": LANGUAGE_OPTIONS,
        "preferred-language": PREFERRED_LANGUAGE,
        "media-encoding": MEDIA_ENCODING,
        "sample-rate": SAMPLE_RATE,
        "show-speaker-label": "true",
    }
    if frozen.token:
        query["X-Amz-Security-Token"] = frozen.token

    canonical_querystring = "&".join(
        f"{_quote(key)}={_quote(query[key])}" for key in sorted(query)
    )
    canonical_headers = f"host:{host}\n"
    signed_headers = "host"
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_request = (
        f"GET\n{canonical_uri}\n{canonical_querystring}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    string_to_sign = (
        f"{algorithm}\n{amz_date}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    signing_key = _signature_key(frozen.secret_key, datestamp, region, service)
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    request_url = (
        f"wss://{host}{canonical_uri}?{canonical_querystring}"
        f"&X-Amz-Signature={signature}"
    )
    return {
        "url": request_url,
        "region": region,
        "languageOptions": LANGUAGE_OPTIONS,
        "preferredLanguage": PREFERRED_LANGUAGE,
        "identifyMultipleLanguages": True,
        "mediaEncoding": MEDIA_ENCODING,
        "sampleRate": SAMPLE_RATE,
        "showSpeakerLabel": True,
        "expiresIn": int(PRESIGN_EXPIRES),
    }


def parse_batch_items(transcript: Dict[str, Any]) -> List[Dict[str, Any]]:
    results = transcript.get("results") or {}
    items = results.get("items") or []
    groups: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for item in items:
        alts = item.get("alternatives") or [{}]
        content = str((alts[0] or {}).get("content") or "")
        if not content:
            continue
        speaker = map_speaker(item.get("speaker_label"))
        start = float(item.get("start_time") or (current or {}).get("start") or 0)
        itype = str(item.get("type") or "pronunciation")
        if current is None or current["speaker"] != speaker:
            current = {"speaker": speaker, "parts": [], "start": start}
            groups.append(current)
        if itype == "punctuation":
            current["parts"].append(content)
        else:
            if current["parts"] and not str(current["parts"][-1]).endswith(" "):
                current["parts"].append(" ")
            current["parts"].append(content)
    out = []
    for group in groups:
        text = "".join(group["parts"]).replace("  ", " ").strip()
        if not text:
            continue
        out.append(
            {
                "speaker": group["speaker"],
                "text": text,
                "start": group["start"],
            }
        )
    return out


def _handle_upload_url(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    if not BUCKET_NAME:
        return _response(500, {"error": "BUCKET_NAME is not configured"})
    try:
        prefix = _user_audio_prefix(user_id)
    except ValueError as exc:
        return _response(400, {"error": str(exc)})
    try:
        size = int(_query(event, "size"))
    except ValueError:
        return _response(400, {"error": "size (bytes) is required"})
    if size < 1 or size > MAX_UPLOAD_BYTES:
        return _response(
            413, {"error": f"size must be between 1 and {MAX_UPLOAD_BYTES} bytes"}
        )
    key = f"{prefix}/{uuid.uuid4().hex}.wav"
    # ContentLength is signed: S3 rejects a PUT whose body length differs.
    url = s3().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": BUCKET_NAME,
            "Key": key,
            "ContentType": "audio/wav",
            "ContentLength": size,
        },
        ExpiresIn=300,
    )
    return _response(
        200,
        {
            "uploadUrl": url,
            "key": key,
            "contentType": "audio/wav",
            "contentLength": size,
            "expiresIn": 300,
        },
    )


def _handle_batch_start(event: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    if not BUCKET_NAME:
        return _response(500, {"error": "BUCKET_NAME is not configured"})
    try:
        payload = _parse_body(event)
    except json.JSONDecodeError:
        return _response(400, {"error": "Invalid JSON body"})
    key = str(payload.get("key") or "").lstrip("/")
    try:
        prefix = _user_audio_prefix(user_id)
    except ValueError as exc:
        return _response(400, {"error": str(exc)})
    if not key.startswith(f"{prefix}/") or ".." in key:
        return _response(400, {"error": "Invalid audio key"})

    job_name = f"meeting-log-{_user_tag(user_id)}-{uuid.uuid4().hex}"
    media_uri = f"s3://{BUCKET_NAME}/{key}"
    try:
        transcribe().start_transcription_job(
            TranscriptionJobName=job_name,
            Media={"MediaFileUri": media_uri},
            MediaFormat="wav",
            IdentifyMultipleLanguages=True,
            LanguageOptions=language_options_list(),
            Settings={
                "ShowSpeakerLabels": True,
                "MaxSpeakerLabels": MAX_SPEAKERS,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("StartTranscriptionJob failed")
        return _response(
            502, {"error": "Failed to start batch transcription", "detail": str(exc)}
        )
    return _response(200, {"jobName": job_name, "status": "IN_PROGRESS"})


def _media_uri_for_user(user_id: str) -> str:
    if not BUCKET_NAME:
        return ""
    return f"s3://{BUCKET_NAME}/{_user_audio_prefix(user_id)}/"


def _job_belongs_to_user(job: Dict[str, Any], user_id: str) -> bool:
    """True when job name has this user's tag, or MediaFileUri is under their prefix."""
    name = str(job.get("TranscriptionJobName") or "")
    try:
        if name.startswith(f"meeting-log-{_user_tag(user_id)}-"):
            return True
        media_prefix = _media_uri_for_user(user_id)
    except ValueError:
        return False
    if not media_prefix:
        return False
    media = str(((job.get("Media") or {}).get("MediaFileUri") or ""))
    return bool(media) and media.startswith(media_prefix)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _handle_batch_job(job_name: str, user_id: str) -> Dict[str, Any]:
    job_name = urllib.parse.unquote(job_name)
    if not job_name.startswith("meeting-log-"):
        return _response(404, {"error": "Job not found"})
    try:
        resp = transcribe().get_transcription_job(TranscriptionJobName=job_name)
    except Exception as exc:  # noqa: BLE001
        logger.exception("GetTranscriptionJob failed")
        return _response(502, {"error": "Failed to get job", "detail": str(exc)})

    job = resp.get("TranscriptionJob") or {}
    if not _job_belongs_to_user(job, user_id):
        return _response(404, {"error": "Job not found"})

    status = job.get("TranscriptionJobStatus") or "UNKNOWN"
    body: Dict[str, Any] = {
        "jobName": job_name,
        "status": status,
        "createdAt": _iso(job.get("CreationTime")),
        "completedAt": _iso(job.get("CompletionTime")),
        "mediaUri": ((job.get("Media") or {}).get("MediaFileUri") or ""),
    }
    if status == "FAILED":
        body["error"] = job.get("FailureReason") or "Transcription failed"
        return _response(200, body)
    if status != "COMPLETED":
        return _response(200, body)

    uri = (job.get("Transcript") or {}).get("TranscriptFileUri") or ""
    if not uri:
        return _response(502, {"error": "Missing TranscriptFileUri"})
    try:
        # Long meetings produce multi-MB transcript JSON; allow more than the
        # default socket timeout so recover/poll can finish.
        with urllib.request.urlopen(uri, timeout=55) as handle:
            transcript = json.loads(handle.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to download transcript")
        return _response(502, {"error": "Failed to download transcript", "detail": str(exc)})

    body["entries"] = parse_batch_items(transcript)
    body["fullText"] = (
        ((transcript.get("results") or {}).get("transcripts") or [{}])[0].get("transcript")
        or ""
    )
    return _response(200, body)


def _handle_batch_jobs(user_id: str, *, limit: int = 20) -> Dict[str, Any]:
    """List recent meeting-log jobs owned by this user (for recover UX)."""
    try:
        user_tag = _user_tag(user_id)
        owner_prefix = f"meeting-log-{user_tag}-"
        media_prefix = _media_uri_for_user(user_id)
    except ValueError as exc:
        return _response(400, {"error": str(exc)})

    limit = max(1, min(int(limit or 20), 50))
    by_name: Dict[str, Dict[str, Any]] = {}

    def _add_summary(row: Dict[str, Any]) -> None:
        name = str(row.get("jobName") or "")
        if name:
            by_name[name] = row

    # 1) Modern jobs: name embeds the per-user tag.
    token: Optional[str] = None
    while len(by_name) < limit:
        kwargs: Dict[str, Any] = {
            "MaxResults": 100,
            "JobNameContains": user_tag,
        }
        if token:
            kwargs["NextToken"] = token
        try:
            resp = transcribe().list_transcription_jobs(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ListTranscriptionJobs failed")
            return _response(502, {"error": "Failed to list jobs", "detail": str(exc)})
        page = resp.get("TranscriptionJobSummaries") or []
        if not page:
            break
        for summary in page:
            name = str(summary.get("TranscriptionJobName") or "")
            if not name.startswith(owner_prefix):
                continue
            _add_summary(
                {
                    "jobName": name,
                    "status": summary.get("TranscriptionJobStatus") or "UNKNOWN",
                    "createdAt": _iso(summary.get("CreationTime")),
                    "completedAt": _iso(summary.get("CompletionTime")),
                    "failureReason": summary.get("FailureReason") or None,
                }
            )
            if len(by_name) >= limit:
                break
        token = resp.get("NextToken")
        if not token:
            break

    # 2) Legacy untagged meeting-log-* jobs: verify MediaFileUri ownership.
    if media_prefix and len(by_name) < limit:
        token = None
        scanned = 0
        while len(by_name) < limit and scanned < 120:
            kwargs = {"MaxResults": 100, "JobNameContains": "meeting-log-"}
            if token:
                kwargs["NextToken"] = token
            try:
                resp = transcribe().list_transcription_jobs(**kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.exception("ListTranscriptionJobs(legacy) failed")
                break
            page = resp.get("TranscriptionJobSummaries") or []
            if not page:
                break
            for summary in page:
                scanned += 1
                name = str(summary.get("TranscriptionJobName") or "")
                if not name.startswith("meeting-log-") or name.startswith(owner_prefix):
                    continue
                if name in by_name:
                    continue
                try:
                    detail = (
                        transcribe()
                        .get_transcription_job(TranscriptionJobName=name)
                        .get("TranscriptionJob")
                        or {}
                    )
                except Exception:  # noqa: BLE001
                    continue
                if not _job_belongs_to_user(detail, user_id):
                    continue
                _add_summary(
                    {
                        "jobName": name,
                        "status": detail.get("TranscriptionJobStatus")
                        or summary.get("TranscriptionJobStatus")
                        or "UNKNOWN",
                        "createdAt": _iso(
                            detail.get("CreationTime") or summary.get("CreationTime")
                        ),
                        "completedAt": _iso(
                            detail.get("CompletionTime") or summary.get("CompletionTime")
                        ),
                        "failureReason": detail.get("FailureReason")
                        or summary.get("FailureReason")
                        or None,
                    }
                )
                if len(by_name) >= limit:
                    break
            token = resp.get("NextToken")
            if not token:
                break

    summaries = sorted(
        by_name.values(),
        key=lambda row: row.get("createdAt") or "",
        reverse=True,
    )
    return _response(200, {"jobs": summaries[:limit]})


def lambda_handler(event, _context):
    method = _method(event)
    path = _path(event)

    user_id = _auth_user(event)
    if not user_id:
        return _response(401, {"error": "unauthorized"})

    if path.endswith("/health") or path == "/health":
        return _response(
            200,
            {
                "status": "ok",
                "service": "meeting-log",
                "languageOptions": LANGUAGE_OPTIONS,
                "preferredLanguage": PREFERRED_LANGUAGE,
                "identifyMultipleLanguages": True,
                "showSpeakerLabel": True,
                "batchEnabled": bool(BUCKET_NAME),
            },
        )

    if path.endswith("/stream-url"):
        if method != "GET":
            return _response(405, {"error": "Method not allowed"})
        try:
            return _response(200, presign_transcribe_websocket_url())
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to presign Transcribe WebSocket URL")
            return _response(
                502, {"error": "Failed to create stream URL", "detail": str(exc)}
            )

    if path.endswith("/upload-url"):
        if method != "GET":
            return _response(405, {"error": "Method not allowed"})
        return _handle_upload_url(event, user_id)

    if path.endswith("/batch-transcribe"):
        if method != "POST":
            return _response(405, {"error": "Method not allowed"})
        return _handle_batch_start(event, user_id)

    if path.endswith("/batch-jobs"):
        if method != "GET":
            return _response(405, {"error": "Method not allowed"})
        try:
            limit = int(_query(event, "limit") or "20")
        except ValueError:
            limit = 20
        return _handle_batch_jobs(user_id, limit=limit)

    if "/batch-job/" in path:
        if method != "GET":
            return _response(405, {"error": "Method not allowed"})
        job_name = path.rsplit("/", 1)[-1]
        return _handle_batch_job(job_name, user_id)

    return _response(404, {"error": "Not found", "path": path})
