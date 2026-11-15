# ob-note 노트 저장소: DynamoDB 기준 + S3 원본 미러

## 목적

기존에는 **로컬 working copy ↔ S3**를 양방향으로 sync했습니다. 읽는 도중 다른 작업(pull, prune,
pending flush)이 끼어들면 파일이 덮어써지거나 사라지고, 트리/읽기가 S3 list·download에 묶여 느렸습니다.

변경 후:

| 역할 | 저장소 |
|---|---|
| 모든 노트 작업의 기준(read / write / list / search / rename / delete) | **DynamoDB** |
| 노트·첨부의 원본 보관, 다운로드, 압축(zip) | **S3** (DynamoDB에서 파생, 항상 최신 유지) |
| 디스크 소비자(그래프, 압축, 공유 렌더)용 임시 사본 | 로컬 working copy (**읽기용 캐시**, 언제든 재생성 가능) |

데이터 흐름은 **한 방향**입니다: `API → DynamoDB → (S3, 로컬 캐시)`.
S3 → 로컬 pull·병합이 없어지므로, 읽기 중 다른 작업이 파일을 망가뜨릴 경로가 사라집니다.

## 테이블

- 이름: `ob-note` (config.json `dynamodb_table`, env `NOTES_DDB_TABLE`)
- 과금: On-demand, PITR 켬, 삭제 보호 켬
- 단일 테이블, 사용자 분리는 파티션 키로 처리

| 키 | 값 |
|---|---|
| `pk` | `USER#{sanitized user segment}` |
| `sk` | `F#{NFC path}` 파일/노트/폴더 · `X#{NFC path}#{id}` S3 삭제 tombstone |

`path`를 sort key에 두는 이유: 경로 유일성이 조건식 없이 보장되고, 폴더 하위 조회가
`begins_with(sk, 'F#폴더/')` 한 번의 Query가 됩니다. (노트 수백 개 규모에서 폴더 rename은 항목 재작성으로 충분)

### `F#` 항목

| 속성 | 설명 |
|---|---|
| `path` | NFC 정규화 경로 (macOS NFD 중복 제거) |
| `kind` | `note`(텍스트, 본문 inline) · `file`(바이너리, S3에만 본문) · `folder`(빈 폴더 마커) |
| `note_id` | 기존 SQLite 레지스트리의 id를 **그대로 승계** (agent 채팅 기록이 이 id를 참조) |
| `title`, `size_bytes`, `md5` | 메타 |
| `content` | 텍스트 본문. 300KB 초과 시 생략(`inline=false`)하고 S3가 본문 보관 (DynamoDB 항목 한도 400KB) |
| `version` | 쓰기마다 +1. 낙관적 잠금(`expected_version`)과 S3 미러 판정에 사용 |
| `created_at`, `updated_at` | ISO8601 UTC |
| `s3_key` | 실제 S3 객체 키 (기존 키가 NFD여도 그대로 보존) |
| `s3_version`, `s3_state` | S3에 반영된 마지막 version / `synced` · `pending` · `error` |
| `outbox`, `outbox_at` | S3 반영이 필요할 때만 존재(sparse). 재시도 시각 겸용 |

### 보조 인덱스

| 이름 | 종류 | 키 | 용도 |
|---|---|---|---|
| `path-index` | **LSI** | `pk` / `path` (본문 제외 INCLUDE) | 트리·목록 조회. 강한 일관성이라 방금 만든 노트가 바로 보임 |
| `note_id-index` | GSI | `note_id` (sparse) | note_id → 경로 조회 (agent 세션) |
| `outbox-index` | GSI | `outbox` / `outbox_at` (sparse, INCLUDE) | S3 반영 대기 항목만 조회 |

`path-index`가 필요한 이유: 베이스 테이블 Query는 projection을 줘도 페이지당 최대 1MB의 **본문까지 읽어서**
노트 수백 개면 한 번의 트리 조회가 여러 번의 왕복이 됩니다. 인덱스에는 본문이 없어 한 번에 끝납니다.
삭제 tombstone은 `path` 대신 `del_path`를 써서 이 인덱스에 들어가지 않습니다.

LSI는 테이블 생성 시점에만 만들 수 있습니다. LSI 없이 만들어진 테이블에서도 앱은 정상 동작하고
(목록 조회만 느린 베이스 테이블 경로로 자동 대체, 로그에 경고), 빠르게 하려면 재생성합니다.
S3가 원본을 갖고 있고 마이그레이션은 재실행해도 안전하므로 다음 순서로 하면 됩니다.

```bash
# 앱이 아직 새 테이블에 쓰기 전이라면(= 테이블 내용이 마이그레이션 복사본뿐일 때) 안전합니다.
aws dynamodb update-table --table-name ob-note --no-deletion-protection-enabled --region us-west-2
aws dynamodb delete-table --table-name ob-note --region us-west-2
aws dynamodb wait table-not-exists --table-name ob-note --region us-west-2
.venv/bin/python migrate_notes_to_dynamodb.py --create-table
```

앱이 이미 새 테이블에 쓰기 시작한 뒤에는 삭제하지 마세요. 새 테이블 이름으로 만든 뒤 백업(PITR)에서
복원하거나, 앱을 잠시 멈추고 `--verify`로 outbox가 0인지 확인한 다음 진행하세요.

## S3 동기화

DynamoDB 자체가 **durable outbox**입니다. 별도의 `pending_s3_ops.json` 큐는 쓰지 않습니다.

- **생성/수정**: DynamoDB 조건부 put (`version` +1, `outbox` 세팅) → 즉시 응답 → 백그라운드에서
  S3 `put_object` → `version`이 그대로일 때만 `s3_version` 갱신 + `outbox` 제거.
  그 사이 또 수정됐다면 조건이 실패하고 새 outbox가 남아 다음 라운드에서 다시 올립니다.
- **삭제 / rename**: 같은 트랜잭션에서 `F#` 항목 삭제 + `X#` tombstone 생성. tombstone을 S3에서
  지운 뒤에 제거합니다. 같은 키에 살아 있는 항목이 다시 생겼다면 삭제하지 않습니다.
- **재시도**: 실패하면 `outbox_at`을 backoff 시각으로 밀어 둡니다. ECS task가 교체돼도
  상태가 DynamoDB에 있으므로 다음 task가 이어서 처리합니다. (주기 reconcile 15초 + 쓰기 직후 즉시)
- **큰 파일 / 바이너리**: 항목 한도 때문에 S3에 먼저 쓰고(동기) 메타만 DynamoDB에 기록합니다.
- **검증**: `md5`와 S3 ETag를 비교해 드리프트를 보고합니다 (`migrate_notes_to_dynamodb.py --verify`).

## 로컬 캐시

`data/{user}/vault/`는 DynamoDB에서 **파생**됩니다. 쓰기 후 write-through, 디스크가 필요한 소비자
(그래프·압축·공유)는 `note_store.materialize*()`로 버전 비교 후 필요한 것만 다시 씁니다.
압축 전에는 S3 반영을 끝낸 뒤(`flush`) 진행하므로 zip은 S3 원본과 일치합니다.

## 기존 레거시 모드

`dynamodb_table`이 설정되지 않았거나 `NOTES_DDB_DISABLE=1`이면 기존 동작(로컬+S3 큐)을 그대로 유지합니다.
