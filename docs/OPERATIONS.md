# HATCHY OCI A1 — GitHub Actions 운영

로컬 systemd 타이머를 GitHub-hosted runner로 옮긴 배포 워크플로입니다. 인스턴스 사양(도쿄, A1 Flex 2 OCPU / 12GB, 부팅 100GB, 기존 VCN/Subnet)은 유지합니다.

## 간격

GitHub cron의 최소 주기는 5분입니다. 90초 cron은 만들 수 없습니다.

각 워크플로 실행은 최대 240초 동안 **90초 간격**으로 생성을 다시 시도하고 끝납니다. 스케줄이 다음 실행을 다시 엽니다. 플랫폼 지연으로 간격이 더 벌어질 수 있습니다.

30초 간격은 `Too many requests for the user`가 반복되어 사용하지 않습니다.

## 상태

| 종료 | 의미 |
|---|---|
| 0 | dry-run 통과, 생성 성공, 또는 이번 실행의 용량 대기가 정상 종료 |
| 10 | 용량 부족 또는 429. 스크립트가 90초 후 재시도 |
| 2 | 인증, 설정, 무료 한도, 네트워크 차단 |
| 4 | 결과 불명. 추가 생성 금지 |

용량 부족은 워크플로 실패로 보지 않습니다. 인증 오류는 용량 부족으로 처리하지 않습니다.

## 중복 방지

매 실행은 OCI 인스턴스 목록과 관리 태그를 다시 조회합니다. 러너 디스크의 상태 파일은 다음 실행으로 넘어가지 않습니다. 조회가 실패하면 생성하지 않습니다.

## 성공 시 중단

인스턴스가 생성되거나 하드 오류로 멈추면 `gh workflow disable oci-a1-watch.yml`을 호출합니다. 비활성화에 실패해도 다음 실행은 기존 인스턴스를 먼저 확인합니다.

`HATCHY_LIVE` 리포지토리 변수가 `true`일 때만 스케줄 실행이 실제 생성(`--once`)을 합니다. 그 전에는 스케줄도 dry-run입니다.

## 비밀

API 개인키는 GitHub Secret `OCI_PRIVATE_KEY_PEM`입니다. 저장소에는 넣지 않습니다. `.env`의 `GH_TOKEN`은 이 PC에서 `gh` 로그인에만 쓰고 커밋하지 않습니다.

## 60일 비활성화

Public 저장소는 60일 동안 커밋 등 활동이 없으면 스케줄 워크플로가 꺼질 수 있습니다. 의미 없는 자동 커밋은 만들지 않습니다. 꺼지면 Actions 탭에서 워크플로를 Enable 하면 됩니다.

## 로컬 타이머

GitHub dry-run이 성공하기 전에는 Ubuntu의 `hatchy-oci-watch.timer`를 끄지 않습니다. 두 곳이 동시에 `--once`를 돌리지 않게, 실운영 전환 직전에 로컬 타이머를 중지합니다.

## 롤백

```bash
systemctl --user enable --now hatchy-oci-watch.timer
gh variable set HATCHY_LIVE --body false
gh workflow disable oci-a1-watch.yml
```
