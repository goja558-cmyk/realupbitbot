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
git clone https://github.com/goja558-cmyk/upbit_bot.git /home/trade/upbit6974
cd /home/trade/upbit6974
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

기존 서버의 `sector_cfg.yaml`, `manager_cfg.yaml`은 유지하거나 새로 작성합니다.

## 업데이트

장 마감 후 매매를 정지한 다음 실행합니다.

```bash
cd /home/trade/upbit6974
git pull --ff-only
# systemd를 쓴다면 서비스명에 맞춰 재시작
sudo systemctl restart trade-manager
```

직접 실행 중이면 기존 `manager.py` 프로세스를 종료한 뒤 다시 시작합니다.

```bash
cd /home/trade/upbit6974
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
## Nasdaq US-stock research backtest

`us_stock_backtest.py` is a separate, read-only research tool. It does not
share live order code with `sector_bot.py` and never calls an order endpoint.
It fetches daily overseas-stock prices from KIS, caches them under
`data/us_daily/`, and compares a low-turnover monthly Nasdaq large-cap rule
with QQQ buy-and-hold.

```bash
source .venv/bin/activate
python3 us_stock_backtest.py fetch --start 2015-01-01
python3 us_stock_backtest.py run --start 2018-01-01 --end 2026-07-15 --fee-bps 20
```

The fixed large-cap universe has survivorship bias. Treat results only as a
first API/strategy sanity check, not as evidence for live trading.

## Four-stock observer (no orders)

`watchlist_observer.py` observes Dongjin Semichem, Silicon2, Classys, and JYP
Ent. It has no order endpoint or automatic trading function. It records
one-minute snapshots from 10:00 to 14:20 KST and five-minute snapshots near
the open and close. Raw files are kept in `data/watchlist_observer/`.

```bash
python3 watchlist_observer.py --once
python3 watchlist_observer.py --daemon
python3 watchlist_observer.py --daily-summary
```

After 15:35 KST the daemon refreshes daily bars, writes
`data/watchlist_observer/daily_summary.csv`, and sends one Telegram close

## Upbit crypto observer (separate Telegram bot)

`upbit_observer.py` polls Upbit's public ticker API only; it never places orders.
It uses the separate `upbit_watchlist_cfg.yaml`, `data/upbit_observer/`, and
`shared/upbit_observer_state.json`. Put a new BotFather token and the new chat ID
in that config; do not reuse the stock bot credentials.

```bash
python3 upbit_observer.py --once
python3 upbit_observer.py --daemon
```

Ubuntu에서 주식 봇과 업비트 봇을 함께 운영하려면 각각 별도 프로세스로 실행합니다.
업데이트는 저장소에서 pull한 뒤 업비트 프로세스만 재시작하면 됩니다.

```bash
<<<<<<< HEAD
cd /home/trade/upbit6974
=======
cd /home/trade/upbit_bot
>>>>>>> 1c6fce2 (Make repository Upbit-only)
git pull --ff-only
source .venv/bin/activate
pkill -f 'upbit_observer.py --daemon' || true
nohup python3 upbit_observer.py --daemon >> logs/upbit_observer.log 2>&1 &
```

Ubuntu에서 코드 변경 후 GitHub로 올릴 때는 다음처럼 실행합니다.

```bash
cd /home/trade/upbit_bot
git add upbit_observer.py README.md requirements.txt
git commit -m "Add Upbit market observer"
git push origin main
```

`upbit_watchlist_cfg.yaml`은 개인 토큰과 새 채팅방 ID가 들어가므로 Git에 올리지 않습니다.
Ubuntu에서 최초 실행 전에 해당 파일을 직접 만들고 다음 값을 입력하세요.

```yaml
telegram_token: "업비트 전용 봇 토큰"
chat_id: "업비트 전용 채팅방 ID"
```
summary. A stale-price check marks a day as unusable when intraday price and
accumulated volume did not change.
