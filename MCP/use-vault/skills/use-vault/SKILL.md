---
name: use-vault
description: ob-note(Obsidian형 vault)에 저장된 마크다운 노트를 조회·검색·생성·수정합니다. 사용자가 "vault", "내 노트", "ob-note", "위키링크", "백링크", "노트 찾아줘", "메모 저장", "vault에 적어줘", "지식베이스" 등을 요청할 때 사용합니다.
---

# use-vault (ob-note)

standalone **ob-note** vault의 마크다운 노트 규칙과 워크플로입니다.  
노트는 `.md`가 Source of Truth입니다. ad-hoc `curl`로 API를 새로 짜지 마세요.

계정(`actor_id` / email)마다 vault가 `{userId}/vault/…`로 분리됩니다.  
노트 경로는 항상 **그 계정 vault 기준 상대경로**입니다 (예: `Meeting/Note.md` — path에 email을 넣지 마세요).

**기본 `actor_id`**: MCP 도구는 `kyopark2014@gmail.com`을 기본값으로 씁니다.  
생략하거나 `ksdyb`·`local-dev`처럼 `@` 없는 닉네임을 넣으면 서버가 이 기본값으로 바꿉니다.  
다른 계정이 필요할 때만 실제 Google email을 넘기세요.  
(`e2npark@gmail.com`, gateway 토큰 이메일, `local-dev`로 고르지 마세요.)

- **MCP 클라이언트**(agentic-work 등): 아래 MCP Tools를 사용하세요.
- **Open Agent (harness)**: MCP 도구 대신 [Open Agent — 노트 저장](#open-agent--노트-저장)의 `VAULT_WRITE` 마커를 사용하세요.

## When to Use

- vault / ob-note / 내 노트 / 위키 노트 조회
- 노트 검색, 파일 트리, 백링크·그래프 확인
- 새 노트 작성, 기존 노트 수정·이어쓰기·이름변경·삭제

## MCP Tools

| 도구 | 용도 |
| --- | --- |
| `vault_health` | 서비스 상태 |
| `vault_tree` | 폴더 트리 |
| `vault_list` | 플랫 파일 목록 |
| `vault_read` | 노트 본문(+메타/백링크) |
| `vault_search` | 전문/메타 검색 |
| `vault_graph` | 위키링크 그래프 |
| `vault_backlinks` | 해당 노트를 가리키는 링크 |
| `vault_write` | 덮어쓰기 저장 |
| `vault_append` | 이어쓰기 |
| `vault_str_replace` | 부분 치환 (old→new) |
| `vault_upload` | 이미지·바이너리 업로드 (`content_base64` 또는 `source_url`) |
| `vault_mkdir` | 폴더 생성 |
| `vault_rename` | 이동/이름변경 |
| `vault_delete` | 파일·폴더 삭제 |
| `vault_rebuild` | 검색/그래프 인덱스 재생성 |

`actor_id`는 생략 가능합니다(기본 `kyopark2014@gmail.com`).  
다른 계정이면 `actor_id="<email>"`을 넘기세요. 닉네임·OS 사용자명은 쓰지 마세요.

## Critical Rules

1. `curl`로 ad-hoc API를 새로 작성하지 마세요. MCP면 vault_* 도구, Open Agent면 VAULT_WRITE를 사용하세요.
2. 경로는 vault 상대경로입니다. 예: `AI/Ontology.md`, `Meeting/Weekly-Sync.md`
3. MCP에서 쓰기 전에 `vault_read`로 확인하세요. **중간 수정**은 `vault_str_replace`(정확한 old→new), **끝 추가**는 `vault_append`, **전체 재작성**만 `vault_write`를 쓰세요.
4. 도구 JSON에는 path와 함께 deep link `url`이 붙습니다. 사용자에게는 한국어로 요약하고,  
   **노트 path를 말할 때는 `[제목](url)` 마크다운 링크를 함께** 전달하세요.
5. **노트 본문 형식**: YAML frontmatter를 **넣지 마세요**. `# 제목` 한 줄로 시작하고 바로 본문을 이어서 쓰세요.
6. **새 노트는 vault 루트에 두지 마세요.** 주제 폴더 아래(`Category/Note.md`)에만 저장합니다.
7. **결과는 가능한 하나의 마크다운에 모으세요.** 분량이 많으면 제목 아래 **목차(TOC)** 를 넣으세요.
8. **이미지는 CloudFront/외부 URL을 그대로 넣지 마세요.** `vault_upload`로 노트와 같은 폴더에 올린 뒤, vault 상대경로(`filename.png`)만 쓰세요. 업로드 API를 `vault_search`로 찾지 마세요.
9. **한국어 문체(합니다체)**: vault에 쓰는 노트 본문은 항상 **합니다체(존댓말)** 로 작성하세요.

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
- skill 스크립트 절대 경로(`/home/.agents/...`)는 code interpreter에 없을 수 있으니 실행하지 마세요.

| 금지 (한다체) | 사용 (합니다체) |
| --- | --- |
| 맞다 / ~이다 / ~다 | 맞습니다 / ~입니다 |
| 추가한다 / 설정한다 | 추가합니다 / 설정합니다 |
| 상태이다 / 필요하다 | 상태입니다 / 필요합니다 |
| ~할 수 있다 / ~된다 | ~할 수 있습니다 / ~됩니다 |
| ~한다. / ~이다. | ~합니다. / ~입니다. |

제목·표 헤더·코드 블록·고유명사는 예외입니다. 서술 문장·결론·권고는 반드시 합니다체로 끝내세요.

```markdown
# Claude Tag

## 목차
- [개요](#개요)
- [세부](#세부)

## 개요

본문…

## 세부

본문…
```

## Folder Organization

새 노트 작성 시 **반드시** 카테고리 폴더에 넣습니다. 루트에 `Something.md`를 만들지 마세요.

### 절차

1. `vault_tree` 또는 `vault_list`로 기존 폴더를 확인합니다.
2. 노트 주제에 맞는 폴더가 **이미 있으면 그 폴더를 재사용**합니다.
3. 없으면 `vault_mkdir`로 새 카테고리 폴더를 만든 뒤 그 안에 노트를 저장합니다.
4. 경로 형식: `<Category>/<NoteTitle>.md` (예: `Meeting/Sprint-Review.md`)

### 카테고리 예시

| 폴더 | 넣을 노트 |
| --- | --- |
| `Productivity` | 할 일, 습관, GTD, 워크플로 |
| `AI` | 모델, 프롬프트, LLM, AI 도구 |
| `Agent` | 에이전트·스킬·런타임·자동화 로그 |
| `Meeting` | 회의록, 싱크, 액션 아이템 |
| `Project` | 프로젝트 메모·계획·로드맵 |
| `Research` | 조사, 논문, 벤치마크, POC |
| `Personal` | 개인 메모, 목표 |
| `Reference` | 치트시트, 컨벤션 |
| `Architecture` | 시스템 설계, ADR |
| `DevOps` | CI/CD, 인프라, 배포 |
| `Cloud` | AWS/GCP/Azure 메모 |
| `Security` | 위협 모델, 권한·시크릿 |
| `Product` | 요구사항, 유저 스토리 |
| `Engineering` | 구현 메모, 버그 분석 |
| `Learning` | 공부 노트, 강의 요약 |
| `Ideas` | 아이디어, 가설 |
| `Writing` | 초안, 발표 원고 |
| `Decision` | 의사결정 로그 |
| `Runbook` | 운영 절차, 장애 대응 |
| `Archive` | 완료·폐기 노트 |

위 목록에 없으면 짧은 PascalCase/영문 폴더명을 새로 만드세요. 한 노트에 주제가 겹치면 **가장 구체적인 하나**만 고릅니다.

### 금지 / 권장

- ❌ vault 루트에 직접 저장
- ❌ 같은 주제를 `Overview.md` / `Details.md`처럼 잘게 쪼개기
- ✅ 기존 `AI/`가 있으면 `AI/New-Topic.md`
- ✅ 없으면 `vault_mkdir(path="Meeting")` 후 `Meeting/Standup-2026-03-17.md`

## Quick Start (MCP)

```text
vault_health(actor_id="kyopark2014@gmail.com")
vault_list(actor_id="kyopark2014@gmail.com")
vault_tree(actor_id="kyopark2014@gmail.com")
vault_read(actor_id="kyopark2014@gmail.com", path="Productivity/Convention.md")
vault_search(actor_id="kyopark2014@gmail.com", query="온톨로지")
vault_mkdir(actor_id="kyopark2014@gmail.com", path="Meeting")
vault_write(actor_id="kyopark2014@gmail.com", path="Meeting/Sprint-Review.md", content="# Sprint Review\n\n본문")
vault_append(actor_id="kyopark2014@gmail.com", path="Agent/Runtime-Log.md", content="\n## Update\n- item\n")
vault_str_replace(actor_id="kyopark2014@gmail.com", path="Meeting/Sprint-Review.md", old_string="## 결론\n\n초안", new_string="## 결론\n\n확정안")
vault_upload(actor_id="kyopark2014@gmail.com", path="Meeting/diagram.png", content_base64="…")
```

쓰기 본문은 항상 `# 제목`으로 시작하세요 (frontmatter 금지). **루트 저장 금지**.  
`vault_str_replace`의 `old_string`은 노트에 **정확히 한 번** 나와야 합니다(여러 곳이면 `replace_all=true` 또는 주변 문맥을 더 포함).

## Note deep links

도구 JSON에는 vault 상대경로 `path`와 함께 **로그인 후 해당 노트로 이동하는** deep link `url`이 붙습니다. public share(`/s/{token}`)가 아닙니다.

```text
https://vault.my-agentic-ai.click/?note=Cloud%2FAWS+Azure+GCP+%EA%B0%80%EA%B2%A9+%EB%B9%84%EA%B5%90.md
```

- 형식: `{vault base}/?note=<urlencoded vault path>`
- **사용자 응답에는 path와 클릭 가능한 `url`을 함께** 보여 주세요.
- list/search/tree 결과는 본문 전체를 붙여넣지 말고, 핵심 요약 + 클릭 가능한 목차(또는 path·url 표)로 정리하세요. MCP Apps UI가 있어도 채팅 목차의 링크는 생략하지 마세요.

## Images

마크다운에 `![alt](https://…cloudfront…/artifacts/…)` 를 넣으면 vault에서 **403**이 납니다.  
이미지를 **노트와 같은 폴더**에 `vault_upload`로 올린 뒤 상대경로로 링크하세요.

```text
vault_upload(
  actor_id="kyopark2014@gmail.com",
  path="Meeting/dxf_analysis.png",
  content_base64="<base64 bytes>",   # 또는 source_url="https://…"
)
```

노트 본문:

```markdown
![DXF 엔티티 분석](dxf_analysis.png)
![2D 도면](dxf_2d_drawing.png)
```

- `path`는 주제 폴더 아래 (예: `Meeting/img.png`). vault 루트 금지.
- `content_base64`와 `source_url` 중 **하나만** 전달하세요.
- **금지**: vault 노트에 CloudFront `/artifacts/*` URL을 그대로 남기기.
- **금지**: 이미지 업로드 방법을 `vault_search`로 검색하기 — MCP 도구 `vault_upload`를 쓰세요.

## Response tips

- `vault_list`/`vault_search` 결과는 path(+ `url`) 표로 정리하세요.
- `vault_read`는 title·tags·backlinks를 함께 보여준 뒤 본문 요약을 하세요. 상단에 deep link를 넣으세요.
- 쓰기 성공 시 path · `url` · `ok: true`를 짧게 확인하면 됩니다. `vault_str_replace`는 `replacements` 횟수도 함께 확인하세요. `vault_upload`는 path·size를 확인한 뒤 노트에는 상대경로만 넣으세요.
- 새 노트는 frontmatter 없이 `# 제목` + 본문만, **카테고리 폴더 경로**와 deep link를 사용자에게 알려 주세요.

## Troubleshooting

### 빈 tree / File not found

`actor_id`가 실제 Google email인지 확인하세요. 기본은 `kyopark2014@gmail.com`입니다.  
닉네임은 MCP가 기본 email로 치환합니다. 노트는 `vault/{email}/…` 아래에만 있습니다.

### 도구 오류 / auth

Runtime이 Secrets Manager의 vault-agent-token으로 VaultAgent HMAC을 서명합니다. 토큰·역할이 없으면 vault API 호출이 실패합니다.
