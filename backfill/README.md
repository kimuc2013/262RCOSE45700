# 데이터 백필

고려대 교육정보 사이트의 학부 공통 학사정보를 수집해 청크·학사일정 파일로 만드는 파이프라인.

## 실행 준비

Python 3.12 이상과 uv를 사용한다 (서빙과 같은 환경). 의존성 버전은 `uv.lock`에 고정되어 있다.

```bash
cd backfill
uv sync --frozen                 # backfill/.venv 생성
cp .env.example .env             # 값 채우기
```

`backfill/.env`는 커밋하지 않는다. 엔드포인트 주소는 공개 저장소에 올리지 않으므로 팀 채널의 Furiosa RAG API 가이드에서 확인한다.

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `BGE_BASE_URL` | 임베딩 단계 | 없음 | 임베딩 API 주소 (`http://<host>:<port>/v1`) |
| `BGE_MODEL` | | `furiosa-ai/bge-m3` | 임베딩 모델 |
| `BGE_REVISION` | | `v2026.4` | 모델 revision (manifest.json에 기록) |
| `EMBED_BATCH_SIZE` | | `32` | 한 요청에 묶어 보낼 문장 수. 세 조가 함께 쓰는 서버라 배치로 보냄 |
| `QWEN_BASE_URL` | VLM 사용 시 | 없음 | 이미지를 읽을 멀티모달 모델 API 주소 (`sources.yaml`의 `vlm_images`만 읽음) |
| `QWEN_VLM_MODEL` | | `furiosa-ai/Qwen3-VL-32B-Instruct` | 멀티모달 모델 (`furiosa-ai/` 포함해야 함) |
| `FURIOSA_API_KEY` | VLM 사용 시 | 없음 | Qwen API 키 |
| `DATA_DIR` | | `data` | 데이터 폴더 (`backfill/` 기준) |
| `USER_AGENT` | | `KU-AcademicInfo-MCP-Backfill/0.1 (student project)` | 크롤러 User-Agent |
| `REQUEST_INTERVAL_SEC` | | `1.0` | 요청 사이 간격(초) |

필수 변수가 비어 있으면 해당 단계가 빠진 변수 이름을 알려주고 멈춘다.

## 실행 (`backfill/`에서)

```bash
uv run python src/crawl.py              # 1단계 크롤링 (--only calendar | pages)
uv run python src/parse_calendar.py     # 학사일정 → data/calendar.jsonl
uv run python src/fetch_attachments.py  # 2단계 첨부 받기 (바뀌지 않은 파일은 다시 받지 않음)
uv run python src/extract.py            # 3단계 텍스트 추출 → data/extracted/{doc_id}.json, 미리보기 .md
uv run python src/clean.py              # 4단계 정제 → data/clean/{doc_id}.json, 미리보기 .md
```

의존성을 바꿀 때는 `pyproject.toml`을 고친 뒤 `uv lock`으로 `uv.lock`을 갱신하고 둘 다 커밋한다.
