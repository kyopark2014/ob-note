"""Display-name → Bedrock model profiles for Open Agent (agentic-work / harness-work)."""

from __future__ import annotations

from typing import Any, Optional

DEFAULT_MODEL = "Claude 4.6 Sonnet"

# UI order (same family as harness-work routes_config.MODELS).
MODEL_NAMES: list[str] = [
    "Claude 5.5 Opus",
    "Claude 5.0 Sonnet",
    "Claude 5.0 Opus",
    "Claude 4.6 Sonnet",
    "Claude Fable 5",
    "Claude Fable 5.1",
    "Claude 4.7 Opus",
    "Claude 4.6 Opus",
    "Claude 4.5 Haiku",
    "Claude 4.5 Sonnet",
    "Claude 4.5 Opus",
    "OpenAI GPT 5.4",
    "OpenAI GPT 5.5",
    "OpenAI GPT 6 Astra",
    "OpenAI GPT 6 Sol",
    "OpenAI GPT 6 Luna",
    "OpenAI GPT 5.6 Sol",
    "OpenAI GPT 5.6 Terra",
    "OpenAI GPT 5.6 Luna",
    "OpenAI OSS 120B",
    "OpenAI OSS 20B",
    "Kimi K3",
    "Nova 2 Lite",
    "Nova Premier",
    "Nova Pro",
    "Nova Lite",
    "Nova Micro",
]

# One profile per display name (region is informational; InvokeHarness uses modelId).
_MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "Claude 5.5 Sonnet": {
        "model_type": "claude",
        "model_id": "global.anthropic.claude-sonnet-5-5",
    },
    "Claude 5.0 Sonnet": {
        "model_type": "claude",
        "model_id": "us.anthropic.claude-sonnet-5",
    },
    "Claude 5.5 Opus": {
        "model_type": "claude",
        "model_id": "us.anthropic.claude-opus-5-5",
    },
    "Claude 5.0 Opus": {
        "model_type": "claude",
        "model_id": "us.anthropic.claude-opus-5",
    },
    "Claude 4.6 Sonnet": {
        "model_type": "claude",
        "model_id": "us.anthropic.claude-sonnet-4-6",
    },
    "Claude Fable 5": {
        "model_type": "claude",
        "model_id": "us.anthropic.claude-fable-5",
    },
    "Claude Fable 5.1": {
        "model_type": "claude",
        "model_id": "us.anthropic.claude-fable-5-1",
    },
    "Claude 4.7 Opus": {
        "model_type": "claude",
        "model_id": "us.anthropic.claude-opus-4-7",
    },
    "Claude 4.6 Opus": {
        "model_type": "claude",
        "model_id": "us.anthropic.claude-opus-4-6-v1",
    },
    "Claude 4.5 Haiku": {
        "model_type": "claude",
        "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    },
    "Claude 4.5 Sonnet": {
        "model_type": "claude",
        "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    },
    "Claude 4.5 Opus": {
        "model_type": "claude",
        "model_id": "us.anthropic.claude-opus-4-5-20251101-v1:0",
    },
    "OpenAI GPT 5.4": {
        "model_type": "openai",
        "model_id": "openai.gpt-5.4",
        "mantle_api": "responses",
    },
    "OpenAI GPT 5.5": {
        "model_type": "openai",
        "model_id": "openai.gpt-5.5",
        "mantle_api": "responses",
    },
    "OpenAI GPT 6 Astra": {
        "model_type": "openai",
        "model_id": "us.openai.gpt-6-astra",
    },
    "OpenAI GPT 6 Sol": {
        "model_type": "openai",
        "model_id": "us.openai.gpt-6-sol",
    },
    "OpenAI GPT 6 Luna": {
        "model_type": "openai",
        "model_id": "us.openai.gpt-6-luna",
    },
    "OpenAI GPT 5.6 Sol": {
        "model_type": "openai",
        "model_id": "us.openai.gpt-5.6-sol",
    },
    "OpenAI GPT 5.6 Terra": {
        "model_type": "openai",
        "model_id": "us.openai.gpt-5.6-terra",
    },
    "OpenAI GPT 5.6 Luna": {
        "model_type": "openai",
        "model_id": "us.openai.gpt-5.6-luna",
    },
    "OpenAI OSS 120B": {
        "model_type": "openai",
        "model_id": "openai.gpt-oss-120b-1:0",
    },
    "OpenAI OSS 20B": {
        "model_type": "openai",
        "model_id": "openai.gpt-oss-20b-1:0",
    },
    "Kimi K3": {
        "model_type": "kimi",
        "model_id": "us.moonshotai.kimi-k3",
        "apiFormat": "chat_completions",
    },
    "Nova 2 Lite": {
        "model_type": "nova",
        "model_id": "us.amazon.nova-2-lite-v1:0",
    },
    "Nova Premier": {
        "model_type": "nova",
        "model_id": "us.amazon.nova-premier-v1:0",
    },
    "Nova Pro": {
        "model_type": "nova",
        "model_id": "us.amazon.nova-pro-v1:0",
    },
    "Nova Lite": {
        "model_type": "nova",
        "model_id": "us.amazon.nova-lite-v1:0",
    },
    "Nova Micro": {
        "model_type": "nova",
        "model_id": "us.amazon.nova-micro-v1:0",
    },
}


def normalize_model_name(name: Optional[str]) -> str:
    value = (name or "").strip()
    if value in _MODEL_PROFILES:
        return value
    return DEFAULT_MODEL


def get_model_profile(model_name: Optional[str]) -> dict[str, Any]:
    return dict(_MODEL_PROFILES[normalize_model_name(model_name)])


def harness_model_config(model_name: Optional[str] = None) -> dict[str, Any]:
    """Build InvokeHarness ``model`` override from a UI display name."""
    profile = get_model_profile(model_name)
    mid = profile.get("model_id") or ""
    bedrock_cfg: dict[str, Any] = {"modelId": mid}
    api_format = profile.get("mantle_api") or profile.get("apiFormat")
    if api_format:
        bedrock_cfg["apiFormat"] = api_format
    return {"bedrockModelConfig": bedrock_cfg}


def list_models() -> dict[str, Any]:
    return {
        "models": list(MODEL_NAMES),
        "default_model": DEFAULT_MODEL,
    }
