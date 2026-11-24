"""Account-scoped UI preferences (pins, sort, theme, …).

DynamoDB mode stores ``META#ui`` on the notes table. Without DynamoDB the same
payload lives in ``.vault/ui-prefs.json`` (local / legacy).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from application import note_store, vault_backend
from application.api.routes_auth import require_user_id

logger = logging.getLogger("routes_prefs")

router = APIRouter(prefix="/api/prefs", tags=["prefs"])

_PREFS_FILE = "ui-prefs.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_path():
    return vault_backend.settings_dir() / _PREFS_FILE


def _read_file_prefs() -> dict[str, Any]:
    path = _file_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("corrupt ui prefs file %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _write_file_prefs(prefs: dict[str, Any]) -> dict[str, Any]:
    clean = note_store.json_safe_prefs(prefs) if hasattr(note_store, "json_safe_prefs") else dict(prefs)
    clean["updated_at"] = _utc_now()
    path = _file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean


@router.get("")
def get_prefs(request: Request) -> dict[str, Any]:
    require_user_id(request)
    if note_store.enabled():
        try:
            return note_store.get_ui_prefs()
        except Exception:
            logger.exception("get_ui_prefs failed; falling back to empty")
            return {}
    return _read_file_prefs()


@router.put("")
def put_prefs(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    require_user_id(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="prefs body must be an object")
    if note_store.enabled():
        try:
            saved = note_store.put_ui_prefs(body)
            return {"ok": True, "prefs": saved}
        except note_store.NoteStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("put_ui_prefs failed")
            raise HTTPException(status_code=500, detail="failed to save prefs") from exc
    saved = _write_file_prefs(body)
    return {"ok": True, "prefs": saved}
