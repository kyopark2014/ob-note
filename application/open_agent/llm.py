"""Bedrock chat models for Open Agent (display name → ChatBedrock*)."""

from __future__ import annotations

import logging
from typing import Any, Optional

import boto3
from botocore.config import Config
from langchain_aws import ChatBedrock, ChatBedrockConverse

from application import models as model_catalog
from application import utils

logger = logging.getLogger("open_agent.llm")


def bedrock_region() -> str:
    cfg = utils.load_config()
    return (
        (cfg.get("region") or "").strip()
        or (os_environ_region())
        or "us-west-2"
    )


def os_environ_region() -> str:
    import os

    return (
        (os.environ.get("AWS_REGION") or "").strip()
        or (os.environ.get("AWS_DEFAULT_REGION") or "").strip()
    )


def _max_tokens(model_type: str, model_id: str) -> int:
    mid = (model_id or "").lower()
    if model_type == "claude":
        if "haiku" in mid:
            return 8192
        return 16384
    if model_type == "kimi":
        return 16384
    if model_type == "openai":
        return 8192
    return 5120


def _rejects_temperature(model_id: str) -> bool:
    """Claude 5 / Fable reject sampling params on Bedrock.

    InvokeModel returns ValidationException: "`temperature` is deprecated for this model."
    """
    mid = (model_id or "").lower()
    return (
        "fable" in mid
        or "claude-sonnet-5" in mid
        or "claude-5-sonnet" in mid
        or "claude-opus-5" in mid
        or "claude-5-opus" in mid
    )


def get_chat_model(model_name: Optional[str] = None) -> Any:
    """Return a LangChain chat model for the UI display name."""
    profile = model_catalog.get_model_profile(model_name)
    model_id = profile.get("model_id") or ""
    model_type = profile.get("model_type") or "claude"
    region = bedrock_region()
    max_tokens = _max_tokens(model_type, model_id)

    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(retries={"max_attempts": 8}, read_timeout=300),
    )

    sampling: dict[str, Any] = (
        {} if _rejects_temperature(model_id) else {"temperature": 0.2}
    )

    # OpenAI / Kimi Mantle paths: Converse with api-format when available.
    api_format = profile.get("mantle_api") or profile.get("apiFormat")
    if model_type in ("openai", "kimi") or api_format:
        kwargs: dict[str, Any] = {
            "model_id": model_id,
            "client": client,
            "max_tokens": max_tokens,
            "region_name": region,
            **sampling,
        }
        if model_type == "claude":
            kwargs["provider"] = "anthropic"
        chat = ChatBedrockConverse(**kwargs)
        chat.streaming = True
        return chat

    if model_type == "nova":
        stop = '"\n\n<thinking>", "\n<thinking>", " <thinking>"'
    elif model_type == "claude":
        stop = "\n\nHuman:"
    else:
        stop = ""

    chat_kwargs: dict[str, Any] = {
        "model_id": model_id,
        "client": client,
        "model_kwargs": {
            "max_tokens": max_tokens,
            **sampling,
            **({"stop_sequences": [stop]} if stop else {}),
        },
        "region_name": region,
        "streaming": True,
    }
    if model_type == "claude":
        chat_kwargs["provider"] = "anthropic"
    return ChatBedrock(**chat_kwargs)
