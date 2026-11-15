# GitHub Actions와 EC2 배포

## 현재 운영 방식과 상태

현재 자동 배포는 EC2의 `ku-mcp-autodeploy.timer`가 수행합니다. GitHub Actions는 PR/main의 Python 테스트와 Docker 빌드를 담당합니다. EC2는 약 2분 간격으로 공개 main과 그 SHA의 push CI를 확인하고, 가장 최근 검사 결과가 success인 새 커밋만 기존 deploy.sh로 배포합니다. PR, 다른 브랜치, 실패하거나 진행 중인 CI는 배포하지 않습니다. 변경이 없으면 CI API 조회나 컨테이너 재시작을 하지 않습니다.

추가 IAM 역할 생성 권한이 없어 GitHub OIDC/SSM 방식 대신 이 방식을 사용합니다. AWS 장기 키, SSH 키, GitHub 쓰기 토큰을 배포 타이머에 저장하지 않습니다. EC2의 기존 SSH 접근으로 최초 설치를 수행하고, 이후에는 EC2가 GitHub로 나가는 연결만 사용합니다. 공개 저장소를 전제로 하므로 비공개 전환 시 자격 증명 및 전달 방식의 재설계가 필요합니다.

GitHub 변수 `AUTO_DEPLOY_MODE=ec2-pull`, `DEPLOY_ENABLED=false`를 사용합니다. 기존 SSM deploy job은 건너뛰는 것이 정상입니다. CI 녹색만으로 실제 EC2 배포 성공을 판단하지 마세요. 실제 성공은 EC2 journal의 `Auto deployment successful: <SHA>`와 `/opt/ku-academic-mcp/current` 및 MCP 호출로 확인합니다. 상태 반영 지연은 확인 간격 약 2분에 빌드와 MCP 검사 시간을 더한 값이며 서비스 중단 없는 배포는 아닙니다.

SSM 방식의 코드는 향후 대안으로 보존했습니다. 아래 OIDC 설정은 현재 타이머 방식에서는 필요하지 않습니다.

## EC2 사전 준비

1. EC2 Linux에 Docker 엔진과 Docker Compose 플러그인을 설치하고 Docker 서비스를 실행합니다. `docker version`, `docker compose version`, `curl --version`, `flock --version`이 동작해야 합니다. 설치 명령은 실제 OS를 확인한 뒤 선택합니다.
2. EC2 인스턴스 프로파일에 SSM Agent용 권한(`AmazonSSMManagedInstanceCore` 또는 동등 정책)을 부여합니다. 기존 S3 역할을 교체해 S3 권한을 잃지 않도록 기존 프로파일에 함께 구성합니다.
3. EC2에서 SSM Agent가 실행되고 Systems Manager의 managed node로 Online 상태여야 합니다. GitHub, 컨테이너 레지스트리, Python 패키지 저장소와 SSM 서비스로 나가는 네트워크 접근이 필요합니다.
4. 필요 시 `/opt/ku-academic-mcp/config/runtime.env`를 root 전용 권한 600으로 생성해 모델 설정을 넣습니다. 이 파일은 Git에 올리지 않습니다. 없으면 첫 배포가 `MODEL_MODE=local`만 생성합니다.

자동 배포는 EC2에서 root로 실행됩니다. 이 역할을 가진 워크플로는 해당 인스턴스에서 명령 실행 권한이 있으므로 IAM 대상을 한 인스턴스로 제한합니다. GitHub의 main 보호 및 필수 CI 검사는 저장소 소유자가 별도로 설정하는 것을 권장합니다.

## SSM 방식으로 전환할 때 관리자에게 요청할 AWS 설정

- GitHub OIDC 공급자 `https://token.actions.githubusercontent.com`, audience `sts.amazonaws.com`.
- 해당 팀 저장소의 main만 신뢰하는 역할. `serving/deploy/oidc-trust.example.json`에서 ACCOUNT_ID를 치환합니다. GitHub 조직에서 OIDC subject 형식을 사용자 지정했다면 실제 subject에 맞춰야 합니다.
- 역할 권한은 `serving/deploy/ssm-permissions.example.json`에서 REGION, ACCOUNT_ID, INSTANCE_ID를 치환합니다. 추가 EC2 생성, IAM 변경, S3 쓰기 권한은 이 파이프라인에 필요하지 않습니다.
- 앞의 EC2용 SSM 프로파일 권한. GitHub용 OIDC 역할과 EC2의 인스턴스 역할은 별개입니다.

관리자 요청 예시:

> GitHub kimuc2013/262RCOSE45700의 main에서만 AssumeRoleWithWebIdentity가 가능한 OIDC 역할을 부탁드립니다. 지정 EC2에 AWS-RunShellScript SendCommand와 GetCommandInvocation을 허용하고, EC2 인스턴스 프로파일에는 SSM Agent 통신 권한을 추가해 주세요. 정책 예시는 저장소 serving/deploy에 있습니다.

## SSM 방식으로 전환할 때 GitHub 변수와 활성화

Settings → Secrets and variables → Actions → Variables에 다음 Repository variables를 설정합니다. 비밀 접근 키는 필요하지 않습니다.

| 변수 | 값 |
|---|---|
| `AWS_REGION` | EC2가 있는 실제 리전 |
| `EC2_INSTANCE_ID` | i-로 시작하는 인스턴스 ID |
| `AWS_DEPLOY_ROLE_ARN` | 관리자가 제공한 OIDC 역할 ARN |
| `DEPLOY_ENABLED` | 최초에는 비워두거나 false, 준비 완료 후 true |

먼저 EC2의 `ku-mcp-autodeploy.timer`를 disable하여 두 배포 경로가 동시에 동작하지 않게 하고 `AUTO_DEPLOY_MODE`를 ssm으로 변경합니다.

1. CI가 통과하는지 확인합니다.
2. SSM의 해당 EC2 Online과 OIDC 역할 구성을 확인합니다.
3. DEPLOY_ENABLED=true로 바꾼 뒤 Actions에서 main을 선택해 수동 실행합니다. 변수 변경만으로 배포가 시작되지는 않습니다.
4. deploy job에서 커밋 SHA와 MCP 검사 성공을 확인합니다.
5. 이후 main에 병합하면 테스트 후 같은 절차가 자동 실행됩니다. PR 코드는 AWS 자격 증명을 받지 않습니다.

관련 공식 문서: [GitHub OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws), [SSM Run Command](https://docs.aws.amazon.com/systems-manager/latest/userguide/walkthrough-cli.html).

## 배포, 데이터와 복구

- `/opt/ku-academic-mcp/releases/<SHA>`: 커밋별 소스. 이미지는 `ku-academic-mcp:<SHA>`.
- `/opt/ku-academic-mcp/current`: 마지막 MCP 검사가 성공한 릴리스.
- `config/runtime.env`: 지속 설정. Git 업데이트로 덮어쓰지 않습니다.
- Docker의 기존 `ku-academic-mcp_academic-data` 볼륨을 유지합니다. DB가 empty일 때만 샘플을 적재합니다. 이미 들어 있는 실제 데이터는 샘플로 덮어쓰지 않습니다.
- 새 이미지 빌드 실패는 실행 중 컨테이너를 재시작하지 않습니다. 재시작 후 MCP 검사가 실패하면 이전 컨테이너의 이미지 ID로 복구를 시도하고, 워크플로는 실패로 남깁니다. 이전 이미지에 새 smoke 모듈이 없으면 복구 검사도 실패로 표시되므로 EC2 확인이 필요합니다.
- 첫 배포에는 복구할 이전 이미지가 없습니다. DB 스키마 변경과 데이터 마이그레이션은 자동 롤백하지 않습니다.
- 워크플로 concurrency와 EC2의 flock으로 겹친 배포를 직렬화합니다. 실행 중 배포는 취소하지 않습니다. GitHub의 대기 실행은 더 최신 실행으로 교체될 수 있습니다.
- 포트는 여전히 EC2 loopback 8000만 사용합니다. HTTPS와 인증을 붙인 외부 MCP 접속은 별도 작업입니다.
- 릴리스와 이미지를 자동 삭제하지 않으므로 디스크 사용량을 확인하고 현재 및 복구 대상이 아닌 오래된 항목만 정리합니다.
- SSM 명령 최대 실행은 900초입니다. 워크플로 타임아웃 뒤에도 원격 명령이 살아 있을 수 있으므로 새 배포 전 SSM 실행 상태를 확인합니다.

## 현재 배포의 운영 명령

EC2 SSH 터미널에서 실행합니다. 실제 코드 릴리스는 `/opt/ku-academic-mcp/current`로 확인합니다. `/home/ec2-user/262RCOSE45700`는 배포한 커밋으로 분리 체크아웃한 소스이며 이 폴더를 수정하는 것만으로 실행 컨테이너가 갱신되지는 않습니다.

```bash
sudo docker ps
sudo docker logs --tail 50 ku-academic-mcp-mcp-1
sudo docker exec ku-academic-mcp-mcp-1 python -m academic_mcp.smoke
sudo docker exec -it ku-academic-mcp-mcp-1 python -m academic_mcp.terminal_demo --color always
```

새 커밋을 수동 배포할 때는 저장소를 fetch하고 확인한 40자리 커밋 SHA를 `sudo bash /home/ec2-user/262RCOSE45700/serving/deploy/deploy.sh <SHA>`에 전달합니다. 배포 스크립트 자체가 변경된 릴리스라면 로컬 소스도 해당 커밋으로 먼저 갱신합니다. 지속 설정 파일과 영구 볼륨은 삭제하지 않습니다.

## EC2 자동 배포 타이머 설치와 운영

확인한 main 코드의 `serving/deploy/install-autodeploy.sh`를 EC2에서 sudo로 실행합니다. Docker와 Compose, Python 3, git, curl, flock이 필요합니다. 설치기는 `/opt/ku-academic-mcp/autodeploy.py`와 systemd service/timer를 만들고 부팅 후 및 각 검사 종료 약 2분 뒤에 실행되도록 설정합니다. 설치 시 최초 검사도 수행합니다. 타이머 코드 자체는 배포 중 덮어쓰지 않으므로 타이머 변경 시 설치기를 다시 실행해야 합니다.

```bash
sudo bash serving/deploy/install-autodeploy.sh
sudo systemctl list-timers ku-mcp-autodeploy.timer
sudo journalctl -u ku-mcp-autodeploy.service -n 50 --no-pager
readlink -f /opt/ku-academic-mcp/current
```

GitHub 공개 API는 인증 없이 사용하며 새 main의 CI를 기다리는 동안만 조회합니다. API 제한, 네트워크 오류나 비정상 응답이면 기존 서비스를 유지하고 다음 검사로 넘깁니다. 약 2분 간격이면 계속 기다리는 경우에도 조회는 시간당 약 30회입니다. GitHub rate limit을 통제하거나 회피하지 않습니다.

배포 시도 전에 `autodeploy.failed-sha`를 기록합니다. 배포 실패 시 deploy.sh가 이전 이미지 복구를 시도하고, 같은 SHA를 주기적으로 재배포하지 않습니다. 새 main SHA는 다음 검사에서 처리할 수 있습니다. 같은 SHA를 재시도하려면 원인을 해결한 뒤 실행합니다.

```bash
sudo rm -f /opt/ku-academic-mcp/autodeploy.failed-sha
sudo systemctl start ku-mcp-autodeploy.service
```

일시 중지는 `sudo systemctl disable --now ku-mcp-autodeploy.timer`입니다. 이미 실행 중인 배포를 강제로 끊지는 않습니다. GitHub의 배포 상태/실패 알림 연동은 현재 없으며 EC2 journal에서 확인합니다. 이 타이머는 지정 서버의 root로 CI가 통과한 main 코드를 실행하므로 저장소의 main 쓰기 권한과 CI 변경을 신뢰하는 구조입니다.
