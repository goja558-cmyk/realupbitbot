"""KIS 일봉 전용 국내 대형주 예측력 연구 도구 (주문 API 미사용).

목적은 자동매매가 아니라, 전일 종가에서 계산한 순위가 이후 5거래일 수익률을
KODEX 200(069500)보다 일관되게 예측하는지 검정하는 것이다.

예시:
  python3 kr_stock_signal_research.py fetch --start 2015-01-01
  python3 kr_stock_signal_research.py run --start 2018-01-01 --end 2026-07-15

장중 10:00~14:20 창은 KIS의 과거 분봉 데이터가 충분히 쌓인 뒤에만 별도로
검증할 수 있다. 이 파일은 그 전에 일봉 신호 자체의 예측력이 있는지 확인한다.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from datetime import date, datetime
from pathlib import Path

from backtest import _load_cfg, _read_csv, _token, fetch_code

BASE = Path(__file__).resolve().parent
RESULT_DIR = BASE / "results" / "kr_stock_research"
BENCHMARK = "069500"  # KODEX 200, 가격수익률 기준 비교

# 현 시점의 유동성 높은 KOSPI 대형주 목록. 고정 현재 구성이라 생존자 편향이 있다.
# 따라서 2018~ 장기 결과는 '신호 가설'의 참고치일 뿐 실전 성과 증명이 아니다.
UNIVERSE = {
    "005930": "삼성전자", "000660": "SK하이닉스", "373220": "LG에너지솔루션",
    "207940": "삼성바이오로직스", "005380": "현대차", "000270": "기아",
    "105560": "KB금융", "055550": "신한지주", "068270": "셀트리온",
    "035420": "NAVER", "012450": "한화에어로스페이스", "329180": "HD현대중공업",
    "009540": "HD한국조선해양", "028260": "삼성물산", "006400": "삼성SDI",
}


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def fetch_all(start: date, end: date) -> None:
    cfg = _load_cfg()
    token = _token(cfg)
    for code in [BENCHMARK, *UNIVERSE]:
        count = fetch_code(code, start, end, token, cfg)
        print(f"{code}: {count}개 일봉 캐시 완료")


def _bars(code: str) -> dict[str, dict]:
    return {row["date"]: row for row in _read_csv(code)}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _write(stamp: str, summary: dict, observations: list[dict]) -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    (RESULT_DIR / f"run_{stamp}_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (RESULT_DIR / f"run_{stamp}_observations.csv").open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["signal_date", "exit_date", "regime", "selected", "basket_return_pct", "benchmark_return_pct", "excess_return_pct"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(observations)


def run(start: date, end: date, top_n: int, horizon: int) -> dict:
    bench = _bars(BENCHMARK)
    shares = {code: _bars(code) for code in UNIVERSE}
    missing = [code for code, data in shares.items() if not data]
    if not bench or missing:
        raise RuntimeError(f"데이터 없음: {', '.join(([BENCHMARK] if not bench else []) + missing)}. 먼저 fetch를 실행하세요.")
    all_dates = sorted(d for d in bench if d <= end.strftime("%Y%m%d"))
    dates = [d for d in all_dates if d >= start.strftime("%Y%m%d")]
    if len(dates) < horizon + 1:
        raise RuntimeError("분석 기간이 너무 짧습니다.")

    observations: list[dict] = []
    # 5거래일을 겹치지 않게 관측해 동일 구간 수익률을 여러 번 세는 문제를 피한다.
    step = horizon
    for idx in range(200, len(all_dates) - horizon, step):
        d = all_dates[idx]
        if d < start.strftime("%Y%m%d"):
            continue
        exit_d = all_dates[idx + horizon]
        bench_closes = [bench[x]["close"] for x in all_dates[:idx + 1]]
        ma200 = _mean(bench_closes[-200:])
        regime = "risk_on" if bench_closes[-1] > ma200 else "cash"
        candidates: list[tuple[float, str]] = []
        for code, data in shares.items():
            history_dates = [x for x in all_dates[:idx + 1] if x in data]
            if len(history_dates) < 101 or exit_d not in data:
                continue
            closes = [data[x]["close"] for x in history_dates]
            if closes[-1] <= _mean(closes[-100:]):
                continue
            # 20/60일 수익률을 동일 가중으로 쓴다. 파라미터 최적화가 아닌 고정 가설이다.
            score = 0.5 * (closes[-1] / closes[-21] - 1) + 0.5 * (closes[-1] / closes[-61] - 1)
            candidates.append((score, code))
        selected = [code for _, code in sorted(candidates, reverse=True)[:top_n]] if regime == "risk_on" else []
        if selected:
            returns = [shares[c][exit_d]["close"] / shares[c][d]["close"] - 1 for c in selected]
            basket_return = _mean(returns)
        else:
            basket_return = 0.0
        bench_return = bench[exit_d]["close"] / bench[d]["close"] - 1
        observations.append({"signal_date": d, "exit_date": exit_d, "regime": regime,
                             "selected": ";".join(selected), "basket_return_pct": round(basket_return * 100, 4),
                             "benchmark_return_pct": round(bench_return * 100, 4),
                             "excess_return_pct": round((basket_return - bench_return) * 100, 4)})

    if not observations:
        raise RuntimeError("유효 관측치가 없습니다. fetch 시작일을 더 과거로 잡으세요.")
    active = [r for r in observations if r["regime"] == "risk_on"]
    excess = [r["excess_return_pct"] for r in active]
    basket = [r["basket_return_pct"] for r in active]
    benchmark = [r["benchmark_return_pct"] for r in active]
    summary = {
        "study": "kr_largecap_5day_predictive_signal_v1", "start": observations[0]["signal_date"],
        "end": observations[-1]["exit_date"], "horizon_trading_days": horizon, "top_n": top_n,
        "total_observations": len(observations), "risk_on_observations": len(active),
        "risk_on_ratio_pct": round(len(active) / len(observations) * 100, 2),
        "basket_mean_return_pct": round(_mean(basket), 4), "benchmark_mean_return_pct": round(_mean(benchmark), 4),
        "mean_excess_return_pct": round(_mean(excess), 4),
        "excess_win_rate_pct": round(sum(x > 0 for x in excess) / len(excess) * 100, 2) if excess else 0.0,
        "excess_return_stdev_pct": round(statistics.stdev(excess), 4) if len(excess) > 1 else 0.0,
        "universe_warning": "현 시점 대형주 고정 목록이라 생존자 편향이 있음.",
        "interpretation": "평균 초과수익이 양수여도 관측치·변동성을 함께 봐야 하며, 이 결과만으로 매매를 승인하면 안 됨.",
        "limitations": "일봉 종가 간 예측력만 검정. 10:00~14:20 체결, 장중 손절, 호가·수수료·세금·배당·상장폐지는 미포함. 주문 API 미사용.",
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _write(stamp, summary, observations)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"결과 저장: {RESULT_DIR / ('run_' + stamp + '_summary.json')}")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="KIS 일봉 기반 국내 대형주 예측력 연구 — 주문 API 미사용")
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("fetch", "run"):
        p = sub.add_parser(name)
        p.add_argument("--start", default="2015-01-01")
        p.add_argument("--end", default=date.today().isoformat())
    run_p = sub.choices["run"]
    run_p.add_argument("--top-n", type=int, default=3)
    run_p.add_argument("--horizon", type=int, default=5)
    args = ap.parse_args()
    start, end = _parse_date(args.start), _parse_date(args.end)
    if start >= end:
        raise SystemExit("--start는 --end보다 과거여야 합니다.")
    if args.command == "fetch":
        fetch_all(start, end)
    else:
        run(start, end, args.top_n, args.horizon)


if __name__ == "__main__":
    main()
