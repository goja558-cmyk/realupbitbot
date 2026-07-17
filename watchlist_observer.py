"""국내 관찰종목 데이터 수집기 — 주문 API를 절대 호출하지 않는다.

관찰 목록: 동진쎄미켐, 실리콘투, 클래시스, JYP Ent.

장중 10:00~14:20에는 1분, 장 초반/마감 전에는 5분 간격으로 현재가·호가·거래량과
일봉 기술 상태를 CSV에 누적한다. 수동 매수 여부와 관계없이 매수/매도는 하지 않는다.

예시:
  python3 watchlist_observer.py --once
  python3 watchlist_observer.py --daemon
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

from backtest import _load_cfg, _read_csv, _token, fetch_code

BASE = Path(__file__).resolve().parent
CFG_FILE = BASE / "watchlist_cfg.yaml"
DATA_DIR = BASE / "data" / "watchlist_observer"
STATE_FILE = BASE / "shared" / "watchlist_observer_state.json"
KST = ZoneInfo("Asia/Seoul")
API_URL = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-price"

WATCHLIST = {
    "005290": "동진쎄미켐", "257720": "실리콘투", "214150": "클래시스", "035900": "JYP Ent.",
}
FIELDS = [
    "timestamp", "session", "code", "name", "price", "prev_close", "change_pct", "open", "high", "low",
    "volume", "value_krw", "bid", "ask", "spread_bps", "day_range_pct", "range_position_pct",
    "since_last_pct", "volume_delta", "value_delta_krw", "ma20", "ma60", "ma200", "ret20_pct", "ret60_pct",
    "state",
]


def _default_config() -> dict:
    return {
        "mode": "OBSERVE_ONLY",  # 이 값은 정보용이며 어떤 값이어도 주문 기능은 존재하지 않는다.
        "core_interval_seconds": 60,
        "edge_interval_seconds": 300,
        "telegram_daily_summary": True,
        "manual_positions": {},  # 수동 매수 기록용. 자동 주문에 사용하지 않는다.
    }


def load_config() -> dict:
    if not CFG_FILE.exists():
        with CFG_FILE.open("w", encoding="utf-8") as f:
            yaml.safe_dump(_default_config(), f, allow_unicode=True, sort_keys=False)
    with CFG_FILE.open(encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    return {**_default_config(), **loaded}


def now_kst() -> datetime:
    return datetime.now(KST)


def session_at(now: datetime, config: dict) -> tuple[str, int]:
    """거래 시간과 다음 수집 간격. 휴장일은 KIS 응답이 없으므로 단순 대기한다."""
    if now.weekday() >= 5:
        return "closed", 300
    clock = now.time()
    if dt_time(9, 0) <= clock < dt_time(10, 0):
        return "open_edge", int(config["edge_interval_seconds"])
    if dt_time(10, 0) <= clock < dt_time(14, 20):
        return "core", int(config["core_interval_seconds"])
    if dt_time(14, 20) <= clock < dt_time(15, 30):
        return "close_edge", int(config["edge_interval_seconds"])
    return "closed", 120


def _integer(data: dict, *keys: str) -> int:
    for key in keys:
        try:
            return int(str(data.get(key, "0")).replace(",", "") or 0)
        except (TypeError, ValueError):
            pass
    return 0


def get_quote(code: str, token: str, cfg: dict) -> dict:
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}",
               "appkey": cfg["app_key"], "appsecret": cfg["app_secret"], "tr_id": "FHKST01010100"}
    r = requests.get(API_URL, headers=headers,
                     params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}, timeout=15)
    if r.status_code >= 400:
        raise RuntimeError(f"{code} KIS HTTP {r.status_code}: {r.text[:200]}")
    payload = r.json()
    if payload.get("rt_cd") not in (None, "0"):
        raise RuntimeError(f"{code} KIS 오류: {payload.get('msg_cd')} {payload.get('msg1')}")
    out = payload.get("output") or {}
    return {
        "price": _integer(out, "stck_prpr"), "prev_close": _integer(out, "stck_sdpr"),
        "open": _integer(out, "stck_oprc"), "high": _integer(out, "stck_hgpr"), "low": _integer(out, "stck_lwpr"),
        "volume": _integer(out, "acml_vol"), "value_krw": _integer(out, "acml_tr_pbmn"),
        "bid": _integer(out, "bidp"), "ask": _integer(out, "askp"),
    }


def daily_indicators(code: str) -> dict:
    rows = _read_csv(code)
    closes = [r["close"] for r in rows]
    if not closes:
        return {"ma20": 0, "ma60": 0, "ma200": 0, "ret20_pct": 0, "ret60_pct": 0, "state": "no_daily_data"}
    def ma(n: int) -> float:
        return sum(closes[-n:]) / n if len(closes) >= n else 0.0
    last = closes[-1]
    ret20 = (last / closes[-21] - 1) * 100 if len(closes) >= 21 else 0.0
    ret60 = (last / closes[-61] - 1) * 100 if len(closes) >= 61 else 0.0
    ma20, ma60, ma200 = ma(20), ma(60), ma(200)
    if ma200 and last < ma200:
        state = "below_ma200"
    elif ma60 and last < ma60:
        state = "below_ma60"
    elif ma20 and last < ma20:
        state = "pullback_above_ma60"
    else:
        state = "above_ma20"
    return {"ma20": round(ma20, 2), "ma60": round(ma60, 2), "ma200": round(ma200, 2),
            "ret20_pct": round(ret20, 3), "ret60_pct": round(ret60, 3), "state": state}


def _path_for(now: datetime) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"snapshots_{now.strftime('%Y%m')}.csv"


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"telegram_sent_dates": []}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _previous_by_code(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    latest: dict[str, dict] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            latest[row.get("code", "")] = row
    return latest


def capture(token: str, cfg: dict, session: str) -> list[dict]:
    now = now_kst()
    path = _path_for(now)
    previous = _previous_by_code(path)
    rows: list[dict] = []
    for code, name in WATCHLIST.items():
        q = get_quote(code, token, cfg)
        if q["price"] <= 0:
            raise RuntimeError(f"{code} 가격이 0입니다.")
        prev = previous.get(code, {})
        prior_price = float(prev.get("price", 0) or 0)
        prior_volume = int(float(prev.get("volume", 0) or 0))
        prior_value = int(float(prev.get("value_krw", 0) or 0))
        change = (q["price"] / q["prev_close"] - 1) * 100 if q["prev_close"] else 0.0
        spread = (q["ask"] / q["bid"] - 1) * 10_000 if q["ask"] and q["bid"] else 0.0
        day_range = (q["high"] / q["low"] - 1) * 100 if q["high"] and q["low"] else 0.0
        position = (q["price"] - q["low"]) / (q["high"] - q["low"]) * 100 if q["high"] > q["low"] else 50.0
        ind = daily_indicators(code)
        rows.append({"timestamp": now.isoformat(timespec="seconds"), "session": session, "code": code, "name": name,
                     **q, "change_pct": round(change, 3), "spread_bps": round(spread, 2),
                     "day_range_pct": round(day_range, 3), "range_position_pct": round(position, 2),
                     "since_last_pct": round((q["price"] / prior_price - 1) * 100, 3) if prior_price else 0.0,
                     "volume_delta": q["volume"] - prior_volume if prior_volume else 0,
                     "value_delta_krw": q["value_krw"] - prior_value if prior_value else 0, **ind})
    header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if header:
            writer.writeheader()
        writer.writerows(rows)
    return rows


def refresh_daily_cache(token: str, cfg: dict) -> None:
    """하루 한 번 일봉을 병합한다. 장중 스냅샷과 분리해 장기 상태 계산에만 사용한다."""
    end = now_kst().date()
    start = end - timedelta(days=420)
    for code in WATCHLIST:
        fetch_code(code, start, end, token, cfg)


def _snapshots_for(day: date) -> list[dict]:
    path = DATA_DIR / f"snapshots_{day.strftime('%Y%m')}.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("timestamp", "").startswith(day.isoformat())]


def _as_float(row: dict, key: str) -> float:
    try:
        return float(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _write_daily_summary(rows: list[dict]) -> Path:
    """날짜·종목 조합을 키로 써서 재시작해도 같은 요약이 중복되지 않게 저장한다."""
    path = DATA_DIR / "daily_summary.csv"
    old: dict[tuple[str, str], dict] = {}
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                old[(r.get("date", ""), r.get("code", ""))] = r
    for row in rows:
        old[(row["date"], row["code"])] = row
    fields = ["date", "code", "name", "close", "day_change_pct", "intraday_high", "intraday_low",
              "intraday_range_pct", "close_range_position_pct", "snapshots", "core_snapshots",
              "data_quality", "ma20", "ma60", "ma200", "ret20_pct", "ret60_pct", "state"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted(old.values(), key=lambda r: (r["date"], r["code"])))
    return path


def build_daily_summary(day: date) -> tuple[list[dict], str]:
    snapshots = _snapshots_for(day)
    grouped = {code: [] for code in WATCHLIST}
    for row in snapshots:
        if row.get("code") in grouped and row.get("session") != "closed":
            grouped[row["code"]].append(row)
    summary: list[dict] = []
    lines = [f"📊 관찰종목 마감 요약 {day:%m/%d}"]
    stale_found = False
    for code, name in WATCHLIST.items():
        rows = grouped[code]
        if not rows:
            summary.append({"date": day.isoformat(), "code": code, "name": name, "data_quality": "no_data"})
            lines.append(f"• {name}: 장중 데이터 없음")
            continue
        prices = [_as_float(r, "price") for r in rows]
        volumes = [_as_float(r, "volume") for r in rows]
        values = [_as_float(r, "value_krw") for r in rows]
        core = [r for r in rows if r.get("session") == "core"]
        # 충분한 장중 표본인데 가격과 누적 거래량이 전혀 변하지 않으면 오래된 KIS 응답으로 본다.
        stale = len(core) >= 5 and len(set(prices)) <= 1 and len(set(volumes)) <= 1 and len(set(values)) <= 1
        quality = "stale" if stale else "ok"
        stale_found = stale_found or stale
        last = rows[-1]
        intraday_high, intraday_low = max(prices), min(prices)
        rng = (intraday_high / intraday_low - 1) * 100 if intraday_low else 0.0
        pos = (prices[-1] - intraday_low) / (intraday_high - intraday_low) * 100 if intraday_high > intraday_low else 50.0
        item = {"date": day.isoformat(), "code": code, "name": name, "close": int(prices[-1]),
                "day_change_pct": round(_as_float(last, "change_pct"), 3), "intraday_high": int(intraday_high),
                "intraday_low": int(intraday_low), "intraday_range_pct": round(rng, 3),
                "close_range_position_pct": round(pos, 2), "snapshots": len(rows), "core_snapshots": len(core),
                "data_quality": quality, "ma20": last.get("ma20", 0), "ma60": last.get("ma60", 0),
                "ma200": last.get("ma200", 0), "ret20_pct": last.get("ret20_pct", 0),
                "ret60_pct": last.get("ret60_pct", 0), "state": last.get("state", "")}
        summary.append(item)
        flag = "⚠️ stale" if stale else item["state"]
        lines.append(f"• {name} {item['day_change_pct']:+.2f}% | 장중 {item['intraday_range_pct']:.2f}% | {flag}")
    if stale_found:
        lines.append("⚠️ 장중 가격·거래량이 갱신되지 않은 종목이 있어 오늘 데이터는 매매 판단에 사용하지 마세요.")
    else:
        lines.append("관찰 전용 요약이며 매수·매도 신호가 아닙니다.")
    return summary, "\n".join(lines)


def _telegram(text: str, cfg: dict) -> None:
    token, chat_id = str(cfg.get("telegram_token", "")).strip(), str(cfg.get("chat_id", "")).strip()
    if not token or not chat_id:
        raise RuntimeError("sector_cfg.yaml에 telegram_token/chat_id가 없습니다.")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text[:4000]}, timeout=12)
    if r.status_code >= 400:
        raise RuntimeError(f"텔레그램 HTTP {r.status_code}: {r.text[:200]}")


def emit_daily_summary(day: date, cfg: dict, observer_cfg: dict, *, force_send: bool = False) -> None:
    rows, message = build_daily_summary(day)
    path = _write_daily_summary(rows)
    print(f"일별 요약 저장: {path}")
    if not observer_cfg.get("telegram_daily_summary", True):
        return
    state = _load_state()
    sent = set(state.get("telegram_sent_dates", []))
    key = day.isoformat()
    if force_send or key not in sent:
        _telegram(message, cfg)
        sent.add(key)
        state["telegram_sent_dates"] = sorted(sent)[-90:]
        _save_state(state)
        print("텔레그램 마감 요약 발송 완료")


def main() -> None:
    ap = argparse.ArgumentParser(description="국내 4종목 관찰 전용 수집기 — 주문 API 미사용")
    ap.add_argument("--once", action="store_true", help="즉시 한 번만 수집")
    ap.add_argument("--daemon", action="store_true", help="시장 시간에 계속 수집")
    ap.add_argument("--refresh-daily", action="store_true", help="일봉 캐시만 갱신")
    ap.add_argument("--daily-summary", action="store_true", help="오늘 일별 CSV 생성 및 텔레그램 요약 발송")
    args = ap.parse_args()
    if not (args.once or args.daemon or args.refresh_daily or args.daily_summary):
        ap.error("--once, --daemon, --refresh-daily, --daily-summary 중 하나가 필요합니다.")
    observer_cfg = load_config()  # 사용자가 수동 보유수량을 적을 기본 YAML을 만든다.
    cfg, token = _load_cfg(), _token(_load_cfg())
    if args.refresh_daily:
        refresh_daily_cache(token, cfg)
        print("일봉 캐시 갱신 완료")
        return
    if args.daily_summary:
        emit_daily_summary(now_kst().date(), cfg, observer_cfg, force_send=True)
        return
    if args.once:
        session, _ = session_at(now_kst(), observer_cfg)
        for row in capture(token, cfg, session):
            print(f"{row['name']} {row['price']:,}원 {row['change_pct']:+.2f}% {row['state']}")
        return
    last_daily_refresh = None
    print("관찰 수집기 시작: 주문 API 미사용 / Ctrl+C로 종료")
    while True:
        now = now_kst()
        session, interval = session_at(now, observer_cfg)
        try:
            if last_daily_refresh != now.date() and now.time() >= dt_time(15, 35):
                refresh_daily_cache(token, cfg)
                emit_daily_summary(now.date(), cfg, observer_cfg)
                last_daily_refresh = now.date()
            if session != "closed":
                rows = capture(token, cfg, session)
                print(f"[{now.strftime('%H:%M:%S')}] {session} " + " | ".join(f"{r['name']} {r['price']:,}" for r in rows))
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[관찰 오류] {exc}")
            # 토큰 만료/일시 오류를 다음 루프에서 회복할 수 있게 토큰을 다시 받는다.
            try:
                token = _token(cfg)
            except Exception:
                pass
        time.sleep(interval)


if __name__ == "__main__":
    main()
