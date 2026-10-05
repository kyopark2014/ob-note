"""Per-folder custom sort order for the vault file tree.

Stored at ``.vault/file_order.json`` so it syncs with the vault (S3 / mount)
without renaming notes. Missing entries fall back to folders-first + name.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from application import vault_backend

logger = logging.getLogger("vault_order")

_ORDER_NAME = "file_order.json"
_lock = threading.RLock()


def _order_path() -> Path:
    return vault_backend.settings_dir() / _ORDER_NAME


def _load() -> dict[str, list[str]]:
    path = _order_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read %s", path)
        return {}
    folders = data.get("folders") if isinstance(data, dict) else None
    if not isinstance(folders, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, names in folders.items():
        if not isinstance(key, str) or not isinstance(names, list):
            continue
        cleaned = [str(n) for n in names if isinstance(n, str) and n.strip()]
        out[key] = cleaned
    return out


def _save(folders: dict[str, list[str]]) -> None:
    settings = vault_backend.settings_dir()
    settings.mkdir(parents=True, exist_ok=True)
    path = _order_path()
    payload = {"folders": folders}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if vault_backend.backend_mode() == "s3":
        try:
            vault_backend.sync_to_s3(f".vault/{_ORDER_NAME}")
        except Exception:
            logger.exception("Failed to sync %s to S3", _ORDER_NAME)


def get_order(folder_rel: str) -> list[str]:
    key = (folder_rel or "").replace("\\", "/").strip("/")
    with _lock:
        return list(_load().get(key, []))


def set_order(folder_rel: str, names: list[str]) -> list[str]:
    """Replace the ordered name list for a folder. Returns the saved list."""
    key = (folder_rel or "").replace("\\", "/").strip("/")
    cleaned = [n for n in names if isinstance(n, str) and n.strip() and n not in {".vault", ".keep", ".gitkeep"}]
    with _lock:
        folders = _load()
        if cleaned:
            folders[key] = cleaned
        else:
            folders.pop(key, None)
        _save(folders)
        return list(folders.get(key, []))


def apply_order(folder_rel: str, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort tree nodes: custom order first, then folders-first + name for the rest."""
    if not nodes:
        return nodes
    order = get_order(folder_rel)
    if not order:
        return sorted(
            nodes,
            key=lambda n: (n.get("type") != "folder", (n.get("name") or "").lower()),
        )
    rank = {name: i for i, name in enumerate(order)}

    def sort_key(n: dict[str, Any]) -> tuple:
        name = n.get("name") or ""
        if name in rank:
            return (0, rank[name])
        return (1, 0 if n.get("type") == "folder" else 1, name.lower())

    return sorted(nodes, key=sort_key)


def notify_renamed(from_path: str, to_path: str) -> None:
    """Update order entries when a file/folder is renamed or moved."""
    from_path = from_path.replace("\\", "/").strip("/")
    to_path = to_path.replace("\\", "/").strip("/")
    if not from_path or from_path == to_path:
        return
    from_parent = str(Path(from_path).parent).replace("\\", "/")
    to_parent = str(Path(to_path).parent).replace("\\", "/")
    if from_parent == ".":
        from_parent = ""
    if to_parent == ".":
        to_parent = ""
    from_name = Path(from_path).name
    to_name = Path(to_path).name

    with _lock:
        folders = _load()
        changed = False

        # Rename key for the folder itself + nested keys
        new_folders: dict[str, list[str]] = {}
        for key, names in folders.items():
            if key == from_path:
                new_folders[to_path] = names
                changed = True
            elif key.startswith(from_path + "/"):
                new_folders[to_path + key[len(from_path) :]] = names
                changed = True
            else:
                new_folders[key] = names
        folders = new_folders

        if from_parent == to_parent:
            names = folders.get(from_parent)
            if names and from_name in names:
                folders[from_parent] = [to_name if n == from_name else n for n in names]
                changed = True
        else:
            src_names = folders.get(from_parent)
            if src_names and from_name in src_names:
                folders[from_parent] = [n for n in src_names if n != from_name]
                if not folders[from_parent]:
                    folders.pop(from_parent, None)
                changed = True
            # Append into destination order if that folder already has a custom order
            dst_names = folders.get(to_parent)
            if dst_names is not None and to_name not in dst_names:
                folders[to_parent] = [*dst_names, to_name]
                changed = True

        if changed:
            _save(folders)


def notify_copied(to_path: str) -> None:
    """Append a copied file into the destination folder's custom order."""
    to_path = to_path.replace("\\", "/").strip("/")
    if not to_path:
        return
    parent = str(Path(to_path).parent).replace("\\", "/")
    if parent == ".":
        parent = ""
    name = Path(to_path).name
    with _lock:
        folders = _load()
        dst_names = folders.get(parent)
        if dst_names is None or name in dst_names:
            return
        folders[parent] = [*dst_names, name]
        _save(folders)


def notify_deleted(path: str) -> None:
    """Remove a path from order maps when deleted."""
    path = path.replace("\\", "/").strip("/")
    if not path:
        return
    parent = str(Path(path).parent).replace("\\", "/")
    if parent == ".":
        parent = ""
    name = Path(path).name

    with _lock:
        folders = _load()
        changed = False
        new_folders: dict[str, list[str]] = {}
        for key, names in folders.items():
            if key == path or key.startswith(path + "/"):
                changed = True
                continue
            if key == parent and name in names:
                filtered = [n for n in names if n != name]
                if filtered:
                    new_folders[key] = filtered
                changed = True
                continue
            new_folders[key] = names
        if changed:
            _save(new_folders)
