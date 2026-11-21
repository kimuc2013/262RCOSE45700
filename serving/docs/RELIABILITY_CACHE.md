# 모델 연결 복구와 검색 순위 캐시

2026-11-21 서빙 변경. 모델 담당자의 `retrieval.py`, 순위 계산, 임계값과 백필은 변경하지 않는다.

## 연결 복구

운영 계측에서 embedding `ReadError`, embedding/rerank `RemoteProtocolError`가 확인됐다. 같은 EC2의 현재 연속 호출은 정상 응답했다. 과거 개별 연결이 끊긴 원인은 공급자 로그 없이는 확정할 수 없다.

서빙의 `ReliableModelClient`는 임베딩과 리랭커의 ReadError, RemoteProtocolError, ConnectError, ConnectTimeout만 최대 한 번 재시도한다. 최초 호출의 시간 예산을 유지하고 요청 취소와 deadline을 재확인한다. HTTP 오류, ReadTimeout, 응답 검증 실패는 재시도하지 않는다. 답변 생성이나 쓰기 작업에는 적용하지 않는다. 원래 연결 풀을 유지하며 물리 HTTP 시도는 실패와 재시도 모두 계측한다. 지속 장애를 성공이나 근거 부족으로 바꾸지 않는다.

## 캐시 계층

- 응답 캐시: 기존 query/history/top_k/semester/target/profile/reference day와 revision 분리를 유지한다. TTL은 기존300초다.
- 검색 순위 캐시: 모델 모듈이 검증한 결과만 별도 저장한다. 원문 query의 정확한 문자열, 실제 후보 범위, profile, reference day, revision으로 분리한다. TTL은 `SERVING_RANKED_CACHE_TTL`(기본1800초)이며 응답 캐시와 합쳐 최대1000행을 유지한다.
- top_k 변경: 리랭커의 후보 수가 동일한 경우에만 최대10개 순위를 저장하고 요청 개수만 반환한다. 설정된 candidates보다 top_k가 크면 후보 범위가 달라지므로 캐시 키도 달라진다. 리랭킹을 끄면 cosine 순위의 prefix만 재사용한다.
- 학기와 대상: 현재 Retriever는 query/chunks/revision/top_k만 입력받고 semester/target을 검색에 사용하지 않는다. 따라서 이 값들은 검색 순위 캐시에만 제외한다. 최종 응답, 최신성 경고와 출처는 매 요청의 메타데이터로 다시 구성한다. 학기/대상이 질문 문장에 있으면 query가 달라져 별도 캐시다. 일정 라우팅과 개인정보 판정은 재사용 전에 그대로 적용한다.
- 동시 요청: 같은 질문의 top_k/메타데이터 변형을 같은 후보 범위 내에서 병합한다. 대기와 작업은 기존 admission/deadline 제한 안에서 실행한다. revision 변경, 실패와 취소 후에는 결과를 발행하지 않는다.
- 사용자마다 의미가 다른 질문을 묶거나 유사도만으로 다른 질문의 결과를 반환하지 않는다. 모델이 질문을 의역하면 여전히 캐시가 분리된다. MCP 도구 설명에서 독립 질문의 원문 전달과 기본top_k 사용을 안내한다.

## 관측

`cache_hit`와 `cache_hit_rate`는 응답 전체 캐시다. `retrieval_cache_hit`는 응답을 새로 만들 때 검색 순위를 재사용했는지다. `effective_cache_hit_rate`는 둘 중 하나 이상 적중한 호출의 비율로 중복 합산하지 않는다. `retrieval_coalesced`는 다른 메타데이터 요청이 진행 중 검색 결과를 기다렸다가 재사용한 경우다. 과거 로그에 새 필드가 없으면 새 계층의 적중을 소급 추정하지 않는다.

대시보드는 응답 캐시와 검색 순위 재사용 포함 비율을 나눠 표시한다. `api_stages.*.error_types`는 본문/질의/인증키 없이 오류 종류만 보여준다. 요청의 `model_calls`에는 실제 재시도도 포함한다. 최종 물리 작업/usage는 API 계측을 기준으로 한다.

통제된 반복 호출 적중률은 자연 사용자 트래픽 적중률이 아니다. 검증 시 같은 요청 순서, 독립된 빈 서빙 캐시, 원문 근거 순위 일치, 실패 수, 모델 호출 수와 지연을 함께 비교한다. 제공자 캐시는 초기화하지 못하므로 모델 자체의 속도 개선이라고 주장하지 않는다.
