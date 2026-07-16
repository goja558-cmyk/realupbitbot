"""KIS 일봉 기반 섹터 ETF 백테스트 (주문 API를 절대 호출하지 않음).

예시:
  python3 backtest.py fetch --start 2018-01-01
  python3 backtest.py run --start 2018-01-01 --end 2026-07-15
  python3 backtest.py grid --start 2023-01-01

데이터는 data/kis_daily/<code>.csv에 캐시된다. 이 파일은 KIS 시세 API만 사용하며
계좌번호, 주문 TR_ID, 매수/매도 API를 사용하지 않는다.
"""
from __future__ import annotations

import argparse
import ast
import csv
import itertools
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import yaml

BASE = Path(__file__).resolve().parent
CFG = BASE / "sector_cfg.yaml"
DATA_DIR = BASE / "data" / "kis_daily"
RESULT_DIR = BASE / "results" / "backtest"

# 실거래 유니버스와 동일하다. 상장 전 날짜에는 CSV가 없으므로 자동 제외된다.
UNIVERSE = {
    "396500": {"name": "TIGER 반도체TOP10", "tag": "성장", "max_weight": .25},
    "449450": {"name": "PLUS K방산", "tag": "모멘텀", "max_weight": .15},
    "494670": {"name": "TIGER 조선TOP10", "tag": "모멘텀", "max_weight": .15},
    "143860": {"name": "TIGER 헬스케어", "tag": "방어", "max_weight": .15},
    "305720": {"name": "KODEX 2차전지산업", "tag": "고위험", "max_weight": .05},
    "445290": {"name": "KODEX 로봇액티브", "tag": "고위험", "max_weight": .05},
    "434730": {"name": "HANARO 원자력iSelect", "tag": "성장", "max_weight": .15},
    "091170": {"name": "KODEX 은행", "tag": "방어", "max_weight": .15},
    "227560": {"name": "TIGER 200 생활소비재", "tag": "방어", "max_weight": .15},
}


@dataclass(frozen=True)
class Params:
    short: int = 20
    long: int = 60
    short_weight: float = 0.5
    top_n: int = 5
    stop_loss: float = -5.0
    trail_start: float = 4.0
    trail_gap: float = 2.0
    fee_bps: float = 10.0       # 편도 비용+슬리피지 0.10%, 보수적으로 가정
    max_per_tag: int = 2
    kofr_reserve: float = 0.15
    cooldown_days: int = 14
    overheat_pct: float = 15.0


def _load_cfg() -> dict:
    if not CFG.exists():
        raise RuntimeError(f"설정 파일 없음: {CFG}")
    with CFG.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def validate_universe() -> None:
    """실전 봇의 활성 ETF 코드와 다르면 백테스트를 중단한다.
    레거시 청산전용 코드는 trade_enabled=False라 비교에서 제외한다.
    """
    tree = ast.parse((BASE / "sector_bot.py").read_text(encoding="utf-8"))
    node = next((n for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "ETF_UNIVERSE" for t in n.targets)), None)
    if node is None:
        raise RuntimeError("sector_bot.py에서 ETF_UNIVERSE를 찾지 못했습니다.")
    live = ast.literal_eval(node.value)
    active = {c for c, m in live.items() if m.get("trade_enabled", True)}
    if active != set(UNIVERSE):
        raise RuntimeError("실전/백테스트 ETF 코드 불일치: "
                           f"실전만={sorted(active-set(UNIVERSE))}, 백테스트만={sorted(set(UNIVERSE)-active)}")


def _token(cfg: dict) -> str:
    # 실전 봇이 발급·저장한 토큰이 아직 유효하면 재사용한다. 백테스트마다
    # 새 토큰을 발급하면 KIS의 토큰 발급 제한/일시 오류에 불필요하게 걸린다.
    token_file = BASE / "shared" / "kis_token.json"
    try:
        cached = json.loads(token_file.read_text(encoding="utf-8"))
        if cached.get("token") and float(cached.get("expire", 0)) > time.time() + 60:
            return cached["token"]
    except (OSError, ValueError, TypeError):
        pass
    app_key, app_secret = cfg.get("app_key"), cfg.get("app_secret")
    if not app_key or not app_secret:
        raise RuntimeError("sector_cfg.yaml의 app_key/app_secret이 필요합니다.")
    r = requests.post(
        "https://openapi.koreainvestment.com:9443/oauth2/tokenP",
        json={"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret},
        timeout=15,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"KIS 토큰 발급 HTTP {r.status_code}: {r.text[:500]}")
    token = r.json().get("access_token")
    if not token:
        raise RuntimeError(f"KIS 토큰 발급 실패: {r.text[:300]}")
    return token


def _read_csv(code: str) -> list[dict]:
    path = DATA_DIR / f"{code}.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        try:
            out.append({"date": r["date"], **{k: float(r[k]) for k in ("open", "high", "low", "close", "value")}})
        except (KeyError, ValueError):
            continue
    return sorted(out, key=lambda x: x["date"])


def _write_csv(code: str, rows: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{code}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "value"])
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda x: x["date"]))


def fetch_code(code: str, start: date, end: date, token: str, cfg: dict) -> int:
    """KIS 일봉 API를 과거 방향으로 요청해 캐시에 병합한다."""
    old = {r["date"]: r for r in _read_csv(code)}
    first_payload = None
    received_rows = 0
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": cfg["app_key"], "appsecret": cfg["app_secret"],
        # FHKST01010400은 최신 약 30건용이다. 이 URL의 기간별 차트 TR ID는
        # FHKST03010100이며, 과거 데이터 수집에 사용해야 한다.
        "tr_id": "FHKST03010100",
    }
    cursor = end
    while cursor >= start:
        # KIS 일봉 응답은 계정/종목에 따라 약 30거래일까지만 돌아올 수 있다.
        # 실전 봇과 동일하게 50일 단위로 요청해야 각 응답 구간이 끊기지 않는다.
        begin = max(start, cursor - timedelta(days=50))
        r = requests.get(
            "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            headers=headers,
            params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code,
                    "fid_input_date_1": begin.strftime("%Y%m%d"),
                    "fid_input_date_2": cursor.strftime("%Y%m%d"),
                    "fid_period_div_code": "D", "fid_org_adj_prc": "0"},
            timeout=20,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"{code} KIS HTTP {r.status_code}: {r.text[:250]}")
        payload = r.json()
        if first_payload is None:
            first_payload = payload
        if payload.get("rt_cd") not in (None, "0"):
            raise RuntimeError(f"{code} KIS 오류: {payload.get('msg_cd')} {payload.get('msg1')}")
        # KIS 계정/응답 버전에 따라 일봉 배열 키가 output2 또는 output으로 다르다.
        # 실전 봇과 동일하게 둘 다 지원한다.
        rows = payload.get("output2") or payload.get("output") or []
        received_rows += len(rows)
        for x in rows:
            try:
                d = x.get("stck_bsop_date", "")
                close = float(x.get("stck_clpr") or 0)
                if d and close > 0:
                    old[d] = {"date": d, "open": float(x.get("stck_oprc") or close),
                              "high": float(x.get("stck_hgpr") or close), "low": float(x.get("stck_lwpr") or close),
                              "close": close,
                              # 일부 응답은 거래대금(acml_tr_pbmn) 대신 거래량(acml_vol)만 준다.
                              "value": float(x.get("acml_tr_pbmn") or 0) or close * float(x.get("acml_vol") or 0)}
            except (TypeError, ValueError):
                pass
        cursor = begin - timedelta(days=1)
        time.sleep(0.22)  # KIS 호출 제한 완화
    _write_csv(code, list(old.values()))
    if not old and received_rows == 0:
        # KIS가 HTTP 200이라도 권한/환경/파라미터 문제를 JSON 본문에 담아 빈
        # output으로 줄 수 있다. 0개를 정상 데이터처럼 표시하지 않는다.
        detail = json.dumps(first_payload or {}, ensure_ascii=False)[:700]
        raise RuntimeError(f"{code} 일봉이 0개입니다. KIS 첫 응답: {detail}")
    return len(old)


def fetch_all(start: date, end: date) -> None:
    cfg, token = _load_cfg(), _token(_load_cfg())
    for code, meta in UNIVERSE.items():
        n = fetch_code(code, start, end, token, cfg)
        print(f"[KIS] {code} {meta['name']}: {n}개 일봉")


def _score(rows: list[dict], at: int, p: Params) -> float | None:
    if at < p.long:
        return None
    now, s, l = rows[at]["close"], rows[at - p.short]["close"], rows[at - p.long]["close"]
    if min(now, s, l) <= 0:
        return None
    return p.short_weight * (now / s - 1) * 100 + (1 - p.short_weight) * (now / l - 1) * 100


def _select(data: dict[str, list[dict]], day: str, p: Params) -> list[str]:
    ranked = []
    for code, rows in data.items():
        idx = next((i for i, r in enumerate(rows) if r["date"] == day), None)
        if idx is None:
            continue
        score = _score(rows, idx, p)
        if score is not None and score > -5.0:
            ranked.append((score, code))
    tags, chosen = {}, []
    for _, code in sorted(ranked, reverse=True):
        tag = UNIVERSE[code]["tag"]
        if tags.get(tag, 0) >= p.max_per_tag:
            continue
        chosen.append(code)
        tags[tag] = tags.get(tag, 0) + 1
        if len(chosen) == p.top_n:
            break
    return chosen


def simulate(data: dict[str, list[dict]], p: Params, start: str, end: str, initial_cash=1_000_000) -> tuple[dict, list[dict], list[dict]]:
    """전일 종가 신호 → 당일 시가 체결. 월초 전면, 매주 월요일 하위 교체 대신
    매주 월요일 목표 바스켓으로 조정한다. 방어/인버스는 별도 검증 대상이라 포함하지 않는다."""
    calendar = sorted({r["date"] for rows in data.values() for r in rows if start <= r["date"] <= end})
    by_day = {c: {r["date"]: r for r in rows} for c, rows in data.items()}
    cash, pos, trades, curve = float(initial_cash), {}, [], []
    last_close = {}
    peak, max_dd = cash, 0.0
    fee = p.fee_bps / 10_000

    def sell(code, bar, reason):
        nonlocal cash
        q, entry, high = pos.pop(code)
        px = min(bar["open"], bar["low"]) if reason == "gap_stop" else bar["open"]
        gross = q * px
        cash += gross * (1 - fee)
        trades.append({"date": bar["date"], "code": code, "name": UNIVERSE[code]["name"], "side": "SELL", "price": round(px, 2), "qty": q, "reason": reason, "pnl_pct": round((px / entry - 1) * 100 - p.fee_bps / 100, 3)})

    for n, d in enumerate(calendar):
        bars = {c: by_day[c].get(d) for c in data}
        # 장중 손절/트레일: 일봉에서는 고가·저가 순서를 모르므로 보수적으로 판단.
        for code in list(pos):
            bar = bars.get(code)
            if not bar:
                continue
            q, entry, high = pos[code]
            # 일봉에서는 고가/저가의 장중 순서를 모른다. 당일 고가로 새 트레일을
            # 만든 뒤 같은 봉의 저가에 적용하지 않고, 전일까지 확정된 high만 쓴다.
            stop_px = entry * (1 + p.stop_loss / 100)
            trail_px = high * (1 - p.trail_gap / 100) if high >= entry * (1 + p.trail_start / 100) else -1
            trigger = max(stop_px, trail_px)
            if bar["low"] <= trigger:
                # 시가가 기준보다 낮으면 시가 체결, 아니면 기준가 체결로 보수 처리
                px = min(bar["open"], trigger)
                gross = q * px
                cash += gross * (1 - fee)
                reason = "trailing" if trail_px >= stop_px else "stop_loss"
                pos.pop(code)
                trades.append({"date": d, "code": code, "name": UNIVERSE[code]["name"], "side": "SELL", "price": round(px, 2), "qty": q, "reason": reason, "pnl_pct": round((px / entry - 1) * 100 - p.fee_bps / 100, 3)})
            elif code in pos:
                pos[code] = (q, entry, max(high, bar["high"]))

        dt = datetime.strptime(d, "%Y%m%d").date()
        # 월요일: 직전 거래일 종가만 사용해 목표 바스켓을 계산하고 시가에 조정한다.
        if dt.weekday() == 0 and n > 0:
            target = _select(data, calendar[n - 1], p)
            for code in list(pos):
                if code not in target and bars.get(code):
                    sell(code, bars[code], "weekly_rebalance")
            buyable = [c for c in target if c not in pos and bars.get(c) and bars[c]["open"] > 0]
            equity_now = cash + sum(q * (bars[c]["open"] if bars.get(c) else last_close.get(c, entry)) for c, (q, entry, _) in pos.items())
            budget = equity_now / max(1, len(target))
            for code in buyable:
                px = bars[code]["open"]
                qty = int((min(budget, cash) * (1 - fee)) // px)
                if qty <= 0:
                    continue
                cost = qty * px * (1 + fee)
                cash -= cost
                pos[code] = (qty, px, px)
                trades.append({"date": d, "code": code, "name": UNIVERSE[code]["name"], "side": "BUY", "price": round(px, 2), "qty": qty, "reason": "weekly_rebalance", "pnl_pct": ""})

        for c, bar in bars.items():
            if bar:
                last_close[c] = bar["close"]
        equity = cash + sum(q * (bars[c]["close"] if bars.get(c) else last_close.get(c, entry)) for c, (q, entry, _) in pos.items())
        peak = max(peak, equity)
        max_dd = min(max_dd, (equity / peak - 1) * 100)
        curve.append({"date": d, "equity": round(equity, 2), "cash": round(cash, 2), "drawdown_pct": round((equity / peak - 1) * 100, 3), "holdings": ";".join(sorted(pos))})

    years = max(1 / 252, len(calendar) / 252)
    final = curve[-1]["equity"] if curve else initial_cash
    summary = {"start": start, "end": end, "trading_days": len(calendar), "initial_cash": initial_cash,
               "final_equity": round(final, 2), "total_return_pct": round((final / initial_cash - 1) * 100, 3),
               "cagr_pct": round(((final / initial_cash) ** (1 / years) - 1) * 100, 3), "mdd_pct": round(max_dd, 3),
               "trade_count": len(trades), "limitations": "일봉 기반: 장중 고가/저가 순서는 알 수 없어 트레일은 전일까지 확정된 고점만 사용. KOSPI 방어/인버스는 미포함.", **p.__dict__}
    return summary, trades, curve


def _save_result(summary, trades, curve, prefix="run"):
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = RESULT_DIR / f"{prefix}_{stamp}"
    with open(f"{base}_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    for suffix, rows in (("trades", trades), ("equity", curve)):
        with open(f"{base}_{suffix}.csv", "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["date"])
            writer.writeheader(); writer.writerows(rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"결과 저장: {base}_summary.json / _trades.csv / _equity.csv")


def main():
    ap = argparse.ArgumentParser(description="KIS 일봉 전용 백테스트 — 주문 API 미사용")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("fetch", "run", "grid"):
        s = sub.add_parser(name)
        s.add_argument("--start", default="2023-01-01")
        s.add_argument("--end", default=date.today().isoformat())
    args = ap.parse_args()
    validate_universe()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if args.cmd == "fetch":
        fetch_all(start, end); return
    data = {c: _read_csv(c) for c in UNIVERSE}
    missing = [c for c, rows in data.items() if not rows]
    if missing:
        raise SystemExit("데이터 없음: " + ", ".join(missing) + "\n먼저: python3 backtest.py fetch --start " + args.start)
    if args.cmd == "run":
        _save_result(*simulate(data, Params(), start.strftime("%Y%m%d"), end.strftime("%Y%m%d")))
        return
    summaries = []
    for short, long, top_n, stop, ts, gap in itertools.product((10, 20, 30), (40, 60, 90), (3, 5), (-3.0, -5.0, -7.0), (4.0, 6.0), (2.0, 3.0)):
        if short >= long:
            continue
        summary, _, _ = simulate(data, Params(short=short, long=long, top_n=top_n, stop_loss=stop, trail_start=ts, trail_gap=gap), start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        summaries.append(summary)
    summaries.sort(key=lambda x: (x["cagr_pct"] + x["mdd_pct"] * 0.5), reverse=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULT_DIR / f"grid_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summaries[0])); w.writeheader(); w.writerows(summaries)
    print(f"그리드 결과: {path}")
    for r in summaries[:10]: print(r)


if __name__ == "__main__":
    main()
