# 고려대 학사정보 MCP 서빙

모델 이외의 데이터와 서빙 플로우를 위한 로컬 개발판입니다. AWS 계정, GPU, NPU 없이 기존 샘플을 넣고 MCP 호출을 검증할 수 있습니다. 발표 자료와 제출본은 수정하지 않았습니다.

## 바로 실행

이 폴더에서 실행합니다. Python 3.12 이상, uv가 필요합니다.

```bash
uv sync --frozen --extra dev
uv run academic-data --samples ./sample
uv run academic-mcp --transport streamable-http
```

주소는 `http://127.0.0.1:8000/mcp`입니다. 로컬 전용이며 AWS 공개 배포용 인증과 HTTPS는 아직 연결하지 않았습니다. 서버만 켰다고 Claude, ChatGPT의 원격 클라이언트가 이 로컬 주소에 접근할 수 있는 것은 아닙니다.

stdio를 지원하는 로컬 클라이언트에서는 `uv run academic-mcp`를 실행합니다. 클라이언트의 작업 디렉터리를 이 폴더로 지정하거나 `uv --directory /절대경로/serving run academic-mcp`를 사용하세요. 다른 디렉터리에 DB를 두려면 `ACADEMIC_DB`를 절대경로로 설정하세요.

```bash
uv run pytest -q
```

## 제공하는 MCP 인터페이스

| 종류 | 이름 | 입력과 역할 |
|---|---|---|
| tool | `get_academic_calendar` | `semester="2026-2"`, `keyword="이중전공"`, 수집된 확정 일정 조회 |
| tool | `search_academic_info` | `query`, 선택 입력 `history`, `top_k`(1~10), `semester`, `target`, 근거 후보 반환 |
| prompt | `grounded_answer` | 출처 표기, 근거 부족 안내, 문서 지시문 무시 지침 |

`grounded_answer`는 세 번째 도구가 아니라 MCP 프롬프트입니다. 반환 지침은 모든 도구 응답에도 포함됩니다. 지침만으로 프롬프트 인젝션 방어가 입증되는 것은 아닙니다.

상태는 `found`, `insufficient_evidence`, `login_required`, `out_of_scope`입니다. 모델 API 오류, 잘못된 입력 등 기술 오류는 MCP 도구 오류로 전달하여 근거 부족과 구별합니다. 응답에는 수집일, 원문 URL, 최신성 경고, 데이터 revision, 캐시 여부가 들어 있습니다.

## 현재 구현 범위

- 기존 JSON에서 일정 43건과 이중전공 청크 9개를 SQLite로 적재합니다. 입력 검증 후 한 트랜잭션으로 교체하고, 실패하면 기존 데이터를 유지합니다.
- 동일 스냅샷은 건너뜁니다. 내용이나 메타데이터가 바뀌면 revision을 바꾸고 캐시와 벡터를 무효화합니다. 청크의 전화번호와 이메일 패턴을 제거합니다.
- 로컬 모드는 한국어 문자 bigram 기반 BM25 기준선입니다. 실제 형태소 분석이나 의미 검색을 구현한 것으로 평가하지 않습니다.
- 로컬 모드의 `found`는 이중전공의 일부 명시적 항목(신청자격, 접수방법, 평가기준, 정의)에 대한 데모 규칙입니다. 일반적인 근거 충분성 분류기가 아닙니다. 나머지는 후보가 있어도 `insufficient_evidence`로 돌려줍니다.
- 로컬 후속 질의는 맥락을 추정하지 않고 독립 질문을 요청합니다. remote 모드에서만 재작성합니다.
- 보수적인 정규화 완전 일치 캐시(TTL 300초, 최대 약 1,000건)를 구현했습니다. 소수점과 부정 표현을 보존하고 대화, 학기, 대상, top-k, 모델 설정, 임계값과 기준일로 분리합니다. 어미를 지우거나 유사도만으로 재사용하지 않습니다.
- 캐시 조회를 검색 대기열과 분리하고, 적중 시 전체 데이터 로딩과 검색 슬롯을 생략합니다. 미적중 작업은 실행 직전 캐시를 재확인합니다. 청크와 일정은 최신 버전의 디코딩 결과를 각각 재사용합니다.
- 동일 요청 병합, 실행과 대기열 제한, 전체 시간 예산을 구현했습니다. 일정 조회는 별도 실행 경로를 사용합니다. 동작과 한계는 [온라인 서빙 제어](docs/ONLINE_CONTROLS.md)를 참고하세요.
- 모델 없는 일정 조회, 검색, 캐시, 거절 경로를 로컬에서 테스트합니다. 샘플 수집일은 2026-10-18이며 최신 사이트와 재대조한 결과가 아닙니다.
- 요청 로그는 stderr에 request ID, 전체 지연, 상태, 모델 호출 수, 캐시 적중 여부만 남깁니다. 원문 질의와 대화는 기록하지 않습니다.

## NPU 서버가 준비되면

`.env.example`은 설정 예시입니다. 로컬 Python 실행은 `.env`를 자동으로 읽지 않으므로 셸 환경변수로 export해야 합니다. 모델 담당자와 [API 계약](docs/MODEL_CONTRACT.md)을 먼저 맞추세요.

```bash
export MODEL_MODE=remote
export MODEL_BASE_URL=https://모델서버주소
export EMBEDDING_MODEL=확정한모델ID
export REWRITE_MODEL=확정한모델ID
export RERANK_MODEL=확정한모델ID
# 필요한 경우 MODEL_API_KEY를 환경변수로 설정
uv run python -m academic_mcp.index
uv run academic-mcp --transport streamable-http
```

벡터는 요청 중에 생성하지 않고 별도 명령으로 미리 만듭니다. 이후에는 질의 임베딩, BM25와 벡터 전수 비교, RRF 결합, 후보 최대 20개 리랭킹 순서입니다. 벡터 전수 비교는 작은 코퍼스용이며 대규모 ANN 인덱스는 아닙니다. 모델을 교체하면 벡터를 다시 만들어야 합니다. 같은 모델 ID로 가중치가 바뀌어도 반드시 인덱스를 다시 만드세요.

`RERANK_MIN_SCORE`는 평가셋으로 보정한 뒤 설정합니다. 미설정이면 remote 모드도 후보를 확정 근거로 표시하지 않습니다. 실제 NPU, 후속 질문 품질, p95 목표는 아직 검증하지 않았습니다.

## 개발용 데모 화면

터미널에서 실제 MCP 호출만 재현하려면 서버 실행 후 다음을 사용합니다. 예시 질문에 해당하는 도구와 입력을 명시적으로 지정하며, LLM이 도구를 선택하거나 답변을 생성하는 단계는 포함하지 않습니다.

```bash
uv run python -m academic_mcp.terminal_demo
# 색상을 강제로 사용 (터미널에서는 기본적으로 자동 적용)
uv run python -m academic_mcp.terminal_demo --color always
# 전체 반환 JSON 확인
uv run python -m academic_mcp.terminal_demo --raw
# 원하는 질문으로 직접 검색 도구 호출
uv run python -m academic_mcp.terminal_demo --tool search_academic_info --arguments '{"query":"이중전공 신청자격은?","semester":"2026-2"}'
```

8000 포트의 MCP 서버를 먼저 실행한 상태에서 별도 터미널로 이 폴더에서 실행합니다.

```bash
uv run python -m academic_mcp.demo
```

브라우저에서 `http://127.0.0.1:8001`을 열면 실제 MCP를 호출할 수 있습니다. 신청 기간, 신청자격, 합격컷, 개인 성적의 예시와 같은 요청 10개 동시 전송 버튼이 있습니다. 최종 답변 생성 모델은 사용하지 않으며 공식 근거와 실제 서버 지표를 표시합니다. 이 화면은 제품 UI가 아니라 개발용 검증 도구입니다.

## Docker 실행

```bash
docker build -t ku-academic-mcp:local .
docker compose run --rm mcp python -m academic_mcp.ingest --db /data/academic.sqlite3 --samples /samples
docker compose up -d
docker compose logs -f
```

컨테이너의 8000 포트는 호스트의 loopback에만 연결합니다. 데이터는 이름 있는 볼륨에 보존합니다. `docker compose down -v`는 데이터를 삭제하므로 사용 시 주의하세요. 원본 샘플은 읽기 전용입니다. `.env`의 모델 URL을 컨테이너에서 사용할 때 `127.0.0.1`은 호스트가 아닌 컨테이너 자신입니다. EC2에서는 실제 NPU 접근 주소를 사용하세요.

## 작업 순서

[AWS 전후 작업표](docs/WORK_PLAN.md)에 현재 완료 범위, 독립적으로 진행할 작업, 외부 연결이 필요한 작업을 정리했습니다.

MCP 구현은 [공식 Python SDK v1](https://py.sdk.modelcontextprotocol.io/v1/)을 사용하고 `uv.lock`으로 실제 버전을 고정합니다. SQLite와 순수 Python 검색은 작은 시작 구성이며 벡터 DB 선정의 최종 결정은 아닙니다.

## 실제 MCP 부하테스트

`serving` 폴더에서 실행합니다. 기존 서버나 DB를 바꾸지 않고 임시 샘플 DB와 별도 로컬 서버를 띄운 후 종료합니다.

```bash
uv run python -m academic_mcp.load_test --concurrency 1 8 32 96 --requests 192 --output data/load-test.json
# 동시성 8에서 시나리오당 2,000건, 총 8,000건
uv run python -m academic_mcp.load_test --concurrency 8 --requests 2000 --output data/load-test-longer.json
```

시나리오는 캐시를 미리 채운 동일 질문, 캐시 없이 시작하는 동일 질문, 매번 다른 캐시 키, 일정 조회입니다. 동일 질문의 cold 시나리오는 첫 요청 묶음 이후 캐시가 채워지므로 순수 미적중 처리량이 아닙니다. 매번 다른 키 시나리오로 미적중 경로를 봅니다. 연결 및 MCP 초기화와 캐시 예열은 타이밍에서 제외하고 클라이언트에서 본 요청 응답 시간을 기록합니다. 독립 MCP 세션마다 요청 하나를 보내고 응답 뒤 다음 요청을 보내는 closed-loop 방식입니다. 지정 동시성이 모두 항상 서버 안에서 실행된다는 뜻은 아닙니다.

p50/p95/p99, 성공 처리량, 오류 코드별 건수, 캐시 적중과 검색 작업 병합 횟수를 JSON에 저장합니다. 전체 지연 분위수에는 거절 응답도 포함하므로 `success_p95_ms` 및 성공률과 함께 봅니다. `insufficient_evidence`는 정상 도구 응답으로 세고 검색 품질은 평가하지 않습니다.

실제 배포 서버에 권한 있는 부하를 보내려면 `--url https://서버주소/mcp`를 지정할 수 있습니다. 이 경우 기존 서버를 시작하거나 종료하지 않습니다. 로컬 단일 실행과 짧은 반복 결과로 EC2, 실제 모델 API, 장시간 운영 용량을 추정하지 않습니다.
