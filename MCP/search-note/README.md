# search-note MCP (Streamable HTTP)

OB Note vault의 **Notes Knowledge Graph**에서 노트를 검색하는 MCP 서버입니다.  
agentic-work `recall_graph_memory`와 동일하게 `POST /api/graph/query`(BFS/DFS + excerpt)를 호출합니다.

전문 검색(`use-vault`의 `vault_search`)과 달리, 라벨·소스 매칭 후 이웃을 순회해 **그래프에 연결된 노트 발췌**를 반환합니다.

## Tools

| Tool | API | 설명 |
|------|-----|------|
| `search_note` | `POST /api/graph/query` | question으로 Notes 그래프 검색 → excerpt 목록 |

모든 호출에 **`actor_id`**(계정 로그인 ID / email)가 필요합니다.

### 반환 형식

성공 (graph memory와 동일):

```json
{
  "text": [
    {
      "type": "excerpt",
      "source": "Ontology.md",
      "path": "AI/Ontology.md",
      "url": "https://vault.../?note=AI%2FOntology.md",
      "related_topics": ["온톨로지"],
      "text": "..."
    }
  ]
}
```

실패:

```json
{"status": "error", "content": [{"text": "..."}]}
```

## Environment

| 변수 | 설명 |
|------|------|
| `OB_DOCS_URL` | vault API base (예: `https://vault.my-agentic-ai.click`) |
| `SHARING_URL` | `OB_DOCS_URL` 미설정 시 fallback |
| `VAULT_AGENT_TOKEN` | Agent HMAC (`Secrets Manager` `ob-note/vault-agent-token`) |
| `PROJECT_NAME` | 기본 `ob-note` |
| `AWS_REGION` | Secrets Manager 조회용 (기본 `us-west-2`) |

`config.json`의 `ob_docs_url` / `region` / `project_name`이 env 미설정 시 사용됩니다.

## Local

```bash
cd MCP/search-note
export OB_DOCS_URL=https://vault.my-agentic-ai.click
export VAULT_AGENT_TOKEN=...   # or rely on Secrets Manager
pip install -r requirements.txt
python -m mcp_server_search_note
# → http://0.0.0.0:8000/mcp
```

## Docker

```bash
cd MCP/search-note
docker build -t search-note-mcp .
docker run --rm -p 8000:8000 \
  -e OB_DOCS_URL=https://vault.my-agentic-ai.click \
  -e VAULT_AGENT_TOKEN=... \
  search-note-mcp
```

## AgentCore Runtime 배포 / 삭제

use-vault와 같은 형식으로 패키지 안에서 배포·삭제합니다.

```bash
cd MCP/search-note
python installer.py      # IAM → Docker/ECR → AgentCore Runtime
python uninstaller.py    # Runtime → ECR → IAM role (--yes 로 확인 생략)
```

ob-note 루트에서도 동일하게 호출할 수 있습니다.

```bash
python installer.py --search-note-only
python uninstaller.py --search-note-only --yes
# 또는 (호환) python create_mcp.py --target search-note
```

ECR + AgentCore Runtime(MCP, IAM SigV4)만 배포합니다 (Gateway 없음).  
자세한 설정은 루트 [README.md ### MCP 활용](../../README.md#mcp-활용)을 보세요.

## 다른 앱에서 연결 (`mcp.json`)

```json
{
  "mcpServers": {
    "search-note": {
      "type": "streamable_http",
      "url": "<config.json search_note_mcp_url>",
      "auth_type": "aws_sigv4",
      "auth_region": "us-west-2",
      "auth_service": "bedrock-agentcore"
    }
  }
}
```
