"""
claudio.py — Supervisor central de la Granja V2
Responsabilidades:
  - Arrancar / detener workers (instancias de bot_granjav2.py)
  - Recibir eventos de cada worker y decidir qué hacer
  - Heartbeat cada 30 min a Telegram con estado de toda la granja
  - Escuchar comandos Telegram (/status /stop /start /selector /restart)
  - Presentar top 3 candidatos de selector con botones inline
  - Monitorear comportamiento de wallet target (inactividad, anomalías)
  - Única acción autónoma: pausar un worker ante anomalía detectada
  - Todo lo demás requiere aprobación de Gerencia vía Telegram
"""

import json, os, time, threading, logging, signal, sys
from pathlib import Path
from dotenv import load_dotenv
import requests

# ── Cargar .env global (Telegram) ────────────────────────
BASE_DIR    = Path(__file__).parent
WORKERS_DIR = BASE_DIR / "workers"

_global_env = BASE_DIR / ".env"
if _global_env.exists():
    load_dotenv(_global_env)
else:
    for w in sorted(WORKERS_DIR.iterdir()):
        _wenv = w / ".env"
        if _wenv.exists():
            load_dotenv(_wenv)
            break

TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Monitoreo de wallet target ────────────────────────────
WALLET_INACTIVITY_H   = 24    # horas sin actividad antes de alertar
WALLET_CHECK_MIN      = 30    # cada cuántos minutos verificar actividad
DATA_API = "https://data-api.polymarket.com"

# ── Logger central ────────────────────────────────────────
log = logging.getLogger("claudio")
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:
    fmt = logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(BASE_DIR / "claudio.log")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)

# ── Telegram ──────────────────────────────────────────────
def tg(msg: str, buttons: list[list[dict]] = None):
    if not TG_TOKEN or not TG_CHAT:
        log.warning("TG sin configurar — mensaje no enviado")
        return None
    payload = {"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=payload, timeout=10
        )
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        log.warning(f"TG_ERROR | {e}")
        return None

def tg_answer_callback(callback_id: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id}, timeout=5
        )
    except:
        pass

def tg_edit(message_id: int, msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/editMessageText",
            json={"chat_id": TG_CHAT, "message_id": message_id,
                  "text": msg, "parse_mode": "HTML"}, timeout=10
        )
    except:
        pass

# ── Estado de la granja ───────────────────────────────────
class WorkerState:
    def __init__(self, worker_id: str, worker_dir: str):
        self.worker_id   = worker_id
        self.worker_dir  = worker_dir
        self.thread      = None
        self.running     = False
        self.copies      = 0
        self.signals     = 0
        self.errors      = 0
        self.balance     = None
        self.paused      = False
        self.last_event  = None
        self.stop_event  = threading.Event()
        # Monitoreo de wallet target
        self.wallet_last_activity_ts  = time.time()  # timestamp último trade visto
        self.wallet_last_checked_ts   = 0
        self.wallet_inactivity_alerted = False        # para no alertar repetidamente

granja: dict[str, WorkerState] = {}

# ── Monitoreo de wallet target ────────────────────────────
def get_wallet_last_trade_ts(addr: str) -> float:
    """Retorna timestamp del trade más reciente de la wallet."""
    try:
        r = requests.get(
            f"{DATA_API}/activity",
            params={"user": addr.lower(), "limit": 5},
            timeout=8
        )
        if not r.ok:
            return 0
        trades = r.json()
        if not isinstance(trades, list) or not trades:
            return 0
        return max(t.get("timestamp", 0) for t in trades)
    except:
        return 0

def monitoreo_wallet_loop():
    """
    Verifica actividad de la wallet target cada WALLET_CHECK_MIN minutos.
    Anomalías que detecta y reporta a Gerencia:
      - Inactividad >24h: wallet dejó de operar
    El bot NO se detiene por inactividad — solo avisa. Gerencia decide.
    """
    while True:
        time.sleep(WALLET_CHECK_MIN * 60)
        for wid, ws in granja.items():
            if not ws.running:
                continue
            try:
                cfg_path = Path(ws.worker_dir) / "config.json"
                cfg = json.loads(cfg_path.read_text())
                wallets = cfg.get("target_wallets", [cfg.get("target_wallet", "")])
                if not wallets or not wallets[0]:
                    continue
                addr = wallets[0].lower()

                ws.wallet_last_checked_ts = time.time()
                last_ts = get_wallet_last_trade_ts(addr)
                if last_ts > 0:
                    ws.wallet_last_activity_ts = last_ts

                horas_inactiva = (time.time() - ws.wallet_last_activity_ts) / 3600

                if horas_inactiva > WALLET_INACTIVITY_H and not ws.wallet_inactivity_alerted:
                    ws.wallet_inactivity_alerted = True
                    tg(
                        f"⚠️ <b>[{wid}] Wallet target inactiva</b>\n"
                        f"👛 {addr[:20]}...\n"
                        f"⏰ Sin actividad hace {horas_inactiva:.0f}h\n\n"
                        f"La granja sigue corriendo. ¿Qué hacemos?",
                        buttons=[
                            [
                                {"text": "🎯 Buscar nueva wallet", "callback_data": f"selector:{wid}"},
                                {"text": "⏸ Pausar worker",        "callback_data": f"stop:{wid}"},
                            ],
                            [
                                {"text": "✅ Mantener (ignoro)",    "callback_data": f"keep:{wid}"},
                            ]
                        ]
                    )
                elif horas_inactiva <= WALLET_INACTIVITY_H:
                    # Resetear alerta si volvió a estar activa
                    ws.wallet_inactivity_alerted = False

            except Exception as e:
                log.warning(f"[{wid}] monitoreo_wallet error | {e}")

# ── Callback de eventos desde workers ────────────────────
def on_worker_event(worker_id: str, event_type: str, data: dict):
    ws = granja.get(worker_id)
    if not ws:
        return
    ws.last_event = event_type

    if event_type == "copy_ok":
        ws.copies  += 1
        ws.signals += 1
        ws.balance  = data.get("balance")
        ws.wallet_last_activity_ts   = time.time()
        ws.wallet_inactivity_alerted = False
        bal_str = f"${ws.balance:.2f}" if ws.balance else "N/A"
        log.info(f"[{worker_id}] COPY #{ws.copies} | {data['slug'][:40]} | ${data['usd']} @{data['price']:.3f}")
        # TG: solo primera copia del día o cada 10
        if ws.copies == 1 or ws.copies % 10 == 0:
            tg(
                f"✅ <b>[{worker_id}] COPY #{ws.copies}</b>\n"
                f"📌 {data['slug'][:40]}\n"
                f"💵 ${data['usd']} @{data['price']:.3f}\n"
                f"💰 Balance: {bal_str}"
            )

    elif event_type == "copy_fail":
        ws.errors  += 1
        ws.signals += 1
        tg(
            f"⚠️ <b>[{worker_id}] COPY FAIL #{data['n']}</b>\n"
            f"📌 {data['slug'][:40]}\n"
            f"❌ {data['detail'][:100]}"
        )

    elif event_type == "sell_ok":
        ws.wallet_last_activity_ts = time.time()
        log.info(f"[{worker_id}] SELL OK | {data['slug'][:40]} | size={data['size']:.4f} @{data['price']:.3f}")

    elif event_type == "sell_fail":
        tg(
            f"⚠️ <b>[{worker_id}] SELL FAIL</b>\n"
            f"📌 {data['slug'][:40]}\n"
            f"❌ {data['detail'][:100]}"
        )

    elif event_type == "no_cash":
        bal = data.get("balance", 0)
        last_no_cash = getattr(ws, "_last_no_cash_log", 0)
        if time.time() - last_no_cash > 1800:
            ws._last_no_cash_log = time.time()
            log.info(f"[{worker_id}] sin cash | ${bal:.2f}")

    elif event_type == "market_resolved":
        slug_resolved = data.get("slug", "")
        log.info(f"[{worker_id}] market_resolved | {slug_resolved[:40]}")

    elif event_type == "consecutive_errors":
        ws.running = False
        ws.paused  = True
        tg(
            f"🚨 <b>[{worker_id}] 3 errores consecutivos — bot pausado</b>\n"
            f"❌ Último error: {data['last_error'][:120]}\n\n"
            f"Razón: fallo repetido en ejecución de órdenes.\n"
            f"¿Qué hacemos?",
            buttons=[
                [
                    {"text": "🎯 Activar Selector", "callback_data": f"selector:{worker_id}"},
                    {"text": "🔄 Reiniciar bot",    "callback_data": f"restart:{worker_id}"},
                ],
                [
                    {"text": "⛔ Parar worker",     "callback_data": f"stop:{worker_id}"},
                ]
            ]
        )

    elif event_type == "skip":
        log.info(f"[{worker_id}] SKIP | {data.get('reason')} | {data.get('slug','')[:40]}")

    elif event_type == "no_wallet":
        tg(
            f"⚠️ <b>[{worker_id}] Sin wallet configurada</b>\n"
            f"Usa /selector para encontrar una wallet candidata.",
            buttons=[[
                {"text": "🎯 Activar Selector", "callback_data": f"selector:{worker_id}"},
            ]]
        )

# ── Gestión de workers ────────────────────────────────────
def start_worker(worker_id: str):
    from bot_granjav2 import run_worker, get_balance
    ws = granja.get(worker_id)
    if not ws:
        log.error(f"Worker {worker_id} no encontrado en granja")
        return
    if ws.running:
        log.info(f"[{worker_id}] Ya estaba corriendo")
        return
    ws.balance = get_balance(ws.worker_dir)
    ws.running = True
    ws.paused  = False
    ws.stop_event.clear()
    ws.wallet_last_activity_ts   = time.time()
    ws.wallet_inactivity_alerted = False

    def thread_target():
        try:
            run_worker(ws.worker_dir, log, on_worker_event, ws.stop_event)
        except Exception as e:
            log.error(f"[{worker_id}] Thread caído | {e}", exc_info=True)
        finally:
            ws.running = False

    ws.thread = threading.Thread(target=thread_target, daemon=True, name=f"worker-{worker_id}")
    ws.thread.start()
    log.info(f"[{worker_id}] Thread arrancado")

def stop_worker(worker_id: str, wait: bool = True):
    ws = granja.get(worker_id)
    if ws:
        ws.stop_event.set()
        ws.running = False
        ws.paused  = False
        if wait and ws.thread and ws.thread.is_alive():
            ws.thread.join(timeout=15)
        log.info(f"[{worker_id}] Detenido")

def discover_workers():
    if not WORKERS_DIR.exists():
        log.warning(f"Directorio workers/ no encontrado en {WORKERS_DIR}")
        return
    for w in sorted(WORKERS_DIR.iterdir()):
        if w.is_dir():
            cfg_path = w / "config.json"
            env_path = w / ".env"
            if cfg_path.exists() and env_path.exists():
                granja[w.name] = WorkerState(w.name, str(w))
                log.info(f"Worker descubierto: {w.name}")
            else:
                log.warning(f"Worker {w.name} incompleto (falta config.json o .env)")

# ── Selector ──────────────────────────────────────────────
_selector_thread = None

def run_selector_for_worker(worker_id: str):
    """Corre selector.py --auto y presenta top 3 candidatos con botones."""
    global _selector_thread
    if _selector_thread and _selector_thread.is_alive():
        tg("🎯 Selector ya está corriendo, espera un momento...")
        return

    tg(
        f"🎯 <b>Selector activado para [{worker_id}]</b>\n"
        f"Analizando wallets calificadas en 3 fuentes...\n"
        f"(Esto toma 2-3 minutos)"
    )

    def _select():
        try:
            import subprocess
            result = subprocess.run(
                ["/root/granja-v2/venv/bin/python3", str(BASE_DIR / "selector.py"), "--auto"],
                capture_output=True, text=True, timeout=300,
                cwd=str(BASE_DIR)
            )
            proposals_path = BASE_DIR / "proposals.json"
            if not proposals_path.exists():
                tg(f"⚠️ Selector no encontró candidatos para [{worker_id}]\nIntenta /selector más tarde.")
                return

            p = json.loads(proposals_path.read_text())
            if p.get("fuente") != "selector":
                tg(f"⚠️ proposals.json no es de selector para [{worker_id}]")
                return

            candidatos = p.get("candidatos", [])
            if not candidatos:
                tg(f"⚠️ Selector no generó candidatos para [{worker_id}]")
                return

            # Presentar top 3 con botones numerados
            def stars(n): return "⭐" * n + "·" * (4 - n)

            msg_lines = [f"🎯 <b>Selector completó análisis</b> — {len(candidatos)} candidato(s)\n"]
            for i, c in enumerate(candidatos[:3], 1):
                w = c.get("wallet", {})
                m = c.get("mercado", {})
                msg_lines.append(
                    f"<b>{i}. {stars(w.get('estrellas', 1))} ({w.get('estrellas',1)}/4)</b>\n"
                    f"   👛 {w.get('addr','?')[:20]}... ({w.get('name','?')[:12]})\n"
                    f"   📊 Score: {w.get('score',0):.3f} | WR: {w.get('win_rate',0)*100:.0f}% | Edge: {w.get('edge',0):.2f}\n"
                    f"   📌 {m.get('slug','?')[:45]}\n"
                    f"   ⏱ {m.get('hours',0):.1f}h | Vol: ${m.get('vol',0):,.0f} | Spread: {m.get('spread') or '?'}\n"
                )

            msg_lines.append(f"\n¿Cuál aprueba para <b>[{worker_id}]</b>?")

            # Botones: uno por candidato + rechazar todos
            buttons = []
            for i in range(min(3, len(candidatos))):
                w = candidatos[i].get("wallet", {})
                buttons.append([{
                    "text": f"✅ Aprobar #{i+1} — {stars(w.get('estrellas',1))}",
                    "callback_data": f"approve_n:{worker_id}:{i}"
                }])
            buttons.append([
                {"text": "🔄 Buscar de nuevo", "callback_data": f"selector:{worker_id}"},
                {"text": "⛔ Cancelar",         "callback_data": f"reject:{worker_id}"},
            ])

            tg("\n".join(msg_lines), buttons=buttons)

        except subprocess.TimeoutExpired:
            tg(f"⏱ Selector tardó demasiado para [{worker_id}]. Intenta de nuevo.")
        except Exception as e:
            tg(f"❌ Error en selector [{worker_id}]: {e}")
            log.error(f"run_selector error | {e}", exc_info=True)

    _selector_thread = threading.Thread(target=_select, daemon=True, name="selector")
    _selector_thread.start()

def apply_proposal(worker_id: str, candidato_idx: int = 0) -> bool:
    """Aplica el candidato N de proposals.json como nueva config del worker."""
    proposals_path = BASE_DIR / "proposals.json"
    if not proposals_path.exists():
        tg("❌ No hay proposals.json para aplicar")
        return False
    try:
        p  = json.loads(proposals_path.read_text())
        ws = granja.get(worker_id)
        if not ws:
            return False

        # Soporte tanto formato selector (lista) como legacy (único)
        if p.get("fuente") == "selector":
            candidatos = p.get("candidatos", [])
            if not candidatos or candidato_idx >= len(candidatos):
                tg("❌ Índice de candidato fuera de rango")
                return False
            elegido = candidatos[candidato_idx]
            wallet_data  = elegido["wallet"]
            mercado_data = elegido["mercado"]
        else:
            # Legacy: hunter/scout
            wallet_data  = p.get("wallet", {})
            mercado_data = p.get("mercado", {})

        cfg_path = Path(ws.worker_dir) / "config.json"
        cfg = json.loads(cfg_path.read_text())

        # Siempre usar target_wallets (lista), eliminar legacy target_wallet
        cfg.pop("target_wallet", None)
        cfg["target_wallets"] = [wallet_data["addr"]]
        cfg["market"] = mercado_data["slug"]

        cfg_path.write_text(json.dumps(cfg, indent=2))
        log.info(
            f"[{worker_id}] config.json actualizado | "
            f"wallet={wallet_data['addr'][:16]}... | "
            f"market={mercado_data['slug'][:30]}"
        )
        return True
    except Exception as e:
        log.error(f"apply_proposal error | {e}")
        tg(f"❌ Error aplicando proposal: {e}")
        return False

# ── Heartbeat ─────────────────────────────────────────────
def heartbeat_loop():
    """
    Heartbeat silencioso — solo log interno cada 30 min.
    Daily report a las 05:00 UTC (08:00 Kuwait) si hubo actividad.
    Telegram solo habla cuando importa.
    """
    from bot_granjav2 import get_balance
    import datetime
    INACTIVITY_ALERT_H   = 6
    MIN_CASH_FOR_ALERT   = 5.0
    _last_daily_report   = 0
    while True:
        time.sleep(1800)
        # Daily report 05:00 UTC
        now_utc = datetime.datetime.utcnow()
        if now_utc.hour == 5 and now_utc.minute < 30:
            ts_today = datetime.datetime(now_utc.year, now_utc.month, now_utc.day, 5, 0).timestamp()
            if _last_daily_report < ts_today:
                _last_daily_report = time.time()
                lines = ["\U0001f331 <b>Granja — Reporte diario 08:00 Kuwait</b>\n"]
                for wid, ws in granja.items():
                    estado = "\U0001f7e2 activo" if ws.running else "\U0001f534 detenido"
                    bal = f"${ws.balance:.2f}" if ws.balance else "N/A"
                    lines.append(
                        f"[{wid}] {estado}\n"
                        f"  Copias hoy: {ws.copies} | Balance: {bal}\n"
                        f"  Errores: {ws.errors}"
                    )
                tg("\n".join(lines))
        for wid, ws in granja.items():
            if not ws.running:
                continue
            bal = get_balance(ws.worker_dir)
            if bal is not None:
                ws.balance = bal
            log.info(f"[{wid}] heartbeat | bal=${ws.balance:.2f} | cp={ws.copies}/{ws.signals}")

            # Alerta de wallet inactiva con cash disponible
            horas_sin_senal = (time.time() - ws.wallet_last_activity_ts) / 3600
            cash = ws.balance or 0
            ya_alertado = getattr(ws, "_inactivity_cash_alerted", False)

            if horas_sin_senal > INACTIVITY_ALERT_H and cash >= MIN_CASH_FOR_ALERT and not ya_alertado:
                ws._inactivity_cash_alerted = True
                try:
                    cfg = json.loads((Path(ws.worker_dir) / "config.json").read_text())
                    wallets = cfg.get("target_wallets", [cfg.get("target_wallet", "?")])
                    wallet_str = wallets[0][:16] + "..." if wallets else "?"
                except:
                    wallet_str = "?"
                tg(
                    f"⏰ <b>[{wid}] Wallet inactiva {horas_sin_senal:.0f}h</b>\n"
                    f"👛 {wallet_str}\n"
                    f"💵 Cash disponible: ${cash:.2f}\n\n"
                    f"La granja tiene capital pero la wallet no opera.\n"
                    f"¿Corremos el selector?",
                    buttons=[[
                        {"text": "🎯 Buscar nueva wallet", "callback_data": f"selector:{wid}"},
                        {"text": "⏸ Ignorar por ahora",   "callback_data": f"keep:{wid}"},
                    ]]
                )
            elif horas_sin_senal <= INACTIVITY_ALERT_H:
                ws._inactivity_cash_alerted = False  # reset si vuelve a operar

# ── Listener de comandos Telegram ─────────────────────────
_tg_offset = 0

def poll_telegram():
    """
    Long-polling de Telegram con backoff exponencial.
    Timeouts son normales en long-polling — no son errores críticos.
    Backoff evita spam de reconexiones cuando Telegram está lento.
    """
    global _tg_offset
    _consecutive_errors = 0
    _MAX_BACKOFF = 60  # máximo 60s entre reintentos

    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                params={"offset": _tg_offset, "timeout": 30,
                        "allowed_updates": ["message", "callback_query"]},
                timeout=40
            )
            if not r.ok:
                _consecutive_errors += 1
                wait = min(5 * _consecutive_errors, _MAX_BACKOFF)
                time.sleep(wait)
                continue
            _consecutive_errors = 0  # reset al éxito
            for upd in r.json().get("result", []):
                _tg_offset = upd["update_id"] + 1
                handle_update(upd)
        except requests.exceptions.ReadTimeout:
            # Timeout es normal en long-polling — no loguear como error
            _consecutive_errors = 0
            time.sleep(1)
        except Exception as e:
            _consecutive_errors += 1
            wait = min(5 * _consecutive_errors, _MAX_BACKOFF)
            log.warning(f"TG_POLL_ERROR | {e} | reintentando en {wait}s")
            time.sleep(wait)

def handle_update(upd: dict):
    if "callback_query" in upd:
        cb = upd["callback_query"]
        tg_answer_callback(cb["id"])
        handle_callback(cb.get("data", ""), cb["message"]["message_id"])
        return

    msg  = upd.get("message", {})
    text = msg.get("text", "").strip()
    if not text:
        return
    if str(msg.get("chat", {}).get("id", "")) != str(TG_CHAT):
        return

    cmd  = text.lower().split()[0]
    args = text.split()[1:]
    target = args[0] if args else (list(granja.keys())[0] if granja else None)

    if cmd in ("/start", "/farm_start"):
        if target and target in granja:
            start_worker(target)
            tg(f"🟢 [{target}] arrancado")
        else:
            for wid in granja:
                start_worker(wid)
            tg("🟢 Todos los workers arrancados")

    elif cmd in ("/stop", "/farm_stop"):
        if target == "all":
            for wid in granja:
                stop_worker(wid)
            tg("🔴 <b>Granja completamente detenida.</b>\nClaudio se apaga en 3 segundos.\nUsa <code>./farm-start.sh</code> desde SSH para volver.")
            time.sleep(3)
            os.kill(os.getpid(), signal.SIGTERM)
        elif target and target in granja:
            stop_worker(target)
            tg(f"🔴 [{target}] detenido\n⏸ Claudio sigue en standby.")
        else:
            for wid in granja:
                stop_worker(wid)
            tg("🔴 Todos los workers detenidos\n⏸ Claudio en standby — usa /start para reanudar.")

    elif cmd in ("/restart", "/farm_restart"):
        wids = [target] if (target and target in granja) else list(granja.keys())
        tg(f"⏳ Reiniciando {', '.join(wids)}...")
        def do_restart(wids):
            for wid in wids:
                stop_worker(wid, wait=True)
            time.sleep(1)
            for wid in wids:
                start_worker(wid)
            tg(f"🔄 {', '.join(wids)} reiniciado ✅")
        threading.Thread(target=do_restart, args=(wids,), daemon=True).start()

    elif cmd in ("/status", "/farm_status"):
        import subprocess
        try:
            result = subprocess.run(
                ["/root/granja-v2/wallet-status.sh"],
                capture_output=True, text=True, timeout=30
            )
            output = result.stdout.strip()
            if len(output) > 4000:
                output = output[:4000] + "\n..."
            tg(f"<pre>{output}</pre>")
        except Exception as e:
            tg(f"❌ Error en status: {e}")

    elif cmd == "/selector":
        worker_id = target if (target and target in granja) else (list(granja.keys())[0] if granja else None)
        if worker_id:
            run_selector_for_worker(worker_id)

    elif cmd == "/workers":
        lines = [f"👷 Workers disponibles ({len(granja)}):"]
        for wid, ws in granja.items():
            estado = "🟢" if ws.running else ("⏸" if ws.paused else "🔴")
            lines.append(f"  {estado} {wid}")
        tg("\n".join(lines))

    elif cmd == "/wallet":
        # Mostrar info de la wallet target actual
        lines = ["👛 <b>Wallets target activas:</b>"]
        for wid, ws in granja.items():
            try:
                cfg = json.loads((Path(ws.worker_dir) / "config.json").read_text())
                wallets = cfg.get("target_wallets", [cfg.get("target_wallet", "?")])
                horas = (time.time() - ws.wallet_last_activity_ts) / 3600
                estado_act = f"activa hace {horas:.0f}h" if ws.running else "worker detenido"
                lines.append(f"\n[{wid}]\n  {wallets[0] if wallets else '?'}\n  {estado_act}")
            except:
                lines.append(f"\n[{wid}] — error leyendo config")
        tg("\n".join(lines))

    elif cmd == "/help":
        tg(
            "🤖 <b>Claudio — Comandos disponibles</b>\n\n"
            "/status [worker]   — Estado de la granja\n"
            "/start [worker]    — Arrancar worker\n"
            "/stop [worker]     — Detener worker (Claudio sigue en standby)\n"
            "/stop all          — Detener TODO incluyendo Claudio\n"
            "/restart [worker]  — Reiniciar worker\n"
            "/selector [worker] — Buscar nueva wallet candidata\n"
            "/wallet            — Ver wallet target activa y su actividad\n"
            "/workers           — Listar workers\n"
            "/help              — Esta ayuda\n\n"
            "Sin [worker] = aplica al primer worker disponible"
        )

def handle_callback(data: str, msg_id: int):
    parts     = data.split(":", 2)
    action    = parts[0]
    worker_id = parts[1] if len(parts) > 1 else None

    if action == "selector" and worker_id:
        tg_edit(msg_id, f"🎯 Selector activado para [{worker_id}]...")
        run_selector_for_worker(worker_id)

    elif action == "approve_n" and worker_id:
        idx = int(parts[2]) if len(parts) > 2 else 0
        tg_edit(msg_id, f"⏳ Aplicando candidato #{idx+1} para [{worker_id}]...")
        if apply_proposal(worker_id, idx):
            stop_worker(worker_id)
            time.sleep(1)
            start_worker(worker_id)
            tg(f"✅ <b>[{worker_id}] Candidato #{idx+1} aprobado y aplicado</b>\nBot reiniciado con nueva wallet target.")
        else:
            tg(f"❌ No se pudo aplicar el candidato #{idx+1} para [{worker_id}]")

    elif action == "restart" and worker_id:
        tg_edit(msg_id, f"⏳ Reiniciando [{worker_id}]...")
        def do_restart_cb(wid):
            stop_worker(wid, wait=True)
            time.sleep(1)
            start_worker(wid)
            tg(f"🔄 [{wid}] reiniciado ✅")
        threading.Thread(target=do_restart_cb, args=(worker_id,), daemon=True).start()

    elif action == "stop" and worker_id:
        stop_worker(worker_id)
        tg_edit(msg_id, f"🔴 [{worker_id}] detenido por Gerencia")

    elif action == "keep" and worker_id:
        # Gerencia decide ignorar la alerta de inactividad
        ws = granja.get(worker_id)
        if ws:
            ws.wallet_inactivity_alerted = True  # no volver a alertar hasta nueva inactividad
            # Resetear timer para que vuelva a alertar en otras 24h
            ws.wallet_last_activity_ts = time.time()
        tg_edit(msg_id, f"✅ [{worker_id}] Inactividad ignorada — continuando")

    elif action == "reject" and worker_id:
        tg_edit(msg_id, f"❌ Propuesta rechazada para [{worker_id}]")
        tg(f"Sin cambios aplicados. Usa /selector {worker_id} para buscar de nuevo.")

# ── Señal de shutdown ─────────────────────────────────────
def shutdown(sig, frame):
    log.info("Claudio recibiendo señal de shutdown...")
    for wid in granja:
        stop_worker(wid)
    tg("🔴 <b>Claudio detenido.</b> Todos los workers pausados.")
    sys.exit(0)

# ── Main ──────────────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info("Claudio iniciando")
    log.info("=" * 50)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)

    discover_workers()
    if not granja:
        log.error("No se encontraron workers en workers/. Revisa la estructura.")
        sys.exit(1)

    # Threads de soporte
    threading.Thread(target=heartbeat_loop,      daemon=True, name="heartbeat").start()
    threading.Thread(target=poll_telegram,       daemon=True, name="telegram-poll").start()
    threading.Thread(target=monitoreo_wallet_loop, daemon=True, name="wallet-monitor").start()

    # Arrancar workers
    for wid in granja:
        start_worker(wid)

    workers_list = "\n".join([f"  • {wid}" for wid in granja])
    tg(
        f"🌱 <b>Claudio iniciado</b>\n"
        f"👷 Workers activos:\n{workers_list}\n\n"
        f"Usa /help para ver comandos disponibles"
    )
    log.info(f"Granja activa con {len(granja)} worker(s)")

    # Loop principal — vigilar threads caídos
    while True:
        time.sleep(60)
        for wid, ws in granja.items():
            if ws.running and ws.thread and not ws.thread.is_alive():
                log.warning(f"[{wid}] Thread muerto inesperadamente — reiniciando")
                ws.running = False
                time.sleep(2)
                start_worker(wid)

if __name__ == "__main__":
    main()

