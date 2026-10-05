# OB Note

Obsidian형 **Local-first Plain Text** vault 웹 앱입니다.  
노트는 `.md`가 Source of Truth이고, 설정은 `.vault/`에 격리되며, 그래프·검색·백링크는 파생 캐시입니다.

`python installer.py`로 CloudFront / ALB / ECS / S3를 만들고 사이트 루트에 배포합니다 (다른 프로젝트와 인프라를 공유하지 않음).

| 항목 | 값 |
|---|---|
| 접속 경로 | `https://vault.my-agentic-ai.click` |
| React 앱 | [`web/`](web/) (`base: /`) |
| Vault (markdown) | S3 API `{userId}/vault/` ↔ working `data/{userId}/vault/` |
| App-data (DB) | S3 Files `/` → ECS `/mnt/app-data` |
| 로컬 working copy | `data/{userId}/vault/` |
| 설정 폴더 | `{user}/.vault/` (`.obsidian` 대체) |
| 공개 공유 인덱스 | `vault/_public/shares_index.json` |

## 아키텍처

```text
CloudFront-for-ob-note
  └─ ALB (alb-for-ob-note)
       └─ /*       → ECS service-for-ob-note
            ├─ working: /app/data/{userId}/vault/  ← S3 API sync {userId}/vault/
            └─ /mnt/app-data/{userId}/notes.db     ← S3 Files bucket root (ECS only)
```

- **계정 분리**: Google `userId`(email)를 path segment로 sanitize해 노트·설정·그래프·sync 큐를 계정별로 격리 (agentic-work와 동일 패턴)
- **Vault s3 모드 (ECS 기본)**: `VAULT_S3_ENABLE=1` — 로컬 working ↔ `s3://{bucket}/{userId}/vault/` sync
  - 저장/삭제 시 pending 큐(`{user}/.vault/pending_s3_ops.json`, S3에도 미러)에 쌓은 뒤 flush
  - Settings **Sync**: pending 업로드를 먼저 끝낸 다음, 해당 계정 S3 prefix에서 **변경분만** 내려받음
  - 부팅 시에는 전역 pull 없이, 로그인 후 계정별 sync
- **App-data (ECS only)**: agentic-work와 같이 S3 Files를 `/mnt/app-data`에 마운트
  - `notes.db`(노트 레지스트리 + agent chat)는 NFS 위에서 직접 열지 않고 **working → persist** 복사
  - working: `data/{user}/vault/.vault/notes.db`
  - durable: `/mnt/app-data/{user}/notes.db` → `s3://{bucket}/{user}/notes.db`
  - 로그인 시 S3 Files에서 notes.db를 **강제 복원** (toons-viewer `load_user_db_from_s3_files`와 동일)
  - durable 없으면 working을 mount에 즉시 seed
  - 이후 API는 idempotent restore만 수행; 변경 후 20초 debounce persist, shutdown flush
- **local 모드**: `data/{userId}/vault/`만 사용 (app-data mount 없음)

## 빠른 시작 (로컬)

```bash
cd ob-note
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
chmod +x run_local.sh
./run_local.sh
```

브라우저: [http://localhost:8502/](http://localhost:8502/)

로컬에서는 `ALLOW_LOCAL_AUTH_BYPASS=1`로 세션 없이 동작합니다.

프론트만 개발할 때:

```bash
# 터미널 1 — API
PYTHONPATH=. ALLOW_LOCAL_AUTH_BYPASS=1 uvicorn application.server:app --port 8502

# 터미널 2 — Vite
cd web && npm install && npm run dev
# http://localhost:5174/
```

## Vault 구조

```text
data/
├── vault/_public/shares_index.json   # token → user_id (공개 /s/{token})
├── alice@example.com/vault/          # working copy
│   ├── 00-Inbox/
│   ├── notes/
│   ├── attachments/
│   └── .vault/
│       ├── app.json
│       ├── shares.json
│       ├── graph.json
│       ├── pending_s3_ops.json
│       └── cache/
└── bob@example.com/vault/
```

`userId`(email)는 path-safe segment로 sanitize됩니다. `_public` 등은 예약 이름입니다.

위키링크 `[[Note]]`, frontmatter(`aliases`, `tags`)를 파싱해 그래프·검색·백링크를 만듭니다.

**좌측 rail → Graph**

| 메뉴 | 동작 |
|---|---|
| Sync | vault markdown 위키링크 인덱스를 갱신하고 Notes Graph HTML 생성 |
| Rebuild | 캐시를 비우고 전체 재빌드 |
| Graph | Notes Graph 모달 (Force Atlas / Neo4j Explore / Holistic View · 검색·범례) |
| Configure | 포함할 폴더·미해결 링크 표시 설정 |

## API

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/health` | 헬스체크 |
| GET | `/api/session` | 공유 세션 확인 |
| GET | `/api/files/tree` | 파일 트리 |
| GET | `/api/files/list?prefix=&ext=` | 플랫 파일 목록 |
| GET | `/api/files/read?path=` | 노트 읽기 |
| PUT | `/api/files/write` | 노트 저장(덮어쓰기) |
| POST | `/api/files/append` | 노트 이어쓰기 |
| POST | `/api/files/sync` | pending flush 후 S3→로컬 incremental pull |
| GET | `/api/files/sync` | pending 큐 상태 |
| POST | `/api/files/share` | 노트·폴더 public 공유 링크 생성/재사용 |
| GET | `/api/files/shares` | 공유 목록 |
| POST | `/api/files/share/delete` | 공유 토큰 삭제 |
| GET | `/s/{token}` | **공개** markdown viewer (쿠키 불필요) |
| GET | `/s/{token}/raw?path=` | **공개** 상대 이미지/첨부 |
| POST | `/api/files/mkdir` | 폴더 생성 |
| POST | `/api/files/rename` | 이동/이름변경 |
| POST | `/api/files/delete` | 삭제 |
| GET | `/api/search?q=` | 검색 |
| GET | `/api/graph` | Notes 위키링크 그래프 JSON |
| GET | `/api/graph/status` | Notes 그래프 동기화 상태 |
| POST | `/api/graph/sync` | Notes Sync (`?full=1` = Rebuild) |
| POST | `/api/graph/rebuild` | Notes 전체 재빌드 |
| GET | `/api/graph/graph` | Notes Graph HTML (iframe) |
| PATCH | `/api/graph/pattern` | 그래프 뷰 패턴 전환 |
| GET/PUT | `/api/graph/sources` | Notes Configure (포함 폴더) |
| GET | `/api/graph/backlinks` | 백링크 |
| GET | `/api/agent/health` | Open Agent(LangGraph) 준비 여부 |
| GET | `/api/agent/models` | 선택 가능한 모델 목록 |
| GET | `/api/agent/note-meta?path=` | 에이전트 칩용 노트 메타 |
| POST | `/api/agent/chat` | Open Agent SSE (`token` / `tool` / `note_updated` / `done`) |

인증: `agent_user_id` 쿠키, `Authorization: Bearer <session>`, 또는 AgentCore용 `Authorization: VaultAgent v1.<payload>.<sig>` (`ob-note/vault-agent-token`).

- Secrets Manager 키: `ob-note/session-signing-key` (웹 세션)
- Secrets Manager 키: `ob-note/vault-agent-token` (use-vault skill / AgentCore)
- 또는 환경변수 `SESSION_SIGNING_KEY` / `VAULT_AGENT_TOKEN`
- 미인증 시 같은 앱의 Google 로그인 UI 표시
- `/s/*` 공개 viewer만 세션 없이 접근 가능

Open Agent 사용법·동작은 [Agent로 Note 수정하기](#agent로-note-수정하기)를 보세요.

## Agent로 Note 수정하기

선택한 마크다운 노트를 **ECS 인프로세스 LangGraph** 에이전트로 요약·수정합니다.  
(이전 AgentCore InvokeHarness + Code Interpreter 경로는 제거되었습니다 — CI 샌드박스 권한으로 노트 저장이 실패하던 문제를 해결하기 위함입니다.)

노트 I/O는 **`vault_read` / `vault_write` / `vault_search` / `vault_list`** 도구가 같은 프로세스에서 vault를 직접 읽고 씁니다.  
계산용으로 `execute_code` / `bash`도 제공하지만, **노트 저장에는 쓰지 않습니다.**

### 여는 방법

| 진입점 | 동작 |
|---|---|
| 노트 우클릭 → **Open agent** | 문서 창 오른쪽에 Agent 패널 오픈 |
| 문서 툴바 **Agent** 아이콘 | 현재 열린 노트로 동일하게 오픈 |

패널 UI는 타임라인 · tool 카드 · 입력창 구성입니다.

### 구성 (LangGraph)

| 항목 | 내용 |
|---|---|
| Runtime | ECS 앱 프로세스 내 LangGraph StateGraph (`application/open_agent/`) |
| Tools | `vault_*` (직접 vault 쓰기) + `execute_code` / `bash` (계산 전용) |
| 모델 | 좌측 rail 하단 **Model** 아이콘에서 선택 (기본 `Claude 4.6 Sonnet`) |
| 세션 | 채팅 `session_id` = 노트 `note_id` (대화방); 히스토리는 SQLite |

첨부/선택 노트 본문은 프롬프트에 넣지 않고, 에이전트가 `vault_read`로 가져옵니다.  
레거시 `<<<VAULT_WRITE>>>` 마커가 응답에 남아 있으면 서버가 파싱해 저장하는 폴백도 유지합니다.

배포 시 `python installer.py`는 Open Agent를 **langgraph** 백엔드로 설정합니다 (InvokeHarness 프로비저닝 없음).

### 요청 흐름

```text
UI (Agent 패널)
  → POST /api/agent/chat  (SSE)
  → LangGraph StateGraph (agent ↔ tools)
  → vault_read / vault_write / vault_search / …
  → 스트림: token / text / tool / tool_result / note_updated
  → 에디터 탭 다시 로드
```

1. **선택 노트**: 입력창 칩으로 경로·크기가 보이고, 본문은 `vault_read`로 가져옵니다.
2. **모델**: rail Model에서 고른 display name이 `model_name`으로 전달되고, 서버가 Bedrock `modelId`로 변환합니다.
3. **도구**: 노트 수정은 `vault_write`, 계산은 `execute_code`/`bash`. UI에는 **tool / tool_result** 카드가 타임라인에 표시됩니다.
4. **최종 답변**: tool 카드 **아래**에 텍스트가 오도록 서버·클라이언트가 타임라인을 맞춥니다.

### 노트 저장 (`vault_write`)

에이전트가 `vault_write(path, content)`를 호출하면 **같은 ECS 프로세스**에서 vault 파일을 덮어쓰고 인덱스를 갱신합니다.  
(Code Interpreter 샌드박스 ACL과 무관합니다.)

레거시 폴백으로 응답에 아래 마커가 있으면 서버가 파싱해 저장합니다.

```text
<<<VAULT_WRITE Meeting/Weekly-Sync.md>>>
# Weekly Sync

본문 전체 (YAML frontmatter 금지)
<<<END_VAULT_WRITE>>>
```

- 선택된 노트 경로만 쓰기가 허용됩니다 (다른 path는 도구가 거부).
- 저장 후 UI에 `vault_write` tool 카드와 `note_updated`가 보이고 Preview/Edit 탭이 갱신됩니다.

### 첨부 (사진 / Load files)

입력창 **+** 는 메시지 입력 전과 관계없이 항상 사용할 수 있습니다.

| 메뉴 | 동작 |
|---|---|
| **사진 첨부** | 이미지 선택 또는 Ctrl/⌘+V 붙여넣기 → **현재 노트와 같은 폴더**에 저장 → 미리보기 칩 |
| **Load files** | 문서/텍스트 등을 같은 폴더에 올린 뒤 칩으로 표시 |

전송 시:

- 이미지: Bedrock Converse로 설명을 뽑아 프롬프트에 합침 (InvokeHarness는 텍스트 전용)
- 텍스트형 파일: 본문을 프롬프트에 포함
- 바이너리: 경로·크기만 안내

파일명은 충돌 방지를 위해 `타임스탬프-원본이름` 형식으로 저장됩니다.

### Preview와 Mermaid

노트 Preview에서 mermaid 펜스 코드 블록(예: flowchart)은 다이어그램으로 렌더링됩니다. Edit 모드에서는 원문 그대로입니다.

### API 요약

| Method | Path | 설명 |
|---|---|---|
| GET | `/api/agent/health` | harness·skill·모델 기본값 |
| GET | `/api/agent/models` | 선택 가능 모델 목록 |
| GET | `/api/agent/note-meta?path=` | 칩용 name/size |
| POST | `/api/agent/chat` | SSE 채팅 (`prompt`, `note_path`, `session_id`, `model_name`, `image_paths`, `file_paths`) |

SSE 이벤트 예: `session`, `token`, `text`, `tool`, `tool_result`, `note_updated`, `done`, `error`.

다른 앱에서 vault를 쓰려면 [외부 공유하기](#외부-공유하기)를 보세요.

## 외부 공유하기

다른 에이전트·앱이 OB Note vault를 읽고 쓰려면 **SKILL** 또는 **MCP**를 사용합니다.  
둘 다 같은 vault HTTP API(`https://vault.my-agentic-ai.click/api/…`)를 호출하며, 계정은 `actor_id` / `USER_ID`(email)로 분리됩니다.

| 방식 | 적합한 경우 | 진입점 |
|---|---|---|
| **SKILL** | AgentCore / LangGraph 런타임에서 스크립트로 vault 조작 | [my-vaults](https://github.com/kyopark2014/agentic-work/tree/main/runtime_agent/langgraph/skills/my-vaults) |
| **MCP** | MCP 클라이언트·다른 AgentCore 앱에서 도구로 vault 조작 | [`MCP/use-vault/`](MCP/use-vault/) + [`create_mcp.py`](create_mcp.py) |

인증: Secrets Manager `ob-note/vault-agent-token` (또는 동일 값의 `agentic-work/vault-agent-token`) → `Authorization: VaultAgent …`.

### SKILL 활용

외부 앱용 vault skill은 agentic-work의 **[my-vaults](https://github.com/kyopark2014/agentic-work/tree/main/runtime_agent/langgraph/skills/my-vaults)** 입니다.  
(ob-note 내부 Open Agent용 `skills/use-vault`와 역할은 같고, 외부 런타임에서는 `my-vaults` 스크립트를 실행합니다.)

```text
Other app (AgentCore / LangGraph)
  → skills/my-vaults/scripts/read_vault.py | write_vault.py
       → https://vault.my-agentic-ai.click/api/…  (VaultAgent HMAC)
            → {USER_ID}/vault/…
```

#### 구성

| 경로 | 역할 |
|------|------|
| [`SKILL.md`](https://github.com/kyopark2014/agentic-work/blob/main/runtime_agent/langgraph/skills/my-vaults/SKILL.md) | when-to-use, 폴더 규칙, deep link, 트러블슈팅 |
| `scripts/read_vault.py` | health / tree / list / read / search / graph / backlinks |
| `scripts/write_vault.py` | write / append / mkdir / rename / delete / rebuild |
| `scripts/lib_vault.py` | HTTP·인증 헬퍼 (직접 실행하지 않음) |

에이전트 working directory 기준 **전체 경로**로 실행하세요 (`scripts/...`로 줄이지 않음).

#### 환경 변수

| 변수 | 기본 | 설명 |
| --- | --- | --- |
| `OB_DOCS_URL` / `VAULT_API_URL` | `https://vault.my-agentic-ai.click` | OB Note base URL |
| `USER_ID` / `CURRENT_USER_ID` | `local-dev` | vault 소유자 email (프로덕션) |
| `VAULT_AGENT_TOKEN` | Secrets Manager `ob-note/vault-agent-token` 등 | Agent HMAC |

#### Quick start

```bash
# 연결 확인
python skills/my-vaults/scripts/read_vault.py health

# 목록 / 검색 / 읽기
python skills/my-vaults/scripts/read_vault.py list --prefix AI
python skills/my-vaults/scripts/read_vault.py search "온톨로지"
python skills/my-vaults/scripts/read_vault.py read AI/Ontology.md

# 쓰기 (본문은 # 제목으로 시작, YAML frontmatter 금지, 루트 저장 금지)
python skills/my-vaults/scripts/write_vault.py mkdir Meeting
python skills/my-vaults/scripts/write_vault.py write Meeting/Sprint-Review.md --content "# Sprint Review\n\n본문"
```

#### 규칙 요약

- 경로는 vault 상대경로 (`Meeting/Note.md`). path에 email을 넣지 않습니다.
- 새 노트는 카테고리 폴더 아래만 (`AI/…`, `Meeting/…`). vault 루트에 두지 않습니다.
- 덮어쓰기 전 `read`, 부분 추가는 `append` 우선.
- 응답 JSON의 `url`(deep link `/?note=…`)을 사용자에게 path와 함께 전달합니다.

로컬 연동:

```bash
export OB_DOCS_URL=http://127.0.0.1:8502
export USER_ID='you@example.com'
python skills/my-vaults/scripts/read_vault.py list
```

자세한 폴더 카테고리·트러블슈팅은 [my-vaults SKILL.md](https://github.com/kyopark2014/agentic-work/blob/main/runtime_agent/langgraph/skills/my-vaults/SKILL.md)를 보세요.

### MCP 활용

같은 vault API를 **AgentCore Runtime MCP**(Streamable HTTP)로 배포하면 MCP 도구로 읽고 쓸 수 있습니다.  
harness-work의 KB MCP는 **Gateway + Runtime**이지만, OB Note는 **Runtime MCP만** 제공합니다 (Gateway 없음).

#### 구성 코드

| 경로 | 역할 |
|------|------|
| [`MCP/use-vault/`](MCP/use-vault/) | Streamable HTTP MCP 서버 (`mcp_server_use_vault.py` + `vault_client.py`) |
| [`create_mcp.py`](create_mcp.py) | Docker → ECR → AgentCore Runtime(MCP) 배포, `config.json`에 URL/ARN 기록 |

MCP 서버는 vault 저장소를 직접 열지 않고 `OB_DOCS_URL`의 HTTP API를 호출합니다.  
도구마다 **`actor_id`**(계정 email/login id)가 필수이며, Runtime은 `VAULT_AGENT_TOKEN`(Secrets Manager `ob-note/vault-agent-token`)으로 `Authorization: VaultAgent …`를 서명합니다.

```text
Other app (SigV4)
  → AgentCore Runtime MCP  URL  (use_vault_mcp_url)
       → MCP tools (vault_read / vault_write / … + actor_id)
            → https://vault…/api/files|search|graph  (VaultAgent HMAC)
                 → {actor_id}/vault/…
```

#### 도구

| Tool | 설명 |
|------|------|
| `vault_health` / `vault_tree` / `vault_list` / `vault_read` | 조회 |
| `vault_search` / `vault_graph` / `vault_backlinks` | 검색·그래프 |
| `vault_write` / `vault_append` / `vault_mkdir` / `vault_rename` / `vault_delete` | 쓰기 |
| `vault_rebuild` | 그래프 재빌드 |

#### 배포

```bash
cd ob-note
python create_mcp.py
```

수행 내용:

1. `ob-note/vault-agent-token` secret ensure  
2. IAM role `role-use-vault-mcp-for-ob-note-{region}` (ECR pull, Secrets, logs)  
3. `MCP/use-vault` 이미지 빌드 → ECR `use_vault_of_ob_note`  
4. AgentCore Runtime 생성/갱신 (`serverProtocol=MCP`, `networkMode=PUBLIC`)  
5. 계정 root에 `InvokeAgentRuntime` resource policy (Gateway 없이 동일 계정 호출 허용)  
6. `config.json`에 `use_vault_mcp_runtime_arn` / `use_vault_mcp_url` 등 저장  

Runtime 엔드포인트는 **IAM SigV4**입니다. `remote_mcp`처럼 서명 없는 HTTP 클라이언트는 **403**이 납니다.

#### 다른 앱 설정 (`mcp.json`)

배포 후 `config.json`의 `use_vault_mcp_url`을 씁니다. 클라이언트는 `auth_type: aws_sigv4` + 서비스 `bedrock-agentcore`로 요청을 서명해야 합니다 (agentic-work websearch Gateway와 동일한 형식).

```json
{
  "mcpServers": {
    "use-vault": {
      "type": "streamable_http",
      "url": "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3AACCOUNT%3Aruntime%2Fuse_vault_of_ob_note-XXXX/invocations?qualifier=DEFAULT",
      "auth_type": "aws_sigv4",
      "auth_region": "us-west-2",
      "auth_service": "bedrock-agentcore"
    }
  }
}
```

| 필드 | 값 |
|------|-----|
| `type` | `streamable_http` |
| `url` | `config.json` → `use_vault_mcp_url` |
| `auth_type` | `aws_sigv4` |
| `auth_region` | Runtime 리전 (기본 `us-west-2`) |
| `auth_service` | `bedrock-agentcore` |

호출 principal에는 `bedrock-agentcore:InvokeAgentRuntime` (해당 Runtime ARN)이 필요합니다.  
도구 호출 시 **`actor_id`에 vault 소유자 email**을 넘기세요.  
로컬 개발만 할 때는 Gateway 없이 `python -m mcp_server_use_vault` → `http://localhost:8000/mcp`도 가능합니다 ([`MCP/use-vault/README.md`](MCP/use-vault/README.md)).

## 노트 deep link (로그인 필요)

주소창에 vault 상대경로를 넣어 **본인 계정**의 노트로 바로 이동합니다. public 공유(`/s/{token}`)가 아닙니다.  
로그인되어 있지 않으면 로그인 화면을 띄운 뒤, 성공하면 해당 노트를 엽니다.

**URL 형식**

```text
https://vault.my-agentic-ai.click/?note=AI/Knowledge%20Graph/Note.md
```

- 쿼리 키: `note` (별칭 `path`도 허용)
- 값은 vault 상대경로. 공백·한글은 URL 인코딩(`%20` 등)
- `.md` 생략 시 자동으로 붙입니다
- 노트를 열 때마다 주소창의 `?note=`가 현재 경로로 갱신되므로, 주소창을 복사해 같은 계정으로 공유할 수 있습니다
- `https://vault…:note?"…"` 형태는 포트/문법상 유효하지 않습니다. 반드시 `/?note=…`를 사용하세요

**예시**

```text
https://vault.my-agentic-ai.click/?note=AI/Ontology.md
https://vault.my-agentic-ai.click/?note=Meeting/Sprint-Review.md
```

## 노트의 public 공유

로그인된 사용자가 markdown 노트 또는 **폴더**를 **쿠키 없이** 볼 수 있는 CloudFront URL로 공유합니다. 서버가 HTML viewer를 렌더합니다.

**UI**

1. 노트 또는 폴더 우클릭 → **Share public link** → 새 탭에서 공개 페이지 오픈
2. Settings → **Shared List** → 제목 · 종류(Note/Folder) · 공유 시각 · URL 목록, **Link**(열기) / **삭제**
3. Settings → **Share permission** → 폴더 공유 위키 범위: Current / 1-hop(기본) / Shared folder / Entire vault

**폴더 공유**

- 폴더당 토큰 하나. 방문 시 해당 폴더의 **직속 `.md`만** 동적으로 나열합니다 (하위 폴더는 포함하지 않음).
- 목록의 노트는 `/s/{token}/n/{Note.md}` 로만 열리며, 노트별 독립 public 토큰은 만들지 않습니다.
- 폴더 토큰을 삭제하면 인덱스·노트·asset URL이 모두 무효화됩니다.
- 노트 본문의 `[[위키링크]]` 는 Settings → **Share permission** 에 따라 공개 범위가 정해집니다 (기본값 **1-hop**).
  - **Current**: 공유 폴더 **직속 `.md`** 끼리만
  - **1-hop**: 직속 노트 + 그 노트들이 **직접** 가리키는 문서(`/s/{token}/w/…`, 상위·다른 폴더 포함). 외부 노트의 추가 hop은 열리지 않음
  - **Shared folder**: 공유 폴더 트리 아래 모든 `.md` (하위 폴더 포함). 인덱스 목록은 여전히 직속만
  - **Entire vault**: vault 내 임의 노트
- **단일 노트 공유**에서도 `[[위키링크]]` 가 동작합니다. 공유 노트에서 **직접** 가리키는 노트만 같은 토큰의 `/s/{token}/w/…` 로 열리며, vault 전체는 노출되지 않습니다.

**노트·폴더 삭제·이동**

- 노트(또는 폴더) **삭제** 시 해당 경로의 public share 는 Shared List / `{user}/.vault/shares.json` 과 `_public/shares_index.json` 에서 함께 제거됩니다. 공개 URL은 더 이상 열리지 않습니다.
- 노트·폴더 **이동·이름 변경** 시 share 경로가 새 위치로 갱신되고, 노트 본문도 S3에 다시 올려 CloudFront에서도 이어집니다.

**URL 형식** (`config.json`의 `sharing_url`)

```text
https://vault.my-agentic-ai.click/s/{token}              # 노트 viewer 또는 폴더 인덱스
https://vault.my-agentic-ai.click/s/{token}/n/{Note.md}  # 폴더 공유 안의 직속 노트
```

**생성 흐름** (인증 필요)

```text
POST /api/files/share  { "path": "folder/Note.md" }   # 노트
POST /api/files/share  { "path": "folder" }           # 폴더
  → {user}/.vault/shares.json 에 token 등록 (type: note|folder)
  → vault/_public/shares_index.json 에 token → user_id 등록
  → { url, url_path, token, title, type, created_at } 반환

GET  /api/files/share/permission
PUT  /api/files/share/permission  { "permission": "one_hop" }
  → current | one_hop | folder | vault  (기본 one_hop, shares.json top-level)
```

**접속 흐름** (인증 불필요)

```text
GET /s/{token}
  → _public/shares_index.json 에서 token → user_id + path (+ type)
  → type=note  : vault .md → HTML viewer
  → type=folder: 직속 .md 목록 → 인덱스 HTML

GET /s/{token}/n/{Note.md}   # folder share only
  → {folder}/{Note.md} 를 HTML viewer 로 반환

GET /s/{token}/w/{vault/path.md}  # Share permission 범위 안의 위키 대상
```

- 노트 본문 상대 이미지(`![](img.png)`)는 `/s/{token}/raw?path=…` (폴더 공유는 `?note=…&path=…`) 로 다시 쓰여 공개 제공됩니다.
- 공개 viewer는 헤딩에 `id`를 붙이고, `## 목차` 아래 항목이 본문 헤딩과 같으면 `#앵커` 링크로 연결합니다.
- SPA catch-all(`/{path}`)은 `api/`, `s/` 를 제외합니다. 공개 URL이 vault 앱 전체가 보이면 **구버전 배포**이거나 롤아웃 전일 수 있습니다.

## ECS / ALB

1. **S3**: 프로젝트 버킷 — `{userId}/vault/` (markdown API sync), `{userId}/backup/` (compress zip). notes.db는 `{userId}/notes.db`
2. **S3 Files (ECS only)**: 버킷 `/` → 컨테이너 `/mnt/app-data` (notes.db persist)
3. **ECS 서비스**: 이 이미지, 포트 `8502`, health `/api/health`
4. **ALB listener rule**: path `/*` (+ CloudFront origin header) → ob-note target group
5. **CloudFront**: ALB origin (`CloudFront-for-ob-note`) + alias `vault.my-agentic-ai.click`  
   (`config.json`의 `custom_domain` / `sharing_url`)

환경변수 예:

```bash
APP_CONFIG_JSON='{...config.json...}'
SESSION_SIGNING_KEY=...
VAULT_S3_ENABLE=1
VAULT_DIR=/app/data
APP_DATA_MOUNT=/mnt/app-data
TASK_DB_MOUNT=/mnt/app-data
```

## Docker

```bash
docker build -t ob-note .
docker run --rm -p 8502:8502 \
  -e ALLOW_LOCAL_AUTH_BYPASS=1 \
  -v "$PWD/data:/app/data" \
  ob-note
```

## 배포 (installer)

`config.json`이 없거나 일부만 있어도 `installer.py`가 전용 인프라를 만든 뒤 배포합니다.

```bash
python installer.py
```

배포 후 URL: `https://vault.my-agentic-ai.click`  
(`custom_domain`이 비어 있거나 ACM이 미발급이면 CloudFront 기본 도메인 사용)

### 커스텀 도메인 (`vault.my-agentic-ai.click`)

- **인프라 계정** (`default` / `262976740991`): CloudFront + ACM(us-east-1)
- **DNS 계정** (`stock` / `567536745292`): Route53 `my-agentic-ai.click`

installer가 `route53_profile`(`stock`)으로 ACM 검증 CNAME과 A/AAAA alias를 자동 등록합니다.

Google OAuth 콘솔 Authorized JavaScript origin에 `https://vault.my-agentic-ai.click` 을 추가하세요.

### 인증하기

installer는 `config.json`의 `google_client_id` 유무에 따라 인증 방식을 정합니다.

| 조건 | `auth_mode` | 로그인 |
|---|---|---|
| `google_client_id` 있음 | `google` | Google OAuth |
| `google_client_id` 없음 | 대화형 선택 → `google` 또는 `cognito` | Google 또는 Cognito `admin` |

#### Google 인증

`google_client_id`가 있으면 Google OAuth를 사용합니다. 없으면 installer가 Google을 선택했을 때 client ID를 물어봅니다.

```json
{
  "auth_mode": "google",
  "google_client_id": "123456789-xxxx.apps.googleusercontent.com"
}
```

프론트는 GIS로 access token을 받은 뒤 세션을 만듭니다.

```ts
// web: Google access token → 세션 쿠키
await api.setSessionWithAccessToken(accessToken);
// POST /api/session  { "access_token": "..." }
```

백엔드는 tokeninfo로 audience를 검증한 뒤 email을 `user_id`로 씁니다.

```python
# application/api/routes_auth.py
idinfo = verify_google_access_token(access_token, google_client_id)
user_id = idinfo["email"]  # 예: alice@example.com
```

#### Cognito 인증

`google_client_id`가 없을 때 Cognito를 선택하면 User Pool·App Client·`admin` 사용자를 만들고, admin 비밀번호는 Secrets Manager에만 저장합니다 (config.json에 평문 저장 안 함).

```text
Secret: ob-note/cognito-admin-password
Username: admin
```

```json
{
  "auth_mode": "cognito",
  "cognito_user_pool_id": "us-west-2_XXXXXXXXX",
  "cognito_client_id": "xxxxxxxx",
  "cognito_admin_username": "admin",
  "cognito_region": "us-west-2"
}
```

프론트는 ID/Password 폼으로 로그인합니다.

```ts
// web: Cognito username/password → 세션 쿠키
await api.loginWithCognito(username, password);
// POST /api/session  { "username": "admin", "password": "..." }
```

백엔드는 Cognito `USER_PASSWORD_AUTH`로 검증합니다.

```python
# application/api/routes_auth.py
client.initiate_auth(
    ClientId=cognito_client_id,
    AuthFlow="USER_PASSWORD_AUTH",
    AuthParameters={"USERNAME": username, "PASSWORD": password},
)
user_id = client.get_user(AccessToken=access_token)["Username"]  # 예: admin
```

Self-signup은 비활성화되어 있습니다(`AllowAdminCreateUserOnly`). 추가 사용자는 [`add_user.py`](./add_user.py)로 등록합니다. `config.json`의 Cognito 설정을 읽어 영구 비밀번호로 사용자를 만든 뒤, Web UI와 동일한 `USER_PASSWORD_AUTH` 로그인까지 검증합니다.

```bash
# 대화형 (username·password 입력)
python add_user.py

# username만 지정 (password는 getpass로 숨김 입력)
python add_user.py --username user01

# 로그인 검증 생략
python add_user.py --username user01 --skip-login-test

# config 경로 지정
python add_user.py --config config.json --username user01
```

비밀번호 정책: 최소 8자, 대문자·소문자·숫자 각 1자 이상 (기호 선택).

로컬 개발만 `ALLOW_LOCAL_AUTH_BYPASS=1`로 User ID 입력을 허용합니다 (loopback).

### config.json

- 없으면 생성합니다. `accountId` / `region` / `s3_bucket` 등은 STS·기본값으로 채웁니다.
- 버킷 기본값: `storage-for-ob-note-{account}-{region}`
- `custom_domain` 기본값: `vault.my-agentic-ai.click` → `sharing_url`
- 인증 키: `auth_mode`, `google_client_id` 또는 `cognito_*` (위 **인증하기** 참고)

### installer가 수행하는 일

0. **인프라 ensure** (`shared_infra.py`)
   - S3 `storage-for-ob-note-{account}-{region}`
   - Secrets: `ob-note/cloudfront-alb-origin-header`, `ob-note/session-signing-key`
   - IAM: `role-ecs-{task,execution}-for-ob-note-{region}`
   - ECS cluster `cluster-for-ob-note`
   - VPC + `alb-for-ob-note` (없으면 생성)
   - ACM + CloudFront `CloudFront-for-ob-note` (alias → `custom_domain`) → `sharing_url`
1. **skills 업로드** (`use-vault` → `s3://…/skills/`)
2. **AgentCore Harness** (`ob_note`, skill=`use-vault`, tools=`exa` + `code`) → `HARNESS_ARN`
3. ECR `ecr-for-ob-note` 빌드/푸시
4. ALB rule `/*` → `TG-for-ob-note` (CloudFront origin header 조건)
5. ECS `service-for-ob-note` on `cluster-for-ob-note`
   - 계정 vault는 로그인 시 `{userId}/vault/`에 생성 (installer가 샘플 노트를 seed하지 않음)

### 제거 (uninstaller)

```bash
python uninstaller.py
python uninstaller.py --yes
python uninstaller.py --yes --keep-s3              # 버킷 유지
python uninstaller.py --yes --keep-s3 --purge-vault-prefix
```

- ECS · TG · `/*` rule · ECR · 로그 · secrets · CloudFront · ALB/VPC/cluster/IAM · S3 삭제
- `--keep-s3`면 버킷만 남깁니다
