# 고려대 학사정보 MCP

팀 저장소: `262RCOSE45700`

실전 SW 프로젝트 Team 7의 공개 학사정보 검색 서버입니다. MCP로 공식 근거와 출처를 반환하며 최종 답변은 사용자의 AI 클라이언트가 작성합니다.

## 저장소 구성

- `serving/`: MCP 서버, 일정 조회, 캐시, 요청 병합, 동시성 및 시간 예산 제어
- `serving/tests/`: 회귀 검사와 실제 MCP HTTP 동시 호출 검사
- `serving/docs/`: 구현 범위, 인터페이스 초안, 검증 및 로컬 부하테스트 결과
- `keynote/sample/`: 기존 공개 학사자료 샘플. 전체 백필 데이터가 아닙니다.

모델 및 백필 담당의 모듈은 아직 통합하지 않았습니다. 로컬 검색은 BM25 기준선과 일부 샘플 질문의 데모 규칙이며 일반 검색 품질 평가 결과가 아닙니다. 실제 모델 API, 유사도 캐시, 인증 기반 유저별 제한과 여러 프로세스 간 공유 제어는 미완료입니다.

## 로컬 실행

Python 3.12 이상과 uv를 사용합니다.

```bash
cd serving
uv sync --frozen --extra dev
uv run academic-data --samples ../keynote/sample
uv run academic-mcp --transport streamable-http
```

다른 터미널에서 실행합니다.

```bash
cd serving
uv run python -m academic_mcp.terminal_demo --color always
uv run pytest -q
```

부하테스트는 기존 DB를 수정하지 않고 별도 샘플 서버를 실행합니다.

```bash
uv run python -m academic_mcp.load_test
```

## EC2 배포

Git과 Docker 및 Docker Compose 플러그인이 설치된 EC2 터미널에서 실행합니다. 설치 명령은 EC2 운영체제에 따라 다릅니다.

```bash
git clone https://github.com/kimuc2013/262RCOSE45700.git
cd 262RCOSE45700/serving
docker build -t ku-academic-mcp:local .
docker compose run --rm mcp python -m academic_mcp.ingest --db /data/academic.sqlite3 --samples /samples
docker compose up -d
docker compose exec -T mcp python -m academic_mcp.terminal_demo --url http://127.0.0.1:8000/mcp --color never
```

현재 포트는 EC2의 `127.0.0.1:8000`에만 열립니다. 인증 및 HTTPS 연결 전에는 이 설정을 유지합니다. 원격 Claude/ChatGPT에서 접속하는 공개 배포는 추가 작업입니다. 데이터는 Docker 영구 볼륨에 저장하므로 `docker compose down -v`로 삭제하지 마세요. S3의 새 JSONL 데이터를 자동으로 적재하는 기능은 아직 연결하지 않았습니다.

## 협업

기능은 별도 브랜치에서 작업하고 테스트 후 main으로 합칩니다. 실제 `.env`, SSH 개인키, AWS 자격 증명, 로컬 DB는 커밋하지 않습니다. `.env.example`만 공유하며 모델 및 데이터 인터페이스 변경 시 담당자 간 계약과 문서를 함께 갱신합니다.

서빙 상세 실행 및 한계는 [serving/README.md](serving/README.md), 최신 검증 기록은 [VALIDATION.md](serving/docs/VALIDATION.md)를 참고하세요.

## 자동 배포

GitHub Actions가 PR과 main 변경에서 테스트 및 컨테이너 빌드를 실행합니다. AWS SSM과 OIDC 준비 후 `DEPLOY_ENABLED=true`를 설정하면 main 병합 시 EC2 배포와 MCP 검사까지 실행합니다. 배포 변수 미설정 시 deploy job은 건너뜁니다. 실제 활성화 조건과 관리자 요청 정책은 [배포 문서](serving/docs/DEPLOYMENT.md)를 참고하세요.
