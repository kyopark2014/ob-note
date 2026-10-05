# Documents / 문서 동기화

Per-user project & drawing document staging for ob-note (vault OCR copy).

## Layout

**Staging** (PDF/registry — parallel to vault, not inside it):

```
data/{sanitize(user)}/documents/
  projects/              uploaded sources + extracted {stem}.md / {stem}.json
  project_list.json
  drawings/
  drawings_list.json
  settings.json          FMP / parallel flags (mirror for sync subprocess)
  out/
    converted/           FMP intermediates (.pdf_pages)
    manifest.json
    .documents_sync_status.json
  artifacts/md/          local markdown publish cache
```

**User settings:** `{vault}/.vault/documents_settings.json` (also mirrored to staging `settings.json`).

**Vault copy target** (visible notes after 「복사」):

```
OCR/Projects/{name}.md
OCR/Drawings/{name}.md
```

## Modules

| File | Role |
|------|------|
| `doc_list.py` | Registry for **projects** + **drawings** only |
| `pdf2text.py` | Shared PDF → text / Foundation Model Parser extractor |
| `sync_documents.py` | Sync CLI: PDF/docs → markdown next to sources |

## Usage

```bash
python documents/sync_documents.py --user alice
python documents/sync_documents.py --user alice --full --model "Claude 4.6 Sonnet"
```

Settings:

- `documents_foundation_model_parser_enabled` (default: `true`)
- `documents_parallel_processing_enabled` (default: `true`)

API prefix: `/api/documents` (Configure / Projects / Drawings / Sync / copy-to-vault).

## 한국어 요약

사용자별 **Projects**·**Drawings** PDF/문서를 업로드하고 Sync하면 마크다운으로 추출합니다.
목록의 **복사**는 채팅 첨부가 아니라 vault의 `OCR/Projects` 또는 `OCR/Drawings`에 `.md`를 저장합니다.
