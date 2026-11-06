# 모델 담당자와 맞출 API 계약 초안

실제 FuriosaAI 런타임에서 확인한 규격이 아닙니다. 서빙 파트는 아래 규격으로 연결 어댑터와 계약 테스트를 준비했습니다. 경로나 응답 형식이 다르면 어댑터를 수정합니다.

| 기능 | 요청 | 응답 |
|---|---|---|
| 재작성 | `POST /v1/chat/completions`, model, messages, temperature=0, response_format=json_object | choices[0].message.content의 JSON 문자열 `{query, semester, target}` |
| 임베딩 | `POST /v1/embeddings`, model, input: 문자열 배열 | data: `[{index, embedding: 숫자 배열}]` |
| 리랭킹 | `POST /v1/rerank`, model, query, documents: 문자열 배열 | results: `[{index, relevance_score}]` |

리랭킹 경로는 `RERANK_PATH`로 변경 가능합니다. 후보마다 점수 하나, 원래 후보의 0부터 시작하는 index가 필요합니다. 현재는 모든 후보 점수를 반환하는 계약입니다. top-n 일부만 반환하는 서버라면 맞춰야 합니다. 임베딩도 입력 개수만큼 벡터가 있어야 합니다.

확인해야 할 사항:

- NPU 주소, AWS에서 접근할 네트워크 경로, 인증 방식
- 세 모델의 지원 여부, 고정 모델 ID와 버전, 임베딩 차원
- 리랭킹 경로와 응답 JSON, 점수의 방향과 범위
- JSON 출력 지원 여부, 재작성 결과에서 맥락 부족을 어떻게 표시할지
- 최대 입력 길이, 배치 크기, 동시 요청 한도와 타임아웃
- 임베딩의 query/document prefix 필요 여부. 현재 어댑터는 prefix를 임의로 추가하지 않음
- 같은 평가셋으로 정할 근거 판정 임계값, 재작성 품질, 후보 수

질의, 대화와 수집 문서는 데이터로 전달합니다. 문서의 지시를 따르지 않는다고 표시하지만 실제 인젝션 방어 평가는 별도로 해야 합니다. 모델 응답이 잘못됐거나 서버 연결에 실패하면 요청이 오류로 끝나며, 임의 모델이나 가짜 벡터로 대체하지 않습니다.
