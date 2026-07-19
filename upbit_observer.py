"""업비트 주요 종목 시세 관찰기 (주문 기능 없음).

예:
  py upbit_observer.py --once
  py upbit_observer.py --daemon
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

BASE = Path(__file__).resolve().parent
CFG_FILE = BASE / "upbit_watchlist_cfg.yaml"
DATA_DIR = BASE / "data" / "upbit_observer"
STATE_FILE = BASE / "shared" / "upbit_observer_state.json"
API = "https://api.upbit.com/v1"
FIELDS = ["timestamp", "market", "name", "price", "change_pct", "change_24h_pct",
          "high_24h", "low_24h", "volume_24h", "value_24h_krw", "bid", "ask", "spread_bps"]
LOG_DIR = BASE / "logs"
LOG_FILE = LOG_DIR / "upbit_observer.log"

DEFAULT = {
    "markets": {"KRW-BTC": "비트코인", "KRW-ETH": "이더리움", "KRW-XRP": "리플",
                "KRW-SOL": "솔라나", "KRW-DOGE": "도지코인"},
    "interval_seconds": 60,
    "telegram_enabled": True,
    "telegram_summary_interval_seconds": 900,
    # 주식 봇과 완전히 별도의 토큰/채팅방을 입력한다.
    "telegram_token": "",
    "chat_id": "",
}


def load_config() -> dict:
    if not CFG_FILE.exists():
        CFG_FILE.write_text(yaml.safe_dump(DEFAULT, allow_unicode=True, sort_keys=False), encoding="utf-8")
    loaded = yaml.safe_load(CFG_FILE.read_text(encoding="utf-8")) or {}
    cfg = {**DEFAULT, **loaded}
    cfg["markets"] = {**DEFAULT["markets"], **(loaded.get("markets") or {})}
    return cfg


def ticker(markets: list[str]) -> list[dict]:
    r = requests.get(f"{API}/ticker", params={"markets": ",".join(markets)}, timeout=15)
    r.raise_for_status()
    return r.json()


def snapshot(cfg: dict) -> list[dict]:
    markets = list(cfg["markets"])
    now = datetime.now(timezone.utc).astimezone()
    rows = []
    for item in ticker(markets):
        bid = float(item.get("trade_price", 0))  # 공개 ticker에는 호가가 없어 체결가로 기록
        rows.append({"timestamp": now.isoformat(timespec="seconds"), "market": item["market"],
                     "name": cfg["markets"].get(item["market"], item["market"]),
                     "price": item["trade_price"], "change_pct": round((item["trade_price"] / item["prev_closing_price"] - 1) * 100, 3),
                     "change_24h_pct": round(item["signed_change_rate"] * 100, 3),
                     "high_24h": item["high_price"], "low_24h": item["low_price"],
                     "volume_24h": item["acc_trade_volume_24h"], "value_24h_krw": item["acc_trade_price_24h"],
                     "bid": bid, "ask": bid, "spread_bps": 0.0})
    return rows


def save(rows: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"snapshots_{datetime.now().strftime('%Y%m')}.csv"
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if f.tell() == 0:
            writer.writeheader()
        writer.writerows(rows)


def send_telegram(text: str, cfg: dict) -> None:
    token, chat_id = str(cfg.get("telegram_token", "")).strip(), str(cfg.get("chat_id", "")).strip()
    if not token or not chat_id:
        raise RuntimeError(f"Telegram settings missing in {CFG_FILE}")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text[:4000]}, timeout=15)
    r.raise_for_status()
    if not r.json().get("ok", False):
        raise RuntimeError(f"Telegram API error: {r.text[:300]}")


def format_summary(rows: list[dict]) -> str:
    lines = ["📊 업비트 시세 관찰 요약"]
    for row in rows:
        lines.append(f"{row['name']} ({row['market']}): {row['price']:,.8g}원 / 24h {row['change_24h_pct']:+.2f}%")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="업비트 주요 종목 관찰기 — 주문 API 미사용")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true")
    group.add_argument("--daemon", action="store_true")
    args = ap.parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("upbit_observer")
    log.info("started config=%s mode=%s", CFG_FILE, "daemon" if args.daemon else "once")
    cfg = load_config()
    last_sent = 0.0
    while True:
        try:
            rows = snapshot(cfg)
            save(rows)
            log.info("snapshot saved: %d markets", len(rows))
            print(format_summary(rows), flush=True)
            now = time.time()
            if cfg.get("telegram_enabled", True) and now - last_sent >= int(cfg["telegram_summary_interval_seconds"]):
                send_telegram(format_summary(rows), cfg)
                log.info("telegram sent: chat_id=%s", str(cfg.get("chat_id", "")))
                last_sent = now
        except Exception as exc:
            print(f"[업비트 오류] {exc}", flush=True)
        if args.once:
            break
        time.sleep(max(5, int(cfg["interval_seconds"])))


if __name__ == "__main__":
    main()
