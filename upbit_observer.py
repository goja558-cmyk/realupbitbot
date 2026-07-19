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
import os
import statistics
import uuid
import hashlib
import urllib.parse
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
import jwt

BASE = Path(__file__).resolve().parent
CFG_FILE = BASE / "upbit_watchlist_cfg.yaml"
DATA_DIR = BASE / "data" / "upbit_observer"
STATE_FILE = BASE / "shared" / "upbit_observer_state.json"
API = "https://api.upbit.com/v1"
FIELDS = ["timestamp", "market", "name", "price", "change_pct", "change_24h_pct", "opening_price",
          "pct_from_open", "high_24h", "low_24h", "volume_24h", "value_24h_krw", "value_share_pct",
          "highest_52_week_price", "lowest_52_week_price", "pct_from_52w_high", "bid", "ask", "spread_bps",
          "price_delta_since_last", "pct_since_last", "volume_delta_since_last", "rolling_std_20", "ma20", "pct_from_ma20"]
LOG_DIR = BASE / "logs"
LOG_FILE = LOG_DIR / "upbit_observer.log"
KST = ZoneInfo("Asia/Seoul")

DEFAULT = {
    "markets": {"KRW-BTC": "비트코인", "KRW-ETH": "이더리움", "KRW-XRP": "리플",
                "KRW-SOL": "솔라나", "KRW-DOGE": "도지코인"},
    "interval_seconds": 60,
    "telegram_enabled": True,
    "telegram_summary_interval_seconds": 900,
    # 주식 봇과 완전히 별도의 토큰/채팅방을 입력한다.
    "telegram_token": "",
    "chat_id": "",
    "telegram_summary_interval_seconds": 900,
}


def _load_dotenv() -> None:
    env = BASE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"'))


def account_balances() -> list[dict]:
    access, secret = os.getenv("UPBIT_ACCESS_KEY", ""), os.getenv("UPBIT_SECRET_KEY", "")
    if not access or not secret:
        return []
    payload = {"access_key": access, "nonce": str(uuid.uuid4())}
    token = jwt.encode(payload, secret, algorithm="HS256")
    r = requests.get(f"{API}/accounts", headers={"Authorization": f"Bearer {token}"}, timeout=15)
    r.raise_for_status()
    return r.json()


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


def orderbook(markets: list[str]) -> dict[str, dict]:
    r = requests.get(f"{API}/orderbook", params={"markets": ",".join(markets)}, timeout=15)
    r.raise_for_status()
    out = {}
    for item in r.json():
        units = item.get("orderbook_units") or [{}]
        top = units[0]
        out[item["market"]] = {"bid": top.get("bid_price", 0), "ask": top.get("ask_price", 0)}
    return out


def previous_rows() -> dict[str, list[dict]]:
    path = DATA_DIR / f"snapshots_{datetime.now(KST).strftime('%Y%m')}_v2.csv"
    history: dict[str, list[dict]] = {}
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                history.setdefault(row.get("market", ""), []).append(row)
    return history


def snapshot(cfg: dict) -> list[dict]:
    markets = list(cfg["markets"])
    now = datetime.now(timezone.utc).astimezone(KST)
    books = orderbook(markets)
    history = previous_rows()
    rows = []
    for item in ticker(markets):
        book = books.get(item["market"], {})
        bid, ask = float(book.get("bid", 0)), float(book.get("ask", 0))
        prices = [float(x.get("price", 0)) for x in history.get(item["market"], []) if float(x.get("price", 0) or 0)]
        last = prices[-1] if prices else 0.0
        recent = (prices + [float(item["trade_price"])])[-20:]
        ma20 = sum(recent) / len(recent)
        rolling_std = statistics.pstdev(recent) if len(recent) > 1 else 0.0
        value = float(item["acc_trade_price_24h"])
        rows.append({"timestamp": now.isoformat(timespec="seconds"), "market": item["market"],
                     "name": cfg["markets"].get(item["market"], item["market"]),
                     "price": item["trade_price"], "change_pct": round((item["trade_price"] / item["prev_closing_price"] - 1) * 100, 3),
                     "change_24h_pct": round(item["signed_change_rate"] * 100, 3),
                     "opening_price": item["opening_price"], "pct_from_open": round((item["trade_price"] / item["opening_price"] - 1) * 100, 3) if item["opening_price"] else 0,
                     "high_24h": item["high_price"], "low_24h": item["low_price"],
                     "volume_24h": item["acc_trade_volume_24h"], "value_24h_krw": value,
                     "value_share_pct": 0.0, "highest_52_week_price": item.get("highest_52_week_price", 0),
                     "lowest_52_week_price": item.get("lowest_52_week_price", 0), "pct_from_52w_high": round((item["trade_price"] / item["highest_52_week_price"] - 1) * 100, 3) if item.get("highest_52_week_price") else 0,
                     "bid": bid, "ask": ask, "spread_bps": round((ask / bid - 1) * 10000, 3) if bid and ask else 0,
                     "price_delta_since_last": round(float(item["trade_price"]) - last, 8) if last else 0,
                     "pct_since_last": round((float(item["trade_price"]) / last - 1) * 100, 5) if last else 0,
                     "volume_delta_since_last": round(float(item["acc_trade_volume_24h"]) - float(history.get(item["market"], [{}])[-1].get("volume_24h", 0) or 0), 8) if history.get(item["market"]) else 0,
                     "rolling_std_20": round(rolling_std, 8), "ma20": round(ma20, 8), "pct_from_ma20": round((float(item["trade_price"]) / ma20 - 1) * 100, 5) if ma20 else 0})
    total_value = sum(float(row["value_24h_krw"]) for row in rows)
    for row in rows:
        row["value_share_pct"] = round(float(row["value_24h_krw"]) / total_value * 100, 3) if total_value else 0
    return rows


def save(rows: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"snapshots_{datetime.now(KST).strftime('%Y%m')}_v2.csv"
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if f.tell() == 0:
            writer.writeheader()
        writer.writerows(rows)


def send_telegram(text: str, cfg: dict) -> None:
    token, chat_id = str(cfg.get("telegram_token", "")).strip(), str(cfg.get("chat_id", "")).strip()
    log = logging.getLogger("upbit_observer.telegram")
    log.info("telegram send attempt chat_id=%s chars=%d", chat_id, len(text))
    if not token or not chat_id:
        log.error("telegram send failed reason=missing_credentials")
        raise RuntimeError(f"Telegram settings missing in {CFG_FILE}")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text[:4000]}, timeout=15)
    r.raise_for_status()
    if not r.json().get("ok", False):
        raise RuntimeError(f"Telegram API error: {r.text[:300]}")
    log.info("telegram send success chat_id=%s status=%s", chat_id, r.status_code)


def format_summary(rows: list[dict]) -> str:
    observed_at = rows[0]["timestamp"] if rows else datetime.now(KST).isoformat(timespec="seconds")
    lines = [f"📊 업비트 시세 관찰 요약 ({observed_at})"]
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
    _load_dotenv()
    try:
        send_telegram("✅ 업비트 감시 봇 정상 작동 중\n시세 수집을 시작했습니다.", cfg)
        log.info("startup telegram sent")
    except Exception as exc:
        log.exception("startup telegram failed: %s", exc)
    last_sent = 0.0
    while True:
        try:
            rows = snapshot(cfg)
            save(rows)
            log.info("snapshot saved: %d markets", len(rows))
            print(format_summary(rows), flush=True)
            now = time.time()
            balances = account_balances()
            held = [x for x in balances if x.get("currency") != "KRW" and float(x.get("balance", 0) or 0) > 0]
            interval = 900 if held else 21600
            message = format_summary(rows) + (f"\n보유 상태: {len(held)}종목 / 알림 주기: {interval // 60}분" if balances else "\n보유 조회: API 키 미설정")
            if cfg.get("telegram_enabled", True) and now - last_sent >= interval:
                send_telegram(message, cfg)
                log.info("telegram sent: chat_id=%s", str(cfg.get("chat_id", "")))
                last_sent = now
        except Exception as exc:
            print(f"[업비트 오류] {exc}", flush=True)
        if args.once:
            break
        time.sleep(max(5, int(cfg["interval_seconds"])))


if __name__ == "__main__":
    main()
