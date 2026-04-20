"""
bot_granjav2.py — Worker de copy-trading puro
Responsabilidad única: detectar BUY/SELL de wallet target → ejecutar → retornar resultado
Sin Telegram. Sin heartbeat. Sin lógica de control.
Claudio es quien orquesta.
"""
import json, time, os, math, logging
from pathlib import Path
from dotenv import load_dotenv
import requests

import sys
# Asegurar que el venv esté en el path
_venv = "/root/granja-v2/venv/lib"
import glob as _glob
for _p in _glob.glob(f"{_venv}/python*/site-packages"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Setup (llamado por Claudio al instanciar el worker) ───
def setup(worker_dir: str):
    """Inicializa el worker con su directorio propio. Retorna config dict."""
    env_path = Path(worker_dir) / ".env"
    cfg_path = Path(worker_dir) / "config.json"

    if not env_path.exists():
        raise FileNotFoundError(f"No se encontró .env en {worker_dir}")
    if not cfg_path.exists():
        raise FileNotFoundError(f"No se encontró config.json en {worker_dir}")

    load_dotenv(env_path)
    cfg = json.loads(cfg_path.read_text())
    return cfg

# ── APIs ──────────────────────────────────────────────────
DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

# ── CLOB Client ───────────────────────────────────────────
_clients = {}  # cache por worker_dir

def get_client(worker_dir: str):
    if worker_dir not in _clients:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        load_dotenv(Path(worker_dir) / ".env", override=True)
        c = ClobClient(
            CLOB_API,
            key=os.getenv("WALLET_PRIVATE_KEY"),
            chain_id=137,
            signature_type=1,
            funder=os.getenv("POLYMARKET_PROXY_ADDRESS")
        )
        c.set_api_creds(ApiCreds(
            api_key=os.getenv("POLY_API_KEY"),
            api_secret=os.getenv("POLY_API_SECRET"),
            api_passphrase=os.getenv("POLY_API_PASSPHRASE")
        ))
        _clients[worker_dir] = c
    return _clients[worker_dir]

def get_balance(worker_dir: str) -> float | None:
    """Lee balance real desde CLOB API."""
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams
        result = get_client(worker_dir).get_balance_allowance(
            BalanceAllowanceParams(asset_type="COLLATERAL")
        )
        return round(int(result.get("balance", 0)) / 1e6, 2)
    except Exception as e:
        return None

# ── Cache de mercados ─────────────────────────────────────
_mkt_cache = {}

def get_market_info(token_id: str) -> dict | None:
    """Obtiene neg_risk, tick_size, end_ts y clob_token desde Gamma API."""
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

        clob_raw  = m.get("clobTokenIds", "[]")
        clob_ids  = json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw
        neg_risk  = bool(m.get("negRisk", False))

        tick_raw = m.get("minimumTickSize")
        if tick_raw:
            tick_size = str(tick_raw)
        else:
            try:
                tid2 = clob_ids[0] if clob_ids else token_id
                tr = requests.get(f"{CLOB_API}/tick-size", params={"token_id": str(tid2)}, timeout=5)
                tick_size = str(tr.json().get("minimum_tick_size", "0.01")) if tr.ok else "0.01"
            except:
                tick_size = "0.01"

        end_ts = 0
        end_str = m.get("endDate") or m.get("endDateIso", "")
        if end_str:
            from datetime import datetime, timezone
            try:
                end_ts = datetime.fromisoformat(end_str.replace("Z", "+00:00")).timestamp()
            except:
                pass

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
        return info
    except:
        return None

def get_midpoint(token_id: str) -> float | None:
    """Retorna midpoint actual desde CLOB /book. None si falla."""
    try:
        r = requests.get(f"{CLOB_API}/book", params={"token_id": str(token_id)}, timeout=5)
        if not r.ok:
            return None
        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        if best_bid and best_ask:
            return (best_bid + best_ask) / 2
        return None
    except:
        return None

def has_orderbook(token_id: str) -> bool:
    """Verifica que el token tiene al menos un ask activo."""
    try:
        r = requests.get(f"{CLOB_API}/book", params={"token_id": str(token_id)}, timeout=5)
        if not r.ok:
            return False
        book = r.json()
        return len(book.get("asks", [])) > 0
    except:
        return False

def market_is_resolved(end_ts: float) -> bool:
    """True si el mercado ya cerró."""
    if end_ts == 0:
        return False
    return time.time() > end_ts

def align_price(price: float, tick_size: str) -> float:
    tick = float(tick_size)
    return round(math.floor(price / tick) * tick, 6)

# ── Ejecución de órdenes ──────────────────────────────────
def execute_buy(worker_dir: str, token_id: str, amount_usd: float) -> tuple[bool, str]:
    """Ejecuta orden BUY de market order. Retorna (ok, detalle)."""
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    try:
        client = get_client(worker_dir)
        args   = MarketOrderArgs(token_id=str(token_id), amount=amount_usd, side=BUY)
        signed = client.create_market_order(args)
        resp   = client.post_order(signed, OrderType.FOK)
        if isinstance(resp, dict) and resp.get("success"):
            return True, f"FOK OK | ${amount_usd}"
        return False, str(resp)
    except Exception as e:
        return False, str(e)

def execute_sell(worker_dir: str, token_id: str, size: float, price: float, tick_size: str, neg_risk: bool) -> tuple[bool, str]:
    """Ejecuta orden SELL. Retorna (ok, detalle)."""
    from py_clob_client.clob_types import OrderArgs, CreateOrderOptions, OrderType
    from py_clob_client.order_builder.constants import SELL
    try:
        client      = get_client(worker_dir)
        sell_price  = align_price(price, tick_size)
        if sell_price <= 0:
            sell_price = float(tick_size)

        args = OrderArgs(
            token_id=str(token_id),
            price=sell_price,
            size=round(size, 4),
            side=SELL,
        )
        options = CreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        signed  = client.create_order(args, options)
        resp    = client.post_order(signed, OrderType.GTC)
        if isinstance(resp, dict) and (resp.get("success") or resp.get("orderID")):
            return True, f"SELL OK | size={size:.4f} @{sell_price}"
        return False, str(resp)
    except Exception as e:
        return False, str(e)

# ── Tracker de señales ────────────────────────────────────
seen_ids     = set()
seen_ids_ts  = {}  # timestamp por ID para expiración
start_ts     = 0  # se fija al arrancar el worker
my_positions = set()  # tokens donde nosotros abrimos BUY

def load_seen_ids(worker_dir: str) -> set:
    """Carga seen_ids desde disco para sobrevivir reinicios."""
    path = Path(worker_dir) / "seen_ids.json"
    try:
        if path.exists():
            data = json.loads(path.read_text())
            # Solo cargar IDs de las últimas 24 horas
            cutoff = time.time() - 86400
            return {tid for tid, ts in data.items() if ts > cutoff}
    except:
        pass
    return set()

def save_seen_ids(worker_dir: str, seen: set, seen_ts: dict):
    """Guarda seen_ids en disco."""
    try:
        path = Path(worker_dir) / "seen_ids.json"
        # Solo guardar últimas 24h para no crecer indefinidamente
        cutoff = time.time() - 86400
        data = {tid: ts for tid, ts in seen_ts.items() if ts > cutoff}
        path.write_text(json.dumps(data))
    except:
        pass

def fetch_signals_wallet(wallet: str) -> list[dict]:
    """Obtiene trades BUY/SELL nuevos de UNA wallet."""
    try:
        r = requests.get(
            f"{DATA_API}/activity",
            params={"user": wallet.lower(), "limit": 20},
            timeout=10
        )
        r.raise_for_status()
        trades = r.json()
        if not isinstance(trades, list):
            return []
    except:
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
        seen_ids_ts[tid] = time.time()

        if ts < start_ts:
            continue

        if side not in ("BUY", "SELL") or price <= 0:
            continue

        signals.append({
            "tx":     tid,
            "slug":   t.get("slug", ""),
            "token":  str(t.get("asset", "")),
            "price":  price,
            "size":   float(t.get("usdcSize", 0) or 0),
            "side":   side,
            "wallet": wallet.lower(),
        })

    return signals

def fetch_signals(target_wallets: list, basket_min: int = 1) -> list[dict]:
    """
    Obtiene señales del basket de wallets.
    Con basket_min=1: retorna todas las señales (comportamiento legacy).
    Con basket_min=2+: retorna solo señales confirmadas por N wallets.
    """
    if isinstance(target_wallets, str):
        target_wallets = [target_wallets]

    # Recopilar señales de todas las wallets
    all_signals = []
    for wallet in target_wallets:
        all_signals.extend(fetch_signals_wallet(wallet))

    if basket_min <= 1:
        return all_signals

    # Agrupar por token+side para detectar consenso
    from collections import defaultdict
    token_votes = defaultdict(lambda: {"wallets": set(), "signals": []})
    for s in all_signals:
        key = f"{s['token']}:{s['side']}"
        token_votes[key]["wallets"].add(s["wallet"])
        token_votes[key]["signals"].append(s)

    # Solo retornar señales con suficiente consenso
    confirmed = []
    for key, data in token_votes.items():
        if len(data["wallets"]) >= basket_min:
            # Tomar la señal con mejor precio (la más reciente)
            best = sorted(data["signals"], key=lambda x: x["price"])[0]
            best["confirmaciones"] = len(data["wallets"])
            best["wallets_confirmaron"] = list(data["wallets"])
            confirmed.append(best)

    return confirmed

# ── Loop principal del worker ─────────────────────────────
def run_worker(worker_dir: str, log: logging.Logger, event_callback, stop_event=None) -> None:
    # Usar logger propio para evitar duplicados con claudio
    bot_log = logging.getLogger(f"bot.{Path(worker_dir).name}")
    bot_log.propagate = False
    if not bot_log.handlers:
        bot_log.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        for h in log.handlers:
            bot_log.addHandler(h)
    log = bot_log
    """
    Loop principal del worker.
    event_callback(worker_id, event_type, data) — Claudio recibe eventos:
      event_type: 'copy_ok' | 'copy_fail' | 'sell_ok' | 'sell_fail' |
                  'no_cash' | 'market_resolved' | 'consecutive_errors' | 'skip'
    """
    cfg           = setup(worker_dir)
    worker_id     = Path(worker_dir).name
    # Soportar tanto target_wallet (legacy) como target_wallets (basket)
    if "target_wallets" in cfg:
        target_wallets = [w.lower() for w in cfg["target_wallets"]]
    else:
        target_wallets = [cfg["target_wallet"].lower()]
    basket_min = cfg.get("basket_min_confirmations", 1)
    poll_sec   = cfg.get("poll_seconds", 10)
    trade_usd  = cfg.get("trade_usd", 1.0)
    target_wallet = target_wallets[0]  # para logs

    # Cargar posiciones previas (bootstrap) si existen
    bootstrap_path = Path(worker_dir) / "positions_bootstrap.json"
    if bootstrap_path.exists():
        try:
            bootstrap = json.loads(bootstrap_path.read_text())
            for asset in bootstrap.get("assets", []):
                my_positions.add(str(asset))
            log.info(f"[{worker_id}] Bootstrap: {len(my_positions)} posiciones cargadas")
        except Exception as e:
            log.warning(f"[{worker_id}] Bootstrap error: {e}")

    global start_ts, seen_ids, seen_ids_ts
    start_ts = time.time()

    # Cargar seen_ids persistidos
    seen_ids = load_seen_ids(worker_dir)
    try:
        path = Path(worker_dir) / "seen_ids.json"
        if path.exists():
            seen_ids_ts = json.loads(path.read_text())
    except:
        seen_ids_ts = {}
    log.info(f"[{worker_id}] seen_ids cargados: {len(seen_ids)} IDs previos")
    wallets_str = ", ".join([w[:14] for w in target_wallets])
    log.info(f"[{worker_id}] Arrancando | basket={len(target_wallets)} wallets | min={basket_min} | {wallets_str}...")

    copies            = 0
    signals_total     = 0
    consecutive_errors = 0

    while True:
        try:
            # Verificar señal de parada
            if stop_event and stop_event.is_set():
                log.info(f"[{worker_id}] Stop event recibido — deteniendo")
                return

            signals = fetch_signals(target_wallets, basket_min)

            # Pre-cargar balance una vez por ciclo
            balance_cache = get_balance(worker_dir) if signals else None

            for ev in signals:
                signals_total += 1
                slug   = ev["slug"]
                token  = ev["token"]
                price  = ev["price"]
                side   = ev["side"]

                log.info(f"[{worker_id}] SIGNAL {side} | {slug[:40]} @{price:.3f}")

                # ── Info del mercado ──
                info = get_market_info(token)
                if not info:
                    log.warning(f"[{worker_id}] SKIP | no_market_info")
                    event_callback(worker_id, "skip", {"reason": "no_market_info", "slug": slug})
                    continue

                clob_token = info["clob_token"]
                neg_risk   = info["neg_risk"]
                tick_size  = info["tick_size"]

                # ── Mercado resuelto ──
                if market_is_resolved(info["end_ts"]):
                    log.warning(f"[{worker_id}] SKIP | market_resolved | {slug[:40]}")
                    event_callback(worker_id, "market_resolved", {"slug": slug, "token": clob_token})
                    continue

                # ── Mercado demasiado lejano (> 48h) ──
                if info["end_ts"] > 0:
                    horas_restantes = (info["end_ts"] - time.time()) / 3600
                    if horas_restantes > 222:
                        log.info(f"[{worker_id}] SKIP | demasiado_lejano | {horas_restantes:.0f}h | {slug[:40]}")
                        continue

                    # Categorias sin edge estructural (Becker 2026 — gap maker-taker alto)
                    BLOCKED_CATS = ['lol-', 'cs2-', 'ufc-', 'cbb-',
                                    'temperature', 'highest-temp',
                                    'nba-', 'nhl-', 'mlb-', 'nfl-', 'epl-',
                                    'of-tweets', 'truth-social', '-posts-this-week',
                                    'will-nyc-have', 'will-seattle-have', 'will-chicago-have']
                    if any(b in slug for b in BLOCKED_CATS):
                        log.info(f"[{worker_id}] SKIP | cat_bloqueada | {slug[:40]}")
                        continue

                # ── Procesar BUY ──
                if side == "BUY":
                    # Una posición por mercado máximo — evitar promediar pérdidas
                    if clob_token in my_positions:
                        log.info(f"[{worker_id}] SKIP BUY | ya tenemos posición en {slug[:40]}")
                        continue
                    # Verificar balance antes de intentar
                    if balance_cache is not None and float(balance_cache) < trade_usd:
                        log.warning(f"[{worker_id}] Sin cash | ${balance_cache:.2f}")
                        event_callback(worker_id, "no_cash", {"balance": balance_cache})
                        continue  # saltar este BUY pero seguir con otros signals (ej: SELLs)
                    if not has_orderbook(clob_token):
                        log.warning(f"[{worker_id}] SKIP | no_orderbook")
                        event_callback(worker_id, "skip", {"reason": "no_orderbook", "slug": slug})
                        continue

                    # ── Drift filter — skip si precio se movió >0.10 desde entrada de target ──
                    midpoint_now = get_midpoint(clob_token)
                    if midpoint_now is not None:
                        drift = midpoint_now - float(price)
                        if drift > 0.10:
                            log.info(f"[{worker_id}] SKIP | drift_alto | {drift:.3f} | {slug[:40]}")
                            continue

                    ok, detail = execute_buy(worker_dir, clob_token, trade_usd)
                    if ok:
                        my_positions.add(clob_token)
                        copies += 1
                        consecutive_errors = 0
                        balance = get_balance(worker_dir)
                        log.info(f"[{worker_id}] COPY #{copies} OK | {detail}")
                        event_callback(worker_id, "copy_ok", {
                            "n": copies, "slug": slug, "price": price,
                            "usd": trade_usd, "balance": balance
                        })
                    else:
                        consecutive_errors += 1
                        log.warning(f"[{worker_id}] COPY FAIL #{consecutive_errors} | {detail[:80]}")
                        event_callback(worker_id, "copy_fail", {
                            "n": consecutive_errors, "slug": slug, "detail": detail
                        })
                        if consecutive_errors >= 3:
                            event_callback(worker_id, "consecutive_errors", {
                                "n": consecutive_errors, "last_error": detail
                            })
                            return  # Claudio decide qué hacer

                # ── Procesar SELL ──
                elif side == "SELL":
                    if clob_token not in my_positions:
                        log.info(f"[{worker_id}] SKIP SELL | no tenemos posición en {slug[:30]}")
                        continue
                    # Buscar nuestro size real en la Data API
                    size_to_sell = 0.0
                    try:
                        proxy = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
                        rp = requests.get(
                            f"{DATA_API}/positions",
                            params={"user": proxy},
                            timeout=8
                        )
                        if rp.ok:
                            for pos in rp.json():
                                if str(pos.get("asset","")) == str(clob_token):
                                    size_to_sell = float(pos.get("size", 0))
                                    break
                    except Exception as ep:
                        log.warning(f"[{worker_id}] No pude leer size real: {ep}")
                    if size_to_sell <= 0:
                        log.warning(f"[{worker_id}] SKIP SELL | size=0 para {slug[:30]}")
                        continue
                    ok, detail = execute_sell(
                        worker_dir, clob_token, size_to_sell, price, tick_size, neg_risk
                    )
                    if ok:
                        consecutive_errors = 0
                        log.info(f"[{worker_id}] SELL OK | {detail}")
                        event_callback(worker_id, "sell_ok", {
                            "slug": slug, "price": price, "size": size_to_sell
                        })
                    else:
                        consecutive_errors += 1
                        log.warning(f"[{worker_id}] SELL FAIL | {detail[:80]}")
                        event_callback(worker_id, "sell_fail", {
                            "slug": slug, "detail": detail
                        })

            # Persistir seen_ids cada ciclo
            save_seen_ids(worker_dir, seen_ids, seen_ids_ts)
            time.sleep(poll_sec)

        except Exception as e:
            log.error(f"[{worker_id}] LOOP_ERROR | {e}", exc_info=True)
            time.sleep(5)
