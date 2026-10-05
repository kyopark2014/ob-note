#!/usr/bin/env python3
"""Turn office uploads into Markdown for Documents Sync.

Reading follows the document skills:

- ``docx`` — ``pandoc -t markdown`` (OOXML fallback)
- ``pptx`` — ``markitdown`` (OOXML fallback)
- ``xlsx`` / ``csv`` — ``markitdown`` (openpyxl / OOXML / csv fallback)

Legacy ``.doc`` / ``.ppt`` / ``.xls`` go through LibreOffice (``soffice``)
into the modern package, then the same readers.
"""

from __future__ import annotations

import csv
import html
import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

_DOCX = {".docx", ".dotx"}
_PPTX = {".pptx", ".potx"}
_XLSX = {".xlsx", ".xlsm", ".xltx"}
_LEGACY = {".doc": "docx", ".ppt": "pptx", ".xls": "xlsx"}
_TEXTISH = {".csv", ".tsv", ".html", ".htm", ".json"}

OFFICE_SOURCE_SUFFIXES = _DOCX | _PPTX | _XLSX | set(_LEGACY) | _TEXTISH


def office_to_markdown(path: Path) -> str:
    """Return Markdown body for an office/text upload. Raises ValueError if empty."""
    src = Path(path)
    if not src.is_file():
        raise ValueError(f"파일을 찾을 수 없습니다: {src}")
    suffix = src.suffix.lower()
    if suffix in _LEGACY:
        body = _via_soffice(src, _LEGACY[suffix])
    elif suffix in _DOCX:
        body = _docx_to_markdown(src)
    elif suffix in _PPTX:
        body = _pptx_to_markdown(src)
    elif suffix in _XLSX:
        body = _xlsx_to_markdown(src)
    elif suffix in {".csv", ".tsv"}:
        body = _csv_to_markdown(src, delimiter="\t" if suffix == ".tsv" else ",")
    elif suffix in {".html", ".htm"}:
        body = _html_to_markdown(src)
    elif suffix == ".json":
        body = _json_to_markdown(src)
    else:
        raise ValueError(f"지원하지 않는 형식입니다: {suffix or src.name}")
    text = (body or "").strip()
    if not text:
        raise ValueError(f"문서에서 텍스트를 추출하지 못했습니다: {src.name}")
    return text


def _usable_markdown(text: str | None) -> str | None:
    """Drop empty output and raw OOXML dumps from a failed reader."""
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    head = stripped[:1200]
    if any(
        token in head
        for token in ("<?xml", "<w:document", "<p:sld", "<worksheet", "## File:")
    ):
        return None
    return stripped


def _run_text(cmd: list[str], *, timeout: int = 180) -> str | None:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return _usable_markdown(proc.stdout)


def _markitdown(path: Path) -> str | None:
    text = _run_text(["markitdown", str(path)])
    if text:
        return text
    try:
        from markitdown import MarkItDown
    except ImportError:
        return None
    try:
        result = MarkItDown().convert(str(path))
    except Exception:
        return None
    content = getattr(result, "text_content", None) or getattr(result, "markdown", None)
    if isinstance(content, str):
        return _usable_markdown(content)
    return None


def _pandoc_markdown(path: Path) -> str | None:
    if shutil.which("pandoc") is None:
        return None
    return _run_text(
        ["pandoc", "-t", "markdown", "--wrap=none", str(path)],
    )


def _docx_to_markdown(path: Path) -> str:
    # docx skill: read with pandoc.
    text = _pandoc_markdown(path)
    if text:
        return text
    text = _markitdown(path)
    if text:
        return text
    return _docx_ooxml(path)


def _pptx_to_markdown(path: Path) -> str:
    # pptx skill: read with markitdown (one block per slide).
    text = _markitdown(path)
    if text:
        return text
    return _pptx_ooxml(path)


def _xlsx_to_markdown(path: Path) -> str:
    # xlsx skill: markitdown for a quick look; openpyxl as the structured reader.
    text = _markitdown(path)
    if text:
        return text
    try:
        import openpyxl
    except ImportError:
        openpyxl = None  # type: ignore[assignment]
    if openpyxl is not None:
        try:
            text = _xlsx_openpyxl(path, openpyxl)
        except Exception:
            text = ""
        if text.strip():
            return text
    return _xlsx_ooxml(path)


def _via_soffice(src: Path, modern_ext: str) -> str:
    if shutil.which("soffice") is None:
        raise ValueError(
            f"구형 {src.suffix} 파일은 LibreOffice(soffice)가 있어야 변환됩니다: {src.name}"
        )
    with tempfile.TemporaryDirectory(prefix="documents-office-") as tmp:
        proc = subprocess.run(
            [
                "soffice",
                "--headless",
                "--convert-to",
                modern_ext,
                "--outdir",
                tmp,
                str(src),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        matches = sorted(Path(tmp).glob(f"*.{modern_ext}"))
        if proc.returncode != 0 or not matches:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise ValueError(
                f"LibreOffice 변환 실패: {src.name}"
                + (f" ({detail[:240]})" if detail else "")
            )
        modern = matches[0]
        if modern_ext == "docx":
            return _docx_to_markdown(modern)
        if modern_ext == "pptx":
            return _pptx_to_markdown(modern)
        return _xlsx_to_markdown(modern)


def _text_of(node: ET.Element, text_tag: str) -> str:
    parts: list[str] = []
    for el in node.iter(text_tag):
        if el.text:
            parts.append(el.text)
        if el.tail:
            parts.append(el.tail)
    return re.sub(r"[ \t]+\n", "\n", "".join(parts)).strip()


def _docx_ooxml(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        try:
            xml = zf.read("word/document.xml")
        except KeyError as exc:
            raise ValueError(f"Word 본문이 없습니다: {path.name}") from exc
    root = ET.fromstring(xml)
    blocks: list[str] = []
    for para in root.iter(_W + "p"):
        line = _text_of(para, _W + "t")
        if line:
            blocks.append(line)
    return "\n\n".join(blocks)


def _pptx_ooxml(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        names = [
            name
            for name in zf.namelist()
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
        ]
        names.sort(key=lambda name: int(re.search(r"(\d+)", name).group(1)))  # type: ignore[union-attr]
        if not names:
            raise ValueError(f"슬라이드가 없습니다: {path.name}")
        blocks: list[str] = []
        for idx, name in enumerate(names, 1):
            root = ET.fromstring(zf.read(name))
            lines: list[str] = []
            for para in root.iter(_A + "p"):
                line = _text_of(para, _A + "t")
                if line:
                    lines.append(line)
            body = "\n".join(lines).strip()
            if body:
                blocks.append(f"## Slide {idx}\n\n{body}")
    return "\n\n".join(blocks)


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        xml = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(xml)
    out: list[str] = []
    for si in root.iter(_S + "si"):
        out.append(_text_of(si, _S + "t"))
    return out


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.attrib.get("t") or ""
    if kind == "inlineStr":
        return _text_of(cell, _S + "t")
    value_el = cell.find(_S + "v")
    if value_el is None or value_el.text is None:
        return ""
    raw = value_el.text
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return raw
    return raw


def _col_row(ref: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]+)(\d+)", ref or "")
    if not match:
        return 0, 0
    col = 0
    for ch in match.group(1):
        col = col * 26 + (ord(ch) - 64)
    return col - 1, int(match.group(2)) - 1


def _xlsx_ooxml(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        shared = _shared_strings(zf)
        sheets = [
            name
            for name in zf.namelist()
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
        ]
        sheets.sort(
            key=lambda name: int(re.search(r"sheet(\d+)", name).group(1))  # type: ignore[union-attr]
        )
        if not sheets:
            raise ValueError(f"시트가 없습니다: {path.name}")
        blocks: list[str] = []
        for idx, name in enumerate(sheets, 1):
            root = ET.fromstring(zf.read(name))
            grid: dict[tuple[int, int], str] = {}
            max_r = 0
            max_c = 0
            for cell in root.iter(_S + "c"):
                col, row = _col_row(cell.attrib.get("r") or "")
                text = _cell_value(cell, shared).strip()
                if not text:
                    continue
                grid[(row, col)] = text
                max_r = max(max_r, row)
                max_c = max(max_c, col)
            rows: list[list[str]] = []
            for row in range(max_r + 1):
                rows.append(
                    [grid.get((row, col), "") for col in range(max_c + 1)]
                )
            table = _rows_to_markdown(rows)
            if table:
                blocks.append(f"## Sheet {idx}\n\n{table}")
    return "\n\n".join(blocks)


def _xlsx_openpyxl(path: Path, openpyxl) -> str:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    blocks: list[str] = []
    try:
        for sheet in wb.worksheets:
            rows: list[list[str]] = []
            for row in sheet.iter_rows(values_only=True):
                values = ["" if cell is None else str(cell) for cell in row]
                if any(v.strip() for v in values):
                    rows.append(values)
            table = _rows_to_markdown(rows)
            if table:
                blocks.append(f"## {sheet.title}\n\n{table}")
    finally:
        wb.close()
    return "\n\n".join(blocks)


def _rows_to_markdown(rows: list[list[str]], *, limit: int = 5000) -> str:
    cleaned = [
        [cell.replace("\n", " ").replace("|", "\\|").strip() for cell in row]
        for row in rows
        if any(str(cell).strip() for cell in row)
    ]
    if not cleaned:
        return ""
    width = max(len(row) for row in cleaned)
    trimmed = False
    if len(cleaned) > limit:
        cleaned = cleaned[:limit]
        trimmed = True

    def pad(row: list[str]) -> list[str]:
        return row + [""] * (width - len(row))

    header = pad(cleaned[0])
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in cleaned[1:]:
        lines.append("| " + " | ".join(pad(row)) + " |")
    if trimmed:
        lines.append("")
        lines.append(f"_… {limit}행에서 잘렸습니다._")
    return "\n".join(lines)


def _csv_to_markdown(path: Path, *, delimiter: str) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    reader = csv.reader(io.StringIO(raw), delimiter=delimiter)
    rows = [list(row) for row in reader]
    return _rows_to_markdown(rows)


def _html_to_markdown(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?i)</h[1-6]\s*>", "\n\n", text)
    text = re.sub(r"(?i)<h([1-6])[^>]*>", lambda m: "\n" + ("#" * int(m.group(1))) + " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _json_to_markdown(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    return f"```json\n{raw}\n```"
