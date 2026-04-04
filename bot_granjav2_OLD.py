"""
bot.py — Granja V2 — Copy-trading minimo viable
Logica: detecta BUY de wallet target → valida → ejecuta $1 → reporta
"""
import json, time, os, math, logging
from pathlib import Path
from dotenv import load_dotenv
import requests

load_dotenv("/root/granja-v2/.env")

# ── Configuracion ─────────────────────────────────────────
CFG           = json.loads(Path("/root/granja-v2/config.json").read_text())
TARGET_WALLET = CFG["target_wallet"].lower()
POLL_SEC      = CFG.get("poll_seconds", 10)
TRADE_USD     = 1.0

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
TG_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Logger ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("/root/granja-v2/bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("v2")

# ── Telegram ──────────────────────────────────────────────
def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log.warning(f"TG_ERROR | {e}")

# ── CLOB Client ───────────────────────────────────────────
_client = None

def get_client():
    global _client
    if _client is None:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        _client = ClobClient(
            CLOB_API,
            key=os.getenv("WALLET_PRIVATE_KEY"),
            chain_id=137,
            signature_type=1,
            funder=os.getenv("POLYMARKET_PROXY_ADDRESS")
        )
        _client.set_api_creds(ApiCreds(
            api_key=os.getenv("POLY_API_KEY"),
            api_secret=os.getenv("POLY_API_SECRET"),
            api_passphrase=os.getenv("POLY_API_PASSPHRASE")
        ))
    return _client

def get_balance():
    """Lee balance real desde CLOB API."""
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams
        result = get_client().get_balance_allowance(
            BalanceAllowanceParams(asset_type="COLLATERAL")
        )
        return round(int(result.get("balance", 0)) / 1e6, 2)
    except Exception as e:
        log.warning(f"BALANCE_ERROR | {e}")
        return None

# ── Cache de mercados ─────────────────────────────────────
_mkt_cache = {}

def get_market_info(token_id):
    """Obtiene neg_risk, tick_size y clob_token desde Gamma API. Cachea resultado."""
    if token_id in _mkt_cache:
        return _mkt_cache[token_id]
    try:
        r = requests.get(
            f"{GAMMA_API}/markets",
            params={"clob_token_ids": token_id},
            timeout=8
        )
        if not r.ok:
            return None
        data = r.json()
        m = data[0] if isinstance(data, list) and data else data
        if not m:
            return None

        # clobTokenIds viene como JSON string en Gamma
        clob_raw = m.get("clobTokenIds", "[]")
        clob_ids = json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw

        neg_risk  = bool(m.get("negRisk", False))
        # Leer tick_size de Gamma, si no está disponible consultar CLOB directamente
        tick_raw = m.get("minimumTickSize")
        if tick_raw:
            tick_size = str(tick_raw)
        else:
            try:
                clob_raw2 = m.get("clobTokenIds", "[]")
                clob_ids2 = json.loads(clob_raw2) if isinstance(clob_raw2, str) else clob_raw2
                tid2 = clob_ids2[0] if clob_ids2 else token_id
                tr = requests.get(f"{CLOB_API}/tick-size", params={"token_id": str(tid2)}, timeout=5)
                tick_size = str(tr.json().get("minimum_tick_size", "0.01")) if tr.ok else "0.01"
            except:
                tick_size = "0.01"

        # Fecha de cierre
        end_ts = 0
        end_str = m.get("endDate") or m.get("endDateIso", "")
        if end_str:
            from datetime import datetime, timezone
            try:
                end_ts = datetime.fromisoformat(
                    end_str.replace("Z", "+00:00")
                ).timestamp()
            except:
                pass

        # Token correcto para CLOB
        clob_token = str(token_id)
        if clob_ids and str(token_id) not in [str(c) for c in clob_ids]:
            clob_token = str(clob_ids[0])

        info = {
            "neg_risk":   neg_risk,
            "tick_size":  tick_size,
            "end_ts":     end_ts,
            "clob_token": clob_token,
        }
        _mkt_cache[token_id] = info
        log.info(f"MARKET | neg={neg_risk} tick={tick_size} token={clob_token[:20]}...")
        return info
    except Exception as e:
        log.warning(f"GAMMA_ERROR | {e}")
        return None

def has_orderbook(token_id):
    """Verifica que el token tiene orderbook activo CON liquidez real."""
    try:
        r = requests.get(
            f"{CLOB_API}/book",
            params={"token_id": str(token_id)},
            timeout=5
        )
        if not r.ok:
            return False
        book = r.json()
        asks = book.get("asks", [])
        bids = book.get("bids", [])
        # Necesitamos al menos un ask para poder comprar
        return len(asks) > 0
    except:
        return False

def time_remaining(end_ts):
    """Segundos restantes antes del cierre del mercado."""
    if end_ts == 0:
        return 99999
    return end_ts - time.time()

def align_price(price, tick_size):
    tick = float(tick_size)
    return round(math.floor(price / tick) * tick, 6)

# ── Ejecucion de orden ────────────────────────────────────
def execute_buy(token_id, price, neg_risk, tick_size):
    """Ejecuta orden BUY de $1. Retorna (ok, detalle)."""
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    try:
        client = get_client()
        args   = MarketOrderArgs(
            token_id=str(token_id),
            amount=TRADE_USD,
            side=BUY,
        )
        signed = client.create_market_order(args)
        resp   = client.post_order(signed, OrderType.FOK)

        if isinstance(resp, dict) and resp.get("success"):
            return True, f"@{p} size={size:.4f}"
        return False, str(resp)
    except Exception as e:
        return False, str(e)

# ── Tracker de wallet ─────────────────────────────────────
seen_ids  = set()
start_ts  = time.time()

def fetch_signals():
    """Obtiene trades BUY nuevos de la wallet target."""
    try:
        r = requests.get(
            f"{DATA_API}/activity",
            params={"user": TARGET_WALLET, "limit": 20},
            timeout=10
        )
        r.raise_for_status()
        trades = r.json()
        if not isinstance(trades, list):
            return []
    except Exception as e:
        log.warning(f"FETCH_ERROR | {e}")
        return []

    signals = []
    for t in trades:
        tid   = t.get("transactionHash", "")
        ts    = t.get("timestamp", 0)
        side  = str(t.get("side", "")).upper()
        price = float(t.get("price", 0) or 0)

        if not tid or tid in seen_ids:
            continue

        seen_ids.add(tid)

        # Ignorar trades anteriores al arranque
        if ts < start_ts:
            continue

        # Solo BUY con precio valido
        if side != "BUY" or price <= 0:
            continue

        signals.append({
            "tx":    tid,
            "slug":  t.get("slug", ""),
            "token": str(t.get("asset", "")),
            "price": price,
            "size":  float(t.get("usdcSize", 0) or 0),
        })

    return signals

# ── Main ──────────────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info("Granja V2 arrancando")
    log.info(f"Target: {TARGET_WALLET}")
    log.info("=" * 50)

    # Balance inicial
    balance = get_balance()
    balance_str = f"${balance:.2f}" if balance is not None else "N/A"

    tg(
        f"🌱 <b>Granja V2 iniciada</b>\n"
        f"🎯 Target: {TARGET_WALLET[:14]}...\n"
        f"💰 Balance: {balance_str}\n"
        f"📊 CP: 0/0\n"
        f"⏱ Polling cada {POLL_SEC}s"
    )

    copies        = 0
    signals_total = 0
    last_hb       = time.time()

    while True:
        try:
            now = time.time()

            # ── Heartbeat cada 5 minutos ──
            if now - last_hb >= 300:
                balance = get_balance()
                balance_str = f"${balance:.2f}" if balance is not None else "N/A"
                tg(
                    f"💓 <b>Granja V2</b>\n"
                    f"💰 Balance: {balance_str}\n"
                    f"📊 CP: {copies}/{signals_total}\n"
                    f"🎯 {TARGET_WALLET[:14]}..."
                )
                last_hb = now

            # ── Fetch señales nuevas ──
            signals = fetch_signals()

            # Verificar balance antes de procesar señales
            if signals:
                balance_check = get_balance()
                if balance_check is not None and balance_check < TRADE_USD:
                    log.warning(f"SKIP_ALL | balance insuficiente ${balance_check:.2f} < ${TRADE_USD}")
                    tg(
                        f"⏸ <b>Sin cash disponible</b>\n"
                        f"💰 Cash: ${balance_check:.2f} — insuficiente para nuevas órdenes\n"
                        f"⏳ Esperando resolución de posiciones abiertas..."
                    )
                    time.sleep(POLL_SEC)
                    continue

            for ev in signals:
                signals_total += 1
                slug  = ev["slug"]
                token = ev["token"]
                price = ev["price"]

                log.info(f"SIGNAL | {slug[:40]} @{price:.3f} ${ev['size']:.0f}")

                # 1. Info del mercado
                info = get_market_info(token)
                if not info:
                    log.warning(f"SKIP | no_market_info | {slug[:40]}")
                    continue

                clob_token = info["clob_token"]
                neg_risk   = info["neg_risk"]
                tick_size  = info["tick_size"]
                remaining  = time_remaining(info["end_ts"])

                # 2. Tiempo restante minimo 2 horas
                if remaining < 7200:
                    log.warning(f"SKIP | too_close | {int(remaining/60)}min | {slug[:40]}")
                    continue

                # 3. Orderbook activo
                if not has_orderbook(clob_token):
                    log.warning(f"SKIP | no_orderbook | {clob_token[:20]}...")
                    continue

                # 4. Ejecutar
                ok, detail = execute_buy(clob_token, price, neg_risk, tick_size)

                if ok:
                    copies += 1
                    balance = get_balance()
                    balance_str = f"${balance:.2f}" if balance is not None else "N/A"
                    log.info(f"COPY | OK #{copies} | {detail} | bal={balance_str}")
                    tg(
                        f"✅ <b>COPY #{copies}</b>\n"
                        f"📌 {slug[:40]}\n"
                        f"💵 ${TRADE_USD} @{price:.3f}\n"
                        f"💰 Balance: {balance_str}"
                    )
                else:
                    log.warning(f"COPY | FAIL | {detail[:80]}")
                    if "error" in detail.lower() or "exception" in detail.lower():
                        tg(f"⚠️ <b>COPY FAIL</b>\n{slug[:40]}\n{detail[:100]}")

            time.sleep(POLL_SEC)

        except KeyboardInterrupt:
            log.info("Detenido.")
            tg(f"🔴 Granja V2 detenida.\nCopies: {copies}/{signals_total}")
            break
        except Exception as e:
            log.error(f"LOOP_ERROR | {e}", exc_info=True)
            time.sleep(5)

if __name__ == "__main__":
    main()
