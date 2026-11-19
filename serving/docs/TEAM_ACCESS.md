# 팀 테스트용 HTTPS MCP

기존 EC2 MCP에 별도 Caddy gateway를 붙입니다. 원본 MCP는 127.0.0.1:8000을 유지하며 공개 포트는 gateway의 TCP 80과 443입니다. /mcp와 올바른 Bearer 키가 있는 요청만 전달합니다. 인증서는 Caddy가 발급하고 Docker 볼륨에 보존하며 자동 갱신합니다. 이 구성은 단일 EC2 팀 테스트용이며 고가용성이나 사용자별 권한, OAuth 로그인을 제공하지 않습니다.

## 설치

1. 도메인을 EC2 IP로 연결하고 인바운드 TCP 80/443을 허용합니다. 도메인이 없으면 `ku-team7.<EC2-IP를-하이픈으로>.sslip.io`처럼 IP DNS를 사용할 수 있습니다. EC2 중지 후 재시작으로 IP가 바뀌면 주소와 인증서 설정을 갱신해야 합니다. 인스턴스 reboot는 stop/start와 다릅니다.
2. root 전용 `/opt/ku-academic-mcp/config/public.env`에 public/.env.example의 세 변수를 설정합니다. owner 키는 기존 키를 유지할 수 있고 팀 공유 키는 별도로 생성합니다. 예시의 문자열은 실제 키로 사용하지 않습니다. 실제 키를 Git이나 로그에 넣지 않습니다.
3. `sudo bash serving/deploy/install-public.sh`를 실행합니다. 이후 main 자동 배포도 private public.env가 있으면 MCP 검사 후 gateway 설정을 갱신합니다. 파일이 없으면 공개 gateway를 만들지 않습니다. 고정된 gateway 이미지, 리소스 제한, 로그 회전과 재시작 정책을 적용합니다. 기존 MCP 데이터와 자동 main 배포는 유지합니다.
4. 외부에서 HTTPS 인증서 검증(검증 생략 옵션 금지), 무인증/잘못된 키의 401, 두 유효 키의 실제 MCP 초기화 및 도구 호출을 확인합니다.
5. 성공한 뒤에만 기존 임시 터널과 그 종료 타이머를 정리하고 클라이언트 URL을 새 주소로 갱신합니다.

## 클라이언트

Streamable HTTP URL은 `https://<MCP_HOST>/mcp`, 헤더는 `Authorization: Bearer <팀 공유 키>`입니다. 사용자별 설정 화면이 다르므로 URL과 HTTP 헤더를 지원하는 클라이언트를 사용합니다. 모든 클라이언트에서 동작한다고 검증한 것은 아닙니다.

Codex config.toml 예시(개인 설정에 입력, 실제 키는 공유 저장소에 커밋하지 않음):

```toml
[mcp_servers.ku_academic]
url = "https://mcp.example.com/mcp"
http_headers = { Authorization = "Bearer REPLACE_WITH_TEAM_TOKEN" }
```

Claude Desktop 등 stdio만 받는 설정에 URL을 그대로 입력하는 방식과는 다릅니다. OAuth만 허용하고 임의 헤더를 지원하지 않는 원격 연결 UI에는 별도 인증 연동이 필요할 수 있습니다.

## 운영

- gateway 설정은 `/opt/ku-academic-mcp/public/`, 비밀값은 `config/public.env`, 인증서는 `ku-mcp-public_certificates` 볼륨에 있습니다.
- `sudo docker compose --project-name ku-mcp-public --env-file /opt/ku-academic-mcp/config/public.env -f /opt/ku-academic-mcp/public/compose.yml ps`로 상태를 확인합니다. `config`의 전체 출력은 키를 포함하므로 사용하지 않습니다.
- 팀 키 변경 후 같은 compose 명령의 `up -d --force-recreate`로 반영합니다. owner 키와 분리되어 기존 소유자 키는 유지할 수 있습니다. 토큰별 사용량 제한은 아직 없고 기존 서버 전체 동시성/시간 예산 제한만 적용됩니다.
- 중지는 같은 compose 명령의 `stop`, 다시 시작은 `up -d`입니다. 인증서 볼륨을 지우지 않습니다. 원본 MCP 및 자동 배포 타이머와 별개입니다.
- 인증 없는 외부 요청 본문은 MCP에 전달되지 않습니다. access log는 켜지 않았으며 로그에 인증키나 질문 원문을 추가하지 않습니다.

공식 문서: [Caddy HTTPS](https://caddyserver.com/docs/automatic-https), [sslip.io/nip.io](https://nip.io/).
