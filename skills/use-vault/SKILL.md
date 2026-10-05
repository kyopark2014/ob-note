---
name: use-vault
description: ob-note vault의 마크다운 노트를 조회·검색·생성·수정합니다. vault, 노트, 위키링크, 백링크, 메모 저장 요청 시 사용합니다.
---

# use-vault (ob-note)

ob-note vault의 마크다운 노트 규칙과 저장 형식을 정의합니다.  
노트는 `.md`가 Source of Truth입니다.

계정(`userId`/email)마다 vault가 분리됩니다 (`{userId}/vault/…`).  
에이전트 호출 시 인증된 userId의 vault만 보이며, 경로는 항상 **그 계정 vault 기준 상대경로**입니다.

## When to Use

- vault / 내 노트 / 노트 조회·검색
- 파일 트리, 백링크·그래프 확인
- 새 노트 작성, 기존 노트 수정·이어쓰기

## Critical Rules

1. 경로는 vault 상대경로입니다. 예: `Meeting/Weekly-Sync.md`
2. **노트 본문**: YAML frontmatter를 **넣지 마세요**. `# 제목` 한 줄로 시작하세요.
3. **새 노트는 vault 루트에 두지 마세요.** 주제 폴더 아래(`Category/Note.md`)에만 저장합니다.
4. 선택 노트 본문이 사용자 메시지에 포함되어 있으면 그 내용을 우선 사용하세요.
5. ad-hoc `curl`로 vault API를 새로 짜지 마세요.

## Open Agent — 노트 저장

선택 노트를 덮어쓸 때는 응답에 `VAULT_WRITE` 마커를 넣으세요. 서버가 파싱해 vault에 저장합니다.

```
<<<VAULT_WRITE Meeting/Note.md>>>
# Note

본문 전체 (YAML frontmatter 금지)
<<<END_VAULT_WRITE>>>
```

- 선택된 노트 경로만 수정하세요.
- 마커 밖의 텍스트로 변경 요약을 한국어로 알려 주세요.
- 읽기만 할 때는 마커를 넣지 마세요.

## Subcommands (참고)

skill에 포함된 `scripts/read_vault.py` · `write_vault.py`는 vault HTTP API 래퍼입니다.  
**code interpreter 샌드박스에는 harness skill 마운트 경로가 보이지 않을 수 있으므로**,  
Open Agent에서는 위 `VAULT_WRITE` 경로를 사용하세요.

| 스크립트 | 용도 |
| --- | --- |
| `read_vault.py` | health / tree / list / read / search / graph / backlinks |
| `write_vault.py` | write / append / mkdir / rename / delete / rebuild |

## Environment

| 키 / 변수 | 설명 |
| --- | --- |
| `OB_DOCS_URL` / `SHARING_URL` | vault API base |
| `VAULT_AGENT_TOKEN` | 스크립트 인증 (Secrets Manager `ob-note/vault-agent-token`) |
