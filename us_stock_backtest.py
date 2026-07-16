"""KIS 해외주식 시세 전용 나스닥 개별주 백테스트.

주문 API는 전혀 호출하지 않는다.

예시:
  python3 us_stock_backtest.py fetch --start 2015-01-01
  python3 us_stock_backtest.py run --start 2018-01-01 --end 2026-07-15

전략 v1
  * QQQ 전일 종가가 200일 이동평균 위일 때만 위험자산 보유
  * 나스닥 대형주 중 전일 종가가 100일선 위인 종목을 대상으로 60일 수익률 순위
  * 상위 3종목을 월초 다음 거래일 시가에 균등 편입
  * 하락 국면은 현금. 손절/뉴스/실적 필터는 의도적으로 넣지 않아 회전율을 낮춤

중요: 이 고정 유니버스는 '현재 살아남은 대형주'로 구성되어 생존자 편향이 있다.
이 파일은 API 연결과 전략 가설의 1차 검증용이며, 장기 실전 근거가 아니다.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from backtest import _load_cfg, _token

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data" / "us_daily"
RESULT_DIR = BASE / "results" / "us_backtest"
API_URL = "https://openapi.koreainvestment.com:9443/uapi/overseas-price/v1/quotations/dailyprice"

# KIS 해외주식 기간별 시세 API의 NAS 거래소 코드만 사용한다.
# 전부 나스닥100 계열의 유동성 높은 종목으로 한정했다. 종목 추가는 검증 후에만 한다.
UNIVERSE = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "AMZN": "Amazon",
    "GOOGL": "Alphabet", "META": "Meta", "AVGO": "Broadcom", "COST": "Costco",
    "NFLX": "Netflix", "AMD": "AMD", "QCOM": "Qualcomm", "CSCO": "Cisco",
}
BENCHMARK = "QQQ"


def _read_csv(symbol: str) -> list[dict]:
    path = DATA_DIR / f"{symbol}.csv"
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            try:
                out.append({"date": r["date"], "open": float(r["open"]),
                            "high": float(r["high"]), "low": float(r["low"]),
                            "close": float(r["close"]), "volume": float(r.get("volume", 0))})
            except (KeyError, TypeError, ValueError):
                continue
    return sorted(out, key=lambda x: x["date"])


def _write_csv(symbol: str, rows: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / f"{symbol}.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda x: x["date"]))


def _number(row: dict, *keys: str) -> float:
    for key in keys:
        raw = row.get(key)
        if raw not in (None, "", "-"):
            try:
                return float(str(raw).replace(",", ""))
            except ValueError:
                pass
    raise ValueError(f"가격 필드 없음: {keys}")


def fetch_symbol(symbol: str, start: date, end: date, token: str, cfg: dict) -> int:
    """KIS 해외주식 기간별시세(v1_해외주식-010)를 과거 방향으로 받아 캐시에 병합한다."""
    merged = {r["date"]: r for r in _read_csv(symbol)}
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}",
               "appkey": cfg["app_key"], "appsecret": cfg["app_secret"], "tr_id": "HHDFS76240000"}
    cursor = end
    requests_made = 0
    first_error = None
    # 응답당 행 수가 계정별로 달라 기준일을 과거로 옮겨가며 수집한다.
    while cursor >= start and requests_made < 80:
        params = {"AUTH": "", "EXCD": "NAS", "SYMB": symbol, "GUBN": "0",
                  "BYMD": cursor.strftime("%Y%m%d"), "MODP": "1"}
        r = requests.get(API_URL, headers=headers, params=params, timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(f"{symbol} KIS HTTP {r.status_code}: {r.text[:500]}")
        payload = r.json()
        if payload.get("rt_cd") not in (None, "0"):
            raise RuntimeError(f"{symbol} KIS 오류: {payload.get('msg_cd')} {payload.get('msg1')}")
        rows = payload.get("output2") or payload.get("output") or []
        if not rows:
            if first_error is None:
                first_error = json.dumps(payload, ensure_ascii=False)[:500]
            break
        earliest: date | None = None
        added = 0
        for row in rows:
            raw_date = row.get("xymd") or row.get("date") or row.get("stck_bsop_date")
            if not raw_date:
                continue
            try:
                d = datetime.strptime(raw_date, "%Y%m%d").date()
                bar = {"date": raw_date, "open": _number(row, "open", "ovrs_nmix_oprc"),
                       "high": _number(row, "high", "ovrs_nmix_hgpr"),
                       "low": _number(row, "low", "ovrs_nmix_lwpr"),
                       "close": _number(row, "clos", "close", "ovrs_nmix_prpr"),
                       "volume": _number(row, "tvol", "volume", "acml_vol")}
            except (ValueError, TypeError):
                continue
            if start <= d <= end:
                if raw_date not in merged:
                    added += 1
                merged[raw_date] = bar
            earliest = d if earliest is None or d < earliest else earliest
        requests_made += 1
        if earliest is None or earliest >= cursor:
            break
        cursor = earliest - timedelta(days=1)
        time.sleep(0.15)
        if cursor < start:
            break
        # 빈 페이지를 무한 반복하지 않도록, 새 데이터가 없고 이미 시작일보다 과거면 중단한다.
        if added == 0 and cursor < start:
            break
    _write_csv(symbol, list(merged.values()))
    count = sum(start.strftime("%Y%m%d") <= r["date"] <= end.strftime("%Y%m%d") for r in merged.values())
    if count == 0:
        raise RuntimeError(f"{symbol} 일봉이 0개입니다. KIS 첫 응답: {first_error or '파싱 실패'}")
    return count


def fetch_all(start: date, end: date) -> None:
    cfg = _load_cfg()
    token = _token(cfg)
    for symbol in [BENCHMARK, *UNIVERSE]:
        count = fetch_symbol(symbol, start, end, token, cfg)
        print(f"{symbol}: {count}개 일봉 캐시 완료")


def _sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def _metrics(equity: list[dict], initial: float) -> dict:
    if not equity:
        return {"final_equity": initial, "total_return_pct": 0.0, "cagr_pct": 0.0, "mdd_pct": 0.0}
    peak = initial
    mdd = 0.0
    for row in equity:
        peak = max(peak, row["equity"])
        mdd = min(mdd, row["equity"] / peak - 1.0)
    years = max(len(equity) / 252.0, 1 / 252.0)
    final = equity[-1]["equity"]
    return {"final_equity": round(final, 2), "total_return_pct": round((final / initial - 1) * 100, 3),
            "cagr_pct": round(((final / initial) ** (1 / years) - 1) * 100, 3), "mdd_pct": round(mdd * 100, 3)}


def _load_bars(symbol: str) -> dict[str, dict]:
    return {r["date"]: r for r in _read_csv(symbol)}


def _write_result(stamp: str, summary: dict, equity: list[dict], trades: list[dict]) -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    (RESULT_DIR / f"run_{stamp}_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    for suffix, rows, fields in (
        ("equity", equity, ["date", "equity", "cash", "positions", "regime"]),
        ("trades", trades, ["date", "symbol", "name", "side", "price", "qty", "reason", "notional", "cost"]),
    ):
        with (RESULT_DIR / f"run_{stamp}_{suffix}.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


def run(start: date, end: date, initial: float, top_n: int, fee_bps: float) -> dict:
    qqq = _load_bars(BENCHMARK)
    stocks = {s: _load_bars(s) for s in UNIVERSE}
    all_dates = [d for d in sorted(qqq) if d <= end.strftime("%Y%m%d")]
    dates = [d for d in all_dates if d >= start.strftime("%Y%m%d")]
    if len(dates) < 1 or sum(d < start.strftime("%Y%m%d") for d in all_dates) < 200:
        raise RuntimeError("시작일 이전 QQQ 일봉이 200개 필요합니다. fetch 시작일을 최소 1년 더 과거로 잡으세요.")
    missing = [s for s, bars in stocks.items() if not bars]
    if missing:
        raise RuntimeError(f"종목 데이터 없음: {', '.join(missing)}. 먼저 fetch를 실행하세요.")

    fee = fee_bps / 10_000
    cash = initial
    pos: dict[str, int] = {}
    trades: list[dict] = []
    equity: list[dict] = []
    total_cost = 0.0
    qqq_closes: list[float] = []
    closes: dict[str, list[float]] = {s: [] for s in UNIVERSE}
    current_month = None

    for d in all_dates:
        qb = qqq[d]
        qqq_closes.append(qb["close"])
        for s in UNIVERSE:
            if d in stocks[s]:
                closes[s].append(stocks[s][d]["close"])

        month = d[:6]
        # 첫 월초에는 전일까지 확정된 정보만 쓴다. 당일 시가에서 체결하므로 룩어헤드가 아니다.
        is_rebalance = month != current_month
        if is_rebalance:
            current_month = month
        selected: list[str] = []
        regime = "warmup"
        if len(qqq_closes) >= 201:
            prior_qqq = qqq_closes[-2] if len(qqq_closes) >= 2 else qqq_closes[-1]
            qqq_ma200 = _sma(qqq_closes[:-1], 200)
            risk_on = qqq_ma200 is not None and prior_qqq > qqq_ma200
            regime = "risk_on" if risk_on else "cash"
            if risk_on:
                candidates = []
                for s in UNIVERSE:
                    values = closes[s]
                    if len(values) < 101:
                        continue
                    prior = values[-2] if len(values) >= 2 else values[-1]
                    history = values[:-1]
                    ma100 = _sma(history, 100)
                    if ma100 is None or prior <= ma100 or len(history) < 60:
                        continue
                    momentum = prior / history[-60] - 1.0
                    candidates.append((momentum, s))
                selected = [s for _, s in sorted(candidates, reverse=True)[:top_n]]

        active = d >= start.strftime("%Y%m%d")
        # 시작일은 월중이어도 첫 진입일로 취급한다. 이후부터는 월초만 교체한다.
        if active and (is_rebalance or not equity) and regime != "warmup":
            # 당일 시가로 기존 비중을 먼저 청산한다.
            for s in list(pos):
                if s in selected:
                    continue
                bar = stocks[s].get(d)
                if not bar:
                    continue
                qty, px = pos.pop(s), bar["open"]
                notional = qty * px
                cost = notional * fee
                cash += notional - cost
                total_cost += cost
                trades.append({"date": d, "symbol": s, "name": UNIVERSE[s], "side": "SELL", "price": round(px, 4),
                               "qty": qty, "reason": "monthly_exit_or_cash", "notional": round(notional, 2), "cost": round(cost, 2)})
            # 유지 종목 포함 시가 기준 총자산으로 목표 금액을 계산한다.
            equity_open = cash + sum(qty * stocks[s][d]["open"] for s, qty in pos.items() if d in stocks[s])
            target = equity_open / len(selected) if selected else 0.0
            # 남은 종목의 과대 비중도 월 1회 정리한다.
            for s in list(pos):
                bar = stocks[s].get(d)
                if not bar:
                    continue
                desired = math.floor(target / (bar["open"] * (1 + fee)))
                if pos[s] > desired:
                    qty, px = pos[s] - desired, bar["open"]
                    notional, cost = qty * px, qty * px * fee
                    pos[s] = desired
                    cash += notional - cost
                    total_cost += cost
                    trades.append({"date": d, "symbol": s, "name": UNIVERSE[s], "side": "SELL", "price": round(px, 4),
                                   "qty": qty, "reason": "monthly_weight", "notional": round(notional, 2), "cost": round(cost, 2)})
            for s in selected:
                bar = stocks[s].get(d)
                if not bar:
                    continue
                desired = math.floor(target / (bar["open"] * (1 + fee)))
                held = pos.get(s, 0)
                qty = min(max(desired - held, 0), math.floor(cash / (bar["open"] * (1 + fee))))
                if qty <= 0:
                    continue
                px, notional, cost = bar["open"], qty * bar["open"], qty * bar["open"] * fee
                cash -= notional + cost
                pos[s] = held + qty
                total_cost += cost
                trades.append({"date": d, "symbol": s, "name": UNIVERSE[s], "side": "BUY", "price": round(px, 4),
                               "qty": qty, "reason": "monthly_momentum", "notional": round(notional, 2), "cost": round(cost, 2)})
        if active:
            mark = cash + sum(qty * stocks[s].get(d, {"close": 0})["close"] for s, qty in pos.items())
            equity.append({"date": d, "equity": round(mark, 2), "cash": round(cash, 2), "positions": ";".join(sorted(pos)), "regime": regime})

    # 같은 구간 QQQ 매수·보유. 첫 시가 매수, 마지막 종가 평가, 동일한 편도 비용 가정.
    first_open = qqq[dates[0]]["open"]
    qqq_qty = math.floor(initial / (first_open * (1 + fee)))
    qqq_cash = initial - qqq_qty * first_open * (1 + fee)
    benchmark = qqq_cash + qqq_qty * qqq[dates[-1]]["close"]
    summary = {"strategy": "nasdaq_largecap_monthly_momentum_v1", "universe_size": len(UNIVERSE),
               "universe_warning": "현재 대형주 고정 목록이라 생존자 편향이 있음. 장기 성과 증명용 아님.",
               "start": dates[0], "end": dates[-1], "trading_days": len(dates), "currency": "USD",
               "initial_cash": initial, **_metrics(equity, initial), "trade_count": len(trades),
               "turnover_usd": round(sum(t["notional"] for t in trades), 2), "estimated_cost_usd": round(total_cost, 2),
               "fee_bps_per_side": fee_bps, "top_n": top_n, "signal": "QQQ 200일선 + 종목 100일선 + 60일 모멘텀, 월초 시가 체결",
               "qqq_buy_hold_final": round(benchmark, 2), "qqq_buy_hold_return_pct": round((benchmark / initial - 1) * 100, 3),
               "limitations": "일봉/달러 기준이며 환율, 세금, 배당, 실적발표 일정, 실제 호가·스프레드, 상장폐지 종목은 미포함. 주문 API는 미사용."}
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _write_result(stamp, summary, equity, trades)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"결과 저장: {RESULT_DIR / ('run_' + stamp + '_summary.json')}")
    return summary


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def main() -> None:
    ap = argparse.ArgumentParser(description="KIS 해외주식 시세 전용 나스닥 개별주 백테스트 — 주문 API 미사용")
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("fetch", "run"):
        p = sub.add_parser(name)
        p.add_argument("--start", default="2015-01-01")
        p.add_argument("--end", default=date.today().isoformat())
    run_p = sub.choices["run"]
    run_p.add_argument("--initial", type=float, default=100_000.0)
    run_p.add_argument("--top-n", type=int, default=3)
    run_p.add_argument("--fee-bps", type=float, default=20.0, help="편도 비용·슬리피지 가정(bps), 기본 0.20%")
    args = ap.parse_args()
    start, end = _parse_date(args.start), _parse_date(args.end)
    if start >= end:
        raise SystemExit("--start는 --end보다 과거여야 합니다.")
    if args.command == "fetch":
        fetch_all(start, end)
    else:
        run(start, end, args.initial, args.top_n, args.fee_bps)


if __name__ == "__main__":
    main()
