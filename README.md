# KIS 섹터 ETF 봇

한국투자증권(KIS) API 기반 국내 섹터 ETF 자동매매 봇과 텔레그램 매니저입니다.

## 공개 저장소 보안 원칙

이 저장소에는 코드만 포함합니다. 아래 파일은 서버에만 두고 Git에 올리지 않습니다.

- `sector_cfg.yaml`, `manager_cfg.yaml`: KIS 키, 텔레그램 토큰, 계좌 설정
- `sector_state.json`, `shared/`: 매매 상태와 IPC 파일
- `logs/`, `data/`, `results/`: 운영 로그와 백테스트 데이터/결과

커밋 전 `git status`에서 위 파일이 보이면 즉시 중단하고 `.gitignore`를 확인하세요.

## Ubuntu 최초 설치

```bash
git clone https://github.com/goja558-cmyk/upbit_bot.git /home/trade/upbit_bot
cd /home/trade/upbit_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

기존 서버의 `sector_cfg.yaml`, `manager_cfg.yaml`은 유지하거나 새로 작성합니다.

## 업데이트

장 마감 후 매매를 정지한 다음 실행합니다.

```bash
cd /home/trade/upbit_bot
git pull --ff-only
# systemd를 쓴다면 서비스명에 맞춰 재시작
sudo systemctl restart trade-manager
```

직접 실행 중이면 기존 `manager.py` 프로세스를 종료한 뒤 다시 시작합니다.

```bash
cd /home/trade/upbit_bot
source .venv/bin/activate
python3 manager.py
```

## 백테스트

백테스트는 주문을 전송하지 않습니다. KIS 시세 API만 호출합니다.

```bash
python3 backtest.py fetch --start 2023-01-01
python3 backtest.py run --start 2023-01-01
```

`grid` 결과는 워크포워드 검증 전 실전 파라미터로 사용하지 마세요.
