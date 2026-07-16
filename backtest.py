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
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import yaml

BASE = Path(__file__).resolve().parent
CFG = BASE / "sector_cfg.yaml"
DATA_DIR = BASE / "data" / "kis_daily"
RESULT_DIR = BASE / "results" / "backtest"
KOSPI_CACHE_CODE = "__KOSPI__"

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

# 2018년부터 존재한 국내 섹터 ETF로 구성한 별도 검증 유니버스.
# 현재 실전 종목을 과거 가격으로 소급하지 않으며, 장기 하락장에 대한 "전략 클래스" 검증에만 쓴다.
# 삼성자산운용 ETF 상품자료의 상장일을 기준으로 골랐고, 실전 성과와 절대 합산하지 않는다.
HISTORICAL_UNIVERSE = {
    "091160": {"name": "KODEX 반도체", "tag": "성장", "max_weight": .20},
    "102960": {"name": "KODEX 기계장비", "tag": "모멘텀", "max_weight": .15},
    "117680": {"name": "KODEX 철강", "tag": "모멘텀", "max_weight": .15},
    "140710": {"name": "KODEX 운송", "tag": "모멘텀", "max_weight": .15},
    "266420": {"name": "KODEX 헬스케어", "tag": "성장", "max_weight": .15},
    "305720": {"name": "KODEX 2차전지산업", "tag": "고위험", "max_weight": .05},
    "091170": {"name": "KODEX 은행", "tag": "방어", "max_weight": .15},
    "266390": {"name": "KODEX 경기소비재", "tag": "방어", "max_weight": .10},
    "266410": {"name": "KODEX 필수소비재", "tag": "방어", "max_weight": .10},
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


def fetch_kospi(start: date, end: date, token: str, cfg: dict) -> int:
    """실전 대피 규칙 검증용 KOSPI 일봉을 KIS 지수 차트 API에서 받는다."""
    old = {r["date"]: r for r in _read_csv(KOSPI_CACHE_CODE)}
    headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}",
               "appkey": cfg["app_key"], "appsecret": cfg["app_secret"], "tr_id": "FHKUP03500100"}
    cursor = end
    while cursor >= start:
        begin = max(start, cursor - timedelta(days=50))
        r = requests.get(
            "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
            headers=headers,
            params={"fid_cond_mrkt_div_code": "U", "fid_input_iscd": "0001",
                    "fid_input_date_1": begin.strftime("%Y%m%d"), "fid_input_date_2": cursor.strftime("%Y%m%d"),
                    "fid_period_div_code": "D"}, timeout=20,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"KOSPI KIS HTTP {r.status_code}: {r.text[:300]}")
        payload = r.json()
        rows = payload.get("output2") or payload.get("output") or []
        for x in rows:
            try:
                d = x.get("stck_bsop_date") or x.get("bsop_date")
                close = float(x.get("bstp_nmix_clpr") or x.get("bstp_nmix_prpr") or x.get("stck_clpr") or 0)
                if d and close > 0:
                    old[d] = {"date": d, "open": float(x.get("bstp_nmix_oprc") or close),
                              "high": float(x.get("bstp_nmix_hgpr") or close), "low": float(x.get("bstp_nmix_lwpr") or close),
                              "close": close, "value": float(x.get("acml_vol") or 0)}
            except (TypeError, ValueError):
                pass
        cursor = begin - timedelta(days=1)
        time.sleep(0.22)
    _write_csv(KOSPI_CACHE_CODE, list(old.values()))
    if not old:
        raise RuntimeError("KOSPI 일봉이 0개입니다. KIS 지수 차트 권한/응답을 확인하세요.")
    return len(old)


def fetch_all(start: date, end: date) -> None:
    cfg, token = _load_cfg(), _token(_load_cfg())
    print(f"[KIS] KOSPI: {fetch_kospi(start, end, token, cfg)}개 일봉")
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


def _ret(rows: list[dict], at: int, days: int) -> float:
    if at < days or rows[at - days]["close"] <= 0:
        return 0.0
    return (rows[at]["close"] / rows[at - days]["close"] - 1) * 100


def _select(data: dict[str, list[dict]], day: str, p: Params, cooldown: dict[str, date]) -> tuple[list[str], set[str]]:
    """실전과 같은 점수 하한·쿨다운·거래대금·과열 필터를 적용한다."""
    ranked = []
    overheat = set()
    signal_day = datetime.strptime(day, "%Y%m%d").date()
    for code, rows in data.items():
        idx = next((i for i, r in enumerate(rows) if r["date"] == day), None)
        if idx is None:
            continue
        ret20 = _ret(rows, idx, 20)
        # 손절/급락 쿨다운은 기간 동안 엄격하게 유지한다. 반등만으로 조기 해제하면
        # 손절 직후 재진입 휘핑쏘가 생기므로 실전 기본 정책과 동일하게 금지한다.
        until = cooldown.get(code)
        if until:
            if signal_day <= until:
                continue
            else:
                del cooldown[code]
        # 실전 register_cooldown_if_needed와 같은 -10% 20일 하락 쿨다운
        if ret20 <= -10.0:
            cooldown[code] = signal_day + timedelta(days=p.cooldown_days)
            continue
        score = _score(rows, idx, p)
        if score is None or score <= -5.0:
            continue
        # 실전의 상대 유동성 필터: 당일 거래대금이 최근 20일 평균의 30% 미만이면 제외.
        recent_values = [r["value"] for r in rows[max(0, idx - 19):idx + 1] if r["value"] > 0]
        if recent_values and rows[idx]["value"] < (sum(recent_values) / len(recent_values)) * 0.3:
            continue
        if _ret(rows, idx, 5) > p.overheat_pct:
            overheat.add(code)
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
    return chosen, overheat


def _score_on_day(data: dict[str, list[dict]], code: str, day: str, p: Params) -> float | None:
    rows = data[code]
    idx = next((i for i, r in enumerate(rows) if r["date"] == day), None)
    return _score(rows, idx, p) if idx is not None else None


def simulate(data: dict[str, list[dict]], p: Params, start: str, end: str, initial_cash=1_000_000,
             cash_defense: bool = False, gap_entry_block: bool = False,
             rebalance_mode: str = "adaptive") -> tuple[dict, list[dict], list[dict]]:
    """전일 종가 신호 → 당일 시가 체결. 월초 전면, 매주 월요일 하위 교체 대신
    매주 월요일 목표 바스켓으로 조정한다. 방어/인버스는 별도 검증 대상이라 포함하지 않는다."""
    calendar = sorted({r["date"] for rows in data.values() for r in rows if start <= r["date"] <= end})
    by_day = {c: {r["date"]: r for r in rows} for c, rows in data.items()}
    cash, pos, trades, curve = float(initial_cash), {}, [], []
    cooldown = {}
    last_close = {}
    peak, max_dd = cash, 0.0
    prior_equity = cash
    defense_active = False
    risk_lock_active = False
    defense_events = {"daily_kill": 0, "mdd_kill": 0, "kospi_defense": 0, "cash_defense_days": 0,
                      "gap_entry_block_days": 0}
    kospi_benchmark = {r["date"]: r for r in _read_csv(KOSPI_CACHE_CODE)}
    kospi = kospi_benchmark if (cash_defense or gap_entry_block) else {}
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
        dt = datetime.strptime(d, "%Y%m%d").date()
        resume_rebalance = False
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
                if reason == "stop_loss":
                    cooldown[code] = dt + timedelta(days=p.cooldown_days)
                trades.append({"date": d, "code": code, "name": UNIVERSE[code]["name"], "side": "SELL", "price": round(px, 2), "qty": q, "reason": reason, "pnl_pct": round((px / entry - 1) * 100 - p.fee_bps / 100, 3)})
            elif code in pos:
                pos[code] = (q, entry, max(high, bar["high"]))

        # 실전 스케줄과 동일하게 첫째 월요일은 전체 교체, 나머지 월요일은 하위 절반만
        # (후보와의 점수차 2% 이상일 때) 교체한다. 이전 버전의 매주 전체 교체는 회전율을
        # 과장해 471건이라는 결과를 만들 수 있었다.
        if defense_active:
            defense_events["cash_defense_days"] += 1
            # KOSPI 회복을 확인한 뒤 다음 월요일에만 재진입한다 (일봉 기반 보수적 근사).
            k = kospi.get(calendar[n - 1]) if n else None
            k5 = [kospi.get(calendar[i]) for i in range(max(0, n - 5), n) if kospi.get(calendar[i])]
            if k and len(k5) >= 5:
                ma5 = sum(x["close"] for x in k5) / len(k5)
                avg_value = sum(x["value"] for x in k5) / len(k5)
                prev_close = k5[-2]["close"] if len(k5) >= 2 else k["close"]
                kospi_return = (k["close"] / prev_close - 1) * 100 if prev_close else 0
                if kospi_return >= 0.5 and k["close"] >= ma5 and (not avg_value or k["value"] >= avg_value * 1.1):
                    defense_active = False
                    resume_rebalance = True
                    # 실전 check_auto_risk_recovery와 동일하게 킬/MDD 대피 뒤에는
                    # 복귀 시점 계좌값을 새 peak로 삼아 같은 과거 낙폭을 반복 발동시키지 않는다.
                    if risk_lock_active:
                        peak = cash
                        risk_lock_active = False

        # 실전과 같은 급락일 신규진입 보류: KOSPI 시가가 전일 종가보다 1.5% 이상 낮으면
        # 리밸런싱 매도/매수를 모두 건너뛰고, 기존 보유는 손절·트레일링으로만 관리한다.
        entry_blocked = False
        if gap_entry_block and n:
            k_open, k_prev = kospi.get(d), kospi.get(calendar[n - 1])
            if k_open and k_prev and k_prev["close"]:
                entry_blocked = (k_open["open"] / k_prev["close"] - 1) * 100 <= -1.5
        if entry_blocked and dt.weekday() == 0:
            defense_events["gap_entry_block_days"] += 1

        is_monthly = dt.day <= 7
        is_quarterly = is_monthly and dt.month in (1, 4, 7, 10)
        scheduled = (dt.weekday() == 0 and n > 0 and (
            rebalance_mode == "adaptive" or
            (rebalance_mode == "monthly" and is_monthly) or
            (rebalance_mode == "quarterly" and is_quarterly)
        ))
        if not defense_active and not entry_blocked and (scheduled or resume_rebalance) and n > 0:
            target, overheat = _select(data, calendar[n - 1], p, cooldown)
            current = list(pos)
            if rebalance_mode != "adaptive" or is_monthly or resume_rebalance:
                desired = target
                to_sell = [c for c in current if c not in desired]
                reason = "defense_resume" if resume_rebalance else f"{rebalance_mode}_rebalance"
            else:
                desired = list(current)
                held_scores = sorted(
                    ((c, _score_on_day(data, c, calendar[n - 1], p)) for c in current),
                    key=lambda x: x[1] if x[1] is not None else float("-inf"),
                )
                candidates = [c for c in target if c not in current]
                to_sell = []
                for old, old_score in held_scores[:max(1, len(held_scores) // 2)]:
                    if not candidates or old_score is None:
                        continue
                    new = candidates[0]
                    new_score = _score_on_day(data, new, calendar[n - 1], p)
                    if new_score is not None and new_score - old_score >= 2.0:
                        desired.remove(old)
                        desired.append(new)
                        to_sell.append(old)
                        candidates.pop(0)
                reason = "weekly_rotation"
            for code in to_sell:
                if bars.get(code):
                    sell(code, bars[code], reason)
            buyable = [c for c in desired if c not in pos and bars.get(c) and bars[c]["open"] > 0]
            equity_now = cash + sum(q * (bars[c]["open"] if bars.get(c) else last_close.get(c, entry)) for c, (q, entry, _) in pos.items())
            # 실전의 KOFR 최소 15% 유보를 현금으로 재현한다. KOFR 자체 수익률은 별도 방어
            # 검증 단계에서 추가한다.
            budget = equity_now * (1 - p.kofr_reserve) / max(1, len(target))
            for code in buyable:
                px = bars[code]["open"]
                code_budget = budget * 0.5 if code in overheat else budget
                qty = int((min(code_budget, cash) * (1 - fee)) // px)
                if qty <= 0:
                    continue
                cost = qty * px * (1 + fee)
                cash -= cost
                pos[code] = (qty, px, px)
                trades.append({"date": d, "code": code, "name": UNIVERSE[code]["name"], "side": "BUY", "price": round(px, 2), "qty": qty, "reason": reason, "pnl_pct": ""})

        for c, bar in bars.items():
            if bar:
                last_close[c] = bar["close"]
        equity = cash + sum(q * (bars[c]["close"] if bars.get(c) else last_close.get(c, entry)) for c, (q, entry, _) in pos.items())
        # 일봉에서는 장중 트리거 시점과 체결가를 알 수 없으므로 종가 전량 현금화만 근사한다.
        dd_before_exit = (equity / peak - 1) * 100 if peak else 0.0
        daily_return = (equity / prior_equity - 1) * 100 if prior_equity else 0.0
        k = kospi.get(d)
        kospi_return = 0.0
        if k and n:
            prev_k = kospi.get(calendar[n - 1])
            if prev_k and prev_k["close"]:
                kospi_return = (k["close"] / prev_k["close"] - 1) * 100
        trigger = ""
        if cash_defense and pos:
            if daily_return <= -4.0:
                trigger = "daily_kill"
            elif dd_before_exit <= -10.0:
                trigger = "mdd_kill"
            elif kospi_return <= -2.0 and daily_return <= -3.0:
                trigger = "kospi_defense"
        if trigger:
            for code, (q, entry, _) in list(pos.items()):
                bar = bars.get(code)
                px = bar["close"] if bar else last_close.get(code, entry)
                cash += q * px * (1 - fee)
                trades.append({"date": d, "code": code, "name": UNIVERSE[code]["name"], "side": "SELL",
                               "price": round(px, 2), "qty": q, "reason": trigger,
                               "pnl_pct": round((px / entry - 1) * 100 - p.fee_bps / 100, 3)})
                pos.pop(code)
            equity = cash
            defense_active = True
            if trigger in ("daily_kill", "mdd_kill"):
                risk_lock_active = True
            defense_events[trigger] += 1
        peak = max(peak, equity)
        max_dd = min(max_dd, (equity / peak - 1) * 100)
        curve.append({"date": d, "equity": round(equity, 2), "cash": round(cash, 2), "drawdown_pct": round((equity / peak - 1) * 100, 3), "holdings": ";".join(sorted(pos))})
        prior_equity = equity

    years = max(1 / 252, len(calendar) / 252)
    final = curve[-1]["equity"] if curve else initial_cash
    sell_dates, stop_dates, quick_reentries, stop_reentries = {}, {}, 0, 0
    for trade in trades:
        code = trade["code"]
        trade_day = datetime.strptime(trade["date"], "%Y%m%d").date()
        if trade["side"] == "SELL":
            sell_dates[code] = trade_day
            if trade["reason"] == "stop_loss":
                stop_dates[code] = trade_day
        elif code in sell_dates and (trade_day - sell_dates[code]).days <= 14:
            quick_reentries += 1
            if code in stop_dates and (trade_day - stop_dates[code]).days <= 14:
                stop_reentries += 1
    gross_notional = sum(float(t["price"]) * int(t["qty"]) for t in trades)
    benchmark_rows = [kospi_benchmark[d] for d in calendar if d in kospi_benchmark]
    benchmark = {}
    if len(benchmark_rows) >= 2:
        b_start, b_final = benchmark_rows[0]["close"], benchmark_rows[-1]["close"]
        b_peak, b_mdd = b_start, 0.0
        for bar in benchmark_rows:
            b_peak = max(b_peak, bar["close"])
            b_mdd = min(b_mdd, (bar["close"] / b_peak - 1) * 100)
        b_years = max(1 / 252, len(benchmark_rows) / 252)
        benchmark = {"kospi_price_return_pct": round((b_final / b_start - 1) * 100, 3),
                     "kospi_price_cagr_pct": round(((b_final / b_start) ** (1 / b_years) - 1) * 100, 3),
                     "kospi_price_mdd_pct": round(b_mdd, 3)}
    summary = {"universe": "historical" if set(UNIVERSE) == set(HISTORICAL_UNIVERSE) else "live",
               "start": start, "end": end, "trading_days": len(calendar), "initial_cash": initial_cash,
               "final_equity": round(final, 2), "total_return_pct": round((final / initial_cash - 1) * 100, 3),
               "cagr_pct": round(((final / initial_cash) ** (1 / years) - 1) * 100, 3), "mdd_pct": round(max_dd, 3),
               "trade_count": len(trades), "turnover_pct_initial": round(gross_notional / initial_cash * 100, 2),
               "estimated_cost_krw": round(gross_notional * fee, 2),
               "estimated_cost_pct_initial": round(gross_notional * fee / initial_cash * 100, 3),
               "estimated_cost_20bps_krw": round(gross_notional * 0.002, 2),
               "estimated_cost_30bps_krw": round(gross_notional * 0.003, 2),
               "reentry_within_14d": quick_reentries, "stop_reentry_within_14d": stop_reentries,
               "cash_defense": cash_defense, "gap_entry_block": gap_entry_block,
               "rebalance_mode": rebalance_mode, **benchmark, **defense_events,
               "limitations": "일봉 기반: 장중 고가/저가 순서는 알 수 없어 트레일은 전일까지 확정된 고점만 사용. cash_defense는 킬스위치/MDD/KOSPI 디펜스를 종가 전량 현금화와 다음 월요일 재진입으로 근사한다. 인버스·장중 체결·호가 스프레드는 미포함. 현 유니버스의 신규 상장 종목 때문에 2025년 이전 동일 유니버스 검증은 불가.", **p.__dict__}
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


def _parameter_candidates():
    """폭을 넓히기보다 인접한 합리적 후보만 비교한다. 데이터 스누핑을 줄이기 위함."""
    for short, long, top_n, stop, ts, gap in itertools.product(
        (10, 20, 30), (40, 60, 90), (3, 5), (-3.0, -5.0, -7.0), (4.0, 6.0, 8.0), (2.0, 3.0)
    ):
        if short < long:
            yield Params(short=short, long=long, top_n=top_n, stop_loss=stop, trail_start=ts, trail_gap=gap)


def _quality(summary: dict) -> float:
    """수익률 하나가 아니라 MDD를 강하게 벌점으로 준 선택 점수."""
    return summary["cagr_pct"] / max(abs(summary["mdd_pct"]), 1.0)


def walkforward(data: dict[str, list[dict]], train_start: str, train_end: str, test_start: str, test_end: str, top_k: int,
                cash_defense: bool = False, gap_entry_block: bool = False, fee_bps: float = 10.0):
    in_sample = []
    for candidate in _parameter_candidates():
        p = replace(candidate, fee_bps=fee_bps)
        result, _, _ = simulate(data, p, train_start, train_end, cash_defense=cash_defense,
                                gap_entry_block=gap_entry_block)
        result["selection_score"] = round(_quality(result), 4)
        in_sample.append((result, p))
    in_sample.sort(key=lambda x: x[0]["selection_score"], reverse=True)
    rows = []
    for rank, (ins, p) in enumerate(in_sample[:top_k], 1):
        oos, _, _ = simulate(data, p, test_start, test_end, cash_defense=cash_defense,
                             gap_entry_block=gap_entry_block)
        rows.append({
            "rank_in_sample": rank, "selection_score": ins["selection_score"],
            "is_cagr_pct": ins["cagr_pct"], "is_mdd_pct": ins["mdd_pct"], "is_trade_count": ins["trade_count"],
            "oos_cagr_pct": oos["cagr_pct"], "oos_mdd_pct": oos["mdd_pct"], "oos_total_return_pct": oos["total_return_pct"],
            "oos_trade_count": oos["trade_count"], **p.__dict__,
        })
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULT_DIR / f"walkforward_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    print(f"워크포워드 결과: {path}")
    for row in rows:
        print(row)


def rolling_walkforward(data: dict[str, list[dict]], start: date, end: date, train_years: int, test_years: int,
                        fee_bps: float) -> None:
    """각 학습 구간에서만 1위를 고른 뒤 바로 다음 구간에 한 번만 적용한다.
    OOS 결과를 보고 후보를 다시 고르는 데이터 스누핑을 막기 위한 장기 검증이다.
    """
    if train_years < 2 or test_years < 1:
        raise ValueError("train-years는 2 이상, test-years는 1 이상이어야 합니다.")
    rows = []
    test_start = date(start.year + train_years, 1, 1)
    while test_start <= end:
        test_end = min(date(test_start.year + test_years, 1, 1) - timedelta(days=1), end)
        train_start = date(test_start.year - train_years, 1, 1)
        candidates = []
        for candidate in _parameter_candidates():
            p = replace(candidate, fee_bps=fee_bps)
            ins, _, _ = simulate(data, p, train_start.strftime("%Y%m%d"),
                                  (test_start - timedelta(days=1)).strftime("%Y%m%d"))
            candidates.append((ins, p))
        candidates.sort(key=lambda item: _quality(item[0]), reverse=True)
        ins, p = candidates[0]
        oos, _, _ = simulate(data, p, test_start.strftime("%Y%m%d"), test_end.strftime("%Y%m%d"))
        rows.append({
            "train_start": train_start.isoformat(), "train_end": (test_start - timedelta(days=1)).isoformat(),
            "test_start": test_start.isoformat(), "test_end": test_end.isoformat(),
            "selection_score": round(_quality(ins), 4), "is_cagr_pct": ins["cagr_pct"], "is_mdd_pct": ins["mdd_pct"],
            "oos_cagr_pct": oos["cagr_pct"], "oos_mdd_pct": oos["mdd_pct"],
            "oos_total_return_pct": oos["total_return_pct"], "oos_trade_count": oos["trade_count"], **p.__dict__,
        })
        test_start = test_end + timedelta(days=1)
    if not rows:
        raise RuntimeError("롤링 검증 구간이 없습니다. start/end 또는 train-years를 확인하세요.")
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULT_DIR / f"rolling_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    print(f"롤링 워크포워드 결과: {path}")
    for row in rows:
        print(row)


def main():
    ap = argparse.ArgumentParser(description="KIS 일봉 전용 백테스트 — 주문 API 미사용")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("fetch", "run", "grid", "walkforward", "rolling"):
        s = sub.add_parser(name)
        s.add_argument("--start", default="2023-01-01")
        s.add_argument("--end", default=date.today().isoformat())
        s.add_argument("--universe", choices=("live", "historical"), default="live",
                       help="live: 실전 ETF / historical: 2018년부터 존재한 섹터 ETF 검증군")
        s.add_argument("--fee-bps", type=float, default=10.0, help="편도 비용+슬리피지 가정 (기본 10bp)")
        if name == "walkforward":
            s.add_argument("--cash-defense", action="store_true",
                           help="Include daily cash-defense approximation")
            s.add_argument("--gap-entry-block", action="store_true",
                           help="Block Monday rebalancing after a KOSPI gap down")
            s.add_argument("--train-end", default="2025-12-31")
            s.add_argument("--test-start", default="2026-01-01")
            s.add_argument("--top-k", type=int, default=5)
        elif name == "rolling":
            s.add_argument("--train-years", type=int, default=3)
            s.add_argument("--test-years", type=int, default=1)
        elif name == "run":
            s.add_argument("--cash-defense", action="store_true",
                           help="Include daily cash-defense approximation")
            s.add_argument("--gap-entry-block", action="store_true",
                           help="Block Monday rebalancing after a KOSPI gap down")
            s.add_argument("--rebalance-mode", choices=("adaptive", "monthly", "quarterly"), default="adaptive")
            s.add_argument("--short", type=int, default=20)
            s.add_argument("--long", type=int, default=60)
            s.add_argument("--top-n", type=int, default=5)
            s.add_argument("--stop-loss", type=float, default=-5.0)
            s.add_argument("--trail-start", type=float, default=4.0)
            s.add_argument("--trail-gap", type=float, default=2.0)
    args = ap.parse_args()
    global UNIVERSE
    if args.universe == "historical":
        UNIVERSE = HISTORICAL_UNIVERSE
    else:
        validate_universe()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if args.cmd == "fetch":
        fetch_all(start, end); return
    data = {c: _read_csv(c) for c in UNIVERSE}
    missing = [c for c, rows in data.items() if not rows]
    if missing:
        raise SystemExit("데이터 없음: " + ", ".join(missing) + "\n먼저: python3 backtest.py fetch --start " + args.start)
    if args.cmd == "run":
        p = Params(short=args.short, long=args.long, top_n=args.top_n, stop_loss=args.stop_loss,
                   trail_start=args.trail_start, trail_gap=args.trail_gap, fee_bps=args.fee_bps)
        if p.short >= p.long or p.top_n < 1:
            raise SystemExit("--short는 --long보다 작고, --top-n은 1 이상이어야 합니다.")
        _save_result(*simulate(data, p, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"),
                               cash_defense=args.cash_defense, gap_entry_block=args.gap_entry_block,
                               rebalance_mode=args.rebalance_mode))
        return
    if args.cmd == "walkforward":
        walkforward(data, start.strftime("%Y%m%d"), date.fromisoformat(args.train_end).strftime("%Y%m%d"),
                    date.fromisoformat(args.test_start).strftime("%Y%m%d"), end.strftime("%Y%m%d"), args.top_k,
                    cash_defense=args.cash_defense, gap_entry_block=args.gap_entry_block, fee_bps=args.fee_bps)
        return
    if args.cmd == "rolling":
        rolling_walkforward(data, start, end, args.train_years, args.test_years, args.fee_bps)
        return
    summaries = []
    for candidate in _parameter_candidates():
        p = replace(candidate, fee_bps=args.fee_bps)
        summary, _, _ = simulate(data, p, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
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
