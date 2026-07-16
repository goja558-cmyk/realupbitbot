"""
==============================================================
  섹터 로테이션 자동매매 봇 v1.0
  전략서 기반 구현 — 기존 kis_bot.py KIS API 인프라 활용

  ▶ 전략 요약:
    - ETF 10종 중 모멘텀 상위 2~3개만 보유
    - 20일+60일 수익률 가중 모멘텀 스코어
    - 킬 스위치 / MDD -10% 전량 KOFR 대피
    - 트레일링 스탑 (+6% 후 고점 -2%)
    - 손절 -5%
    - 10만원 소액 테스트 — 종목당 최소 단위 분배

  ▶ 실행:
    python sector_bot.py

  ▶ 설정 파일: sector_cfg.yaml (아래 양식 참고)
    app_key:        "여기에_KIS_APP_KEY"
    app_secret:     "여기에_KIS_APP_SECRET"
    account_no:     "12345678"     # 계좌번호 앞 8자리
    account_suffix: "01"           # 계좌번호 뒤 2자리
    telegram_token: "봇토큰"
    chat_id:        "내채팅ID"
    mock:           true           # true=모의투자 false=실투자
    total_budget:   100000         # 전체 예산(원)
==============================================================
"""

BOT_VERSION = "1.0"
BOT_NAME    = "섹터로테이션봇"
BOT_TAG     = "📊 섹터"

import os, sys, time, json, yaml, requests, threading, math, traceback, csv
from datetime import datetime, date, timedelta
from colorama import Fore, Style, init as colorama_init

colorama_init(autoreset=True)

# ============================================================
# [1] 경로 설정
# ============================================================
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CFG_FILE      = os.path.join(BASE_DIR, "sector_cfg.yaml")
LOG_FILE      = os.path.join(BASE_DIR, "sector_trade_log.csv")
STATE_FILE    = os.path.join(BASE_DIR, "sector_state.json")
SHARED_DIR    = os.path.join(BASE_DIR, "shared")
LOG_DIR       = os.path.join(BASE_DIR, "logs", "sector")
MARKET_LOG_DIR = os.path.join(LOG_DIR, "market_snapshots")
os.makedirs(SHARED_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(MARKET_LOG_DIR, exist_ok=True)


def cprint(msg, color=Fore.WHITE, bright=False):
    prefix = Style.BRIGHT if bright else ""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{prefix}{color}[{ts}] {msg}{Style.RESET_ALL}")


# ============================================================
# [2] ETF 유니버스 (전략서 4.1)
# ============================================================
ETF_UNIVERSE = {
    # listed_on은 백테스트/운영 점검용이다. KIS 일봉이 존재하는 실제 거래일부터만
    # 모멘텀을 계산하며, 상장 전 수익률을 구성종목으로 소급 합성하지 않는다.
    "396500": {"name": "TIGER 반도체TOP10",    "max_weight": 0.25, "tag": "성장",   "listed_on": "2021-08-10"},
    "449450": {"name": "PLUS K방산",           "max_weight": 0.15, "tag": "모멘텀", "listed_on": "2023-01-05"},
    "494670": {"name": "TIGER 조선TOP10",      "max_weight": 0.15, "tag": "모멘텀", "listed_on": "2024-10-22"},
    "143860": {"name": "TIGER 헬스케어",       "max_weight": 0.15, "tag": "방어",   "listed_on": "2011-07-18"},
    "305720": {"name": "KODEX 2차전지산업",    "max_weight": 0.05, "tag": "고위험", "listed_on": "2018-09-12"},
    "445290": {"name": "KODEX 로봇액티브",     "max_weight": 0.05, "tag": "고위험", "listed_on": "2022-11-15"},
    "434730": {"name": "HANARO 원자력iSelect", "max_weight": 0.15, "tag": "성장",   "listed_on": "2022-06-27"},
    "091170": {"name": "KODEX 은행",           "max_weight": 0.15, "tag": "방어",   "listed_on": "2006-06-27"},
    "227560": {"name": "TIGER 200 생활소비재", "max_weight": 0.15, "tag": "방어",   "listed_on": "2015-10-07"},

    # 이전 버전의 잘못된 매핑으로 이미 보유했을 수 있는 코드다. 잔고 동기화·청산은
    # 계속 지원하지만 새 리밸런싱 매수 후보에서는 제외한다.
    "152100": {"name": "PLUS 200 (레거시 청산전용)", "max_weight": 0.0, "tag": "레거시", "listed_on": "2012-01-10", "trade_enabled": False},
    "381170": {"name": "TIGER 미국테크TOP10 INDXX (레거시 청산전용)", "max_weight": 0.0, "tag": "레거시", "listed_on": "2021-04-08", "trade_enabled": False},
}
KOFR_CODE    = "449170"
KOFR_NAME    = "TIGER KOFR금리액티브"
INVERSE_CODE = "114800"
INVERSE_NAME = "KODEX 인버스"

# ============================================================
# [3] 전략 파라미터
# ============================================================
MOMENTUM_DAYS_SHORT  = 20     # 단기 모멘텀 기간
MOMENTUM_DAYS_LONG   = 60     # 장기 모멘텀 기간
MOMENTUM_W_SHORT     = 0.5
MOMENTUM_W_LONG      = 0.5
TOP_N_ETF            = 5      # 상위 몇 개 편입 (30만원 기준 5개)
MIN_SCORE_THRESHOLD  = -5.0   # 이하면 대피 (0% → -5%로 완화)

TRAIL_START_PCT  = 4.0        # 트레일링 스탑 시작 수익률 (6%→4%)
TRAIL_GAP_PCT    = 2.0        # 트레일링 스탑 간격 (%)
STOP_LOSS_PCT    = -5.0       # 손절 (%)
MAX_DD_PCT       = -10.0      # 계좌 MDD 한도 (%)
KILL_DAY_LOSS    = -4.0       # 킬 스위치 일일 손실 (-3%→-4% 완화)

# ── 대피 단계 파라미터 ─────────────────────────────────────
D1_KOSPI_PCT   = -2.0   # NORMAL→DEFENSE_1 코스피 조건 (-1.5%→-2% 완화)
D1_PF_PCT      = -3.0   # NORMAL→DEFENSE_1 포트폴리오 수익률 조건 (-2%→-3% 완화)
D2_KOSPI_PCT   = -2.0   # DEFENSE_1→2 코스피 조건
D2_CONSEC      = 2      # DEFENSE_1→2 연속하락일 조건
D3_KOSPI_PCT   = -3.0   # DEFENSE_2→3 코스피 조건
D3_CONSEC      = 3      # DEFENSE_2→3 연속하락일 조건

D_DOWN_3TO2    = -1.5   # DEFENSE_3→2 완화 조건 (코스피)
D_DOWN_2TO1    = -0.5   # DEFENSE_2→1 완화 조건 (코스피)
D_DOWN_1WAIT   =  0.0   # DEFENSE_1→복귀대기 조건 (코스피)

INVERSE_TRAIL_GAP   = 2.0    # 인버스 트레일링 스탑 간격 (%)
INVERSE_STOP_LOSS   = -4.0   # 인버스 손절 (%)

RESUME_KOSPI_PCT    =  0.5   # 자동 복귀 코스피 조건 (+1%→+0.5% 완화)
RESUME_VOL_RATIO    =  1.1   # 자동 복귀 거래량 비율 (1.2→1.1 완화)
OVERHEAT_PCT     = 15.0       # 과열 필터: 5일 수익률 초과 시 비중 축소 (제외→축소)

COOLDOWN_DAYS    = 14         # 섹터 쿨다운 일수

TRADE_START_H    = 9
TRADE_START_M    = 10
TRADE_BUY_END_H  = 15
TRADE_BUY_END_M  = 20
TRADE_SELL_END_H = 15
TRADE_SELL_END_M = 25
NO_BUY_FRIDAY    = True

MIN_VOLUME_KRW   = 5_000_000_000  # 일 거래대금 50억
MAX_SPREAD_PCT   = 0.5
KOFR_MIN_RATIO   = 0.15           # KOFR 상시 최소 비중
MAX_ETF_PER_TAG  = 2              # 동일 테마 집중 한도
MARKET_SNAPSHOT_INTERVAL = 30 * 60 # 장중 ETF 상태 기록 주기 (초)

# ============================================================
# [4] 전역 상태
# ============================================================
_cfg             = {}
TELEGRAM_TOKEN   = ""
CHAT_ID          = ""
TOTAL_BUDGET     = 100_000
IS_MOCK          = True
AUTO_ENTRY_ENABLED = True
AUTO_RISK_RECOVERY_ENABLED = True
AUTO_RISK_RECOVERY_DAYS = 1

portfolio        = {}     # {code: {qty, avg_price, high_price, entry_date}}
cooldown_list    = {}     # {code: date until}
kill_switch_active = False
mdd_active       = False
peak_value       = 0.0
initial_value    = 0.0
daily_pnl_krw    = 0
daily_loss_base  = 100_000
last_reset_day   = None
trade_count      = 0
manual_pause     = False  # /kill 또는 /stop은 운영자만 해제하는 하드 정지
risk_lock_day    = None   # 자동 킬/MDD가 걸린 날짜

# ── 대피 시스템 상태 ──────────────────────────────────────
# defense_stage: NORMAL / DEFENSE_1 / DEFENSE_2 / DEFENSE_3 / DEFENSE_WAIT
defense_stage        = "NORMAL"
kospi_baseline       = 0.0      # 당일 장 시작 시 코스피 기준가
_kospi_baseline_set  = False    # 당일 기준가 캡처 여부
consecutive_down_days = 0       # 코스피 연속 하락일수
inverse_peak_return  = 0.0      # 인버스 보유 중 고점 수익률
defense_down_date    = None     # 단계 하향 조건 충족 시작일 (1일 유지 확인)
# 로그용 시장 국면 값. 국면 판별 기능이 비활성/미구현이어도 방어·주문 로깅은
# 절대 중단되지 않아야 하므로 안전한 기본값을 둔다.
_market_regime       = ""
_last_update_id  = 0
_tg_lock         = threading.Lock()
_defense_lock    = threading.Lock()   # 대피 단계 진입 중복 방지
_order_lock      = threading.Lock()   # 동일 종목 반대매매 포함 중복 방지
_orders_in_flight = set()
_pending_orders = {}  # {code: {side, qty, previous_qty, ts}} — 접수 후 실제 잔고 반영 대기
_market_snapshot_lock = threading.Lock()
_account_action_lock = threading.RLock()  # 리밸런싱·방어·복귀의 계좌 단위 직렬화
_last_main_error_alert = 0.0
_last_api_error_alert = {}


def _account_action(fn):
    """서로 다른 스레드가 동시에 계좌 비중을 바꾸지 않게 한다."""
    def wrapped(*args, **kwargs):
        if not _account_action_lock.acquire(blocking=False):
            cprint(f"[계좌 작업 보류] {fn.__name__} — 다른 계좌 작업 진행 중", Fore.YELLOW)
            return None
        try:
            return fn(*args, **kwargs)
        finally:
            _account_action_lock.release()
    return wrapped

# ── /hold 수동 포지션 ─────────────────────────────────────
# {code: {qty, avg_price, high_price, entry_date,
#          sl_type, sl_val,          # 손절: "pct"/-5.0 or "price"/48000
#          tp_type, tp_val,          # 익절: "trail"/"trail_be"/"pct"/"price"
#          trail_start, trail_gap,   # 트레일링 시작%, 간격%
#          be_pct, be_active,        # 본절 기준%, 활성화여부
#          name}}
hold_positions = {}
_hold_session  = {}   # 등록 단계별 임시 저장

# KIS 인증
_access_token    = ""
_token_expire    = 0.0
_TOKEN_FILE      = os.path.join(BASE_DIR, "shared", "kis_token.json")


def _save_kis_token():
    """발급된 토큰을 파일에 저장 — 재시작/장외에도 재사용 가능."""
    try:
        tmp = _TOKEN_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"token": _access_token, "expire": _token_expire}, f)
        os.replace(tmp, _TOKEN_FILE)
    except Exception as e:
        cprint(f"[토큰 저장 오류] {e}", Fore.YELLOW)


def _load_kis_token():
    """파일에서 토큰 복원 — 유효기간 내면 재사용."""
    global _access_token, _token_expire
    if not os.path.exists(_TOKEN_FILE):
        return
    try:
        with open(_TOKEN_FILE) as f:
            data = json.load(f)
        token  = data.get("token", "")
        expire = float(data.get("expire", 0))
        if token and time.time() < expire:
            _access_token = token
            _token_expire = expire
            remain = (expire - time.time()) / 3600
            cprint(f"✅ KIS 토큰 파일 복원 (잔여 {remain:.1f}h)", Fore.CYAN)
        else:
            cprint("[토큰] 파일 토큰 만료 — 재발급 필요", Fore.YELLOW)
    except Exception as e:
        cprint(f"[토큰 복원 오류] {e}", Fore.YELLOW)

# Rate limit
_bucket_tokens   = 4.0
_bucket_last     = time.time()
_BUCKET_CAP      = 4
_BUCKET_RATE     = 3.0

# ============================================================
# [4-B] IPC: 매니저 ↔ 섹터봇 통신
#   - upbit_bot.py 와 동일한 패턴 사용
#   - cmd 파일명: shared/cmd_stock.json  (manager가 stock 워커에 쓰는 경로)
#   - result 파일명: shared/result_stock.json
# ============================================================
_IPC_CMD_FILE    = None
_IPC_RESULT_FILE = None
_IPC_REQ_ID      = ""
_is_ipc_context  = False
_IPC_THREAD_STARTED = False
_MANAGER_PID_FILE   = os.path.join(SHARED_DIR, "manager.pid")


def _init_ipc():
    global _IPC_CMD_FILE, _IPC_RESULT_FILE
    _IPC_CMD_FILE    = os.path.join(SHARED_DIR, "cmd_stock.json")
    _IPC_RESULT_FILE = os.path.join(SHARED_DIR, "result_stock.json")


def _manager_is_running():
    if not os.path.exists(_MANAGER_PID_FILE):
        return False
    try:
        with open(_MANAGER_PID_FILE) as f:
            pid = int(f.read().strip())
        # /proc는 Linux 전용이다. Windows에서는 tasklist로 PID 생존 여부를 확인한다.
        if os.name == "nt":
            import subprocess
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=3,
            )
            return str(pid) in result.stdout
        return os.path.exists(f"/proc/{pid}")
    except Exception:
        return False


def _get_result_file():
    if not _IPC_RESULT_FILE:
        return None
    if _IPC_REQ_ID:
        base = _IPC_RESULT_FILE.replace(".json", "")
        return f"{base}_{_IPC_REQ_ID}.json"
    return _IPC_RESULT_FILE


def _atomic_write_result(data: dict):
    rf = _get_result_file()
    if not rf:
        return
    tmp = rf + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, rf)
    except Exception as e:
        cprint(f"[IPC 쓰기 오류] {e}", Fore.YELLOW)


def _write_ipc_result(result_text, keyboard=None):
    """IPC 컨텍스트일 때만 결과 파일에 씀."""
    if not _is_ipc_context:
        return
    _atomic_write_result({"result": result_text, "keyboard": keyboard, "ts": time.time()})


def _start_ipc_thread():
    """매니저 명령을 0.3초마다 폴링하는 스레드."""
    global _IPC_THREAD_STARTED
    if _IPC_THREAD_STARTED:
        return
    _IPC_THREAD_STARTED = True

    def _ipc_loop():
        while True:
            try:
                if _IPC_CMD_FILE and os.path.exists(_IPC_CMD_FILE):
                    tmp = _IPC_CMD_FILE + ".read"
                    try:
                        os.rename(_IPC_CMD_FILE, tmp)
                    except OSError:
                        time.sleep(0.3)
                        continue
                    try:
                        with open(tmp, encoding="utf-8") as f:
                            data = json.load(f)
                        os.remove(tmp)
                        cmd_text = data.get("cmd", "")
                        req_id   = data.get("req_id", "")
                        if cmd_text:
                            cprint(f"[IPC] {cmd_text}", Fore.CYAN)
                            globals()["_IPC_REQ_ID"]     = req_id
                            globals()["_is_ipc_context"] = True
                            try:
                                _handle_ipc_cmd(cmd_text)
                            finally:
                                globals()["_is_ipc_context"] = False
                                globals()["_IPC_REQ_ID"]     = ""
                    except Exception as e:
                        cprint(f"[IPC 처리 오류] {e}", Fore.YELLOW)
                        try:
                            os.remove(tmp)
                        except Exception:
                            pass
            except Exception as e:
                cprint(f"[IPC 루프 오류] {e}", Fore.YELLOW)
            time.sleep(0.3)

    t = threading.Thread(target=_ipc_loop, daemon=True, name="ipc-sector")
    t.start()
    cprint("✅ IPC 스레드 시작 (cmd_stock.json 감시 중)", Fore.CYAN)


def _handle_ipc_cmd(text):
    """매니저에서 받은 명령 처리 → _write_ipc_result로 응답."""
    global kill_switch_active, mdd_active, peak_value, manual_pause, daily_loss_base
    # 매니저가 Telegram 업데이트를 단독 수신한다. /hold의 인라인 버튼과
    # 숫자 입력은 일반 명령과 구분해 현재 hold 세션에 전달한다.
    if text.startswith("__hold_callback__ "):
        _handle_callback(text.removeprefix("__hold_callback__ ").strip())
        return
    if text.startswith("__hold_text__ "):
        _hold_text_input(text.removeprefix("__hold_text__ ").strip())
        return

    cmd = text.strip().split()
    if not cmd:
        return
    c = cmd[0].lower()

    if c in ("/status", "/s", "/상태"):
        _ipc_send_status()
    elif c in ("/portfolio", "/p", "/포트"):
        _ipc_send_portfolio()
    elif c in ("/scores", "/score", "/스코어"):
        _ipc_send_scores()
    elif c in ("/snapshot", "/시장기록"):
        threading.Thread(target=_log_market_snapshot, kwargs={"trigger": "manual"}, daemon=True).start()
        _write_ipc_result("[normal] 📝 ETF 시장 상태 기록을 시작했습니다. logs/sector/market_snapshots 폴더를 확인하세요.")
    elif c in ("/why", "/왜", "/왜안사"):
        holdings = list(portfolio.keys())
        stage = defense_stage
        scores = get_all_scores() or {}
        ranked = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)
        lines = [
            f"🔍 왜 안사? [{stage}]",
            f"━━━━━━━━━━━━━━━━━━━━",
        ]
        if stage != "NORMAL":
            lines.append(f"❌ 대피 단계: {stage} → 매수 불가")
        elif kill_switch_active:
            lines.append("❌ 킬스위치 ON → 매수 불가")
        elif mdd_active:
            lines.append("❌ MDD 한도 초과 → 매수 불가")
        else:
            lines.append("✅ 매수 가능 상태")
        lines.append("─────────────────")
        if ranked:
            lines.append("📊 모멘텀 스코어 상위 3개:")
            for i, (code, d) in enumerate(ranked[:3], 1):
                held = "📦" if code in holdings else ""
                cd = "⏸쿨다운" if code in cooldown_list else ""
                lines.append(f"{i}. {ETF_UNIVERSE.get(code,{}).get('name',code)}{held}{cd}: {d['score']:+.1f}%")
        _write_ipc_result("[normal] " + "\n".join(lines))
    elif c in ("/bollinger", "/bb"):
        holdings = list(portfolio.keys())
        msg = "\n\n".join(get_bollinger_status(c) for c in holdings) if holdings else "보유없음"
        _write_ipc_result("[normal] " + msg)
    elif c in ("/investor", "/수급"):
        holdings = list(portfolio.keys())
        msg = "\n\n".join(get_investor_status(c) for c in holdings) if holdings else "보유없음"
        _write_ipc_result("[normal] " + msg)
    elif c in ("/rebalance", "/r", "/리밸"):
        _write_ipc_result("[normal] 🔄 리밸런싱 시작 (백그라운드)...")
        threading.Thread(target=_do_rebalance, daemon=True).start()
    elif c in ("/kofr", "/대피"):
        _write_ipc_result("[critical] 🚨 DEFENSE_1 대피 실행 중...")
        threading.Thread(target=lambda: _enter_defense_1("매니저 명령"), daemon=True).start()
    elif c in ("/resume", "/복귀"):
        _write_ipc_result("[normal] 🟢 복귀 실행 중...")
        threading.Thread(target=_do_resume, daemon=True).start()
    elif c in ("/defense", "/대피단계"):
        _write_ipc_result(f"[normal] 🛡 현재 대피 단계: {defense_stage}")
    elif c in ("/kill", "/킬"):
        kill_switch_active = True
        manual_pause = True
        _save_state()
        _write_ipc_result("[critical] 🔴 킬 스위치 ON")
    elif c in ("/unkill", "/킬해제"):
        kill_switch_active = False
        manual_pause = False
        _save_state()
        _write_ipc_result("[normal] 🟢 킬 스위치 OFF — 매매 재개")
    elif c in ("/start",):
        kill_switch_active = False
        mdd_active = False
        manual_pause = False
        # 재시작 직후 TOTAL_BUDGET을 당일 기준액으로 쓰면, 실제 계좌 규모가
        # 예산과 다른 경우 가짜 일일 손실로 킬 스위치가 즉시 발동한다.
        actual = _calc_account_value()
        if actual is not None and actual > 0:
            daily_loss_base = actual
        _save_state()
        _write_ipc_result("[normal] 🟢 섹터봇 재개됨")
    elif c in ("/stop",):
        kill_switch_active = True
        manual_pause = True
        _save_state()
        _write_ipc_result("[critical] ⏹ 섹터봇 정지 (킬스위치 ON)")
    elif c in ("/sync", "/싱크"):
        real = get_kis_holdings()
        if real is None:
            _write_ipc_result("[normal] ⚠️ KIS 잔고 조회 실패")
            return
        # ETF 유니버스 + KOFR만 반영
        valid_codes = set(ETF_UNIVERSE.keys()) | {KOFR_CODE}
        new_portfolio = {}
        for code, pos in real.items():
            if code in valid_codes:
                old = portfolio.get(code, {})
                new_portfolio[code] = {
                    "qty":        pos["qty"],
                    "avg_price":  pos["avg_price"],
                    "high_price": max(old.get("high_price", 0), pos["avg_price"]),
                    "entry_date": old.get("entry_date", str(date.today())),
                }
        portfolio.clear()
        portfolio.update(new_portfolio)
        # 싱크 후 peak_value 비정상 감지 → 재설정
        pf_val = sum(pos["avg_price"] * pos["qty"] for pos in new_portfolio.values())
        actual = pf_val if pf_val > 0 else TOTAL_BUDGET
        if peak_value > actual * 3:   # peak가 현재 평가액 3배 초과면 비정상
            peak_value = actual
            mdd_active = False
            cprint(f"[싱크] peak_value 비정상 → {actual:,.0f}원 재설정", Fore.YELLOW)
        _save_state()
        lines = ["✅ 싱크 완료", f"보유: {len(portfolio)}종목"]
        if not portfolio:
            lines.append("  실제 KIS ETF 보유 없음 — 이전 봇 상태를 비웠습니다.")
        for code, pos in list(portfolio.items()):
            name = ETF_UNIVERSE.get(code, {}).get("name", KOFR_NAME if code == KOFR_CODE else code)
            lines.append(f"  {name}: {pos['qty']}주 @ {pos['avg_price']:,.0f}원")
        _write_ipc_result("[normal] " + "\n".join(lines))

    elif c == "/hold":
        # IPC 컨텍스트에서는 텔레그램 직접 폴링 불가 → 직접 처리
        _cmd_hold(cmd)

    elif c in ("/help", "/도움말"):
        _write_ipc_result(
            "[normal] 📋 섹터봇 명령어\n"
            "/status    — 현재 상태\n"
            "/portfolio — 보유 ETF\n"
            "/scores    — 모멘텀 스코어\n"
            "/rebalance — 수동 리밸런싱\n"
            "/kofr      — DEFENSE_1 대피\n"
            "/resume    — 복귀 (ETF 재매수)\n"
            "/defense   — 현재 대피 단계\n"
            "/kill      — 킬 스위치 ON\n"
            "/unkill    — 킬 스위치 OFF\n"
            "/start     — 재개\n"
            "/stop      — 정지\n"
            "/sync      — KIS 실제 잔고로 동기화\n"
            "/hold      — 수동 포지션 목록\n"
            "/hold 코드 가격 수량 — 수동 포지션 등록"
        )
    else:
        _write_ipc_result(f"[normal] ⚠️ 알 수 없는 명령: {text}")


def _write_sector_status():
    """shared/status_sector.json — 매니저 pinned 메시지용.
    장 마감 후 KIS 토큰 없으면 _price_cache(마지막 종가) 사용."""
    try:
        holdings = []
        unrealized = 0.0
        for code, pos in list(portfolio.items()):
            name  = ETF_UNIVERSE.get(code, {}).get("name", KOFR_NAME if code == KOFR_CODE else code)
            avg_p = pos["avg_price"]
            qty   = pos["qty"]
            # 현재가: KIS → 캐시 순으로 시도
            cur_p = 0
            h = kis_headers("FHKST01010100")
            if h:
                info = get_price_info(code)
                cur_p = info.get("price", 0)
            if cur_p <= 0:
                cur_p = _get_cached_price(code)
            if avg_p > 0 and cur_p > 0 and qty > 0:
                unrealized += (cur_p - avg_p) * qty
            holdings.append({
                "name":      name,
                "avg_price": avg_p,
                "cur_price": cur_p,
                "qty":       qty,
            })
        pnl_total = daily_pnl_krw + unrealized
        data = {
            "holding":    len(portfolio) > 0,
            "pnl_today":  daily_pnl_krw,   # 실현 손익
            "pnl_total":  pnl_total,        # 실현 + 미실현
            "unrealized": unrealized,
            "trades":     trade_count,
            "holdings":   holdings,
            "ts":         time.time(),
        }
        path = os.path.join(SHARED_DIR, "status_sector.json")
        tmp  = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        cprint(f"[status_sector 오류] {e}", Fore.YELLOW)


def _ipc_send_status():
    global peak_value
    val  = _calc_portfolio_value()
    cash = get_cash_balance()
    cash_display = cash if cash is not None else 0
    if peak_value <= 0 and not portfolio:
        peak_value = TOTAL_BUDGET
    dd   = (val - peak_value) / peak_value * 100 if peak_value > 0 and portfolio else 0.0
    lines = [
        f"📊 섹터봇 현황",
        f"━━━━━━━━━━━━━━━━━━",
        f"포트폴리오: {val:,.0f}원",
        f"현금:       {cash_display:,.0f}원" + (" (조회 실패)" if cash is None else ""),
        f"오늘 손익:  {daily_pnl_krw:+,}원",
        f"MDD:        {dd:.1f}%",
        f"킬스위치:   {'🔴 ON' if kill_switch_active else '🟢 OFF'}",
        f"MDD모드:    {'🔴 ON' if mdd_active else '🟢 OFF'}",
        f"대피단계:   {defense_stage}",
        f"보유:       {len(portfolio)}종목",
    ]
    for code, pos in list(portfolio.items()):
        name    = ETF_UNIVERSE.get(code, {}).get("name", KOFR_NAME if code == KOFR_CODE else code)
        avg_p   = pos["avg_price"]
        qty     = pos["qty"]
        info    = get_price_info(code)
        cur_p   = info.get("price", 0) if info else 0
        if cur_p <= 0:
            cur_p = _get_cached_price(code)
        if cur_p > 0 and avg_p > 0:
            diff_pct = (cur_p - avg_p) / avg_p * 100
            diff_krw = (cur_p - avg_p) * qty
            lines.append(
                f"  {name}: {avg_p:,.0f}→{cur_p:,.0f}원 "
                f"{diff_pct:+.1f}% ({diff_krw:+,.0f}원)"
            )
        else:
            lines.append(f"  {name}: {qty}주 @ {avg_p:,.0f}원")
    _write_ipc_result("[normal] " + "\n".join(lines))


def _ipc_send_portfolio():
    if not portfolio:
        _write_ipc_result("[normal] 보유 ETF 없음 (현금 대기)")
        return
    lines = ["📦 보유 ETF"]
    for code, pos in list(portfolio.items()):
        name = ETF_UNIVERSE.get(code, {}).get("name", KOFR_NAME if code == KOFR_CODE else code)
        lines.append(f"  {name}: {pos['qty']}주 @ {pos['avg_price']:,.0f}원 (편입:{pos['entry_date']})")
    _write_ipc_result("[normal] " + "\n".join(lines))


def _ipc_send_scores():
    """IPC 컨텍스트에서 스코어 계산 — 동기 실행."""
    scores = get_all_scores()
    if not scores:
        _write_ipc_result("[normal] ❌ 스코어 계산 실패 (데이터 없음)")
        return
    ranked = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)
    lines  = ["📊 모멘텀 스코어 순위"]
    for i, (code, data) in enumerate(ranked, 1):
        name = ETF_UNIVERSE.get(code, {}).get("name", code)
        cd   = " 🚫쿨다운" if is_cooldown(code) else ""
        held = " 📦보유" if code in portfolio else ""
        lines.append(f"{i}. {name}{cd}{held}: {data['score']:+.1f}%")
    _write_ipc_result("[normal] " + "\n".join(lines))

# ============================================================
# [5] 설정 로드
# ============================================================
def load_config():
    global _cfg, TELEGRAM_TOKEN, CHAT_ID, TOTAL_BUDGET, IS_MOCK
    global initial_value, peak_value, daily_loss_base
    global AUTO_ENTRY_ENABLED, AUTO_RISK_RECOVERY_ENABLED, AUTO_RISK_RECOVERY_DAYS
    if not os.path.exists(CFG_FILE):
        _make_default_config()
        cprint(f"❌ {CFG_FILE} 파일이 없어서 기본 양식을 생성했습니다.", Fore.RED, bright=True)
        cprint("   sector_cfg.yaml 에 KIS API 키와 텔레그램 정보를 입력 후 재실행하세요.", Fore.YELLOW)
        sys.exit(1)
    with open(CFG_FILE, encoding="utf-8") as f:
        _cfg = yaml.safe_load(f) or {}
    TELEGRAM_TOKEN  = _cfg.get("telegram_token", "")
    CHAT_ID         = str(_cfg.get("chat_id", ""))
    TOTAL_BUDGET    = int(_cfg.get("total_budget", 100_000))
    # YAML의 boolean은 그대로 쓰되, 문자열 입력도 안전하게 처리한다.
    # 기존 bool("false")는 True가 되어 실투자/모의투자 모드가 뒤바뀔 수 있었다.
    raw_mock = _cfg.get("mock", True)
    if isinstance(raw_mock, str):
        IS_MOCK = raw_mock.strip().lower() in ("1", "true", "yes", "y", "on")
    else:
        IS_MOCK = bool(raw_mock)
    initial_value   = float(_cfg.get("initial_value", TOTAL_BUDGET))
    peak_value      = float(_cfg.get("peak_value",    initial_value))
    daily_loss_base = TOTAL_BUDGET  # 장 시작 시 _daily_reset()에서 실제값으로 갱신됨
    auto_cfg = _cfg.get("automation", {}) or {}
    AUTO_ENTRY_ENABLED = bool(auto_cfg.get("auto_entry_enabled", True))
    AUTO_RISK_RECOVERY_ENABLED = bool(auto_cfg.get("auto_risk_recovery_enabled", True))
    AUTO_RISK_RECOVERY_DAYS = max(1, int(auto_cfg.get("risk_recovery_days", 1)))
    _init_ipc()   # IPC 파일 경로 초기화
    cprint(f"✅ 설정 로드 — 예산:{TOTAL_BUDGET:,}원 / {'모의투자' if IS_MOCK else '실투자'}", Fore.GREEN)


def _make_default_config():
    default = {
        "app_key":        "여기에_KIS_APP_KEY",
        "app_secret":     "여기에_KIS_APP_SECRET",
        "account_no":     "12345678",
        "account_suffix": "01",
        "telegram_token": "여기에_텔레그램_봇_토큰",
        "chat_id":        "여기에_내_채팅_ID",
        "mock":           True,
        "total_budget":   100000,
        "automation": {
            "auto_entry_enabled": True,
            "auto_risk_recovery_enabled": True,
            "risk_recovery_days": 1,
        },
    }
    with open(CFG_FILE, "w", encoding="utf-8") as f:
        yaml.dump(default, f, allow_unicode=True, default_flow_style=False)


# ============================================================
# [6] 인프라: Rate Limit / API / KIS 인증
# ============================================================
def _acquire_token():
    global _bucket_tokens, _bucket_last
    now = time.time()
    _bucket_tokens = min(_BUCKET_CAP, _bucket_tokens + (now - _bucket_last) * _BUCKET_RATE)
    _bucket_last   = now
    if _bucket_tokens < 1.0:
        time.sleep((1.0 - _bucket_tokens) / _BUCKET_RATE)
        _bucket_tokens = 0.0
    else:
        _bucket_tokens -= 1.0


def api_call(method, url, **kwargs):
    _acquire_token()
    for attempt in range(3):
        try:
            r = getattr(requests, method)(url, timeout=10, **kwargs)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", 2)))
                continue
            cprint(f"[API {r.status_code}] {url[-60:]}", Fore.YELLOW)
            if r.status_code >= 500:
                key = url.split("?")[0]
                now = time.time()
                if now - _last_api_error_alert.get(key, 0) >= 300:
                    _last_api_error_alert[key] = now
                    send_msg(f"⚠️ KIS API 서버 오류 {r.status_code}\n신규 주문·판단은 해당 호출에서 중단됩니다.\n{url[-60:]}", force=True)
        except Exception as e:
            cprint(f"[API 오류 {attempt+1}] {e}", Fore.RED)
            time.sleep(2)
    return {}


def _prod_url():
    """주문용 URL — 모의/실투자 구분"""
    return (
        "https://openapivts.koreainvestment.com:29443"
        if IS_MOCK
        else "https://openapi.koreainvestment.com:9443"
    )

def _market_url():
    """시세/차트 조회용 URL — 항상 실서버 (모의서버는 데이터 30일 제한)"""
    return "https://openapi.koreainvestment.com:9443"


def get_kis_token():
    global _access_token, _token_expire
    # 메모리 토큰 유효하면 재사용
    if _access_token and time.time() < _token_expire:
        return _access_token

    now_dt = datetime.now()
    is_weekday    = now_dt.weekday() < 5
    is_market_hours = 8 <= now_dt.hour < 16

    # 장중(평일 8~16시): 신규 발급
    if is_weekday and is_market_hours:
        res = api_call("post", f"{_prod_url()}/oauth2/tokenP", json={
            "grant_type": "client_credentials",
            "appkey":     _cfg.get("app_key", ""),
            "appsecret":  _cfg.get("app_secret", ""),
        })
        _access_token = res.get("access_token", "")
        # API가 유효기간을 주면 우선 사용하고, 갱신 여유 5분을 둔다.
        # 구형 응답처럼 expires_in이 없을 때만 기존의 보수적 23시간 값을 쓴다.
        try:
            expires_in = int(res.get("expires_in", 0) or 0)
        except (TypeError, ValueError):
            expires_in = 0
        ttl = max(60, expires_in - 300) if expires_in else 3600 * 23
        _token_expire = time.time() + ttl
        if _access_token:
            cprint("✅ KIS 토큰 발급 성공", Fore.GREEN)
            _save_kis_token()
        else:
            cprint("❌ KIS 토큰 발급 실패", Fore.RED)
        return _access_token

    # 장외/주말: 파일에서 복원 시도
    if not _access_token:
        _load_kis_token()

    # 파일 토큰도 만료됐으면 장외에서도 재발급 시도 (하루 1회)
    if not _access_token or time.time() >= _token_expire:
        cprint("[토큰] 만료 — 장외 재발급 시도", Fore.YELLOW)
        res = api_call("post", f"{_prod_url()}/oauth2/tokenP", json={
            "grant_type": "client_credentials",
            "appkey":     _cfg.get("app_key", ""),
            "appsecret":  _cfg.get("app_secret", ""),
        })
        _access_token = res.get("access_token", "")
        _token_expire = time.time() + 3600 * 23
        if _access_token:
            cprint("✅ KIS 토큰 장외 발급 성공", Fore.GREEN)
            _save_kis_token()
        else:
            cprint("❌ KIS 토큰 장외 발급 실패", Fore.RED)

    if _access_token and time.time() < _token_expire:
        remain = (_token_expire - time.time()) / 3600
        cprint(f"[토큰] 사용 중 (잔여 {remain:.1f}h)", Fore.CYAN)
        return _access_token

    return ""


def kis_headers(tr_id):
    tok = get_kis_token()
    if not tok:
        return None
    return {
        "Content-Type":  "application/json; charset=utf-8",
        "authorization": f"Bearer {tok}",
        "appkey":        _cfg.get("app_key", ""),
        "appsecret":     _cfg.get("app_secret", ""),
        "tr_id":         tr_id,
        "custtype":      "P",
    }


def _acnt():
    return _cfg.get("account_no", ""), _cfg.get("account_suffix", "01")


# ============================================================
# [7] 텔레그램
# ============================================================
def send_msg(text, force=False):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        cprint(f"[TG 미설정] {text[:80]}", Fore.YELLOW)
        return
    h = datetime.now().hour
    if 2 <= h < 7 and not force:
        return
    tagged = f"[{BOT_TAG}]\n{text}"
    with _tg_lock:
        for attempt in range(2):
            try:
                res = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={"chat_id": CHAT_ID, "text": tagged[:4000]},
                    timeout=5,
                )
                if res.status_code == 200:
                    return
            except Exception as e:
                cprint(f"[TG 오류] {e}", Fore.YELLOW)
            time.sleep(1)


def send_msg_kb(text: str, keyboard: list = None, force: bool = False) -> int:
    """인라인 키보드 포함 메시지 전송. keyboard=None 이면 send_msg 동일."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        cprint(f"[TG 미설정] {text[:80]}", Fore.YELLOW)
        return 0
    h = datetime.now().hour
    if 2 <= h < 7 and not force:
        return 0
    tagged  = f"[{BOT_TAG}]\n{text}"
    payload = {"chat_id": CHAT_ID, "text": tagged[:4000]}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    with _tg_lock:
        for attempt in range(2):
            try:
                res = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json=payload, timeout=5,
                )
                if res.status_code == 200:
                    return res.json().get("result", {}).get("message_id", 0)
            except Exception as e:
                cprint(f"[TG 오류] {e}", Fore.YELLOW)
            time.sleep(1)
    return 0


def answer_callback(callback_query_id: str, text: str = ""):
    """버튼 콜백 응답 — 스피너 제거"""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=3,
        )
    except Exception:
        pass


def poll_telegram():
    global _last_update_id
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        res = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": _last_update_id + 1, "timeout": 2},
            timeout=5,
        )
        if res.status_code != 200:
            return
        for upd in res.json().get("result", []):
            _last_update_id = upd["update_id"]
            # ── 인라인 버튼 콜백 처리 ──────────────────────
            callback = upd.get("callback_query", {})
            if callback:
                cq_id   = callback.get("id", "")
                cq_data = callback.get("data", "")
                cq_chat = str(callback.get("message", {}).get("chat", {}).get("id", ""))
                if cq_chat == CHAT_ID and cq_data:
                    answer_callback(cq_id)
                    _handle_callback(cq_data)
                continue
            # ── 일반 텍스트 메시지 처리 ────────────────────
            msg  = upd.get("message", {})
            text = msg.get("text", "").strip()
            if str(msg.get("chat", {}).get("id", "")) != CHAT_ID:
                continue
            if text:
                _handle_cmd(text.split())
    except Exception:
        pass


def _poll_hold_callbacks():
    """매니저 실행 중에도 hold_ 콜백과 /hold 세션 숫자 입력만 선별 처리.
    매니저가 소비하면 안 되는 update는 건드리지 않고 update_id만 진행."""
    global _last_update_id
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        res = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": _last_update_id + 1, "timeout": 2},
            timeout=5,
        )
        if res.status_code != 200:
            return
        for upd in res.json().get("result", []):
            _last_update_id = upd["update_id"]

            # ── hold_ 콜백만 처리 ──────────────────────────
            callback = upd.get("callback_query", {})
            if callback:
                cq_data = callback.get("data", "")
                cq_chat = str(callback.get("message", {}).get("chat", {}).get("id", ""))
                if cq_chat == CHAT_ID and cq_data.startswith("hold_"):
                    answer_callback(callback.get("id", ""))
                    _handle_callback(cq_data)
                continue

            # ── /hold 세션 진행 중 숫자 입력만 처리 ────────
            if _hold_session.get("step"):
                msg  = upd.get("message", {})
                text = msg.get("text", "").strip()
                if str(msg.get("chat", {}).get("id", "")) == CHAT_ID and text:
                    if not text.startswith("/"):
                        _hold_text_input(text)
    except Exception:
        pass


def _handle_cmd(cmd):
    global kill_switch_active, manual_pause, mdd_active, daily_loss_base
    if not cmd:
        return

    # /hold 세션 진행 중이면 숫자 입력을 세션으로 라우팅
    if _hold_session.get("step") and not cmd[0].startswith("/"):
        _hold_text_input(" ".join(cmd))
        return

    c = cmd[0].lower()

    if c in ("/status", "/s", "/상태"):
        _send_status()
    elif c in ("/portfolio", "/p", "/포트"):
        _send_portfolio()
    elif c in ("/scores", "/score", "/스코어"):
        _send_scores()
    elif c in ("/snapshot", "/시장기록"):
        threading.Thread(target=_log_market_snapshot, kwargs={"trigger": "manual"}, daemon=True).start()
        send_msg("📝 ETF 시장 상태 기록을 시작했습니다.", force=True)
    elif c in ("/why", "/왜", "/왜안사"):
        holdings = list(portfolio.keys())
        stage = defense_stage
        scores = get_all_scores() or {}
        ranked = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)
        lines = [f"🔍 왜 안사? [{stage}]", "━━━━━━━━━━━━━━━━━━━━"]
        if stage != "NORMAL":
            lines.append(f"❌ 대피 단계: {stage}")
        elif kill_switch_active:
            lines.append("❌ 킬스위치 ON")
        elif mdd_active:
            lines.append("❌ MDD 한도 초과")
        else:
            lines.append("✅ 매수 가능 상태")
        lines.append("─────────────────")
        for i, (code, d) in enumerate(ranked[:3], 1):
            held = "📦" if code in holdings else ""
            lines.append(f"{i}. {ETF_UNIVERSE.get(code,{}).get('name',code)}{held}: {d['score']:+.1f}%")
        send_msg("\n".join(lines), force=True)
    elif c in ("/bollinger", "/bb"):
        for code in (list(portfolio.keys()) or ["보유없음"]):
            send_msg(get_bollinger_status(code) if code != "보유없음" else "보유없음", force=True)
    elif c in ("/investor", "/수급"):
        for code in (list(portfolio.keys()) or ["보유없음"]):
            send_msg(get_investor_status(code) if code != "보유없음" else "보유없음", force=True)
    elif c in ("/rebalance", "/r", "/리밸"):
        send_msg("🔄 수동 리밸런싱 실행 중...", force=True)
        threading.Thread(target=_do_rebalance, daemon=True).start()
    elif c in ("/kofr", "/대피"):
        send_msg("🚨 DEFENSE_1 대피 실행 중...", force=True)
        threading.Thread(target=lambda: _enter_defense_1("수동 대피"), daemon=True).start()
    elif c in ("/resume", "/복귀"):
        send_msg("🟢 복귀 실행 중...", force=True)
        threading.Thread(target=_do_resume, daemon=True).start()
    elif c in ("/kill", "/킬"):
        kill_switch_active = True
        manual_pause = True
        _save_state()
        send_msg("🔴 킬 스위치 ON — 모든 매매 중단\n/unkill 로 해제", force=True)
    elif c in ("/unkill", "/킬해제"):
        kill_switch_active = False
        manual_pause = False
        _save_state()
        send_msg("🟢 킬 스위치 OFF — 매매 재개", force=True)
    elif c in ("/start",):
        kill_switch_active = False
        mdd_active = False
        manual_pause = False
        actual = _calc_account_value()
        if actual is not None and actual > 0:
            daily_loss_base = actual
        _save_state()
        send_msg("🟢 섹터봇 재개됨", force=True)
    elif c in ("/sync", "/싱크"):
        real = get_kis_holdings()
        if real is None:
            send_msg("⚠️ KIS 잔고 조회 실패", force=True)
            return
        valid_codes = set(ETF_UNIVERSE.keys()) | {KOFR_CODE}
        new_portfolio = {}
        for code, pos in real.items():
            if code in valid_codes:
                old = portfolio.get(code, {})
                new_portfolio[code] = {
                    "qty":        pos["qty"],
                    "avg_price":  pos["avg_price"],
                    "high_price": max(old.get("high_price", 0), pos["avg_price"]),
                    "entry_date": old.get("entry_date", str(date.today())),
                }
        portfolio.clear()
        portfolio.update(new_portfolio)
        _save_state()
        lines = ["✅ 싱크 완료", f"보유: {len(portfolio)}종목"]
        if not portfolio:
            lines.append("  실제 KIS ETF 보유 없음 — 이전 봇 상태를 비웠습니다.")
        for code, pos in list(portfolio.items()):
            name = ETF_UNIVERSE.get(code, {}).get("name", KOFR_NAME if code == KOFR_CODE else code)
            lines.append(f"  {name}: {pos['qty']}주 @ {pos['avg_price']:,.0f}원")
        send_msg("\n".join(lines), force=True)

    elif c in ("/help", "/도움말"):
        send_msg(
            "📋 명령어 목록\n"
            "/status    — 현재 상태\n"
            "/portfolio — 보유 ETF\n"
            "/scores    — 모멘텀 스코어 순위\n"
            "/rebalance — 수동 리밸런싱\n"
            "/kofr      — DEFENSE_1 대피\n"
            "/resume    — 복귀 (ETF 재매수)\n"
            "/kill      — 킬 스위치 ON\n"
            "/unkill    — 킬 스위치 OFF\n"
            "/sync      — KIS 실제 잔고로 동기화\n"
            "/hold      — 수동 포지션 목록\n"
            "/hold 코드 가격 수량 — 수동 포지션 등록",
            force=True,
        )

    elif c == "/hold":
        _cmd_hold(cmd)


# ============================================================
# [8] 시장 데이터
# ============================================================
PRICE_CACHE_TTL = 300  # 오래된 가격으로 손절·트레일링 판단을 하지 않도록 5분만 사용
_price_cache: dict = {}   # {code: {"price": int, "ts": float}} — 마지막 성공 가격 캐시
_last_price_data_alert = {}


def _get_cached_price(code):
    cached = _price_cache.get(code)
    if cached and time.time() - cached.get("ts", 0) <= PRICE_CACHE_TTL:
        return cached.get("price", 0)
    return 0

def _get_price_naver(code: str) -> int:
    """네이버 금융에서 현재가(또는 종가) 조회 — KIS 토큰 없을 때 fallback."""
    try:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code != 200:
            return 0
        import re
        # <p class="no_today"><em ...><span class="blind">현재가</span>10,245</em>
        m = re.search(r'no_today[^"]*"[^>]*>.*?<span class="blind">[^<]+</span>([\d,]+)', res.text, re.S)
        if m:
            return int(m.group(1).replace(",", ""))
    except Exception as e:
        cprint(f"[네이버 가격 오류 {code}] {e}", Fore.YELLOW)
    return 0


def get_price_info(code):
    """현재가, 전일종가, 거래대금, 호가 조회.
    KIS 토큰이 없거나 장 마감 후에는 네이버 금융 → 캐시 순으로 fallback."""
    h = kis_headers("FHKST01010100")
    if h:
        res = api_call(
            "get",
            f"{_market_url()}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=h,
            params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
        )
        out = res.get("output", {})
        if out:
            try:
                info = {
                    "price":      int(out.get("stck_prpr",    0) or 0),
                    "prev_close": int(out.get("stck_sdpr",    0) or 0),
                    "bid":        int(out.get("bidp",          0) or 0),
                    "ask":        int(out.get("askp",          0) or 0),
                    "volume_krw": int(out.get("acml_tr_pbmn",  0) or 0),
                    "open":       int(out.get("stck_oprc",     0) or 0),
                    "high":       int(out.get("stck_hgpr",     0) or 0),
                    "low":        int(out.get("stck_lwpr",     0) or 0),
                }
                if info["price"] > 0:
                    _price_cache[code] = {"price": info["price"], "ts": time.time()}
                return info
            except Exception as e:
                cprint(f"[가격 파싱 오류 {code}] {e}", Fore.YELLOW)

    # KIS 실패 → 네이버 fallback
    naver_price = _get_price_naver(code)
    if naver_price > 0:
        _price_cache[code] = {"price": naver_price, "ts": time.time()}
        return {"price": naver_price}

    # 네이버도 실패 → 캐시 사용
    cached_price = _get_cached_price(code)
    if cached_price:
        return {"price": cached_price}

    now = time.time()
    if now - _last_price_data_alert.get(code, 0) >= 600:
        _last_price_data_alert[code] = now
        cprint(f"[가격 데이터 없음] {code} — 매매 판단을 건너뜀", Fore.RED)
        send_msg(f"⚠️ {code} 가격 데이터 없음 — 해당 종목 매매 판단을 건너뜁니다.", force=True)

    return {}


def get_daily_chart(code, n_days=70):
    """일봉 종가 + 거래대금 리스트 반환 (오래된 것 먼저)
    반환: (closes, vol_krw_list) 튜플"""
    h = kis_headers("FHKST01010400")
    if not h:
        return [], []

    closes_all  = []
    vol_all     = []
    date_to     = date.today()

    for _ in range(5):
        date_from = date_to - timedelta(days=50)
        res = api_call(
            "get",
            f"{_market_url()}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            headers=h,
            params={
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd":         code,
                "fid_input_date_1":       date_from.strftime("%Y%m%d"),
                "fid_input_date_2":       date_to.strftime("%Y%m%d"),
                "fid_period_div_code":    "D",
                "fid_org_adj_prc":        "0",
            },
        )
        output = res.get("output2", []) or res.get("output", [])
        if not output:
            break

        batch_c = []
        batch_v = []
        for row in output:
            try:
                c = int(row.get("stck_clpr", 0) or 0)
                v = int(row.get("acml_tr_pbmn", 0) or 0)
                if c > 0:
                    batch_c.append(c)
                    batch_v.append(v)
            except Exception:
                pass

        if not batch_c:
            break

        closes_all = batch_c + closes_all
        vol_all    = batch_v + vol_all

        if len(closes_all) >= n_days:
            break

        date_to = date_from - timedelta(days=1)
        time.sleep(0.2)

    return closes_all[-n_days:], vol_all[-n_days:]


def get_kospi_info():
    """코스피 현재 정보 반환: price, change_pct(전일대비), volume"""
    h = kis_headers("FHKUP03500100")
    if not h:
        return {}
    res = api_call(
        "get",
        f"{_prod_url()}/uapi/domestic-stock/v1/quotations/inquire-index-price",
        headers=h,
        params={"fid_cond_mrkt_div_code": "U", "fid_input_iscd": "0001"},
    )
    out = res.get("output", {})
    try:
        return {
            "price":      float(out.get("bstp_nmix_prpr",      0) or 0),
            "change_pct": float(out.get("bstp_nmix_prdy_ctrt", 0) or 0),
            "volume":     int(out.get("acml_vol",              0) or 0),
        }
    except Exception:
        return {}


def get_kospi_change_pct():
    """코스피 당일 등락률 (전일 종가 대비) — 기존 호환용"""
    return get_kospi_info().get("change_pct", 0.0)


def get_kospi_intraday_pct():
    """코스피 장중 등락률 — 당일 기준가(kospi_baseline) 대비"""
    if kospi_baseline <= 0:
        return 0.0
    price = get_kospi_info().get("price", 0.0)
    if price <= 0:
        return 0.0
    return (price - kospi_baseline) / kospi_baseline * 100


def get_kospi_volume_ratio():
    """코스피 현재 거래량 / 최근 5일 평균 거래량"""
    h = kis_headers("FHKST03010100")
    if not h:
        return 1.0
    today_str = date.today().strftime("%Y%m%d")
    past_str  = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
    res = api_call(
        "get",
        f"{_prod_url()}/uapi/domestic-stock/v1/quotations/inquire-index-daily-price",
        headers=h,
        params={
            "fid_cond_mrkt_div_code": "U",
            "fid_input_iscd":         "0001",
            "fid_input_date_1":       past_str,
            "fid_input_date_2":       today_str,
            "fid_period_div_code":    "D",
        },
    )
    rows = res.get("output2", []) or res.get("output", [])
    vols = []
    for r in rows:
        try:
            v = int(r.get("acml_vol", 0) or 0)
            if v > 0:
                vols.append(v)
        except Exception:
            pass
    if len(vols) < 2:
        return 1.0
    avg5  = sum(vols[1:6]) / min(len(vols[1:6]), 5)  # 전일 포함 최대 5일
    today = vols[0] if vols else avg5
    return today / avg5 if avg5 > 0 else 1.0


def get_kospi_ma5():
    """코스피 5일 이동평균"""
    h = kis_headers("FHKST03010100")
    if not h:
        return 0.0
    today_str = date.today().strftime("%Y%m%d")
    past_str  = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
    res = api_call(
        "get",
        f"{_prod_url()}/uapi/domestic-stock/v1/quotations/inquire-index-daily-price",
        headers=h,
        params={
            "fid_cond_mrkt_div_code": "U",
            "fid_input_iscd":         "0001",
            "fid_input_date_1":       past_str,
            "fid_input_date_2":       today_str,
            "fid_period_div_code":    "D",
        },
    )
    rows = res.get("output2", []) or res.get("output", [])
    closes = []
    for r in rows[1:6]:   # 오늘 제외, 최근 5거래일
        try:
            c = float(r.get("bstp_nmix_clpr", 0) or 0)
            if c > 0:
                closes.append(c)
        except Exception:
            pass
    return sum(closes) / len(closes) if closes else 0.0


def update_consecutive_down_days():
    """연속하락일수 갱신 — 매일 장 시작 전 1회 호출"""
    global consecutive_down_days
    h = kis_headers("FHKST03010100")
    if not h:
        return
    today_str = date.today().strftime("%Y%m%d")
    past_str  = (date.today() - timedelta(days=15)).strftime("%Y%m%d")
    res = api_call(
        "get",
        f"{_prod_url()}/uapi/domestic-stock/v1/quotations/inquire-index-daily-price",
        headers=h,
        params={
            "fid_cond_mrkt_div_code": "U",
            "fid_input_iscd":         "0001",
            "fid_input_date_1":       past_str,
            "fid_input_date_2":       today_str,
            "fid_period_div_code":    "D",
        },
    )
    rows = res.get("output2", []) or res.get("output", [])
    closes = []
    for r in rows[:8]:
        try:
            c = float(r.get("bstp_nmix_clpr", 0) or 0)
            if c > 0:
                closes.append(c)
        except Exception:
            pass
    # 최신→과거 순 → 연속 하락일 카운트
    count = 0
    for i in range(len(closes) - 1):
        if closes[i] < closes[i + 1]:   # 오늘 < 전일 → 하락
            count += 1
        else:
            break
    consecutive_down_days = count
    cprint(f"[연속하락일] {count}일", Fore.CYAN)


def get_cash_balance():
    """주문 가능 현금 조회. 조회 실패는 0원이 아니라 None으로 구분한다."""
    tr = "VTTC8908R" if IS_MOCK else "TTTC8908R"
    h  = kis_headers(tr)
    if not h:
        return None
    acnt_no, suffix = _acnt()
    res = api_call(
        "get",
        f"{_prod_url()}/uapi/domestic-stock/v1/trading/inquire-psbl-order",
        headers=h,
        params={
            "CANO":         acnt_no,
            "ACNT_PRDT_CD": suffix,
            "PDNO":         "005930",
            "ORD_UNPR":     "0",
            "ORD_DVSN":     "02",
            "CMA_EVLU_AMT_ICLD_YN": "Y",
            "OVRS_ICLD_YN": "N",
        },
    )
    try:
        output = res.get("output")
        if not isinstance(output, dict):
            return None
        return int(output.get("ord_psbl_cash", 0) or 0)
    except Exception:
        return None


def _wait_for_cash_settlement(before_cash, expected_release=0, timeout=15):
    """매도 체결금이 주문가능 현금에 반영될 때까지 짧게 재조회한다."""
    if before_cash is None:
        return get_cash_balance()
    target = before_cash + max(0, expected_release) * 0.8
    deadline = time.time() + timeout
    latest = before_cash
    while time.time() < deadline:
        cash = get_cash_balance()
        if cash is not None:
            latest = cash
            if cash >= target:
                return cash
        time.sleep(1)
    cprint(f"[현금 반영 대기] 제한시간 경과 — {latest:,.0f}원 기준 진행", Fore.YELLOW)
    return latest


def get_kis_holdings():
    """KIS 실제 보유 주식 잔고 조회 → 성공 시 dict, 실패 시 None."""
    tr = "VTTC8434R" if IS_MOCK else "TTTC8434R"
    h  = kis_headers(tr)
    if not h:
        return None
    acnt_no, suffix = _acnt()
    result = {}
    try:
        res = api_call(
            "get",
            f"{_prod_url()}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=h,
            params={
                "CANO":            acnt_no,
                "ACNT_PRDT_CD":    suffix,
                "AFHR_FLPR_YN":    "N",
                "OFL_YN":          "",
                "INQR_DVSN":       "02",
                "UNPR_DVSN":       "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN":       "01",
                "CTX_AREA_FK100":  "",
                "CTX_AREA_NK100":  "",
            },
        )
        rows = res.get("output1")
        if not isinstance(rows, list):
            return None
        for item in rows:
            code = item.get("pdno", "")
            qty  = int(item.get("hldg_qty", 0) or 0)
            avg  = float(item.get("pchs_avg_pric", 0) or 0)
            if code and qty > 0:
                result[code] = {"qty": qty, "avg_price": avg}
    except Exception as e:
        cprint(f"[잔고 조회 오류] {e}", Fore.YELLOW)
        return None
    return result


# ============================================================
# [9-B] 자동 싱크
# ============================================================
def _auto_sync_from_kis(timeout=20, expected=None):
    """매매 직후 KIS 잔고를 폴링해서 portfolio 즉시 동기화.
    timeout 초 동안 잔고 반영될 때까지 대기."""
    valid_codes = set(ETF_UNIVERSE.keys()) | {KOFR_CODE} | set(hold_positions.keys())
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        real = get_kis_holdings()
        if real is not None:
            if expected:
                code, side, previous_qty = expected
                actual_qty = real.get(code, {}).get("qty", 0)
                confirmed = actual_qty > previous_qty if side == "BUY" else actual_qty < previous_qty
                if not confirmed:
                    continue
            # ETF/KOFR 포트폴리오 싱크
            new_portfolio = {}
            for code, pos in real.items():
                if code in (set(ETF_UNIVERSE.keys()) | {KOFR_CODE}):
                    old = portfolio.get(code, {})
                    new_portfolio[code] = {
                        "qty":        pos["qty"],
                        "avg_price":  pos["avg_price"],
                        "high_price": max(old.get("high_price", 0), pos["avg_price"]),
                        "entry_date": old.get("entry_date", str(date.today())),
                    }
            portfolio.clear()
            portfolio.update(new_portfolio)
            # hold 포지션 싱크 (잔고에 없으면 제거)
            for code in list(hold_positions.keys()):
                if code not in real:
                    del hold_positions[code]
                    cprint(f"[싱크] hold {code} 잔고 없음 → 제거", Fore.YELLOW)
            _save_state()
            if expected:
                _pending_orders.pop(expected[0], None)
            cprint(f"[자동싱크] 완료 — ETF {len(portfolio)}종목", Fore.CYAN)
            return True
    if expected:
        code, side, _ = expected
        send_msg(f"⚠️ {code} {side} 주문 체결 확인 시간 초과 — KIS 미체결/잔고를 확인하세요.", force=True)
    cprint("[자동싱크] 타임아웃", Fore.YELLOW)
    return False


# ============================================================
# [9] 주문
# ============================================================
def get_tick_size(price):
    if price < 2000:    return 1
    if price < 5000:    return 5
    if price < 20000:   return 10
    if price < 50000:   return 50
    if price < 200000:  return 100
    if price < 500000:  return 500
    return 1000


def send_order(code, side, qty, price=0):
    """side: BUY or SELL / price=0 → 시장가"""
    side = str(side).upper()
    code = str(code).strip()
    if side not in ("BUY", "SELL") or not (code.isdigit() and len(code) == 6) or qty <= 0:
        cprint(f"[주문 차단] 잘못된 주문값: {side} {code} x {qty}", Fore.RED)
        return False
    order_key = code
    with _order_lock:
        if order_key in _orders_in_flight:
            cprint(f"[주문 차단] 동일 종목 주문 처리 중: {side} {code}", Fore.YELLOW)
            return False
        _orders_in_flight.add(order_key)

    try:
        return _send_order_once(code, side, qty, price)
    finally:
        with _order_lock:
            _orders_in_flight.discard(order_key)


def _send_order_once(code, side, qty, price=0):
    """주문 API 호출의 단일 실행부. send_order()를 통해서만 호출한다."""
    tr_map = {
        ("BUY",  True):  "VTTC0802U",
        ("BUY",  False): "TTTC0802U",
        ("SELL", True):  "VTTC0801U",
        ("SELL", False): "TTTC0801U",
    }
    h = kis_headers(tr_map[(side, IS_MOCK)])
    if not h:
        return False
    acnt_no, suffix = _acnt()
    body = {
        "CANO":         acnt_no,
        "ACNT_PRDT_CD": suffix,
        "PDNO":         code,
        "ORD_DVSN":     "01" if price == 0 else "00",
        "ORD_QTY":      str(qty),
        "ORD_UNPR":     str(price) if price > 0 else "0",
    }
    res = api_call("post",
        f"{_prod_url()}/uapi/domestic-stock/v1/trading/order-cash",
        headers=h, json=body,
    )
    if res.get("rt_cd") == "0":
        name = ETF_UNIVERSE.get(code, {}).get("name", KOFR_NAME if code == KOFR_CODE else code)
        cprint(
            f"✅ [{side}] {name} {qty}주 @ {'시장가' if price==0 else f'{price:,}원'}",
            Fore.GREEN, bright=True,
        )
        return True
    cprint(f"❌ 주문 실패 [{side}] {code} — {res.get('msg1','')}", Fore.RED)
    return False



# ============================================================
# [PATCH] 볼린저밴드 + 투자자 동향
# ============================================================
import numpy as _np_ind

_BB_PERIOD = 20
_BB_K      = 2.0
_BB_NEAR   = 0.15   # %B 이하면 하단 근처
_INV_FILTER = False  # 섹터봇 ETF는 외국인/기관 0이 흔해서 필터 비활성화
_INV_CACHE  = {}
_INV_TTL    = 300   # 5분 캐시


def get_minute_candles(code, count=30):
    """당일 분봉 조회 FHKST03010200"""
    from datetime import datetime as _dt
    h = kis_headers("FHKST03010200")
    if not h:
        return []
    now_str = _dt.now().strftime("%H%M%S")
    res = api_call(
        "get",
        f"{_prod_url()}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
        headers=h,
        params={
            "FID_ETC_CLS_CODE":       "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD":         code,
            "FID_INPUT_HOUR_1":       now_str,
            "FID_PW_DATA_INCU_YN":    "N",
        }
    )
    try:
        return [
            {
                "open":  int(c.get("stck_oprc", 0)),
                "high":  int(c.get("stck_hgpr", 0)),
                "low":   int(c.get("stck_lwpr", 0)),
                "close": int(c.get("stck_prpr", 0)),
            }
            for c in (res.get("output2", []) or [])[:count]
        ]
    except Exception as e:
        cprint(f"[분봉 오류] {e}", Fore.YELLOW)
        return []


def calc_bollinger(candles, period=20, k=2.0):
    """볼린저밴드 계산. 반환: (upper, mid, lower, pct_b)"""
    if len(candles) < period:
        return None, None, None, None
    closes = [c["close"] for c in candles[-period:]]
    mid    = sum(closes) / period
    std    = float(_np_ind.std(closes))
    upper  = mid + k * std
    lower  = mid - k * std
    cur    = closes[-1]
    pct_b  = (cur - lower) / (upper - lower) if (upper - lower) > 0 else 0.5
    return upper, mid, lower, pct_b


def is_hammer(c):
    body = abs(c["close"] - c["open"])
    if body == 0: return False
    lw = min(c["open"], c["close"]) - c["low"]
    uw = c["high"] - max(c["open"], c["close"])
    return lw >= body * 2 and uw <= body * 0.3


def is_bullish_reversal(candles):
    if len(candles) < 2: return False
    return candles[-2]["close"] < candles[-2]["open"] and candles[-1]["close"] > candles[-1]["open"]


def check_bollinger_signal(code):
    """볼린저 신호. 반환: (통과여부, 메시지)"""
    return True, "BB필터비활성"  # 섹터봇 ETF 매수에는 불필요


def get_investor_flow(code):
    """투자자 동향. 반환: (외국인_순매수, 기관_순매수)"""
    import time as _t
    now = _t.time()
    cached = _INV_CACHE.get(code)
    if cached and now - cached[0] < _INV_TTL:
        return cached[1], cached[2]
    h = kis_headers("FHKST01010900")
    if not h:
        return None, None
    res = api_call(
        "get",
        f"{_prod_url()}/uapi/domestic-stock/v1/quotations/inquire-investor",
        headers=h,
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
    )
    try:
        out = res.get("output", [])
        row = out[0] if isinstance(out, list) and out else out
        frgn = int(row.get("frgn_ntby_qty", 0) or 0)
        inst = int(row.get("orgn_ntby_qty", 0) or 0)
        _INV_CACHE[code] = (now, frgn, inst)
        return frgn, inst
    except Exception as e:
        cprint(f"[투자자 동향 오류] {e}", Fore.YELLOW)
        return None, None


def check_investor_signal(code):
    """투자자 필터. 반환: (통과여부, 메시지)"""
    if not _INV_FILTER:
        return True, "필터OFF"
    frgn, inst = get_investor_flow(code)
    if frgn is None:
        return True, "조회실패(통과)"
    msg = f"외국인{frgn:+,} 기관{inst:+,}"
    return (frgn > 0 or inst > 0), msg


def get_bollinger_status(code):
    candles = get_minute_candles(code, 30)
    if not candles:
        return f"{code}: 분봉 없음"
    upper, mid, lower, pct_b = calc_bollinger(candles)
    cur = candles[-1]["close"]
    if pct_b is None:
        return f"{code}: 계산 불가"
    return (
        f"📊 {code} 볼린저\n"
        f"현재가: {cur:,}원\n"
        f"상단: {upper:,.0f}  중간: {mid:,.0f}  하단: {lower:,.0f}\n"
        f"%B: {pct_b:.2f}  망치: {'✅' if candles and is_hammer(candles[-1]) else '❌'}"
        f"  양봉전환: {'✅' if is_bullish_reversal(candles) else '❌'}"
    )


def get_investor_status(code):
    frgn, inst = get_investor_flow(code)
    if frgn is None:
        return f"{code}: 조회 실패"
    return (
        f"👥 {code} 수급\n"
        f"외국인: {frgn:+,}주 {'✅' if frgn > 0 else '❌'}\n"
        f"기관:   {inst:+,}주 {'✅' if inst > 0 else '❌'}\n"
        f"신호: {'✅통과' if (frgn > 0 or inst > 0) else '❌차단'}"
    )

def buy_etf(code, budget_krw):
    """ETF 매수 — 예산 내 최대 수량"""
    if code in _pending_orders:
        cprint(f"[매수 보류] {code} 기존 주문 체결 확인 중", Fore.YELLOW)
        return 0
    info = get_price_info(code)
    if not info or info["price"] <= 0:
        cprint(f"[매수 실패] {code} 가격 조회 실패", Fore.YELLOW)
        return 0
    # ── 투자자 수급 필터 ────────────────────────────────────
    inv_ok, inv_msg = check_investor_signal(code)
    cprint(f"[매수필터] {code} 수급:{inv_msg}", Fore.CYAN)
    if not inv_ok:
        send_msg(f"⚠️ {code} 수급차단 — {inv_msg}", force=True)
        return 0
    # ────────────────────────────────────────────────────────
    price = info.get("ask") or info["price"]
    cash = get_cash_balance()
    if cash is None:
        cprint(f"[매수 차단] {code} 주문가능 현금 조회 실패", Fore.YELLOW)
        return 0
    # API 응답 지연이나 병렬 리밸런싱으로 예산보다 현금이 적어진 경우를 막는다.
    budget_krw = min(int(budget_krw), cash)
    qty   = int(budget_krw / price)
    if qty <= 0:
        cprint(f"[매수 건너뜀] {code} 예산 {budget_krw:,}원 < 1주 ({price:,}원)", Fore.YELLOW)
        return 0
    tick        = get_tick_size(price)
    order_price = price + tick
    ok = send_order(code, "BUY", qty, order_price)
    if ok:
        previous_qty = portfolio.get(code, {}).get("qty", 0)
        _pending_orders[code] = {"side": "BUY", "qty": qty, "previous_qty": previous_qty, "ts": time.time()}
        _log_trade(code, "BUY_SUBMITTED", qty, order_price, reason="리밸런싱")
        send_msg(f"🟡 ETF 매수 주문 접수\n{code} {qty}주 @ {order_price:,}원\n체결 확인 후 보유에 반영합니다.", force=True)
        threading.Thread(
            target=_auto_sync_from_kis, args=(20, (code, "BUY", previous_qty)), daemon=True
        ).start()
    return qty if ok else 0


def sell_etf(code, qty=None, reason=""):
    """ETF 매도 — qty=None이면 전량"""
    pos = portfolio.get(code)
    if not pos:
        return False
    if qty is None:
        qty = pos["qty"]
    if qty <= 0:
        return False
    info  = get_price_info(code)
    price = (info.get("bid") or info.get("price", 0)) if info else 0
    tick  = get_tick_size(price) if price > 0 else 0
    order_price = max(0, price - tick) if price > 0 else 0
    ok = send_order(code, "SELL", qty, order_price)
    if ok:
        pnl = (order_price - pos["avg_price"]) * qty if order_price > 0 else 0
        avg_p  = pos["avg_price"]
        high_p = pos.get("high_price", avg_p)
        entry  = pos.get("entry_date")
        hold_days = (date.today() - date.fromisoformat(entry)).days if entry else None
        peak_pnl  = (high_p - avg_p) / avg_p * 100 if avg_p > 0 else 0
        final_pnl = (order_price - avg_p) / avg_p * 100 if avg_p > 0 else 0

        _record_sell(code, qty, pnl, reason)
        _log_trade(code, "SELL", qty, order_price, pnl=pnl, reason=reason)
        _log_trade_detail(
            code=code, side="SELL", qty=qty, price=order_price,
            pnl=pnl, reason=reason,
            kospi_pct=get_kospi_change_pct(),
            hold_days=hold_days, avg_price=avg_p, high_price=high_p,
        )
        # 트레일링/손절 이력 기록
        if reason in ("트레일링스탑", "손절", "인버스익절", "인버스손절"):
            _log_trailing(
                code=code,
                trigger_type=reason,
                entry_price=avg_p,
                high_price=high_p,
                exit_price=order_price,
                peak_pnl_pct=peak_pnl,
                final_pnl_pct=final_pnl,
                hold_days=hold_days,
            )
        # 매도 시그널 텔레그램 보고
        name = ETF_UNIVERSE.get(code, {}).get("name", KOFR_NAME if code == KOFR_CODE else code)
        icon = "🟢" if pnl >= 0 else "🔴"
        send_msg(
            f"{icon} ETF 매도 완료\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"종목: {name} ({code})\n"
            f"매도가: {order_price:,}원  수량: {qty}주\n"
            f"매수가: {avg_p:,}원  손익: {final_pnl:+.2f}% ({pnl:+,}원)\n"
            f"이유: {reason}",
            force=True,
        )
        # 매도 후 즉시 자동 싱크
        threading.Thread(target=_auto_sync_from_kis, daemon=True).start()
    return ok


# ============================================================
# [10] 포트폴리오 기록
# ============================================================
def _record_buy(code, price, qty):
    global peak_value
    if code in portfolio:
        old   = portfolio[code]
        total = old["qty"] + qty
        avg   = (old["avg_price"] * old["qty"] + price * qty) / total
        portfolio[code]["qty"]        = total
        portfolio[code]["avg_price"]  = avg
        portfolio[code]["high_price"] = max(old["high_price"], price)
    else:
        portfolio[code] = {
            "qty":        qty,
            "avg_price":  price,
            "high_price": price,
            "entry_date": str(date.today()),
        }
    # 매수 후 포트폴리오 평가액으로 peak 갱신 (현금 잔고 API 없이)
    pf_val = sum(pos["avg_price"] * pos["qty"] for pos in portfolio.values())
    if pf_val > peak_value:
        peak_value = pf_val
    _save_state()


def _record_sell(code, qty, pnl, reason):
    global daily_pnl_krw, trade_count
    daily_pnl_krw += int(pnl)
    trade_count   += 1
    if code in portfolio:
        remaining = portfolio[code]["qty"] - qty
        if remaining <= 0:
            del portfolio[code]
        else:
            portfolio[code]["qty"] = remaining
    _save_state()


def _log_trade(code, side, qty, price, pnl=0, reason=""):
    name        = ETF_UNIVERSE.get(code, {}).get("name", KOFR_NAME if code == KOFR_CODE else code)
    header_need = not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        if header_need:
            f.write("dt,code,name,side,qty,price,pnl_krw,reason\n")
        f.write(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')},"
            f"{code},{name},{side},{qty},{price},{int(pnl)},{reason}\n"
        )


def _get_log_path(prefix=None):
    """날짜별 단일 로그 파일 경로 반환."""
    return os.path.join(LOG_DIR, f"sector_{date.today().strftime('%Y-%m-%d')}.csv")


def _write_sector_log(row_dict):
    """날짜별 단일 파일에 기록. log_type으로 행 구분."""
    path   = _get_log_path()
    header = not os.path.exists(path)
    cols   = [
        "dt", "log_type",
        "code", "name", "side", "qty", "price", "avg_price", "pnl_krw", "pnl_pct",
        "reason", "score", "regime", "kospi_pct", "hold_days", "high_price",
        "old_stage", "new_stage", "consecutive_down_days", "pf_ret",
        "trigger_type", "entry_price", "exit_price", "peak_pnl_pct", "final_pnl_pct", "capture_ratio_pct",
    ]
    try:
        with open(path, "a", encoding="utf-8") as f:
            if header:
                f.write(",".join(cols) + "\n")
            row = [str(row_dict.get(c, "")) for c in cols]
            f.write(",".join(row) + "\n")
    except Exception as e:
        cprint(f"[섹터 로그 오류] {e}", Fore.YELLOW)


def _log_trade_detail(code, side, qty, price, pnl=0, reason="",
                      score=None, regime=None, kospi_pct=None,
                      hold_days=None, avg_price=None, high_price=None):
    name    = ETF_UNIVERSE.get(code, {}).get("name", KOFR_NAME if code == KOFR_CODE else code)
    pnl_pct = round((price - avg_price) / avg_price * 100, 3) if avg_price and avg_price > 0 else ""
    _write_sector_log({
        "dt":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "log_type":  "trade",
        "code":      code, "name": name,
        "side":      side, "qty": qty, "price": price,
        "avg_price": avg_price or "", "pnl_krw": int(pnl) if pnl else 0,
        "pnl_pct":   pnl_pct, "reason": reason,
        "score":     round(score, 2) if score is not None else "",
        "regime":    regime or _market_regime or "",
        "kospi_pct": round(kospi_pct, 2) if kospi_pct is not None else "",
        "hold_days": hold_days or "", "high_price": high_price or "",
    })


def _log_defense(old_stage, new_stage, reason="", kospi_pct=None, pf_ret=None):
    _write_sector_log({
        "dt":                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "log_type":              "defense",
        "old_stage":             old_stage, "new_stage": new_stage,
        "reason":                reason,
        "kospi_pct":             round(kospi_pct, 2) if kospi_pct is not None else "",
        "pf_ret":                round(pf_ret, 2) if pf_ret is not None else "",
        "consecutive_down_days": consecutive_down_days,
        "regime":                _market_regime or "",
    })


def _log_scores(scores: dict, trigger: str = "schedule"):
    """ETF 전체 스코어를 날짜별 로그에 기록 (리밸런싱 체크마다)."""
    dt_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for code, d in scores.items():
        name = ETF_UNIVERSE.get(code, {}).get("name", code)
        held = code in portfolio
        _write_sector_log({
            "dt":       dt_str,
            "log_type": "score",
            "code":     code,
            "name":     name,
            "score":    round(d["score"], 2),
            "reason":   trigger,
            "side":     "HOLD" if held else "",
            "regime":   _market_regime or "",
            "kospi_pct": "",
        })


def _log_daily_snapshot():
    """장마감 후 1회 — 포트폴리오 상태 + 전 ETF 스코어 스냅샷.
    logs/sector/snapshot_YYYY-MM-DD.csv 에 기록."""
    snap_dir  = os.path.join(LOG_DIR, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    snap_path = os.path.join(snap_dir, f"snapshot_{date.today().strftime('%Y-%m-%d')}.csv")
    cols = [
        "dt", "code", "name", "held", "qty", "avg_price", "cur_price",
        "pnl_pct", "score", "ret5", "ret20", "defense_stage",
        "kill_switch", "mdd_active", "daily_pnl_krw", "peak_value",
    ]
    try:
        scores = get_all_scores()
        dt_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows   = []
        for code in ETF_UNIVERSE:
            name   = ETF_UNIVERSE[code]["name"]
            pos    = portfolio.get(code, {})
            held   = code in portfolio
            qty    = pos.get("qty", 0)
            avg_p  = pos.get("avg_price", 0)
            info   = get_price_info(code)
            cur_p  = info.get("price", 0) if info else 0
            if cur_p <= 0:
                cur_p = _get_cached_price(code)
            pnl_pct = round((cur_p - avg_p) / avg_p * 100, 2) if avg_p > 0 and cur_p > 0 else ""
            sc      = scores.get(code, {})
            rows.append({
                "dt":            dt_str,
                "code":          code,
                "name":          name,
                "held":          "Y" if held else "N",
                "qty":           qty,
                "avg_price":     avg_p,
                "cur_price":     cur_p,
                "pnl_pct":       pnl_pct,
                "score":         round(sc.get("score", 0), 2) if sc else "",
                "ret5":          round(sc.get("ret5",  0), 2) if sc else "",
                "ret20":         round(sc.get("ret20", 0), 2) if sc else "",
                "defense_stage": defense_stage,
                "kill_switch":   "Y" if kill_switch_active else "N",
                "mdd_active":    "Y" if mdd_active else "N",
                "daily_pnl_krw": daily_pnl_krw,
                "peak_value":    round(peak_value, 0),
            })
        header = not os.path.exists(snap_path)
        with open(snap_path, "a", encoding="utf-8") as f:
            if header:
                f.write(",".join(cols) + "\n")
            for row in rows:
                f.write(",".join(str(row.get(c, "")) for c in cols) + "\n")
        cprint(f"[스냅샷] {snap_path} 기록 완료 ({len(rows)}종목)", Fore.CYAN)
    except Exception as e:
        cprint(f"[스냅샷 오류] {e}", Fore.YELLOW)


def _log_market_snapshot(trigger="interval"):
    """장중 ETF 관측값을 누적한다.

    한 시점에 ETF 유니버스 전체를 한 행씩 기록해, 이후 어떤 ETF가 선택됐고
    당시 모멘텀·유동성·보유 상태·방어 단계가 어땠는지 분석할 수 있게 한다.
    기록 실패는 매매 로직에 영향을 주지 않는다.
    """
    if not _market_snapshot_lock.acquire(blocking=False):
        return
    try:
        path = os.path.join(MARKET_LOG_DIR, f"market_{date.today().strftime('%Y-%m-%d')}.csv")
        fields = [
            "dt", "trigger", "code", "name", "tag", "max_weight",
            "held", "qty", "avg_price", "high_price", "cur_price", "change_pct", "pnl_pct",
            "score", "ret5", "ret20", "avg_volume_krw",
            "kospi_price", "kospi_change_pct", "defense_stage", "kill_switch", "mdd_active",
            "daily_pnl_krw", "portfolio_value", "cash", "account_value", "peak_value",
        ]
        scores = get_all_scores()
        kospi = get_kospi_info() or {}
        cash = get_cash_balance()
        pf_value = _calc_portfolio_value()
        account_value = pf_value + cash if cash is not None else ""
        dt_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for code, meta in ETF_UNIVERSE.items():
            pos = portfolio.get(code, {})
            info = get_price_info(code) or {}
            cur_price = info.get("price", 0)
            if cur_price <= 0:
                cur_price = _get_cached_price(code)
            avg_price = pos.get("avg_price", 0)
            score = scores.get(code, {})
            rows.append({
                "dt": dt_str, "trigger": trigger, "code": code,
                "name": meta.get("name", code), "tag": meta.get("tag", ""),
                "max_weight": meta.get("max_weight", ""),
                "held": "Y" if code in portfolio else "N", "qty": pos.get("qty", 0),
                "avg_price": avg_price, "high_price": pos.get("high_price", 0),
                "cur_price": cur_price, "change_pct": info.get("change_pct", ""),
                "pnl_pct": round((cur_price - avg_price) / avg_price * 100, 3)
                           if cur_price and avg_price else "",
                "score": score.get("score", ""), "ret5": score.get("ret5", ""),
                "ret20": score.get("ret20", ""), "avg_volume_krw": score.get("avg_vol_krw", ""),
                "kospi_price": kospi.get("price", ""), "kospi_change_pct": kospi.get("change_pct", ""),
                "defense_stage": defense_stage, "kill_switch": "Y" if kill_switch_active else "N",
                "mdd_active": "Y" if mdd_active else "N", "daily_pnl_krw": daily_pnl_krw,
                "portfolio_value": round(pf_value), "cash": cash if cash is not None else "",
                "account_value": round(account_value) if account_value != "" else "",
                "peak_value": round(peak_value),
            })
        header = not os.path.exists(path)
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            if header:
                writer.writeheader()
            writer.writerows(rows)
        cprint(f"[시장기록] {len(rows)}종목 기록 ({trigger})", Fore.CYAN)
    except Exception as e:
        cprint(f"[시장기록 오류] {e}", Fore.YELLOW)
    finally:
        _market_snapshot_lock.release()


def _log_trailing(code, trigger_type, entry_price, high_price, exit_price,
                  peak_pnl_pct, final_pnl_pct, hold_days=None):
    name = ETF_UNIVERSE.get(code, {}).get("name", code)
    kept = round(final_pnl_pct / peak_pnl_pct * 100, 1) if peak_pnl_pct and peak_pnl_pct != 0 else ""
    _write_sector_log({
        "dt":               datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "log_type":         "trailing",
        "code":             code, "name": name,
        "trigger_type":     trigger_type,
        "entry_price":      entry_price, "high_price": high_price, "exit_price": exit_price,
        "peak_pnl_pct":     round(peak_pnl_pct, 2),
        "final_pnl_pct":    round(final_pnl_pct, 2),
        "capture_ratio_pct": kept,
        "hold_days":        hold_days or "",
        "regime":           _market_regime or "",
    })


def _save_state():
    data = {
        "portfolio":            portfolio,
        "hold_positions":       hold_positions,
        "cooldown_list":        {k: str(v) for k, v in cooldown_list.items()},
        "kill_switch":          kill_switch_active,
        "mdd_active":           mdd_active,
        "peak_value":           peak_value,
        "initial_value":        initial_value,
        "daily_pnl_krw":        daily_pnl_krw,
        "daily_loss_base":      daily_loss_base,
        "trade_count":          trade_count,
        "manual_pause":         manual_pause,
        "risk_lock_day":        str(risk_lock_day) if risk_lock_day else None,
        "last_reset_day":       str(last_reset_day) if last_reset_day else None,
        "defense_stage":        defense_stage,
        "consecutive_down_days": consecutive_down_days,
        "inverse_peak_return":  inverse_peak_return,
        "defense_down_date":    str(defense_down_date) if defense_down_date else None,
        "ts":                   time.time(),
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _load_state():
    global portfolio, cooldown_list, kill_switch_active, mdd_active
    global peak_value, initial_value, daily_pnl_krw, daily_loss_base, trade_count, last_reset_day
    global manual_pause, risk_lock_day
    global defense_stage, consecutive_down_days, inverse_peak_return, defense_down_date
    global hold_positions
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        portfolio             = data.get("portfolio", {})
        hold_positions        = data.get("hold_positions", {})
        raw_cd                = data.get("cooldown_list", {})
        cooldown_list         = {k: date.fromisoformat(v) for k, v in raw_cd.items()}
        kill_switch_active    = data.get("kill_switch", False)
        mdd_active            = data.get("mdd_active", False)
        initial_value         = float(data.get("initial_value", TOTAL_BUDGET))
        peak_value            = float(data.get("peak_value",    initial_value))
        daily_pnl_krw         = int(data.get("daily_pnl_krw",   0))
        daily_loss_base       = float(data.get("daily_loss_base", daily_loss_base))
        trade_count           = int(data.get("trade_count",      0))
        manual_pause          = bool(data.get("manual_pause", False))
        rld = data.get("risk_lock_day")
        risk_lock_day         = date.fromisoformat(rld) if rld else None
        lrd = data.get("last_reset_day")
        last_reset_day        = date.fromisoformat(lrd) if lrd else None
        defense_stage         = data.get("defense_stage", "NORMAL")
        consecutive_down_days = int(data.get("consecutive_down_days", 0))
        inverse_peak_return   = float(data.get("inverse_peak_return", 0.0))
        ddd = data.get("defense_down_date")
        defense_down_date     = date.fromisoformat(ddd) if ddd else None
        cprint(f"✅ 상태 복원 — 포트폴리오 {len(portfolio)}종목 / 대피:{defense_stage}", Fore.GREEN)

        # peak_value 자동 보정 — API 없이 포트폴리오 기준으로 검증
        pf_val = sum(pos["avg_price"] * pos["qty"] for pos in portfolio.values())
        pf_ref = pf_val if pf_val > 0 else TOTAL_BUDGET
        if peak_value > pf_ref * 3:
            cprint(f"[상태 복원] peak_value 비정상({peak_value:,.0f}) → {pf_ref:,.0f}원으로 재설정", Fore.YELLOW)
            peak_value = pf_ref
            mdd_active = False
    except Exception as e:
        cprint(f"[상태 복원 오류] {e}", Fore.YELLOW)


# ============================================================
# [11] 모멘텀 스코어 계산
# ============================================================
def calc_momentum_score(closes):
    """20일+60일 가중 모멘텀 스코어 (%) — None이면 데이터 부족"""
    if len(closes) < MOMENTUM_DAYS_LONG + 1:
        return None
    c_now = closes[-1]
    c_20  = closes[-(MOMENTUM_DAYS_SHORT + 1)]
    c_60  = closes[-(MOMENTUM_DAYS_LONG  + 1)]
    r20   = (c_now / c_20 - 1) * 100 if c_20 > 0 else 0.0
    r60   = (c_now / c_60 - 1) * 100 if c_60 > 0 else 0.0
    return round(MOMENTUM_W_SHORT * r20 + MOMENTUM_W_LONG * r60, 3)


def _ret_n(closes, n):
    if len(closes) < n + 1:
        return 0.0
    return (closes[-1] / closes[-(n + 1)] - 1) * 100


def get_all_scores():
    """전 ETF 모멘텀 스코어 계산 → {code: {score, ret5, ret20, closes, avg_vol_krw}}"""
    scores = {}
    for code, meta in ETF_UNIVERSE.items():
        # 과거 잘못된 코드로 매수된 잔고는 매도/동기화만 하고 신규 편입은 금지한다.
        if not meta.get("trade_enabled", True):
            continue
        closes, vol_list = get_daily_chart(code, 70)
        if not closes:
            cprint(f"  [{code}] 데이터 없음", Fore.YELLOW)
            continue
        score = calc_momentum_score(closes)
        if score is None:
            cprint(f"  [{code}] 데이터 부족 ({len(closes)}일)", Fore.YELLOW)
            continue
        # 20일 평균 거래대금 계산
        recent_vol = vol_list[-20:] if len(vol_list) >= 20 else vol_list
        avg_vol_krw = sum(recent_vol) / len(recent_vol) if recent_vol else 0
        scores[code] = {
            "score":       score,
            "ret5":        _ret_n(closes, 5),
            "ret20":       _ret_n(closes, 20),
            "closes":      closes,
            "avg_vol_krw": avg_vol_krw,
        }
        time.sleep(0.3)
    return scores


# ============================================================
# [12] 필터
# ============================================================
def is_tradeable_time(for_buy=True):
    now  = datetime.now()
    h, m = now.hour, now.minute
    if h < TRADE_START_H or (h == TRADE_START_H and m < TRADE_START_M):
        return False
    if for_buy:
        if h > TRADE_BUY_END_H or (h == TRADE_BUY_END_H and m >= TRADE_BUY_END_M):
            return False
        if NO_BUY_FRIDAY and now.weekday() == 4:
            return False
    else:
        if h > TRADE_SELL_END_H or (h == TRADE_SELL_END_H and m >= TRADE_SELL_END_M):
            return False
    return True


def is_kospi_no_trade():
    """코스피 -3% 이하만 신규 매수 금지 (+3% 급등은 허용)"""
    chg = get_kospi_change_pct()
    if chg <= -3.0:
        cprint(f"[No Trade Zone] 코스피 {chg:+.1f}%", Fore.YELLOW)
        return True
    return False


def is_overheat(closes):
    return _ret_n(closes, 5) > OVERHEAT_PCT


def is_cooldown(code, closes=None):
    until = cooldown_list.get(code)
    if not until:
        return False
    # 조건부 해제: 20일 수익률이 회복됐으면 쿨다운 해제
    if closes is not None and _ret_n(closes, 20) > 0:
        del cooldown_list[code]
        cprint(f"[쿨다운 해제] {code} 20일 수익률 회복 → 편입 허용", Fore.GREEN)
        return False
    if date.today() <= until:
        return True
    del cooldown_list[code]
    return False


def check_liquidity(info, avg_volume_krw=None):
    """유동성 체크. avg_volume_krw가 있으면 상대 기준 사용, 없으면 절대 기준."""
    vol_krw = info.get("volume_krw", 0)
    if avg_volume_krw and avg_volume_krw > 0:
        # 당일 거래대금이 20일 평균의 30% 미만이면 유동성 부족
        if vol_krw < avg_volume_krw * 0.3:
            return False
    else:
        if vol_krw < MIN_VOLUME_KRW:
            return False
    bid = info.get("bid", 0)
    ask = info.get("ask", 0)
    p   = info.get("price", 1)
    if bid > 0 and ask > 0 and p > 0:
        if (ask - bid) / p * 100 > MAX_SPREAD_PCT:
            return False
    return True


def register_cooldown_if_needed(code, closes):
    """20일 수익률 -10% 이하 → 쿨다운 등록"""
    # 이미 쿨다운이면 is_cooldown()이 해제/유지 판단을 담당한다.
    if code in cooldown_list:
        return True
    if _ret_n(closes, 20) <= -10.0:
        until = date.today() + timedelta(days=COOLDOWN_DAYS)
        cooldown_list[code] = until
        cprint(f"[쿨다운 등록] {code} 20일 {_ret_n(closes,20):.1f}% → {until}까지", Fore.YELLOW)
        return True
    return False


# ============================================================
# [13] 킬 스위치 / MDD / 대피 시스템
# ============================================================
def _calc_portfolio_value():
    total = 0
    for code, pos in list(portfolio.items()):
        info  = get_price_info(code)
        raw   = info.get("price", 0) if info else 0
        price = raw if raw > 0 else pos["avg_price"]
        total += price * pos["qty"]
    return total


def _calc_account_value():
    """ETF 평가액과 주문가능 현금을 합산한다. 현금 조회 실패 시 None."""
    cash = get_cash_balance()
    if cash is None:
        return None
    return _calc_portfolio_value() + cash


def check_kill_switch():
    global kill_switch_active, risk_lock_day
    if daily_loss_base <= 0:
        return kill_switch_active
    # 미실현 손실 포함: 현재 총 평가액과 당일 시작 기준 비교
    total = _calc_account_value()
    if total is None:
        cprint("[킬스위치] 잔고 조회 실패 — 체크 건너뜀", Fore.YELLOW)
        return kill_switch_active
    # total이 비정상적으로 낮으면 (API 오류) 체크 건너뜀
    if total < TOTAL_BUDGET * 0.1:
        cprint(f"[킬스위치] 총액 비정상({total:,.0f}원) — 체크 건너뜀", Fore.YELLOW)
        return kill_switch_active
    loss_pct = (total - daily_loss_base) / daily_loss_base * 100
    if loss_pct <= KILL_DAY_LOSS and not kill_switch_active:
        kill_switch_active = True
        risk_lock_day = date.today()
        send_msg(
            f"🔴 킬 스위치 발동!\n"
            f"당일 손실: {loss_pct:.1f}% (미실현 포함)\n"
            f"→ DEFENSE_1 진입",
            force=True,
        )
        threading.Thread(target=lambda: _enter_defense_1("킬 스위치"), daemon=True).start()
    return kill_switch_active


def check_mdd():
    global mdd_active, peak_value, risk_lock_day
    if peak_value <= 0:
        return mdd_active
    total = _calc_account_value()
    if total is None or total < TOTAL_BUDGET * 0.1:
        cprint("[MDD 스킵] 계좌 평가액 조회 실패 또는 비정상값", Fore.YELLOW)
        return mdd_active
    if total > peak_value:
        peak_value = total
    dd = (total - peak_value) / peak_value * 100
    if dd <= MAX_DD_PCT and not mdd_active:
        mdd_active = True
        risk_lock_day = date.today()
        send_msg(
            f"🚨 MDD 한도 도달! ({dd:.1f}%)\n→ DEFENSE_1 진입",
            force=True,
        )
        threading.Thread(target=lambda: _enter_defense_1("MDD 한도"), daemon=True).start()
    return mdd_active


# ── 대피 공통 유틸 ─────────────────────────────────────────
def _sell_all_etf(exclude=None):
    """인버스 제외 전 종목 시장가 매도. exclude: 추가 제외 코드 집합."""
    excluded = {INVERSE_CODE}
    if exclude:
        excluded.update(exclude)
    failed = []
    for code in list(portfolio.keys()):
        if code in excluded:
            continue
        ok = sell_etf(code, reason="대피매도")
        if not ok:
            # 재시도 2회
            for _ in range(2):
                time.sleep(1)
                ok = sell_etf(code, reason="대피매도_재시도")
                if ok:
                    break
        if not ok:
            failed.append(code)
        else:
            time.sleep(0.5)
    if failed:
        send_msg(f"⚠️ 매도 실패: {', '.join(failed)}\n수동 처리 필요", force=True)
    return len(failed) == 0


def _get_inverse_return_pct():
    """인버스 ETF 현재 수익률(%)"""
    pos = portfolio.get(INVERSE_CODE)
    if not pos or pos["avg_price"] <= 0:
        return 0.0
    info  = get_price_info(INVERSE_CODE)
    price = (info.get("price", 0) if info else 0) or pos["avg_price"]
    return (price - pos["avg_price"]) / pos["avg_price"] * 100


def _buy_inverse_to_ratio(target_ratio):
    """전체 자산 대비 인버스 비중이 target_ratio가 되도록 추가 매수."""
    cash      = get_cash_balance()
    if cash is None:
        cprint("[인버스 매수] 현금 조회 실패 — 건너뜀", Fore.YELLOW)
        return
    pf_val    = _calc_portfolio_value()
    total_val = pf_val + cash
    if total_val <= 0:
        return
    inv_pos   = portfolio.get(INVERSE_CODE)
    inv_val   = 0.0
    if inv_pos:
        info    = get_price_info(INVERSE_CODE)
        price   = (info.get("price", 0) if info else 0) or inv_pos["avg_price"]
        inv_val = price * inv_pos["qty"]
    target_val = total_val * target_ratio
    need       = target_val - inv_val
    if need < 5_000:
        return
    buy_etf(INVERSE_CODE, int(need))


def _sell_inverse_by_ratio(sell_ratio):
    """전체 자산의 sell_ratio 만큼 인버스 매도."""
    pos = portfolio.get(INVERSE_CODE)
    if not pos:
        return
    pf_val    = _calc_portfolio_value()
    cash      = get_cash_balance()
    if cash is None:
        cprint("[인버스 매도] 현금 조회 실패 — 건너뜀", Fore.YELLOW)
        return
    total_val = pf_val + cash
    info      = get_price_info(INVERSE_CODE)
    price     = (info.get("price", 0) if info else 0) or pos["avg_price"]
    if price <= 0:
        return
    sell_val = total_val * sell_ratio
    qty      = min(int(sell_val / price), pos["qty"])
    if qty > 0:
        sell_etf(INVERSE_CODE, qty, reason="단계하향매도")


def _set_defense_stage(new_stage, reason=""):
    global defense_stage, inverse_peak_return, defense_down_date
    old = defense_stage
    defense_stage = new_stage
    if new_stage == "NORMAL":
        inverse_peak_return = 0.0
        defense_down_date   = None
    cprint(f"[대피단계] {old} → {new_stage}  {reason}", Fore.MAGENTA, bright=True)
    send_msg(
        f"🛡 대피 단계 변경\n{old} → {new_stage}\n{reason}",
        force=True,
    )
    # DEFENSE 로그 기록
    _log_defense(
        old_stage  = old,
        new_stage  = new_stage,
        reason     = reason,
        kospi_pct  = get_kospi_intraday_pct(),
        pf_ret     = (_calc_portfolio_value() - initial_value) / initial_value * 100 if initial_value > 0 else None,
    )
    _save_state()


# ── 단계 상향 ─────────────────────────────────────────────
@_account_action
def _enter_defense_1(reason=""):
    """NORMAL → DEFENSE_1: 전 ETF 매도 후 현금 100%"""
    with _defense_lock:
        if defense_stage != "NORMAL":
            return
        send_msg(f"🚨 DEFENSE_1 진입 — {reason}\nETF 전량 매도 중...", force=True)
        _sell_all_etf()
        _set_defense_stage("DEFENSE_1", reason)


@_account_action
def _enter_defense_2():
    """DEFENSE_1 → DEFENSE_2: 현금 30% 인버스 매수"""
    global inverse_peak_return
    with _defense_lock:
        if defense_stage != "DEFENSE_1":
            return
        send_msg("📉 DEFENSE_2 진입 — 인버스 30% 매수", force=True)
        inverse_peak_return = 0.0
        _buy_inverse_to_ratio(0.30)
        _set_defense_stage("DEFENSE_2")


@_account_action
def _enter_defense_3():
    """DEFENSE_2 → DEFENSE_3: 인버스 50%까지 확대"""
    with _defense_lock:
        if defense_stage != "DEFENSE_2":
            return
        send_msg("📉 DEFENSE_3 진입 — 인버스 50%까지 확대", force=True)
        _buy_inverse_to_ratio(0.50)
        _set_defense_stage("DEFENSE_3")


# ── 단계 하향 (루프마다 호출) ─────────────────────────────
def _step_down_defense():
    """단계 하향 — 1일 유지 확인 후 10%씩 단계적 매도"""
    global defense_down_date
    kospi_pct = get_kospi_intraday_pct()
    today     = date.today()

    if defense_stage == "DEFENSE_3":
        if kospi_pct > D_DOWN_3TO2:
            if defense_down_date is None:
                defense_down_date = today
            elif today > defense_down_date:        # 1일 유지 확인
                inv_ratio = _current_inverse_ratio()
                if inv_ratio > 0.30:
                    _sell_inverse_by_ratio(0.10)
                else:
                    _set_defense_stage("DEFENSE_2", "지수 회복")
        else:
            defense_down_date = None

    elif defense_stage == "DEFENSE_2":
        if kospi_pct > D_DOWN_2TO1:
            if defense_down_date is None:
                defense_down_date = today
            elif today > defense_down_date:
                inv_ratio = _current_inverse_ratio()
                if inv_ratio > 0.20:
                    _sell_inverse_by_ratio(0.10)
                elif inv_ratio > 0.0:
                    sell_etf(INVERSE_CODE, reason="단계하향완료")
                else:
                    _set_defense_stage("DEFENSE_1", "인버스 정리 완료")
        else:
            defense_down_date = None

    elif defense_stage == "DEFENSE_1":
        if kospi_pct > D_DOWN_1WAIT:
            if defense_down_date is None:
                defense_down_date = today
            elif today > defense_down_date:
                _set_defense_stage("DEFENSE_WAIT", "복귀 대기 시작")
        else:
            defense_down_date = None


def _current_inverse_ratio():
    """현재 인버스 비중 (0.0 ~ 1.0)"""
    pos = portfolio.get(INVERSE_CODE)
    if not pos:
        return 0.0
    info  = get_price_info(INVERSE_CODE)
    price = (info.get("price", 0) if info else 0) or pos["avg_price"]
    inv_val   = price * pos["qty"]
    pf_val    = _calc_portfolio_value()
    cash      = get_cash_balance()
    if cash is None:
        return 0.0
    total_val = pf_val + cash
    return inv_val / total_val if total_val > 0 else 0.0


# ── 인버스 익절 / 손절 (단계와 독립적으로 항상 체크) ────────
def check_inverse_exit():
    global inverse_peak_return
    if INVERSE_CODE not in portfolio:
        return
    ret = _get_inverse_return_pct()
    # 고점 갱신
    if ret > inverse_peak_return:
        inverse_peak_return = ret
    # 트레일링 익절: 고점 > 0% AND 현재 <= 고점 - 2%
    if inverse_peak_return > 0 and ret <= inverse_peak_return - INVERSE_TRAIL_GAP:
        cprint(f"[인버스 익절] 고점:{inverse_peak_return:.1f}% 현재:{ret:.1f}%", Fore.CYAN)
        send_msg(
            f"💰 인버스 트레일링 익절\n고점:{inverse_peak_return:.1f}% / 현재:{ret:.1f}%",
            force=True,
        )
        sell_etf(INVERSE_CODE, reason="인버스익절")
        inverse_peak_return = 0.0
        return
    # 손절 -4%
    if ret <= INVERSE_STOP_LOSS:
        cprint(f"[인버스 손절] {ret:.1f}%", Fore.RED)
        send_msg(f"🔴 인버스 손절 {ret:.1f}%", force=True)
        sell_etf(INVERSE_CODE, reason="인버스손절")
        inverse_peak_return = 0.0


# ── 단계 상향 체크 (루프마다 호출) ──────────────────────────
def check_defense_escalate():
    """장중 단계 상향 조건 체크"""
    kospi_pct = get_kospi_intraday_pct()
    pf_val    = _calc_portfolio_value()
    cash      = get_cash_balance()
    if cash is None:
        cprint("[대피 단계] 현금 조회 실패 — 단계 판단 건너뜀", Fore.YELLOW)
        return
    total_val = pf_val + cash
    pf_ret    = (total_val - initial_value) / initial_value * 100 if initial_value > 0 else 0.0

    if defense_stage == "NORMAL":
        if kospi_pct <= D1_KOSPI_PCT and pf_ret <= D1_PF_PCT:
            threading.Thread(
                target=lambda: _enter_defense_1(
                    f"코스피{kospi_pct:.1f}% / 포트{pf_ret:.1f}%"
                ), daemon=True
            ).start()

    elif defense_stage == "DEFENSE_1":
        if kospi_pct <= D2_KOSPI_PCT and consecutive_down_days >= D2_CONSEC:
            # 하락 추세 확인: 코스피 현재가 < 5일 이동평균
            ma5   = get_kospi_ma5()
            price = get_kospi_info().get("price", 0.0)
            if ma5 <= 0 or price < ma5:
                threading.Thread(target=_enter_defense_2, daemon=True).start()

    elif defense_stage == "DEFENSE_2":
        if kospi_pct <= D3_KOSPI_PCT and consecutive_down_days >= D3_CONSEC:
            ma5   = get_kospi_ma5()
            price = get_kospi_info().get("price", 0.0)
            if ma5 <= 0 or price < ma5:
                threading.Thread(target=_enter_defense_3, daemon=True).start()


# ── 복귀 ────────────────────────────────────────────────────
def check_defense_resume():
    """자동 복귀 조건 체크 — DEFENSE_WAIT 상태에서만. 3개 중 2개 이상 충족 시 복귀."""
    if defense_stage != "DEFENSE_WAIT":
        return
    kospi_pct = get_kospi_intraday_pct()
    vol_ratio = get_kospi_volume_ratio()
    ma5   = get_kospi_ma5()
    price = get_kospi_info().get("price", 0.0)

    cond_kospi = kospi_pct >= RESUME_KOSPI_PCT
    cond_vol   = vol_ratio >= RESUME_VOL_RATIO
    cond_ma    = ma5 > 0 and price >= ma5

    met = sum([cond_kospi, cond_vol, cond_ma])
    if met < 2:
        return
    send_msg(
        f"🟢 자동 복귀 조건 충족 ({met}/3) — ETF 재매수 시작\n"
        f"코스피: {'✅' if cond_kospi else '❌'} {kospi_pct:+.1f}%  "
        f"거래량: {'✅' if cond_vol else '❌'} {vol_ratio:.1f}배  "
        f"5일선: {'✅' if cond_ma else '❌'}",
        force=True
    )
    _do_resume()


def check_auto_risk_recovery():
    """자동 킬/MDD 이후의 보수적 자동 복구.

    운영자가 /kill 또는 /stop으로 멈춘 경우(manual_pause)는 절대 자동 해제하지 않는다.
    자동 위험정지는 다음 거래일 이후, 보유 ETF가 없고 시장 회복 3조건을 모두 만족할 때만
    해제한 뒤 한 번 리밸런싱한다.
    """
    global kill_switch_active, mdd_active, peak_value, initial_value, risk_lock_day
    if not AUTO_RISK_RECOVERY_ENABLED or manual_pause or not (kill_switch_active or mdd_active):
        return
    if risk_lock_day is None:
        risk_lock_day = date.today()
        _save_state()
        return
    if (date.today() - risk_lock_day).days < AUTO_RISK_RECOVERY_DAYS:
        return
    if portfolio:  # 실제 보유/인버스가 있으면 방어 상태를 유지한다.
        return
    account_value = _calc_account_value()
    kospi_pct = get_kospi_intraday_pct()
    vol_ratio = get_kospi_volume_ratio()
    ma5 = get_kospi_ma5()
    price = get_kospi_info().get("price", 0.0)
    if account_value is None or not (kospi_pct >= RESUME_KOSPI_PCT and vol_ratio >= RESUME_VOL_RATIO
                                     and ma5 > 0 and price >= ma5):
        return
    kill_switch_active = False
    mdd_active = False
    risk_lock_day = None
    peak_value = account_value
    initial_value = account_value
    _set_defense_stage("NORMAL", "자동 위험정지 복구")
    _save_state()
    send_msg("🟢 자동 위험정지 복구 — 시장 회복 확인 후 리밸런싱 시작", force=True)
    threading.Thread(target=_do_rebalance, daemon=True).start()


@_account_action
def _do_resume():
    global consecutive_down_days
    """인버스 매도 + ETF 재매수 + NORMAL 전환"""
    if INVERSE_CODE in portfolio:
        sell_etf(INVERSE_CODE, reason="복귀매도")
        time.sleep(2)
    _set_defense_stage("NORMAL", "복귀 완료")
    consecutive_down_days = 0
    threading.Thread(target=_do_rebalance, daemon=True).start()


# ── (하위 호환) KOFR 대피 명령을 DEFENSE_1로 연결 ───────────
def _evacuate_to_kofr(reason):
    """기존 코드 호환용 — DEFENSE_1 진입으로 처리"""
    _enter_defense_1(reason)


# ============================================================
# [14] 보유 포지션 모니터링 (트레일링 스탑 / 손절)
# ============================================================
def monitor_positions():
    if not portfolio or not is_tradeable_time(for_buy=False):
        return

    for code in list(portfolio.keys()):
        if code == INVERSE_CODE:
            continue   # 인버스는 check_inverse_exit() 에서 별도 관리
        pos  = portfolio.get(code)
        if not pos:
            continue
        info  = get_price_info(code)
        if not info or info["price"] <= 0:
            continue

        price   = info["price"]
        avg     = pos["avg_price"]
        high    = pos["high_price"]
        pnl_pct = (price - avg) / avg * 100 if avg > 0 else 0.0

        # 고점 갱신
        if price > high:
            portfolio[code]["high_price"] = price
            high = price

        # 트레일링 스탑: +6% 후 고점 대비 -2%
        if pnl_pct >= TRAIL_START_PCT:
            trail_price = high * (1 - TRAIL_GAP_PCT / 100)
            if price <= trail_price:
                name = ETF_UNIVERSE.get(code, {}).get("name", code)
                cprint(f"[트레일링] {name} {pnl_pct:+.1f}% → 매도", Fore.CYAN)
                send_msg(f"💰 트레일링 스탑\n{name}\n수익: {pnl_pct:+.1f}%", force=True)
                sell_etf(code, reason="트레일링스탑")
                continue

        # 손절 -5%
        if pnl_pct <= STOP_LOSS_PCT:
            name = ETF_UNIVERSE.get(code, {}).get("name", code)
            cprint(f"[손절] {name} {pnl_pct:+.1f}%", Fore.RED)
            send_msg(f"🔴 손절\n{name}\n손실: {pnl_pct:+.1f}%", force=True)
            sell_etf(code, reason="손절")
            cooldown_list[code] = date.today() + timedelta(days=COOLDOWN_DAYS)


# ============================================================
# [15] 리밸런싱
# ============================================================
@_account_action
def _do_rebalance():
    """모멘텀 기반 ETF 교체 — NORMAL 상태에서만 실행"""
    global portfolio

    # 모든 호출 경로(스케줄·자동복구·Telegram 수동 명령)에 동일한 매수 시간과
    # 금요일 신규매수 제한을 적용한다.
    if not is_tradeable_time(for_buy=True):
        cprint("[리밸런싱] 신규 매수 가능 시간이 아니어서 건너뜀", Fore.YELLOW)
        return
    if defense_stage != "NORMAL":
        send_msg(f"⚠️ 리밸런싱 불가 — 현재 {defense_stage} 상태\n/resume 으로 복귀 후 재시도", force=True)
        return

    cprint("[리밸런싱] 모멘텀 스코어 계산 시작...", Fore.CYAN, bright=True)
    send_msg("🔄 리밸런싱 시작 — 스코어 계산 중 (약 30초)...", force=True)

    scores = get_all_scores()
    if not scores:
        send_msg("❌ 리밸런싱 실패 — 데이터 없음", force=True)
        return

    # 필터 적용 — 과열 종목은 제외 아닌 비중 축소 태그
    filtered = {}
    overheat_codes = set()
    for code, data in scores.items():
        closes = data["closes"]
        if is_cooldown(code, closes):
            cprint(f"  [{code}] 쿨다운 — 제외", Fore.YELLOW)
            continue
        if register_cooldown_if_needed(code, closes):
            continue
        info = get_price_info(code)
        if info and not check_liquidity(info, data.get("avg_vol_krw")):
            cprint(f"  [{code}] 유동성 부족 — 제외", Fore.YELLOW)
            continue
        if data["score"] <= MIN_SCORE_THRESHOLD:
            cprint(f"  [{code}] 스코어 {data['score']:.1f}% ≤ {MIN_SCORE_THRESHOLD}% — 제외", Fore.YELLOW)
            continue
        if is_overheat(closes):
            cprint(f"  [{code}] 과열 — 비중 50% 축소", Fore.YELLOW)
            overheat_codes.add(code)
        filtered[code] = data

    # 상위 TOP_N 선정. 동일 테마가 상위권을 독식해도 한 테마당 최대 2개로 제한한다.
    ranked    = sorted(filtered.items(), key=lambda x: x[1]["score"], reverse=True)
    top_codes = []
    tag_counts = {}
    for code, _ in ranked:
        tag = ETF_UNIVERSE.get(code, {}).get("tag", "기타")
        if tag_counts.get(tag, 0) >= MAX_ETF_PER_TAG:
            cprint(f"  [{code}] 테마 집중 한도({tag}) — 제외", Fore.YELLOW)
            continue
        top_codes.append(code)
        tag_counts[tag] = tag_counts.get(tag, 0) + 1
        if len(top_codes) >= TOP_N_ETF:
            break

    # 유효 종목이 아예 없으면 KOFR 대피
    if not top_codes:
        send_msg("⚠️ 유효 종목 없음 → KOFR 대피", force=True)
        _evacuate_to_kofr("모멘텀 없음")
        return

    # 유효 종목이 TOP_N 미달이면 경고만 (있는 것만 매수)
    if len(top_codes) < TOP_N_ETF:
        send_msg(
            f"⚠️ 유효 종목 {len(top_codes)}개 (기준 {TOP_N_ETF}개 미달) — 있는 것만 매수 진행",
            force=True,
        )

    # 교체 대상 매도 — Top5에 없는 것만 매도 (기존 보유 Top5는 유지)
    # 단, 트레일링 스탑이 이미 발동 중인 종목은 수익 극대화를 위해 유지
    current   = [c for c in portfolio if c != KOFR_CODE]
    to_sell   = []
    for c in current:
        if c in top_codes:
            continue
        pos = portfolio.get(c, {})
        price_now = get_price_info(c)
        if price_now and price_now.get("price", 0) > 0 and pos.get("avg_price", 0) > 0:
            pnl_pct = (price_now["price"] - pos["avg_price"]) / pos["avg_price"] * 100
            # 트레일링 활성 구간(+4% 이상)이면 리밸런싱 매도 보류
            if pnl_pct >= TRAIL_START_PCT:
                cprint(f"  [{c}] 트레일링 활성({pnl_pct:.1f}%) — 리밸런싱 매도 보류", Fore.CYAN)
                continue
        to_sell.append(c)

    cash_before_sell = get_cash_balance()
    expected_release = 0
    for code in to_sell:
        name = ETF_UNIVERSE.get(code, {}).get("name", code)
        pos = portfolio.get(code, {})
        quote = get_price_info(code) or {}
        expected_release += (quote.get("bid") or quote.get("price") or pos.get("avg_price", 0)) * pos.get("qty", 0)
        cprint(f"  [교체 매도] {name}", Fore.YELLOW)
        sell_etf(code, reason="리밸런싱")
        time.sleep(1)

    if to_sell:
        cash_after_sell = _wait_for_cash_settlement(cash_before_sell, expected_release)
    else:
        cash_after_sell = get_cash_balance()

    # 신규 매수 — 과열 종목은 절반 예산만 사용
    cash        = cash_after_sell
    if cash is None:
        send_msg("⚠️ 리밸런싱 중단 — 주문가능 현금 조회 실패", force=True)
        return
    kofr_rsv    = int(TOTAL_BUDGET * KOFR_MIN_RATIO)
    investable  = max(0, min(cash, TOTAL_BUDGET) - kofr_rsv)
    # 1주도 살 수 없는 고가 ETF 때문에 계획 비중이 무너지는 것을 막는다.
    # 예산 안에서 1주 매수가 가능한 종목 수로 자동 축소한 뒤 다시 균등 배분한다.
    planned_codes = list(top_codes)
    planned_prices = {}
    for code in planned_codes:
        info = get_price_info(code) or {}
        planned_prices[code] = info.get("ask") or info.get("price", 0)
    while len(planned_codes) > 1:
        per_candidate = investable // len(planned_codes)
        unaffordable = [c for c in planned_codes if not planned_prices.get(c) or planned_prices[c] > per_candidate]
        if not unaffordable:
            break
        # 점수가 낮은 비매수 가능 후보부터 제외한다.
        drop = min(unaffordable, key=lambda c: filtered[c]["score"])
        planned_codes.remove(drop)
        cprint(f"  [{drop}] 1주 가격이 균등 예산보다 높아 편입 수 축소", Fore.YELLOW)
    if not planned_codes or not planned_prices.get(planned_codes[0], 0):
        send_msg("⚠️ 리밸런싱 중단 — 예산으로 매수 가능한 ETF가 없습니다.", force=True)
        return
    per_etf = investable // len(planned_codes)
    bought  = []
    for code in planned_codes:
        if code in portfolio:
            cprint(f"  [{code}] 이미 보유 — 건너뜀", Fore.CYAN)
            continue
        budget = per_etf // 2 if code in overheat_codes else per_etf
        if budget < 5_000:
            cprint(f"  [{code}] 예산 부족 ({budget:,}원)", Fore.YELLOW)
            continue
        qty = buy_etf(code, budget)
        if qty > 0:
            bought.append(code)
            if code in overheat_codes:
                send_msg(f"⚠️ {ETF_UNIVERSE.get(code,{}).get('name',code)} 과열 — 절반 예산({budget:,}원) 편입", force=True)
        time.sleep(1)

    score_lines = []
    for code, data in ranked[:5]:
        name = ETF_UNIVERSE.get(code, {}).get("name", code)
        mark = "✅" if code in top_codes else "  "
        score_lines.append(f"{mark} {name}: {data['score']:+.1f}%")
    # 제외 종목 이유 표시
    excluded = {c: d for c, d in filtered.items() if c not in top_codes}
    all_universe = set(ETF_UNIVERSE.keys()) - {KOFR_CODE}
    for code in all_universe:
        name = ETF_UNIVERSE.get(code, {}).get("name", code)
        if code not in filtered:
            score_lines.append(f"  ✖ {name}: 스코어≤0 또는 유동성부족")

    send_msg(
        f"✅ 리밸런싱 완료\n"
        f"매도: {len(to_sell)}개  매수: {len(bought)}개\n\n"
        f"📊 스코어 TOP5:\n" + "\n".join(score_lines),
        force=True,
    )


@_account_action
def _do_weekly_rotation():
    """하위 50% 교체 — 스코어 차이 2% 미만이면 생략"""
    current = [c for c in portfolio if c != KOFR_CODE]
    if len(current) < 2:
        return
    scores = get_all_scores()
    if not scores:
        return

    held_scores = {c: scores[c]["score"] for c in current if c in scores}
    sorted_held = sorted(held_scores.items(), key=lambda x: x[1])
    bottom      = sorted_held[:max(1, len(sorted_held) // 2)]
    all_ranked  = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)

    for b_code, b_score in bottom:
        for c_code, c_data in all_ranked:
            if c_code in current or is_cooldown(c_code):
                continue
            diff = c_data["score"] - b_score
            if diff < 2.0:
                cprint(f"[주간교체] 차이 {diff:.1f}% < 2% — 생략 ({b_code})", Fore.CYAN)
                break
            b_name = ETF_UNIVERSE.get(b_code, {}).get("name", b_code)
            c_name = ETF_UNIVERSE.get(c_code, {}).get("name", c_code)
            cprint(f"[주간교체] {b_name} → {c_name} (차이 {diff:.1f}%)", Fore.CYAN)
            before_cash = get_cash_balance()
            old_pos = portfolio.get(b_code, {})
            old_quote = get_price_info(b_code) or {}
            expected_release = (old_quote.get("bid") or old_quote.get("price") or old_pos.get("avg_price", 0)) * old_pos.get("qty", 0)
            sell_etf(b_code, reason="주간교체")
            cash = _wait_for_cash_settlement(before_cash, expected_release)
            if cash is None:
                cprint("[주간교체] 현금 조회 실패 — 신규 매수 생략", Fore.YELLOW)
                break
            kofr_rsv = int(TOTAL_BUDGET * KOFR_MIN_RATIO)
            available = max(0, min(cash, TOTAL_BUDGET) - kofr_rsv)
            replacement_budget = min(int(expected_release), available)
            if replacement_budget >= 5_000:
                buy_etf(c_code, replacement_budget)
            break


# ============================================================
# [16] 스케줄러
# ============================================================
_last_monthly_rebal = None
_last_weekly_rebal  = None


def check_rebalance_schedule():
    global _last_monthly_rebal, _last_weekly_rebal
    today = date.today()
    now   = datetime.now()

    # 9:15~9:30 사이에만 실행
    if not (now.hour == 9 and 15 <= now.minute <= 30):
        return

    # 스코어 계산 및 로그 기록 (매주 월요일 9:15~9:30)
    if today.weekday() == 0:
        scores = get_all_scores()
        if scores:
            _log_scores(scores, trigger="weekly_check")

    # 월 1회 전체 교체: 이달 첫 번째 월요일
    if today.weekday() == 0 and today.day <= 7:
        if _last_monthly_rebal != today:
            _last_monthly_rebal = today
            cprint("[스케줄] 월 1회 전체 리밸런싱", Fore.CYAN, bright=True)
            threading.Thread(target=_do_rebalance, daemon=True).start()
            return

    # 주 1회 하위 50% 교체: 매주 월요일
    if today.weekday() == 0 and _last_weekly_rebal != today:
        _last_weekly_rebal = today
        cprint("[스케줄] 주 1회 하위 ETF 교체", Fore.CYAN)
        threading.Thread(target=_do_weekly_rotation, daemon=True).start()


# ============================================================
# [17] 일간 리셋 / 상태 메시지
# ============================================================
def _daily_reset():
    global daily_pnl_krw, trade_count, last_reset_day, daily_loss_base
    today = date.today()
    if last_reset_day == today:
        return
    if last_reset_day is not None:
        send_msg(
            f"📊 일간 리포트 [{last_reset_day}]\n"
            f"손익: {daily_pnl_krw:+,}원\n"
            f"거래: {trade_count}회\n"
            f"보유: {len(portfolio)}종목",
            force=True,
        )
    daily_pnl_krw   = 0
    trade_count     = 0
    last_reset_day  = today
    # 킬스위치 기준 = 당일 시작 시점 실제 총 평가액 (포트 + 현금)
    pf_val          = _calc_portfolio_value()
    cash            = get_cash_balance()
    actual_total    = pf_val + cash if cash is not None else 0
    daily_loss_base = actual_total if actual_total > TOTAL_BUDGET * 0.2 else TOTAL_BUDGET
    cprint(f"[일간 리셋] {today} / 기준액: {daily_loss_base:,}원", Fore.CYAN)
    _save_state()


def _send_status():
    kospi = get_kospi_change_pct()
    val   = _calc_portfolio_value()
    cash  = get_cash_balance()
    cash_display = cash if cash is not None else 0
    # MDD는 포트폴리오 평가액 기준 (현금 포함 시 매도 직후 튀는 문제 방지)
    dd    = (val - peak_value) / peak_value * 100 if peak_value > 0 and portfolio else 0.0
    lines = [
        f"📊 섹터봇 현황 [{datetime.now().strftime('%H:%M:%S')}]",
        f"━━━━━━━━━━━━━━━━━━",
        f"코스피:   {kospi:+.1f}%",
        f"포트폴리오: {val:,.0f}원",
        f"현금:     {cash_display:,.0f}원" + (" (조회 실패)" if cash is None else ""),
        f"오늘 손익: {daily_pnl_krw:+,}원",
        f"MDD:      {dd:.1f}%",
        f"킬스위치: {'🔴 ON' if kill_switch_active else '🟢 OFF'}",
        f"대피단계: {defense_stage}",
        f"━━━━━━━━━━━━━━━━━━",
    ]
    # ETF 포지션
    if portfolio:
        for code, pos in list(portfolio.items()):
            info  = get_price_info(code)
            price = info.get("price", pos["avg_price"]) if info else pos["avg_price"]
            pnl_p = (price - pos["avg_price"]) / pos["avg_price"] * 100 if pos["avg_price"] > 0 else 0.0
            pnl_w = int((price - pos["avg_price"]) * pos["qty"])
            name  = ETF_UNIVERSE.get(code, {}).get("name", KOFR_NAME if code == KOFR_CODE else code)
            icon  = "🟢" if pnl_p >= 0 else "🔴"
            lines.append(f"  {icon} {name}: {pos['qty']}주 {pnl_p:+.1f}% ({pnl_w:+,}원)")
    else:
        lines.append("  ETF 없음 (현금 대기)")

    # hold 포지션
    if hold_positions:
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("📌 수동 포지션")
        for code, pos in list(hold_positions.items()):
            info  = get_price_info(code)
            avg   = pos["avg_price"]
            qty   = pos["qty"]
            name  = pos.get("name", code)
            # 매도 조건 요약 (비율이면 실제 가격도 표시)
            if pos["sl_type"] == "pct":
                sl_price = avg * (1 + pos["sl_val"] / 100)
                sl_str = f"{pos['sl_val']:+.0f}% ({sl_price:,.0f}원)"
            else:
                sl_pct = (pos["sl_val"] - avg) / avg * 100
                sl_str = f"{pos['sl_val']:,}원 ({sl_pct:+.1f}%)"
            if pos["tp_type"] == "pct":
                tp_price = avg * (1 + pos["tp_val"] / 100)
                tp_str = f"+{pos['tp_val']:.0f}% ({tp_price:,.0f}원)"
            elif pos["tp_type"] == "price":
                tp_pct = (pos["tp_val"] - avg) / avg * 100
                tp_str = f"{pos['tp_val']:,}원 ({tp_pct:+.1f}%)"
            elif pos["tp_type"] in ("trail", "trail_be"):
                trail_price = avg * (1 + pos["trail_start"] / 100)
                tp_str = f"트레일+{pos['trail_start']:.0f}% ({trail_price:,.0f}원)/-{pos['trail_gap']:.0f}%"
            else:
                tp_str = pos["tp_type"]
            if info and info.get("price", 0) > 0:
                price = info["price"]
                pnl_p = (price - avg) / avg * 100
                pnl_w = int((price - avg) * qty)
                icon  = "🟢" if pnl_p >= 0 else "🔴"
                lines.append(
                    f"  {icon} {name}: {qty}주 {pnl_p:+.1f}% ({pnl_w:+,}원)\n"
                    f"    손절:{sl_str}  익절:{tp_str}"
                )
            else:
                lines.append(
                    f"  ⚪ {name}: {qty}주 @ {avg:,}원 (장외)\n"
                    f"    손절:{sl_str}  익절:{tp_str}"
                )

    send_msg("\n".join(lines), force=True)


def _send_portfolio():
    if not portfolio:
        send_msg("보유 ETF 없음 (현금 대기 중)", force=True)
        return
    lines = ["📦 보유 ETF 현황"]
    for code, pos in list(portfolio.items()):
        info  = get_price_info(code)
        price = info.get("price", pos["avg_price"]) if info else pos["avg_price"]
        pnl_p = (price - pos["avg_price"]) / pos["avg_price"] * 100 if pos["avg_price"] > 0 else 0.0
        pnl_w = int((price - pos["avg_price"]) * pos["qty"])
        name  = ETF_UNIVERSE.get(code, {}).get("name", KOFR_NAME if code == KOFR_CODE else code)
        lines.append(
            f"\n{name} ({code})\n"
            f"  {pos['qty']}주 @ 평균 {pos['avg_price']:,.0f}원\n"
            f"  현재 {price:,}원 ({pnl_p:+.1f}% / {pnl_w:+,}원)\n"
            f"  편입일: {pos['entry_date']}"
        )
    send_msg("\n".join(lines), force=True)


def _send_scores():
    send_msg("📊 스코어 계산 중... (약 30초)", force=True)
    def _calc():
        scores  = get_all_scores()
        ranked  = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)
        lines   = ["📊 모멘텀 스코어 순위\n━━━━━━━━━━━━"]
        for i, (code, data) in enumerate(ranked, 1):
            name = ETF_UNIVERSE.get(code, {}).get("name", code)
            cd   = " 🚫쿨다운" if is_cooldown(code) else ""
            held = " 📦보유" if code in portfolio else ""
            lines.append(
                f"{i}. {name}{cd}{held}\n"
                f"   스코어: {data['score']:+.1f}%  5일: {data['ret5']:+.1f}%  20일: {data['ret20']:+.1f}%"
            )
        send_msg("\n".join(lines), force=True)
    threading.Thread(target=_calc, daemon=True).start()


# ============================================================
# [18] 메인 루프
# ============================================================
def main():
    global kospi_baseline, _kospi_baseline_set, _last_main_error_alert
    global peak_value, initial_value, mdd_active, kill_switch_active
    cprint("=" * 52, Fore.CYAN, bright=True)
    cprint(f"  {BOT_NAME} v{BOT_VERSION} 시작", Fore.CYAN, bright=True)
    cprint("=" * 52, Fore.CYAN, bright=True)

    load_config()
    _load_kis_token()   # 파일 토큰 먼저 복원 (재시작/장외 대비)
    get_kis_token()     # 장중이면 신규 발급, 장외면 파일 토큰 그대로 사용
    _load_state()
    _start_ipc_thread()   # 매니저 ↔ 섹터봇 IPC 수신 스레드 시작

    # 시작 시 KIS 실제 잔고로 포트폴리오 자동 동기화
    try:
        real = get_kis_holdings()
        if real is not None:
            had_local_positions = bool(portfolio)
            valid_codes = set(ETF_UNIVERSE.keys()) | {KOFR_CODE}
            new_portfolio = {}
            for code, pos in real.items():
                if code in valid_codes:
                    old_pos = portfolio.get(code, {})
                    new_portfolio[code] = {
                        "qty":        pos["qty"],
                        "avg_price":  pos["avg_price"],
                        "high_price": max(old_pos.get("high_price", 0), pos["avg_price"]),
                        "entry_date": old_pos.get("entry_date", str(date.today())),
                    }
            portfolio.clear()
            portfolio.update(new_portfolio)
            # NORMAL 상태에서 로컬에만 남은 보유가 KIS에는 전혀 없다면,
            # 과거 가상 포지션의 peak/MDD가 다음 날까지 오염되지 않게 초기화한다.
            if had_local_positions and not portfolio and defense_stage == "NORMAL" and not manual_pause:
                actual = _calc_account_value()
                if actual is not None:
                    peak_value = actual
                    initial_value = actual
                    mdd_active = False
                    kill_switch_active = False
                    cprint("[시작 싱크] 로컬 유령 포지션 제거 → 위험 기준 재설정", Fore.YELLOW)
            _save_state()
            cprint(f"[시작 싱크] KIS 잔고 동기화 완료 — {len(portfolio)}종목", Fore.GREEN)
        else:
            cprint("[시작 싱크] KIS 잔고 조회 실패 — 기존 상태 유지", Fore.YELLOW)
    except Exception as e:
        cprint(f"[시작 싱크 오류] {e}", Fore.YELLOW)

    send_msg(
        f"🚀 섹터로테이션 봇 v{BOT_VERSION} 시작\n"
        f"예산: {TOTAL_BUDGET:,}원\n"
        f"모드: {'모의투자' if IS_MOCK else '실투자'}\n"
        f"ETF 유니버스: {len(ETF_UNIVERSE)}종\n"
        f"상위 편입: {TOP_N_ETF}개\n\n"
        f"/help 로 명령어 확인",
        force=True,
    )

    last_monitor      = 0.0
    last_tg_poll      = 0.0
    last_schedule     = 0.0
    last_defense_chk  = 0.0
    last_token_check  = time.time()
    last_status_write = 0.0
    last_market_snapshot = 0.0
    last_snapshot_day = None   # 장마감 스냅샷 날짜 (하루 1회)
    last_auto_entry_day = None
    _hold_open_reported_day  = None   # 장 시작 hold 보고 날짜
    _hold_close_reported_day = None   # 장 마감 hold 보고 날짜
    monitor_interval  = 120
    defense_interval  = 300

    while True:
        try:
            now_ts = time.time()
            now_dt = datetime.now()

            # 상태 파일 기록 (60초마다)
            if now_ts - last_status_write >= 60:
                _write_sector_status()
                last_status_write = now_ts

            # 장 시작 hold 보고 (평일 9:10~9:20, 하루 1회)
            if (now_dt.weekday() < 5 and now_dt.hour == 9
                    and 10 <= now_dt.minute <= 20
                    and _hold_open_reported_day != now_dt.date()):
                _hold_open_reported_day = now_dt.date()
                threading.Thread(target=_send_hold_open_report, daemon=True).start()

            # 장 마감 hold 보고 (평일 15:35~15:45, 하루 1회)
            if (now_dt.weekday() < 5 and now_dt.hour == 15
                    and 35 <= now_dt.minute <= 45
                    and _hold_close_reported_day != now_dt.date()):
                _hold_close_reported_day = now_dt.date()
                threading.Thread(target=_send_hold_close_report, daemon=True).start()

            # 장마감 스냅샷 (평일 15:35 이후 하루 1회)
            if (now_dt.weekday() < 5 and now_dt.hour == 15 and now_dt.minute >= 35
                    and last_snapshot_day != now_dt.date()):
                last_snapshot_day = now_dt.date()
                threading.Thread(target=_log_daily_snapshot, daemon=True).start()

            # 토큰 갱신 (23시간마다 — 장외/주말 포함)
            if now_ts - last_token_check > 3600 * 23:
                get_kis_token()
                last_token_check = now_ts

            # 텔레그램 폴링 (3초마다)
            # 매니저 실행 중: hold_ 콜백과 세션 숫자 입력만 선별 처리
            # 매니저 없을 때: 전체 폴링
            if now_ts - last_tg_poll >= 3:
                if not _manager_is_running():
                    poll_telegram()
                last_tg_poll = now_ts

            # 일간 리셋
            _daily_reset()

            # 장 시작 시 코스피 기준가 캡처 (9:10~9:15 사이 1회)
            if now_dt.hour == 9 and 10 <= now_dt.minute <= 15:
                if not _kospi_baseline_set:
                    info = get_kospi_info()
                    p    = info.get("price", 0.0)
                    if p > 0:
                        kospi_baseline      = p
                        _kospi_baseline_set = True
                        cprint(f"[코스피 기준가] {kospi_baseline:,.2f}", Fore.CYAN)
                        update_consecutive_down_days()
            elif now_dt.hour < 9:
                _kospi_baseline_set = False   # 익일 초기화

            # 킬스위치 / MDD 체크 (1분마다)
            if now_ts - last_monitor >= 60:
                if not kill_switch_active:
                    check_kill_switch()
                if not kill_switch_active and not mdd_active:
                    check_mdd()

            # 장중 루틴
            if is_tradeable_time(for_buy=False):
                # 정상 상태인데 실제 ETF 보유가 없으면 매일 한 번 자동 재진입한다.
                # 기존 월요일 리밸런싱만으로는 현금 상태가 며칠 지속될 수 있었다.
                if (AUTO_ENTRY_ENABLED and not portfolio and not manual_pause
                        and not kill_switch_active and not mdd_active and defense_stage == "NORMAL"
                        and now_dt.hour == 9 and 20 <= now_dt.minute <= 30
                        and last_auto_entry_day != now_dt.date()):
                    last_auto_entry_day = now_dt.date()
                    cprint("[자동진입] 빈 포트폴리오 → 모멘텀 리밸런싱", Fore.CYAN, bright=True)
                    threading.Thread(target=_do_rebalance, daemon=True, name="auto-entry").start()

                # 분석용 시장 관측 로그: 장중 30분마다, 매매와 분리된 스레드에서 기록
                if now_ts - last_market_snapshot >= MARKET_SNAPSHOT_INTERVAL:
                    last_market_snapshot = now_ts
                    threading.Thread(
                        target=_log_market_snapshot,
                        kwargs={"trigger": "interval"},
                        daemon=True,
                        name="market-snapshot",
                    ).start()

                # 포지션 모니터링 (2분마다)
                if now_ts - last_monitor >= monitor_interval:
                    if not kill_switch_active and not mdd_active and defense_stage == "NORMAL":
                        monitor_positions()
                    monitor_hold_positions()   # 수동 포지션은 항상 감시
                    if defense_stage != "NORMAL":
                        check_inverse_exit()
                    last_monitor = now_ts

                # 대피 단계 체크 (5분마다)
                if now_ts - last_defense_chk >= defense_interval:
                    if not kill_switch_active:
                        check_defense_escalate()
                        _step_down_defense()
                        check_defense_resume()
                    check_auto_risk_recovery()
                    last_defense_chk = now_ts

                # 리밸런싱 스케줄 체크 (5분마다)
                if now_ts - last_schedule >= 300:
                    if not kill_switch_active and not mdd_active:
                        check_rebalance_schedule()
                    last_schedule = now_ts

            time.sleep(2)

        except KeyboardInterrupt:
            cprint("\n[종료] Ctrl+C", Fore.YELLOW)
            send_msg("⏹ 섹터봇 수동 종료", force=True)
            _save_state()
            break
        except Exception as e:
            cprint(f"[메인 루프 오류] {e}", Fore.RED)
            traceback.print_exc()
            now_err = time.time()
            if now_err - _last_main_error_alert >= 300:
                _last_main_error_alert = now_err
                send_msg(f"⚠️ 섹터봇 메인 루프 오류\n{type(e).__name__}: {str(e)[:300]}\n5분 내 반복 오류는 묶어서 알립니다.", force=True)
            time.sleep(10)


# ============================================================
# [HOLD] 수동 포지션 관리 — /hold 명령어 기반 단계별 등록
# ============================================================

def _auto_params(price: float) -> dict:
    """주가 구간별 자동 손/익절 파라미터"""
    if price < 5_000:
        return {"sl": -7.0, "trail_start": 5.0, "trail_gap": 3.0}
    elif price < 20_000:
        return {"sl": -5.0, "trail_start": 4.0, "trail_gap": 2.0}
    elif price < 100_000:
        return {"sl": -4.0, "trail_start": 3.0, "trail_gap": 1.5}
    else:
        return {"sl": -3.0, "trail_start": 2.5, "trail_gap": 1.2}


def _get_stock_name(code: str) -> str:
    """KIS API로 종목명 조회 — ETF 유니버스 우선, 실패 시 코드 반환"""
    if code in ETF_UNIVERSE:
        return ETF_UNIVERSE[code]["name"]
    if code == KOFR_CODE:
        return KOFR_NAME
    h = kis_headers("FHKST01010100")
    if not h:
        return code
    try:
        res = api_call(
            "get",
            f"{_prod_url()}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=h,
            params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
        )
        name = res.get("output", {}).get("hts_kor_isnm", "").strip()
        return name if name else code
    except Exception:
        return code


# ── 단계별 플로우 ─────────────────────────────────────────

def _hold_step1_ask_sl():
    """1단계: 손절 방식 선택"""
    avg  = _hold_session.get("avg_price", 0)
    auto = _auto_params(avg)
    send_msg_kb(
        f"📌 손절 방식을 선택하세요\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"매수가: {avg:,.0f}원\n\n"
        f"🤖 자동 → 주가구간 기준 자동 적용\n"
        f"   ({avg:,.0f}원 기준: {auto['sl']:+.1f}%  →  {avg*(1+auto['sl']/100):,.0f}원)\n\n"
        f"📉 비율 → 직접 % 입력\n"
        f"💰 가격 → 특정 가격 지정",
        keyboard=[
            [
                {"text": f"🤖 자동 ({auto['sl']:+.1f}%)", "callback_data": "hold_sl_auto"},
                {"text": "📉 비율 (%)",                    "callback_data": "hold_sl_pct"},
                {"text": "💰 가격 지정",                    "callback_data": "hold_sl_price"},
            ],
            [{"text": "❌ 취소", "callback_data": "hold_cancel"}],
        ],
        force=True,
    )
    _hold_session["step"] = "sl_type"


def _hold_step2_ask_tp():
    """2단계: 익절 방식 선택"""
    avg  = _hold_session.get("avg_price", 0)
    auto = _auto_params(avg)
    sl_t = _hold_session.get("sl_type", "auto")
    if sl_t == "auto":
        sl_desc = f"자동 {auto['sl']:+.1f}% ({avg*(1+auto['sl']/100):,.0f}원)"
    elif sl_t == "pct":
        v = _hold_session.get("sl_val", 0)
        sl_desc = f"{v:+.1f}% ({avg*(1+v/100):,.0f}원)"
    else:
        v = _hold_session.get("sl_val", 0)
        sl_desc = f"{v:,.0f}원 ({(v-avg)/avg*100:+.1f}%)"

    send_msg_kb(
        f"📌 익절 방식을 선택하세요\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"손절: {sl_desc} ✅\n\n"
        f"📈 트레일링 → 고점 추적 자동 매도\n"
        f"   자동 기준: +{auto['trail_start']}% 후 고점-{auto['trail_gap']}%\n\n"
        f"🎯 비율 → 수익 X%에 즉시 매도\n"
        f"💰 가격 → 특정 가격에 즉시 매도\n"
        f"🔀 트레일링+본절 → 트레일링 + 본전보호 함께",
        keyboard=[
            [
                {"text": "📈 트레일링",      "callback_data": "hold_tp_trail"},
                {"text": "🎯 비율 (%)",       "callback_data": "hold_tp_pct"},
                {"text": "💰 가격 지정",       "callback_data": "hold_tp_price"},
            ],
            [
                {"text": "🔀 트레일링+본절",  "callback_data": "hold_tp_trail_be"},
                {"text": "❌ 취소",           "callback_data": "hold_cancel"},
            ],
        ],
        force=True,
    )
    _hold_session["step"] = "tp_type"


def _hold_step3_confirm():
    """3단계: 최종 확인"""
    s    = _hold_session
    avg  = s.get("avg_price", 0)
    qty  = s.get("qty", 0)
    name = s.get("name", s.get("code", ""))
    auto = _auto_params(avg)

    # 손절 설명
    sl_t = s.get("sl_type", "auto")
    if sl_t == "auto":
        sl_pct   = auto["sl"]
        sl_price = avg * (1 + sl_pct / 100)
        sl_desc  = f"자동 {sl_pct:+.1f}% → {sl_price:,.0f}원"
    elif sl_t == "pct":
        sl_pct   = s["sl_val"]
        sl_price = avg * (1 + sl_pct / 100)
        sl_desc  = f"{sl_pct:+.1f}% → {sl_price:,.0f}원"
    else:
        sl_price = s["sl_val"]
        sl_pct   = (sl_price - avg) / avg * 100
        sl_desc  = f"{sl_price:,.0f}원 ({sl_pct:+.1f}%)"

    # 익절 설명
    tp_t = s.get("tp_type", "trail")
    ts   = s.get("trail_start", auto["trail_start"])
    tg   = s.get("trail_gap",   auto["trail_gap"])
    be   = s.get("be_pct", 0.0)
    if tp_t == "trail":
        tp_desc = f"트레일링: +{ts}% 후 고점-{tg}%"
    elif tp_t == "trail_be":
        tp_desc = f"트레일링: +{ts}% 후 고점-{tg}%\n본절보호: +{be}% 후 매수가 이하 즉시매도"
    elif tp_t == "pct":
        v = s.get("tp_val", 0)
        tp_desc = f"익절 +{v}% → {avg*(1+v/100):,.0f}원"
    else:
        v = s.get("tp_val", 0)
        tp_desc = f"익절 {v:,.0f}원 ({(v-avg)/avg*100:+.1f}%)"

    send_msg_kb(
        f"✅ 포지션 등록 확인\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"종목: {name} ({s.get('code','')})\n"
        f"매수가: {avg:,.0f}원 / {qty}주\n"
        f"투자금: {avg*qty:,.0f}원\n"
        f"─────────────────\n"
        f"손절: {sl_desc}\n"
        f"익절: {tp_desc}\n"
        f"─────────────────\n"
        f"등록하면 봇이 자동으로 감시합니다.",
        keyboard=[
            [
                {"text": "✅ 등록",  "callback_data": "hold_confirm"},
                {"text": "❌ 취소", "callback_data": "hold_cancel"},
            ]
        ],
        force=True,
    )
    _hold_session["step"] = "confirm"


def _hold_register():
    """최종 등록"""
    s    = _hold_session
    code = s.get("code", "")
    avg  = s.get("avg_price", 0.0)
    qty  = s.get("qty", 0)
    name = s.get("name", code)
    auto = _auto_params(avg)

    sl_type = s.get("sl_type", "auto")
    if sl_type == "auto":
        sl_type = "pct"
        sl_val  = auto["sl"]
    else:
        sl_val = s.get("sl_val", auto["sl"])

    tp_type     = s.get("tp_type", "trail")
    tp_val      = s.get("tp_val", None)
    trail_start = s.get("trail_start", auto["trail_start"])
    trail_gap   = s.get("trail_gap",   auto["trail_gap"])
    be_pct      = s.get("be_pct", 0.0)

    hold_positions[code] = {
        "qty":         qty,
        "avg_price":   avg,
        "high_price":  avg,
        "entry_date":  str(date.today()),
        "sl_type":     sl_type,
        "sl_val":      sl_val,
        "tp_type":     tp_type,
        "tp_val":      tp_val,
        "trail_start": trail_start,
        "trail_gap":   trail_gap,
        "be_pct":      be_pct,
        "be_active":   False,
        "name":        name,
    }
    _save_state()
    _hold_session.clear()

    # 손절 요약
    if sl_type == "pct":
        sl_desc = f"{sl_val:+.1f}% ({avg*(1+sl_val/100):,.0f}원)"
    else:
        sl_desc = f"{sl_val:,.0f}원"

    # 익절 요약
    if tp_type in ("trail", "trail_be"):
        tp_desc = f"트레일링 +{trail_start}%→고점-{trail_gap}%"
        if tp_type == "trail_be":
            tp_desc += f" + 본절+{be_pct}%"
    elif tp_type == "pct":
        tp_desc = f"+{tp_val}% ({avg*(1+tp_val/100):,.0f}원)"
    else:
        tp_desc = f"{tp_val:,.0f}원"

    send_msg(
        f"✅ 포지션 등록 완료!\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"종목: {name} ({code})\n"
        f"매수가: {avg:,.0f}원 / {qty}주\n"
        f"─────────────────\n"
        f"손절: {sl_desc}\n"
        f"익절: {tp_desc}\n"
        f"─────────────────\n"
        f"지금부터 자동 감시 시작! 🔍",
        force=True,
    )
    cprint(f"[HOLD 등록] {name}({code}) {qty}주 @ {avg:,.0f}원", Fore.GREEN, bright=True)


# ── 콜백 처리 ────────────────────────────────────────────

def _handle_callback(data: str):
    """인라인 버튼 콜백 전체 처리"""
    s    = _hold_session
    avg  = s.get("avg_price", 0.0)
    auto = _auto_params(avg)

    # 취소
    if data == "hold_cancel":
        _hold_session.clear()
        send_msg("❌ 포지션 등록 취소됨", force=True)
        return

    # ── 손절 방식 ────────────────────────────────────────
    if data == "hold_sl_auto":
        s["sl_type"] = "auto"
        _hold_step2_ask_tp()

    elif data == "hold_sl_pct":
        s["sl_type"] = "pct"
        s["step"]    = "sl_val_pct"
        send_msg(
            f"📉 손절 비율 입력\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"숫자만 입력하세요 (예: 5 → -5%)\n"
            f"매수가 {avg:,.0f}원 기준\n"
            f"  3% → {avg*0.97:,.0f}원\n"
            f"  5% → {avg*0.95:,.0f}원\n"
            f"  7% → {avg*0.93:,.0f}원",
            force=True,
        )

    elif data == "hold_sl_price":
        s["sl_type"] = "price"
        s["step"]    = "sl_val_price"
        send_msg(
            f"💰 손절 가격 입력\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"숫자만 입력하세요 (예: 48500)\n"
            f"매수가: {avg:,.0f}원",
            force=True,
        )

    # ── 익절 방식 ────────────────────────────────────────
    elif data == "hold_tp_trail":
        s["tp_type"]     = "trail"
        s["trail_start"] = auto["trail_start"]
        s["trail_gap"]   = auto["trail_gap"]
        s["be_pct"]      = 0.0
        s["step"]        = "trail_custom"
        send_msg_kb(
            f"📈 트레일링 스탑\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"고점을 추적하다가 X% 빠지면 자동 매도\n\n"
            f"자동 기준: +{auto['trail_start']}% 후 고점-{auto['trail_gap']}%\n"
            f"  ({avg:,.0f}원 기준 +{auto['trail_start']}% = {avg*(1+auto['trail_start']/100):,.0f}원)",
            keyboard=[
                [
                    {"text": f"✅ 자동 (+{auto['trail_start']}% / -{auto['trail_gap']}%)",
                     "callback_data": "hold_trail_auto"},
                    {"text": "✏️ 직접 설정",
                     "callback_data": "hold_trail_custom"},
                ],
                [{"text": "❌ 취소", "callback_data": "hold_cancel"}],
            ],
            force=True,
        )

    elif data == "hold_tp_pct":
        s["tp_type"] = "pct"
        s["step"]    = "tp_val_pct"
        send_msg(
            f"🎯 익절 비율 입력\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"숫자만 입력하세요 (예: 3 → +3%)\n"
            f"매수가 {avg:,.0f}원 기준\n"
            f"  3% → {avg*1.03:,.0f}원\n"
            f"  5% → {avg*1.05:,.0f}원\n"
            f" 10% → {avg*1.10:,.0f}원",
            force=True,
        )

    elif data == "hold_tp_price":
        s["tp_type"] = "price"
        s["step"]    = "tp_val_price"
        send_msg(
            f"💰 익절 가격 입력\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"숫자만 입력하세요 (예: 55000)\n"
            f"매수가: {avg:,.0f}원",
            force=True,
        )

    elif data == "hold_tp_trail_be":
        s["tp_type"]     = "trail_be"
        s["trail_start"] = auto["trail_start"]
        s["trail_gap"]   = auto["trail_gap"]
        s["step"]        = "trail_be_custom"
        send_msg_kb(
            f"🔀 트레일링 + 본절 보호\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• 트레일링: 수익이 X% 나면 고점 추적 시작\n"
            f"• 본절보호: 수익이 Y% 났다가 매수가 이하로 내려오면 즉시 매도\n\n"
            f"자동 기준:\n"
            f"  트레일링: +{auto['trail_start']}% 후 고점-{auto['trail_gap']}%\n"
            f"  본절보호: +1.0% 이상 후 활성화",
            keyboard=[
                [
                    {"text": f"✅ 자동 (트레일링+본절 1%)",
                     "callback_data": "hold_trail_be_auto"},
                    {"text": "✏️ 직접 설정",
                     "callback_data": "hold_trail_be_custom"},
                ],
                [{"text": "❌ 취소", "callback_data": "hold_cancel"}],
            ],
            force=True,
        )

    # ── 트레일링 자동/직접 ───────────────────────────────
    elif data == "hold_trail_auto":
        _hold_step3_confirm()

    elif data == "hold_trail_custom":
        s["step"] = "trail_start_val"
        send_msg(
            f"✏️ 트레일링 시작 수익률 입력\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"고점 추적을 시작할 수익률 (%)\n"
            f"숫자만 입력 (예: 3 → +3%)\n"
            f"추천: {auto['trail_start']}%",
            force=True,
        )

    elif data == "hold_trail_be_auto":
        s["be_pct"] = 1.0
        _hold_step3_confirm()

    elif data == "hold_trail_be_custom":
        s["step"] = "be_val"
        send_msg(
            f"✏️ 본절 보호 기준 수익률 입력\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"수익이 X% 이상 났을 때 본절 보호 ON\n"
            f"숫자만 입력 (예: 1 → +1%)\n"
            f"추천: 1.0%",
            force=True,
        )

    # ── 최종 확인/등록 ───────────────────────────────────
    elif data == "hold_confirm":
        _hold_register()

    # ── 즉시 매도 ────────────────────────────────────────
    elif data.startswith("hold_sell_"):
        code = data.replace("hold_sell_", "")
        pos  = hold_positions.get(code)
        if pos:
            name = pos.get("name", code)
            ok   = send_order(code, "SELL", pos["qty"], 0)
            if ok:
                pnl_krw = 0
                info    = get_price_info(code)
                if info and info.get("price", 0) > 0:
                    pnl_krw = int((info["price"] - pos["avg_price"]) * pos["qty"])
                _log_trade(code, "SELL", pos["qty"], info.get("price", 0) if info else 0,
                           pnl=pnl_krw, reason="수동즉시매도")
                del hold_positions[code]
                _save_state()
                send_msg(f"✅ [{name}] 즉시 매도 완료\n손익: {pnl_krw:+,}원", force=True)
            else:
                send_msg(f"❌ [{name}] 매도 주문 실패", force=True)

    elif data.startswith("hold_del_"):
        code = data.replace("hold_del_", "")
        if code in hold_positions:
            name = hold_positions[code].get("name", code)
            del hold_positions[code]
            _save_state()
            send_msg(f"🗑 [{name}] 포지션 등록 삭제됨 (매도 없이)", force=True)


# ── 텍스트 숫자 입력 처리 ────────────────────────────────

def _hold_text_input(text: str):
    """진행 중인 /hold 세션에서 숫자 텍스트 처리"""
    s    = _hold_session
    step = s.get("step", "")
    avg  = s.get("avg_price", 0.0)
    auto = _auto_params(avg)

    try:
        val = float(text.replace(",", "").replace("원", "").replace("%", "").strip())
    except ValueError:
        send_msg(f"⚠️ 숫자만 입력해 주세요\n입력값: {text}", force=True)
        return

    if step == "sl_val_pct":
        s["sl_val"] = -abs(val)
        _hold_step2_ask_tp()

    elif step == "sl_val_price":
        if val >= avg:
            send_msg(
                f"⚠️ 손절 가격이 매수가보다 높습니다\n"
                f"손절가: {val:,.0f}원 / 매수가: {avg:,.0f}원\n"
                f"다시 입력해 주세요.",
                force=True,
            )
            return
        s["sl_val"] = val
        _hold_step2_ask_tp()

    elif step == "tp_val_pct":
        s["tp_val"] = abs(val)
        _hold_step3_confirm()

    elif step == "tp_val_price":
        if val <= avg:
            send_msg(
                f"⚠️ 익절 가격이 매수가보다 낮습니다\n"
                f"익절가: {val:,.0f}원 / 매수가: {avg:,.0f}원\n"
                f"다시 입력해 주세요.",
                force=True,
            )
            return
        s["tp_val"] = val
        _hold_step3_confirm()

    elif step == "trail_start_val":
        s["trail_start"] = abs(val)
        s["step"]        = "trail_gap_val"
        send_msg(
            f"✏️ 트레일링 간격 입력\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"시작 수익률: +{abs(val):.1f}% ✅\n\n"
            f"고점 대비 X% 빠지면 매도\n"
            f"숫자만 입력 (예: 2 → 고점-2%)\n"
            f"추천: {auto['trail_gap']}%",
            force=True,
        )

    elif step == "trail_gap_val":
        s["trail_gap"] = abs(val)
        _hold_step3_confirm()

    elif step == "be_val":
        s["be_pct"]      = abs(val)
        s["trail_start"] = auto["trail_start"]
        s["trail_gap"]   = auto["trail_gap"]
        s["step"]        = "be_trail_ask"
        send_msg_kb(
            f"✅ 본절 기준: +{abs(val):.1f}% 설정됨\n\n"
            f"트레일링 기준은 어떻게 설정할까요?",
            keyboard=[
                [
                    {"text": f"✅ 자동 (+{auto['trail_start']}% / -{auto['trail_gap']}%)",
                     "callback_data": "hold_trail_be_auto"},
                    {"text": "✏️ 직접 설정",
                     "callback_data": "hold_trail_custom"},
                ],
            ],
            force=True,
        )

    else:
        send_msg("⚠️ 예상치 못한 입력입니다.\n/hold 로 다시 시작해 주세요.", force=True)
        _hold_session.clear()


# ── /hold 명령 처리 ──────────────────────────────────────

def _cmd_hold(cmd: list):
    """/hold  또는  /hold 종목코드 매수가 수량"""
    if len(cmd) == 1:
        # 등록된 포지션 있으면 목록, 없으면 사용법 안내
        if hold_positions:
            _send_hold_list()
        else:
            send_msg(
                "📌 수동 포지션 관리\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "내가 직접 산 주식을 등록하면\n"
                "봇이 손/익절을 자동으로 처리해줍니다.\n\n"
                "📝 등록 방법\n"
                "  /hold 종목코드 매수가 수량\n\n"
                "📌 예시\n"
                "  /hold 005930 50000 10\n"
                "    → 삼성전자 50,000원에 10주 산 경우\n\n"
                "  /hold 000660 120000 5\n"
                "    → SK하이닉스 120,000원에 5주 산 경우\n\n"
                "입력 후 버튼으로\n"
                "손절/익절 기준을 설정합니다.",
                force=True,
            )
        return

    if len(cmd) < 4:
        send_msg(
            "📌 수동 포지션 등록\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "사용법: /hold 종목코드 매수가 수량\n\n"
            "예시:\n"
            "  /hold 005930 50000 10\n"
            "  /hold 000660 120000 5\n\n"
            "입력 후 버튼으로 손/익절 기준을 설정합니다.\n\n"
            "/hold → 등록된 포지션 목록 보기",
            force=True,
        )
        return

    code = cmd[1].strip().upper()
    try:
        avg_price = float(cmd[2].replace(",", "").replace("원", ""))
        qty       = int(cmd[3].replace(",", "").replace("주", ""))
    except ValueError:
        send_msg(
            "⚠️ 입력 형식 오류\n"
            "사용법: /hold 종목코드 매수가 수량\n"
            "예: /hold 005930 50000 10",
            force=True,
        )
        return

    if avg_price <= 0 or qty <= 0:
        send_msg("⚠️ 매수가와 수량은 0보다 커야 합니다.", force=True)
        return

    if avg_price < qty and qty > 50:
        send_msg(
            f"⚠️ 혹시 가격과 수량이 바뀐 건 아닌가요?\n"
            f"입력값: 가격 {avg_price:,.0f}원 / 수량 {qty}주\n\n"
            f"올바른 순서: /hold 종목코드 가격 수량\n"
            f"예: /hold 005930 50000 10",
            force=True,
        )
        return

    send_msg(f"🔍 종목 조회 중... ({code})", force=True)
    name = _get_stock_name(code)

    _hold_session.clear()
    _hold_session.update({
        "code":      code,
        "avg_price": avg_price,
        "qty":       qty,
        "name":      name,
    })
    _hold_step1_ask_sl()


def _send_hold_list():
    """수동 포지션 목록 + 관리 버튼"""
    if not hold_positions:
        send_msg(
            "📋 등록된 수동 포지션 없음\n\n"
            "/hold 종목코드 매수가 수량\n"
            "으로 등록하세요.\n\n"
            "예: /hold 005930 50000 10",
            force=True,
        )
        return

    lines = ["📋 수동 포지션 현황\n━━━━━━━━━━━━━━━━━━━━"]
    kb    = []

    for code, pos in list(hold_positions.items()):
        name  = pos.get("name", code)
        avg   = pos["avg_price"]
        qty   = pos["qty"]
        high  = pos.get("high_price", avg)
        info  = get_price_info(code)
        cur   = info.get("price", 0) if info else 0

        if cur > 0:
            pnl_pct = (cur - avg) / avg * 100
            pnl_krw = int((cur - avg) * qty)
            lines.append(
                f"\n{name} ({code})\n"
                f"  {qty}주 @ 매수 {avg:,.0f}원\n"
                f"  현재 {cur:,.0f}원  {pnl_pct:+.1f}%  ({pnl_krw:+,}원)\n"
                f"  고점: {high:,.0f}원"
            )
        else:
            lines.append(f"\n{name} ({code})\n  {qty}주 @ {avg:,.0f}원")

        # 손절 요약
        if pos["sl_type"] == "pct":
            sl_p = avg * (1 + pos["sl_val"] / 100)
            lines.append(f"  손절: {pos['sl_val']:+.1f}% → {sl_p:,.0f}원")
        else:
            lines.append(f"  손절: {pos['sl_val']:,.0f}원")

        # 익절 요약
        tp_t = pos.get("tp_type", "trail")
        if tp_t in ("trail", "trail_be"):
            be_txt = f" + 본절+{pos['be_pct']}%" if tp_t == "trail_be" else ""
            lines.append(f"  익절: 트레일링 +{pos['trail_start']}%→-{pos['trail_gap']}%{be_txt}")
        elif tp_t == "pct":
            tp_p = avg * (1 + pos["tp_val"] / 100)
            lines.append(f"  익절: +{pos['tp_val']}% → {tp_p:,.0f}원")
        else:
            lines.append(f"  익절: {pos['tp_val']:,.0f}원")

        kb.append([
            {"text": f"🔴 {name} 즉시매도", "callback_data": f"hold_sell_{code}"},
            {"text": "🗑 등록삭제",          "callback_data": f"hold_del_{code}"},
        ])

    send_msg_kb("\n".join(lines), keyboard=kb, force=True)


# ── 수동 포지션 감시 루프 ─────────────────────────────────

def _send_hold_open_report():
    """장 시작 hold 포지션 보고 — 전일 종가 대비 현황"""
    if not hold_positions:
        return
    lines = [
        f"📋 수동 포지션 장 시작 보고 [{datetime.now().strftime('%m/%d %H:%M')}]",
        "━━━━━━━━━━━━━━━━━━"
    ]
    for code, pos in hold_positions.items():
        info = get_price_info(code)
        name = pos.get("name", code)
        avg  = pos["avg_price"]
        qty  = pos["qty"]
        if info and info.get("price", 0) > 0:
            price   = info["price"]
            pnl_pct = (price - avg) / avg * 100
            pnl_krw = int((price - avg) * qty)
            chg_pct = info.get("change_pct", 0)
            # 손절/익절 기준 거리
            sl_val  = pos["sl_val"]
            tp_val  = pos["tp_val"]
            sl_dist = (price - sl_val) / price * 100 if pos["sl_type"] == "price" else pnl_pct - sl_val
            lines.append(
                f"📌 {name} ({code})\n"
                f"  현재: {price:,}원  등락: {chg_pct:+.2f}%\n"
                f"  손익: {pnl_pct:+.2f}% ({pnl_krw:+,}원)\n"
                f"  손절까지: {sl_dist:+.1f}%  |  {qty}주 보유"
            )
        else:
            lines.append(f"📌 {name} ({code}) — 가격 조회 실패")
    send_msg("\n".join(lines), force=True)


def _send_hold_close_report():
    """장 마감 hold 포지션 결과 보고"""
    if not hold_positions:
        send_msg("📋 수동 포지션 마감 보고: 보유 없음", force=True)
        return
    lines = [
        f"📋 수동 포지션 마감 보고 [{datetime.now().strftime('%m/%d %H:%M')}]",
        "━━━━━━━━━━━━━━━━━━"
    ]
    total_pnl = 0
    for code, pos in hold_positions.items():
        info = get_price_info(code)
        name = pos.get("name", code)
        avg  = pos["avg_price"]
        qty  = pos["qty"]
        high = pos.get("high_price", avg)
        if info and info.get("price", 0) > 0:
            price    = info["price"]
            pnl_pct  = (price - avg) / avg * 100
            pnl_krw  = int((price - avg) * qty)
            high_pct = (high - avg) / avg * 100
            chg_pct  = info.get("change_pct", 0)
            total_pnl += pnl_krw
            # 트레일링 기준가
            trail_price = high * (1 - pos["trail_gap"] / 100) if pos["tp_type"] in ("trail", "trail_be") else 0
            icon = '🟢' if pnl_pct >= 0 else '🔴'
            trail_str = f"\n  트레일기준: {trail_price:,.0f}원" if trail_price > 0 else ""
            lines.append(
                f"{icon} {name} ({code})\n"
                f"  종가: {price:,}원  당일: {chg_pct:+.2f}%\n"
                f"  손익: {pnl_pct:+.2f}% ({pnl_krw:+,}원)\n"
                f"  고점: {high:,}원 ({high_pct:+.1f}%){trail_str}"
            )
        else:
            lines.append(f"{'⚪'} {name} ({code}) — 가격 조회 실패")
    lines.append(f"━━━━━━━━━━━━━━━━━━")
    lines.append(f"합계 손익: {total_pnl:+,}원")
    send_msg("\n".join(lines), force=True)


def monitor_hold_positions():
    """수동 등록 포지션 감시 — 손절/익절/트레일링 자동 처리"""
    if not hold_positions or not is_tradeable_time(for_buy=False):
        return

    for code in list(hold_positions.keys()):
        pos  = hold_positions.get(code)
        if not pos:
            continue
        info = get_price_info(code)
        if not info or info.get("price", 0) <= 0:
            continue

        price   = info["price"]
        avg     = pos["avg_price"]
        high    = pos.get("high_price", avg)
        name    = pos.get("name", code)
        qty     = pos["qty"]
        pnl_pct = (price - avg) / avg * 100 if avg > 0 else 0.0
        pnl_krw = int((price - avg) * qty)

        # 고점 갱신
        if price > high:
            hold_positions[code]["high_price"] = price
            high = price

        triggered = False
        reason    = ""

        # ── 손절 ─────────────────────────────────────────
        if pos["sl_type"] == "pct":
            sl_hit = pnl_pct <= pos["sl_val"]
        else:
            sl_hit = price <= pos["sl_val"]

        if sl_hit:
            triggered = True
            reason    = "수동손절"
            cprint(f"[HOLD 손절] {name} {pnl_pct:+.1f}%", Fore.RED, bright=True)
            send_msg(
                f"🔴 수동 포지션 손절\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"종목: {name} ({code})\n"
                f"현재: {price:,}원  손익: {pnl_pct:+.1f}% ({pnl_krw:+,}원)",
                force=True,
            )

        # ── 비율/가격 익절 ───────────────────────────────
        elif pos["tp_type"] == "pct" and pnl_pct >= pos["tp_val"]:
            triggered = True
            reason    = "수동익절"
            cprint(f"[HOLD 익절] {name} {pnl_pct:+.1f}%", Fore.GREEN, bright=True)
            send_msg(
                f"💰 수동 포지션 익절\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"종목: {name} ({code})\n"
                f"현재: {price:,}원  손익: {pnl_pct:+.1f}% ({pnl_krw:+,}원)",
                force=True,
            )

        elif pos["tp_type"] == "price" and price >= pos["tp_val"]:
            triggered = True
            reason    = "수동익절"
            cprint(f"[HOLD 익절가] {name} {price:,}원", Fore.GREEN, bright=True)
            send_msg(
                f"💰 수동 포지션 익절\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"종목: {name} ({code})\n"
                f"목표: {pos['tp_val']:,.0f}원 → 현재: {price:,}원\n"
                f"손익: {pnl_pct:+.1f}% ({pnl_krw:+,}원)",
                force=True,
            )

        # ── 트레일링 (+ 본절) ────────────────────────────
        elif pos["tp_type"] in ("trail", "trail_be"):

            # 본절 보호 활성화 체크
            if pos["tp_type"] == "trail_be" and pos.get("be_pct", 0) > 0:
                if pnl_pct >= pos["be_pct"] and not pos.get("be_active"):
                    hold_positions[code]["be_active"] = True
                    cprint(f"[HOLD 본절 ON] {name} +{pnl_pct:.1f}%", Fore.CYAN)

                if pos.get("be_active") and price <= avg:
                    triggered = True
                    reason    = "수동본절"
                    cprint(f"[HOLD 본절] {name} {pnl_pct:+.1f}%", Fore.YELLOW)
                    send_msg(
                        f"🛡 수동 포지션 본절 보호\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"종목: {name} ({code})\n"
                        f"현재: {price:,}원  손익: {pnl_pct:+.1f}% ({pnl_krw:+,}원)",
                        force=True,
                    )

            # 트레일링 스탑
            if not triggered and pnl_pct >= pos["trail_start"]:
                trail_price = high * (1 - pos["trail_gap"] / 100)
                if price <= trail_price:
                    triggered = True
                    reason    = "수동트레일링"
                    cprint(f"[HOLD 트레일링] {name} 고점:{high:,}→기준:{trail_price:,.0f} 현재:{price:,}", Fore.CYAN)
                    send_msg(
                        f"📉 수동 포지션 트레일링 스탑\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"종목: {name} ({code})\n"
                        f"고점: {high:,}원 → 기준가: {trail_price:,.0f}원\n"
                        f"현재: {price:,}원  손익: {pnl_pct:+.1f}% ({pnl_krw:+,}원)",
                        force=True,
                    )

        # ── 매도 실행 ─────────────────────────────────────
        if triggered:
            ok = send_order(code, "SELL", qty, 0)  # 시장가
            if ok:
                _log_trade(code, "SELL", qty, price, pnl=pnl_krw, reason=reason)
                del hold_positions[code]
                _save_state()
                cprint(f"[HOLD 매도완료] {name} {pnl_krw:+,}원", Fore.GREEN)
                # 매도 결과 텔레그램 보고
                icon = "🟢" if pnl_krw >= 0 else "🔴"
                send_msg(
                    f"{icon} 수동 포지션 매도 완료\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"종목: {name} ({code})\n"
                    f"매도가: {price:,}원  수량: {qty}주\n"
                    f"매수가: {avg:,}원\n"
                    f"손익: {pnl_pct:+.2f}% ({pnl_krw:+,}원)\n"
                    f"이유: {reason}",
                    force=True,
                )
                # 매도 후 즉시 자동 싱크
                threading.Thread(target=_auto_sync_from_kis, daemon=True).start()
            else:
                send_msg(f"❌ [{name}] 매도 주문 실패 — 수동 확인 필요!", force=True)


if __name__ == "__main__":
    main()
