# 검색 모듈 사용법 (서빙 연결용)

모델 파트가 만든 검색 모듈입니다. 질문을 받아 근거 청크와 출처, 근거 판정을 반환합니다. 서빙은 `Retriever.search` 하나만 호출하면 되고, 임베딩·벡터 검색·리랭킹·임계값 판정은 모듈 안에서 처리합니다.

- 코드: `serving/src/academic_mcp/retrieval.py`
- 테스트: `serving/tests/test_retrieval.py`
- 서빙은 `MODEL_MODE=retrieval`일 때 이 모듈을 호출합니다. 기본 local 모드는 BM25입니다. 운영 설정 전환과 실제 API 검증이 별도로 필요합니다.

## 호출

```python
from academic_mcp.retrieval import Retriever

retriever = Retriever.from_env(store)        # 서버 시작 시 한 번. 네트워크 호출 없음
result = retriever.search(query, top_k=5, chunks=chunks, revision=revision)
```

| 인자 | 설명 |
|---|---|
| `query` | 검색할 질문. 비어 있으면 `ValueError` |
| `top_k` | 반환할 청크 수(1 이상) |
| `chunks`, `revision` | 서빙이 이미 읽은 `store.table_snapshot('chunks')` 결과. 둘을 함께 넘기면 그 스냅샷으로 검색합니다. 생략하면 모듈이 현재 스냅샷을 읽습니다 |

`search`는 동기 함수입니다. 기존 검색처럼 작업 스레드에서 호출하면 됩니다. 요청 시간 예산(`checkpoint`, `remaining_timeout`)을 따르므로 예산이 끝나면 `DeadlineExceeded`가 납니다.

## 반환

```json
{
  "status": "found",
  "evidence": [
    {
      "id": "grade_retake#재수강 성적처리",
      "doc_title": "교육정보 > 성적 > 재수강",
      "section": "재수강 성적처리",
      "text": "...",
      "url": "https://registrar.korea.ac.kr/eduinfo/info/grade_retake.do",
      "collected_at": "2026-11-12",
      "score": 0.934395,
      "score_type": "reranker"
    }
  ],
  "score_type": "reranker",
  "min_rerank_score": 0.2
}
```

- `evidence`: 관련도 순. DB에 저장된 청크의 필드를 모두 그대로 담고 `score`, `score_type`만 추가합니다. 지금 `service.py`가 만드는 `selected`와 같은 형태이며 `content_kind`는 붙이지 않습니다.
- `score_type`: `reranker`(리랭커 점수, 0~1) 또는 `dense_cosine`(리랭커를 끈 경우). 점수는 순위용이며 정답 확률이 아닙니다.
- `min_rerank_score`: 판정에 쓴 임계값. 설정하지 않았으면 `null`.

### status

서빙의 상태값 이름을 그대로 씁니다.

| status | 의미 |
|---|---|
| `found` | 임계값이 설정돼 있고 1위 리랭커 점수가 임계값 이상 |
| `insufficient_evidence` | 임계값 미달, 또는 검색할 청크가 없음. 미달일 때도 `evidence`에 후보를 담아 반환하지만 확정 근거가 아닙니다 |
| `evidence_available` | 임계값이 설정되지 않아 판정하지 않음(기본값) |

`login_required`, `out_of_scope`, `needs_clarification`은 모듈이 다루지 않습니다. 서빙이 검색 전에 판단하는 현재 방식을 유지합니다.

### 오류

모델 호출 실패, 응답 형식 오류, 색인 없음은 예외로 올립니다. `insufficient_evidence`로 바꾸지 않습니다.

| 예외 | 경우 |
|---|---|
| `httpx.HTTPError` | 연결 실패, 타임아웃, 4xx/5xx |
| `ValueError` | 잘못된 입력, 모델 응답 형식 오류, 응답의 모델명이 설정과 다름, 벡터 수나 차원 불일치 |
| `RuntimeError` | 현재 데이터 revision에 맞는 색인이 없음 |

## 캐시 키

`retriever.profile()`은 결과를 바꾸는 설정(엔드포인트, 모델, 후보 수, 임계값, 문서 입력 형식)을 dict로 반환합니다. 서빙 캐시 키에 넣으면 설정이 바뀔 때 이전 결과가 재사용되지 않습니다.

## 설정 (환경변수)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `RETRIEVAL_EMBEDDING_URL` | 필수 | 임베딩 전체 URL (`http://<host>:<port>/v1/embeddings`) |
| `RETRIEVAL_RERANK_URL` | 리랭커 사용 시 필수 | 리랭커 전체 URL (`http://<host>:<port>/v1/rerank`). 임베딩과 포트가 다릅니다 |
| `RETRIEVAL_EMBEDDING_MODEL` | `furiosa-ai/bge-m3` | |
| `RETRIEVAL_RERANK_MODEL` | `furiosa-ai/bge-reranker-v2-m3` | |
| `RETRIEVAL_USE_RERANKER` | `true` | `false`면 벡터 순위만 사용 |
| `RETRIEVAL_CANDIDATES` | `20` | 리랭커에 넘길 벡터 검색 후보 수 |
| `RETRIEVAL_MIN_RERANK_SCORE` | 미설정 | 근거 판정 임계값. 평가셋 검토 후 모델 파트가 정합니다 |
| `RETRIEVAL_TIMEOUT_SECONDS` | `15` | 모델 호출 1회 제한 |
| `RETRIEVAL_API_KEY` | 미설정 | 현재 엔드포인트는 인증이 없습니다 |

엔드포인트 주소는 공개 저장소에 적지 않습니다. `compose.yaml`은 환경변수를 하나씩 나열하므로, 컨테이너에서 쓰려면 위 변수들을 추가해야 합니다.

## 색인

문서 벡터는 요청 중에 만들지 않고 미리 만듭니다. 기존 `vectors` 테이블에 저장합니다.

```bash
python -m academic_mcp.retrieval index                      # 현재 데이터 revision의 청크를 임베딩
python -m academic_mcp.retrieval search "휴학은 최대 몇 년?"  # 모듈만 따로 확인
```

- 새 스냅샷을 적재하면(`Store.replace`) 벡터가 삭제됩니다. **적재 후에는 색인을 다시 실행해야 하며**, 그 전까지 `search`는 `RuntimeError`를 냅니다.
- 청크 308개 기준 약 20초가 걸렸습니다.
- 기존 `python -m academic_mcp.index`가 만든 벡터와는 키가 달라 서로 재사용하지 않습니다.

## 확인한 것 (2026-11-16)

실제 Furiosa 엔드포인트와 실제 백필 스냅샷 `2026-11-12-2cc834ab`(청크 308개)로 EC2에서 순차 실행한 결과입니다. 평가 질문 64개는 검토 전 초안이므로 수치는 참고용입니다.

| 항목 | 값 |
|---|---|
| Recall@1 / @3 / @5 (답변 가능 52문항) | 0.808 / 0.942 / 0.981 |
| 같은 질문의 BM25 Recall@1 / @5 | 0.346 / 0.865 |
| 검색 1회 지연 | p50 약 1.07초, p95 약 1.10초(리랭킹이 약 850ms) |

동시 요청에서의 지연과 엔드포인트의 동시 요청 한도는 확인하지 않았습니다. 임계값은 아직 정하지 않았습니다.

## 서빙 연결과 운영 (2026-11-18)

서빙은 서버 시작 시 Retriever.from_env(store)를 한 번 생성하며, 기존 시간 예산과 동시 요청 제어가 적용되는 작업 스레드에서 search를 호출합니다. 서빙이 이미 읽은 chunks와 revision을 그대로 넘기고, 반환 status/score/임계값을 재판정하지 않습니다. content_kind, 최신성 경고와 요청 지표만 서빙에서 추가합니다. retriever.profile()은 서빙 캐시 키에 포함됩니다. 개인정보 조회와 범위 밖 질문은 검색 전에 차단하고, 모호한 후속 질문은 클라이언트가 독립 질문으로 다시 호출합니다. 이 모듈에 재작성 API가 추가된 것은 아닙니다.

1. 실제 endpoint와 RETRIEVAL_* 설정을 Git 밖 runtime.env에 넣고 MODEL_MODE=retrieval로 설정합니다. Compose는 위의 모든 RETRIEVAL_* 값을 전달합니다. 종전 MODEL_BASE_URL/REWRITE_MODEL 등은 이 모드에 필요하지 않습니다.
2. 배포는 새 컨테이너 활성화 전에 `python -m academic_mcp.prepare_retrieval`로 현재 revision/index_key의 색인 존재를 확인하고, 없으면 만듭니다. API 준비 실패 시 기존 컨테이너를 유지합니다. 활성화 후 실제 검색까지 smoke 검사하며, 실패하면 이전 이미지 복구를 시도합니다.
3. 운영 중 새 스냅샷을 반영할 때는 서버 검색을 일시 중지하는 점검 시간을 두고 다음을 실행합니다. 적재 트랜잭션과 모델 API 색인은 하나의 원자적 작업이 아닙니다. 색인 실패 시 새 자료는 적재되지만 검색은 기술 오류를 내므로 재색인을 완료한 후 다시 개방합니다. BM25로 자동 전환하지 않습니다.

```bash
# 실제 환경변수가 전달된 컨테이너에서 실행
python -m academic_mcp.ingest --db /data/academic.sqlite3 --snapshot /path/to/snapshot --reindex
# 기존 자료에 색인이 없을 때 준비 또는 확인
python -m academic_mcp.prepare_retrieval
# 기존 색인의 강제 재생성은 모듈 CLI 사용 후 서버 재시작
python -m academic_mcp.retrieval index
python -m academic_mcp.smoke --search
```

최종 임계값은 모델 담당자의 검토 결과를 사용합니다. 서빙 연동을 위해 임시 점수를 운영 기본값으로 넣지 않습니다. 테스트의 모의 API 결과는 실제 API의 품질/지연 측정으로 해석하지 않습니다.
