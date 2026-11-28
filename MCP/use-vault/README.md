# use-vault MCP (Streamable HTTP)

ob-note vault의 마크다운 노트를 조회·검색·생성·수정하는 MCP 서버입니다.  
vault HTTP API를 `VaultAgent` 인증으로 호출합니다.

## Skills over MCP

SEP-2640 `io.modelcontextprotocol/skills` 확장을 선언합니다.  
서버가 vault 워크플로 지침(`SKILL.md`)을 게시하고, 호스트가 필요할 때만 본문을 읽습니다.

- 본문(단일 소스): [`skills/use-vault/SKILL.md`](skills/use-vault/SKILL.md)
- 서버 구현: [`vault_skills.py`](vault_skills.py) — `Skills(Extension)` + `MethodBinding` / `TextResource`
- 등록: `mcp_server_use_vault.py` → `extensions=[apps, skills]`
- 설계 문서: `agentic-work/skills-over-mcp.md`

### Server capabilities

`capabilities` 딕셔너리를 직접 쓰지 않습니다. `MCPServer(extensions=[apps, skills])`에 Extension을 넘기면 SDK가 `initialize` 응답에 자동으로 넣습니다.

| Extension | `identifier` | `settings()` | advertise 위치 |
|-----------|--------------|--------------|-----------|
| `Skills` (`vault_skills.py`) | `io.modelcontextprotocol/skills` | `{"directoryRead": False}` | `capabilities.extensions[…]` |
| `Apps` (`vault_apps.py`) | `io.modelcontextprotocol/ui` | `{}` | 동일 |

SDK `_apply_extension`이 각 Extension의 tools / resources / methods를 등록하고  
`ServerCapabilities.extensions[identifier] = settings()` 로 advertise합니다.
`initialize` 시 `get_capabilities()`가 핸들러 유무 + extension map을 합칩니다.

```json
{
  "capabilities": {
    "tools": { "listChanged": false },
    "resources": { "subscribe": false, "listChanged": false },
    "extensions": {
      "io.modelcontextprotocol/ui": {},
      "io.modelcontextprotocol/skills": { "directoryRead": false }
    }
  }
}
```

- `tools` — `@mcp.tool()` 등록으로 생김
- `resources` — Skills/Apps `TextResource` 등록으로 `resources/list`·`read` 핸들러가 생겨 생김
- `extensions` — 각 Extension의 `identifier` + `settings()`

호스트(`mcp_skills.server_supports_skills`)는 `extensions`에 `io.modelcontextprotocol/skills` 키가 있는지만 보고 skills API를 호출합니다.

`directoryRead: false`는 스킬 파일 목록을 **「디렉터리 실시간 조회」로 열어 주지 않는다**는 뜻입니다.

**Skills 확장에서의 의미**

| 방식 | 조건 | 동작 |
|------|------|------|
| 정적 매니페스트 | 기본 (`directoryRead: false` 또는 생략/`{}`) | `skills/list`의 `resources: [{ uri, digest, size }, …]`에 목록 고정. 호스트는 **그 URI만** `resources/read` |
| 디렉터리 읽기 | `directoryRead: true` | `resources/directory/read`로 스킬 루트 **실시간 리스팅** |

스펙 기본이 `false`인 이유: 대부분 skill은 배포 시 파일이 고정된 번들이고, digest/size 검증이 매니페스트에 의존합니다. 디렉터리 탐색 결과를 보유 manifest의 확장으로 취급하거나 새 파일을 스킬 일부로 노출하면 안 됩니다(MUST NOT).

**use-vault에서 `false`인 이유**

- 게시 대상이 `skills/use-vault/` **정적 패키지**(현재 SKILL.md 하나)뿐
- 기동 시 `load_skill_catalog`로 메모리·digest/size 고정 → list entry가 complete
- 변경 시 재배포·재시작으로만 매니페스트 갱신 → `resources/directory/read` 불필요

그래서 `Skills.settings()`가 `{"directoryRead": False}`를 반환합니다.

### 서버가 제공하는 메서드

| 메서드 | 핸들러 | 역할 |
|--------|--------|------|
| `skills/list` | `_handle_list` | 카탈로그. entry마다 `uri` · `frontmatter` · `resources`(URI + SHA-256 digest + size). envelope: `resultType` · `ttlMs` · `cacheScope` |
| `skills/get` | `_handle_get` | URI 단건 조회. 없으면 `-32602` (`INVALID_PARAMS`) |
| `resources/read` | `TextResource` (`skill://use-vault/…`) | `SKILL.md`(및 부속 파일) 바이트. list에 넣은 digest/size와 동일 바이트여야 함 |

시작 시 `skills/<name>/`를 읽어 메모리 카탈로그·매니페스트를 만듭니다 (`directoryRead: false` — 디렉터리 실시간 조회 없음).

```text
Host                          use-vault MCP
  │  initialize (capabilities: skills + resources)
  │◄─────────────────────────────┤
  │  skills/list                 │
  │─────────────────────────────►│  _handle_list → 메타만 (본문 없음)
  │  resources/read(skill://…)   │  (필요 시에만)
  │─────────────────────────────►│  TextResource → SKILL.md 바이트
```

`skills/list` 응답 요지:

```json
{
  "resultType": "complete",
  "ttlMs": 300000,
  "cacheScope": "public",
  "skills": [
    {
      "uri": "skill://use-vault/SKILL.md",
      "frontmatter": { "name": "use-vault", "description": "…" },
      "resources": [
        { "uri": "skill://use-vault/SKILL.md", "digest": "sha256:…", "size": 8705 }
      ]
    }
  ]
}
```

### 클라이언트가 어떻게 쓰는지 (agentic-work)

호스트 쪽은 `agentic-work/runtime_agent/langgraph/mcp_skills.py` + `skill.py`입니다.

1. **발견** — use-vault 연결 후 capability에 skills가 있으면 `skills/list`만 호출. 본문 pre-fetch 금지. description만 `<available_skills>`에 노출.
2. **활성화** — 모델이 `get_skill_instructions`를 호출하면 `resources/read(uri)`로 본문을 읽고, 보관해 둔 manifest의 **size · SHA-256 digest · frontmatter**와 대조. 불일치 시 본문 미사용.
3. **실행** — skill 지침에 따라 `vault_*` MCP tools를 `tools/call`로 호출 (노트 I/O는 skill이 아니라 도구).

```text
chat.create_agent
  → discover_mcp_skills → skills/list          # 메타 등록
모델
  → get_skill_instructions → resources/read    # lazy + 검증
  → vault_read / vault_write / …               # 실제 작업
```

참고: 서버는 `skills/get`도 구현하지만, list entry가 complete라 agentic-work 호스트는 현재 get을 호출하지 않습니다.

## Tools

모든 도구에 **`actor_id`**(Google 로그인 email)를 넘길 수 있습니다.  
기본값은 `kyopark2014@gmail.com`이며, 생략하거나 `@` 없는 닉네임(`ksdyb`, `local-dev` 등)이면 서버가 기본값으로 치환합니다.  
환경변수 `USE_VAULT_DEFAULT_ACTOR_ID`로 오버라이드할 수 있습니다.

| Tool | API | 설명 |
|------|-----|------|
| `vault_health` | `GET /api/health` | 헬스 |
| `vault_tree` | `GET /api/files/tree` | 파일 트리 |
| `vault_list` | `GET /api/files/list` | prefix/ext 목록 |
| `vault_read` | `GET /api/files/read` | 노트 본문 |
| `vault_search` | `GET /api/search` | 전문 검색 |
| `vault_graph` | `GET /api/graph` | 위키링크 그래프 |
| `vault_backlinks` | `GET /api/graph/backlinks` | 백링크 |
| `vault_write` | `PUT /api/files/write` | 덮어쓰기 |
| `vault_append` | `POST /api/files/append` | 이어쓰기 |
| `vault_str_replace` | read → replace → write | 부분 치환 (`old_string`→`new_string`) |
| `vault_upload` | `POST /api/files/upload` | 이미지·바이너리 (`content_base64` 또는 `source_url`) |
| `vault_mkdir` | `POST /api/files/mkdir` | 폴더 생성 |
| `vault_rename` | `POST /api/files/rename` | 이름/경로 변경 |
| `vault_delete` | `POST /api/files/delete` | 삭제 |
| `vault_rebuild` | `POST /api/graph/rebuild` | 그래프 재빌드 |

노트 규칙: vault 상대경로, YAML frontmatter 금지(`# 제목`으로 시작), 새 노트는 주제 폴더 아래만 (`Category/Note.md`).

## Environment

| 변수 | 설명 |
|------|------|
| `OB_DOCS_URL` | vault API base (예: `https://vault.my-agentic-ai.click`) |
| `SHARING_URL` | `OB_DOCS_URL` 미설정 시 fallback |
| `VAULT_AGENT_TOKEN` | Agent HMAC (`Secrets Manager` `ob-note/vault-agent-token`) |
| `PROJECT_NAME` | 기본 `ob-note` |
| `AWS_REGION` | Secrets Manager 조회용 (기본 `us-west-2`) |

로컬 개발에서는 `SESSION_SIGNING_KEY` 또는 loopback(`http://127.0.0.1:8502`)도 가능합니다.  
`config.json`의 `ob_docs_url` / `region` / `project_name`이 env 미설정 시 사용됩니다.

## Local

```bash
cd MCP/use-vault
export OB_DOCS_URL=https://vault.my-agentic-ai.click
export VAULT_AGENT_TOKEN=...   # or rely on Secrets Manager / SESSION_SIGNING_KEY
pip install -r requirements.txt
python -m mcp_server_use_vault
# → http://0.0.0.0:8000/mcp
```

## Docker

```bash
cd MCP/use-vault
docker build -t use-vault-mcp .
docker run --rm -p 8000:8000 \
  -e OB_DOCS_URL=https://vault.my-agentic-ai.click \
  -e VAULT_AGENT_TOKEN=... \
  use-vault-mcp
```

## 다른 앱에서 연결 (remote_mcp)

```json
{
  "type": "remote_mcp",
  "config": {
    "remoteMcp": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

Cursor 등 MCP 클라이언트에서는 Streamable HTTP URL로 `http://localhost:8000/mcp`를 등록하면 됩니다.

## AgentCore Runtime 배포 / 삭제

korea-weather MCP와 같은 형식으로 패키지 안에서 배포·삭제합니다.

```bash
cd MCP/use-vault
python installer.py      # IAM → Docker/ECR → AgentCore Runtime
python uninstaller.py    # Runtime → ECR → IAM role (--yes 로 확인 생략)
```

ob-note 루트에서도 동일하게 호출할 수 있습니다.

```bash
python installer.py --use-vault-only
python uninstaller.py --use-vault-only --yes
# 또는 (호환) python create_mcp.py
```

ECR + AgentCore Runtime(MCP, IAM SigV4)만 배포합니다 (Gateway 없음).  
자세한 설정·`mcp.json` 예시는 루트 [README.md ### MCP 활용](../../README.md#mcp-활용)을 보세요.

## MCP App (UI)

읽기 도구 6개(`vault_tree`, `vault_list`, `vault_read`, `vault_search`, `vault_graph`, `vault_backlinks`)는
`ui://vault/viewer.html`에 연결되어 MCP Apps 호스트(agentic-work, Claude 등)에서 채팅 안 UI로 표시됩니다.

- 인라인: 검색/목록은 카드 캐러셀(최대 8개), 노트는 요약 카드. 전체 화면: 트리·본문·백링크·그래프.
- 결과는 `CallToolResult`: `content` JSON(모델이 읽는 값)과 `structuredContent` 모두
  `{ok, kind, ...}`와 노트마다 deep link `url`을 담습니다(`content`에도 `ok`/`kind`를 넣어
  호스트가 `structuredContent`를 누락해도 MCP App UI 폴백이 동작). 쓰기 도구도 `path`/`url`을 반환합니다.
  에이전트는 채팅 목차·목록에 `[제목](url)` 마크다운 링크를 포함해야 합니다 (MCP Apps UI와 병행).
- 코드: `vault_apps.py`(도구·리소스), `ui/`(뷰어 소스, `python3 MCP/use-vault/ui/build.py`로 `ui/dist` 생성 후 **커밋**).
- UI 변경 후에는 이 MCP 런타임을 다시 배포해야 합니다.

