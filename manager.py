"""섹터 ETF 봇 전용 텔레그램 매니저.

manager.py는 Telegram 업데이트를 단독으로 수신하고, sector_bot.py는 KIS 주문과
전략 실행만 담당한다. 두 프로세스는 shared/cmd_stock.json 및 result_stock*.json으로
통신한다.
"""

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime

import requests
import yaml

MANAGER_VERSION = "2.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_FILE = os.path.join(BASE_DIR, "manager_cfg.yaml")
SHARED_DIR = os.path.join(BASE_DIR, "shared")
PID_FILE = os.path.join(SHARED_DIR, "manager.pid")
OFFSET_FILE = os.path.join(SHARED_DIR, "manager_tg_offset.json")
STATUS_FILE = os.path.join(SHARED_DIR, "status_sector.json")
CMD_FILE = os.path.join(SHARED_DIR, "cmd_stock.json")
os.makedirs(SHARED_DIR, exist_ok=True)

TELEGRAM_TOKEN = ""
CHAT_ID = ""
BOT_SCRIPT = "sector_bot.py"
RESTART_DELAY = 10

_last_update_id = 0
_telegram_lock = threading.Lock()
_command_lock = threading.Lock()
_ipc_lock = threading.Lock()
_stop_event = threading.Event()


def cprint(text):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {text}", flush=True)


def _as_bool(value, default=False):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value) if value is not None else default


def create_default_config():
    sample = """# 섹터 ETF 봇 전용 매니저 설정
telegram_token: \"여기에_봇_토큰\"
chat_id: \"여기에_채팅_ID\"

stock:
  enabled: true
  script: \"sector_bot.py\"
  restart_delay_seconds: 10
"""
    with open(CFG_FILE, "w", encoding="utf-8") as f:
        f.write(sample)


def load_config():
    global TELEGRAM_TOKEN, CHAT_ID, BOT_SCRIPT, RESTART_DELAY
    if not os.path.exists(CFG_FILE):
        create_default_config()
        raise RuntimeError(f"{CFG_FILE} 기본 파일을 만들었습니다. 토큰과 chat_id를 입력하세요.")
    with open(CFG_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    TELEGRAM_TOKEN = str(cfg.get("telegram_token", "")).strip()
    CHAT_ID = str(cfg.get("chat_id", "")).strip()
    stock = cfg.get("stock", {}) or {}
    if not _as_bool(stock.get("enabled", True), True):
        raise RuntimeError("manager_cfg.yaml의 stock.enabled가 false입니다.")
    BOT_SCRIPT = str(stock.get("script", "sector_bot.py")).strip()
    RESTART_DELAY = max(2, int(stock.get("restart_delay_seconds", 10)))
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("manager_cfg.yaml에 telegram_token과 chat_id를 입력하세요.")
    if not os.path.isfile(os.path.join(BASE_DIR, BOT_SCRIPT)):
        raise RuntimeError(f"섹터봇 파일이 없습니다: {BOT_SCRIPT}")


def telegram(method, *, timeout=8, **payload):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
            json=payload,
            timeout=timeout,
        )
        data = response.json()
        return data if data.get("ok") else {}
    except Exception as exc:
        cprint(f"[텔레그램 오류] {exc}")
        return {}


def send_message(text, keyboard=None):
    payload = {"chat_id": CHAT_ID, "text": text[:4000]}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    telegram("sendMessage", **payload)


def stock_keyboard():
    return [
        [{"text": "📊 상태", "callback_data": "/status"},
         {"text": "📦 보유", "callback_data": "/portfolio"}],
        [{"text": "📈 스코어", "callback_data": "/scores"},
         {"text": "🔍 왜 안 사?", "callback_data": "/why"}],
        [{"text": "📝 시장 기록", "callback_data": "/snapshot"},
         {"text": "🔄 잔고 동기화", "callback_data": "/sync"}],
        [{"text": "🔄 리밸런싱", "callback_data": "/rebalance"},
         {"text": "🛡 KOFR 대피", "callback_data": "/kofr"}],
        [{"text": "▶ 재개", "callback_data": "/resume"},
         {"text": "🛑 킬 스위치", "callback_data": "/kill"}],
    ]


def save_offset():
    tmp = OFFSET_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"offset": _last_update_id}, f)
    os.replace(tmp, OFFSET_FILE)


def load_offset():
    global _last_update_id
    try:
        with open(OFFSET_FILE, encoding="utf-8") as f:
            _last_update_id = int(json.load(f).get("offset", 0))
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        _last_update_id = 0


def is_authorized(chat_id):
    return str(chat_id) == CHAT_ID


def atomic_json_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def send_ipc(command, req_id):
    """섹터봇이 이전 명령을 가져갈 때까지 기다린 뒤 원자적으로 기록한다."""
    deadline = time.time() + 5
    while os.path.exists(CMD_FILE) and time.time() < deadline:
        time.sleep(0.05)
    if os.path.exists(CMD_FILE):
        return False
    with _ipc_lock:
        atomic_json_write(CMD_FILE, {"cmd": command, "req_id": req_id, "ts": time.time()})
    cprint(f"[IPC→sector] {command}")
    return True


def read_ipc_result(req_id, timeout=15):
    path = os.path.join(SHARED_DIR, f"result_stock_{req_id}.json")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                with _ipc_lock:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    os.remove(path)
                return data.get("result", ""), data.get("keyboard")
            except (OSError, json.JSONDecodeError):
                pass
        time.sleep(0.15)
    return None, None


def clean_result(result):
    return result.replace("[normal] ", "").replace("[critical] ", "").replace("[silent] ", "")


def forward(command, timeout=15, reply=True):
    req_id = uuid.uuid4().hex[:12]
    if not send_ipc(command, req_id):
        if reply:
            send_message("⚠️ 이전 명령을 처리 중입니다. 잠시 후 다시 시도하세요.", stock_keyboard())
        return None
    result, keyboard = read_ipc_result(req_id, timeout)
    if result is None:
        if reply:
            send_message("⚠️ 섹터봇 응답 시간이 초과됐습니다. 프로세스 상태를 확인하세요.", stock_keyboard())
        return None
    if reply and clean_result(result).strip():
        send_message(clean_result(result), keyboard or stock_keyboard())
    return result


class SectorWorker:
    def __init__(self):
        self.process = None
        self.thread = None
        self.last_lines = []

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name="sector-worker")
        self.thread.start()

    def _run(self):
        script_path = os.path.join(BASE_DIR, BOT_SCRIPT)
        while not _stop_event.is_set():
            try:
                cprint(f"[섹터봇 시작] {BOT_SCRIPT}")
                self.process = subprocess.Popen(
                    [sys.executable, script_path], cwd=BASE_DIR,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
                for raw in self.process.stdout:
                    line = raw.rstrip()
                    if line:
                        cprint(f"[섹터봇] {line}")
                        self.last_lines.append(line)
                        self.last_lines = self.last_lines[-30:]
                rc = self.process.wait()
                if _stop_event.is_set():
                    break
                send_message(f"⚠️ 섹터봇이 종료됐습니다 (code {rc}). {RESTART_DELAY}초 후 재시작합니다.")
                time.sleep(RESTART_DELAY)
            except Exception as exc:
                cprint(f"[워커 오류] {exc}")
                time.sleep(RESTART_DELAY)

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        _stop_event.set()
        if self.is_running():
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()


WORKER = SectorWorker()


def status_summary():
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "📊 섹터봇 상태 파일이 아직 없습니다. 시작 직후이거나 봇이 멈췄을 수 있습니다."
    holdings = data.get("holdings", [])
    return (
        "📊 섹터봇 현황\n"
        f"실행: {'🟢' if WORKER.is_running() else '🔴'}\n"
        f"대피 단계: {data.get('defense_stage', '알 수 없음')}\n"
        f"킬 스위치: {'ON' if data.get('kill_switch') else 'OFF'}\n"
        f"보유 ETF: {len(holdings)}개\n"
        f"당일 손익: {data.get('pnl_today', 0):+,}원"
    )


HELP = """🤖 섹터 ETF 봇 명령어
/status — 상태
/portfolio — 보유 ETF
/scores — 모멘텀 순위
/why — 현재 매수/대기 이유
/rebalance — 수동 리밸런싱
/kofr — 방어 단계 진입
/resume — 정상 모드 복귀
/kill /unkill — 킬 스위치
/sync — KIS 잔고 동기화
/snapshot — ETF 시장 상태 즉시 기록
/hold — 수동 포지션 관리
/hold 005930 50000 10 — 수동 포지션 등록
/restart — 섹터봇 재시작
/menu — 버튼 메뉴"""


ALLOWED = {"/status", "/portfolio", "/scores", "/why", "/rebalance", "/kofr", "/resume",
           "/kill", "/unkill", "/start", "/stop", "/sync", "/snapshot", "/defense", "/bollinger", "/investor"}


def handle_command(text):
    text = text.strip()
    if not text:
        return
    with _command_lock:
        command = text.split()[0].lower()
        if command in ("/menu", "/help", "/도움말"):
            send_message(HELP, stock_keyboard())
        elif command == "/restart":
            if WORKER.is_running():
                WORKER.process.terminate()
                send_message("🔄 섹터봇 재시작을 요청했습니다.", stock_keyboard())
            else:
                send_message("⚠️ 섹터봇이 실행 중이 아닙니다. 워커가 자동 재시작을 시도합니다.", stock_keyboard())
        elif command == "/manager_status":
            send_message(status_summary(), stock_keyboard())
        elif command == "/hold":
            forward(text, timeout=20)
        elif command in ALLOWED:
            forward(text, timeout=30 if command in ("/scores", "/sync") else 15)
        elif not command.startswith("/"):
            # /hold 설정 중 숫자 입력은 섹터봇의 세션으로만 전달한다.
            forward("__hold_text__ " + text, timeout=10)
        else:
            send_message("알 수 없는 명령입니다. /help를 입력하세요.", stock_keyboard())


def answer_callback(callback_id):
    telegram("answerCallbackQuery", callback_query_id=callback_id, timeout=3)


def poll_telegram():
    global _last_update_id
    if not _telegram_lock.acquire(blocking=False):
        return
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": _last_update_id + 1, "timeout": 2}, timeout=6,
        )
        data = response.json()
        if not data.get("ok"):
            # 409 Conflict는 같은 봇 토큰을 사용하는 다른 getUpdates 프로세스가 있다는 뜻이다.
            cprint(f"[텔레그램 API 거부] {data.get('error_code', '?')}: {data.get('description', '알 수 없는 오류')}")
            return
        for update in data.get("result", []):
            _last_update_id = update["update_id"]
            save_offset()
            message = update.get("message", {})
            if message and is_authorized(message.get("chat", {}).get("id")):
                text = message.get("text", "").strip()
                if text:
                    cprint(f"[텔레그램 명령 수신] {text[:80]}")
                    threading.Thread(target=handle_command, args=(text,), daemon=True).start()
            elif message:
                cprint(f"[텔레그램 차단] 허용되지 않은 chat_id: {message.get('chat', {}).get('id', '')}")
            callback = update.get("callback_query", {})
            if callback and is_authorized(callback.get("message", {}).get("chat", {}).get("id")):
                answer_callback(callback.get("id", ""))
                data = callback.get("data", "").strip()
                if data.startswith("hold_"):
                    threading.Thread(target=forward, args=("__hold_callback__ " + data,), kwargs={"timeout": 10}, daemon=True).start()
                elif data:
                    threading.Thread(target=handle_command, args=(data,), daemon=True).start()
    except Exception as exc:
        cprint(f"[텔레그램 폴링 오류] {exc}")
    finally:
        _telegram_lock.release()


def write_pid():
    tmp = PID_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    os.replace(tmp, PID_FILE)


def cleanup():
    WORKER.stop()
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def run_manager():
    load_config()
    load_offset()
    write_pid()
    WORKER.start()
    send_message(f"🚀 섹터 ETF 매니저 v{MANAGER_VERSION} 시작", stock_keyboard())
    cprint("매니저 실행 중 (Ctrl+C로 종료)")
    try:
        while True:
            poll_telegram()
            time.sleep(1)
    except KeyboardInterrupt:
        cprint("종료 요청")
    finally:
        cleanup()
        send_message("⏹ 섹터 ETF 매니저 종료")


if __name__ == "__main__":
    run_manager()
