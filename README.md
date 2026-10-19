# hatchy-oci-a1-deployer

Oracle Cloud 도쿄 Always Free ARM `HATCHY` 인스턴스를 GitHub Actions에서 확보합니다.

- Shape `VM.Standard.A1.Flex`, 2 OCPU, 12GB, 부팅 100GB
- 재시도 간격 90초 (GitHub cron 최소 5분 안에서 반복)
- 용량 부족은 정상 대기, 성공 또는 하드 오류 시 워크플로 비활성화

운영: [docs/OPERATIONS.md](docs/OPERATIONS.md)

이 PC에서 저장소를 만들 때는 `/home/ha/hatchy-oci-a1-deployer/.env`의 `GH_TOKEN`만 채우면 됩니다.
