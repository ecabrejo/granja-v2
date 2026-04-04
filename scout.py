"""
scout.py — Buscador independiente de wallets candidatas
Enfoque: wallet-first (no market-first)
Lógica: wallets buenas → qué mercado están operando AHORA
NO modifica config.json, NO toca claudio, NO toca hunter.
Solo reporta candidatos para que Gerencia decida.

Fuentes de wallets:
  A) polymarketanalytics sortBy=win_rate  — traders consistentes
  B) polymarketanalytics sortBy=pnl       — traders de alto volumen
  Wallets en ambas fuentes reciben bonus +5 en score_final.
"""
import json, time, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

DATA_API     = "https://data-api.polymarket.com"
GAMMA_API    = "https://gamma-api.polymarket.com"
CLOB_API     = "https://clob.polymarket.com"
ANALYTICS    = "https://polymarketanalytics.com/api"
HEADERS      = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

MIN_WIN_RATE   = 0.55   # mínimo 55% win rate (solo Fuente A)
MIN_PNL        = 1000   # mínimo $1000 PnL histórico (ambas fuentes)
MIN_POSITIONS  = 10     # mínimo 10 posiciones históricas
MIN_WIN_RATE_B = 0.50   # win rate mínimo para Fuente B (más permisivo)
VENTANA_H      = 4      # actividad reciente: últimas 4 horas
MIN_MARKET_VOL = 50000  # volumen mínimo del mercado
MAX_HORAS      = 48     # máximo horas restantes del mercado
MAX_SPREAD     = 0.50   # máximo spread aceptable

# ── Helpers ───────────────────────────────────────────────
def fmt_time(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%m/%d %H:%M')

def calcular_consistencia(win_amount, loss_amount, total_positions, overall_gain):
    """
    Score de consistencia 0-100.
    Penaliza pérdidas grandes relativas a ganancias.
    Premia ganancias distribuidas (no concentradas en pocas apuestas enormes).
    """
    if win_amount <= 0:
        return 0
    loss_abs = abs(loss_amount)
    if loss_abs == 0:
        ratio = 1.0
    else:
        ratio = min(win_amount / loss_abs, 10) / 10  # normalizado 0-1

    if total_positions > 0:
        avg_gain = overall_gain / total_positions
        if avg_gain > 50000:
            distribucion = 0.3
        elif avg_gain > 10000:
            distribucion = 0.6
        elif avg_gain > 1000:
            distribucion = 0.9
        else:
            distribucion = 1.0
    else:
        distribucion = 0

    return round((ratio * 0.6 + distribucion * 0.4) * 100, 1)

def _parsear_entrada_analytics(e):
    """Convierte una entrada de analytics a dict normalizado."""
    return {
        "addr":       e.get("trader", "").lower(),
        "name":       e.get("trader_name", "?"),
        "win_rate":   float(e.get("win_rate", 0)),
        "pnl":        float(e.get("overall_gain", 0)),
        "positions":  int(e.get("total_positions", 0)),
        "rank":       int(e.get("rank", 9999)),
        "tags":       e.get("trader_tags", ""),
        "win_amount": float(e.get("win_amount", 0)),
        "loss_amount":float(e.get("loss_amount", 0)),
    }

# ── Fuente A: top por win_rate ─────────────────────────────
def cargar_top_wallets_wr():
    """Traders con mejor win rate sostenido. Filtro estricto."""
    print("📡 [A] polymarketanalytics sortBy=win_rate...", end="", flush=True)
    try:
        r = requests.get(
            f"{ANALYTICS}/traders-tag-performance",
            params={"tag": "Overall", "sortBy": "win_rate", "order": "desc", "limit": 200},
            headers=HEADERS,
            timeout=15
        )
        data = r.json()
        data = data.get("data", data) if isinstance(data, dict) else data
        wallets = []
        for e in data:
            w = _parsear_entrada_analytics(e)
            if w["win_rate"] >= MIN_WIN_RATE and w["pnl"] >= MIN_PNL and w["positions"] >= MIN_POSITIONS:
                wallets.append(w)
        print(f" {len(wallets)} wallets")
        return wallets
    except Exception as e:
        print(f" ERROR: {e}")
        return []

# ── Fuente B: top por PnL absoluto ────────────────────────
def cargar_top_wallets_pnl():
    """
    Traders con mayor PnL histórico absoluto.
    Filtro de WR más permisivo (50%) para capturar traders de alto volumen
    que la Fuente A podría no incluir.
    """
    print("📡 [B] polymarketanalytics sortBy=pnl...", end="", flush=True)
    try:
        r = requests.get(
            f"{ANALYTICS}/traders-tag-performance",
            params={"tag": "Overall", "sortBy": "pnl", "order": "desc", "limit": 200},
            headers=HEADERS,
            timeout=15
        )
        data = r.json()
        data = data.get("data", data) if isinstance(data, dict) else data
        wallets = []
        for e in data:
            w = _parsear_entrada_analytics(e)
            if w["win_rate"] >= MIN_WIN_RATE_B and w["pnl"] >= MIN_PNL and w["positions"] >= MIN_POSITIONS:
                wallets.append(w)
        print(f" {len(wallets)} wallets")
        return wallets
    except Exception as e:
        print(f" ERROR: {e}")
        return []

def merge_fuentes(wallets_a: list, wallets_b: list) -> tuple[list, set]:
    """
    Combina ambas listas. Deduplicación por addr.
    Retorna (lista_unificada, set_de_addrs_en_ambas_fuentes).
    Para wallets duplicadas, conserva el mayor PnL (suelen coincidir).
    """
    seen   = {}
    addrs_a = {w["addr"] for w in wallets_a}
    addrs_b = {w["addr"] for w in wallets_b}
    en_ambas = addrs_a & addrs_b

    for w in wallets_a + wallets_b:
        addr = w["addr"]
        if addr not in seen or w["pnl"] > seen[addr]["pnl"]:
            seen[addr] = w

    merged = list(seen.values())
    print(f"   → {len(wallets_a)} (A) + {len(wallets_b)} (B) = {len(merged)} únicas | {len(en_ambas)} en ambas fuentes ✨")
    return merged, en_ambas

# ── Ver actividad reciente de una wallet ──────────────────
def get_actividad_reciente(addr, ventana_h=4):
    """Retorna trades de las últimas ventana_h horas."""
    cutoff = time.time() - ventana_h * 3600
    try:
        r = requests.get(
            f"{DATA_API}/activity",
            params={"user": addr.lower(), "limit": 30},
            timeout=8
        )
        trades = r.json()
        if not isinstance(trades, list):
            return []
        return [t for t in trades if t.get("timestamp", 0) > cutoff]
    except:
        return []

# ── Obtener info del mercado ──────────────────────────────
_market_cache = {}

def get_market_info(slug):
    if slug in _market_cache:
        return _market_cache[slug]
    _vacio = {
        "vol": 0, "horas": 0, "active": False, "closed": True,
        "token_id": "", "neg_risk": False, "tick_size": "0.01",
        "has_liquidity": False, "spread": None, "midpoint": None
    }
    try:
        r = requests.get(
            f"{GAMMA_API}/markets",
            params={"slug": slug, "limit": 1},
            timeout=8
        )
        data = r.json()
        m = data[0] if isinstance(data, list) and data else {}
        if not m:
            _market_cache[slug] = _vacio
            return _vacio

        vol    = float(m.get("volume24hr", 0) or m.get("volume", 0) or 0)
        active = m.get("active", True)
        closed = m.get("closed", False)

        clob_raw = m.get("clobTokenIds", "[]")
        try:
            clob_ids = json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw
            token_id = str(clob_ids[0]) if clob_ids else ""
        except:
            token_id = ""

        neg_risk  = bool(m.get("negRisk", False))
        tick_size = str(m.get("minimumTickSize", "0.01") or "0.01")

        horas = 0
        end   = m.get("endDate", "")
        if end:
            try:
                end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                horas  = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            except:
                pass

        has_liquidity = False
        spread        = None
        midpoint      = None
        if token_id:
            try:
                rb = requests.get(f"{CLOB_API}/book",
                    params={"token_id": token_id}, timeout=5)
                if rb.ok:
                    book = rb.json()
                    asks = book.get("asks", [])
                    bids = book.get("bids", [])
                    has_liquidity = len(asks) > 0 and len(bids) > 0
                    if has_liquidity:
                        spread = round(float(asks[0]["price"]) - float(bids[0]["price"]), 4)
                rm = requests.get(f"{CLOB_API}/midpoint",
                    params={"token_id": token_id}, timeout=5)
                if rm.ok:
                    midpoint = float(rm.json().get("mid", 0.5))
            except:
                pass

        result = {
            "vol": vol, "horas": horas, "active": active, "closed": closed,
            "token_id": token_id, "neg_risk": neg_risk, "tick_size": tick_size,
            "has_liquidity": has_liquidity, "spread": spread, "midpoint": midpoint
        }
        _market_cache[slug] = result
        return result
    except:
        _market_cache[slug] = _vacio
        return _vacio

# ── Main ──────────────────────────────────────────────────
def main():
    print("=" * 65)
    print("🦅 SCOUT — Buscador wallet-first de candidatos")
    print(f"   Filtros A: WR≥{MIN_WIN_RATE*100:.0f}% | B: WR≥{MIN_WIN_RATE_B*100:.0f}%")
    print(f"   PnL≥${MIN_PNL:,} | Pos≥{MIN_POSITIONS} | Ventana: {VENTANA_H}h")
    print("=" * 65)
    print()

    # ── Cargar y mergear fuentes ──────────────────────────
    wallets_a = cargar_top_wallets_wr()
    wallets_b = cargar_top_wallets_pnl()
    top_wallets, en_ambas = merge_fuentes(wallets_a, wallets_b)

    if not top_wallets:
        print("❌ Sin wallets calificadas. Verifica conexión.")
        return

    print(f"\n🔍 Verificando actividad reciente de {len(top_wallets)} wallets en paralelo...")

    candidatos = []
    import threading
    lock        = threading.Lock()
    completados = [0]

    def procesar_wallet(w):
        addr   = w["addr"]
        trades = get_actividad_reciente(addr, VENTANA_H)
        if not trades:
            return []

        mercados_activos = defaultdict(lambda: {"buys": 0, "sells": 0, "vol": 0, "last_ts": 0})
        for t in trades:
            side = t.get("side", "").upper()
            slug = t.get("slug", "")
            if not slug or "updown" in slug:
                continue
            mercados_activos[slug]["last_ts"] = max(mercados_activos[slug]["last_ts"], t.get("timestamp", 0))
            mercados_activos[slug]["vol"]    += float(t.get("usdcSize", 0) or 0)
            if side == "BUY":
                mercados_activos[slug]["buys"] += 1
            elif side == "SELL":
                mercados_activos[slug]["sells"] += 1

        resultados = []
        for slug, mdata in mercados_activos.items():
            if mdata["buys"] == 0:
                continue
            minfo = get_market_info(slug)
            if minfo["closed"] or minfo["horas"] < 0.5:
                continue
            if minfo["horas"] > MAX_HORAS:
                continue
            if minfo["vol"] < MIN_MARKET_VOL:
                continue
            if not minfo["has_liquidity"]:
                continue
            # Filtrar mercados ya casi resueltos
            if minfo.get("midpoint") is not None:
                mid = minfo["midpoint"]
                if mid < 0.10 or mid >= 0.90:
                    continue

            hace_min     = int((time.time() - mdata["last_ts"]) / 60)
            consistencia = calcular_consistencia(
                w.get("win_amount", w["pnl"]),
                w.get("loss_amount", 0),
                w.get("positions", 1),
                w["pnl"]
            )
            # Bonus por aparecer en ambas fuentes
            bonus_fuentes = 5 if addr in en_ambas else 0

            resultados.append({
                "addr":        addr,
                "name":        w["name"],
                "win_rate":    w["win_rate"],
                "pnl":         w["pnl"],
                "rank":        w["rank"],
                "tags":        w["tags"],
                "slug":        slug,
                "buys":        mdata["buys"],
                "sells":       mdata["sells"],
                "vol_trade":   mdata["vol"],
                "hace_min":    hace_min,
                "vol_market":  minfo["vol"],
                "horas":       minfo["horas"],
                "consistencia":consistencia,
                "win_amount":  w.get("win_amount", 0),
                "loss_amount": w.get("loss_amount", 0),
                "token_id":    minfo.get("token_id", ""),
                "neg_risk":    minfo.get("neg_risk", False),
                "tick_size":   minfo.get("tick_size", "0.01"),
                "spread":      minfo.get("spread"),
                "midpoint":    minfo.get("midpoint"),
                "en_ambas":    addr in en_ambas,
                "score_final": round(
                    w["win_rate"] * 40 +
                    (consistencia / 100) * 30 +
                    (1.0 - abs((minfo.get("midpoint") or 0.5) - 0.5) * 2) * 20 +
                    max(0, 1 - hace_min / 120) * 10 +
                    bonus_fuentes, 1
                ),
            })
        return resultados

    # max_workers=10 para no saturar la API con 200+ wallets en paralelo
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(procesar_wallet, w): w for w in top_wallets}
        for future in as_completed(futures):
            with lock:
                completados[0] += 1
                pct = completados[0] / len(top_wallets) * 100
                print(f"   [{completados[0]}/{len(top_wallets)}] {pct:.0f}% verificando...", end="\r", flush=True)
            try:
                resultados = future.result()
                with lock:
                    candidatos.extend(resultados)
            except:
                pass

    print(f"\n✅ {len(candidatos)} candidatos encontrados\n")

    if not candidatos:
        print("❌ Ninguna wallet calificada está activa en un mercado válido ahora mismo.")
        print(f"   Intenta ampliar la ventana (actual: {VENTANA_H}h) o bajar los filtros.")
        return

    candidatos.sort(key=lambda x: -x["score_final"])

    print(f"{'#':<3} {'SCORE':>6} {'WR':>5} {'CONS':>6} {'PnL hist':>10} {'BUYs':>4} {'$Vol':>7} {'Hace':>5} {'Mid':>5} {'Horas':>6}  WALLET")
    print("─" * 110)

    for i, c in enumerate(candidatos[:10], 1):
        pnl_str   = f"${c['pnl']:>,.0f}"
        wr_str    = f"{c['win_rate']*100:.0f}%"
        vol_str   = f"${c['vol_trade']:,.0f}"
        cons_str  = f"{c['consistencia']:.0f}/100"
        loss_str  = f"-${abs(c['loss_amount']):,.0f}" if c['loss_amount'] else "$0"
        mid_str   = f"{c.get('midpoint',0):.2f}" if c.get('midpoint') else "?"
        score_str = f"{c['score_final']:.0f}/100"
        ambas_tag = " ✨" if c.get("en_ambas") else ""
        print(f"{i:<3} {score_str:>6} {wr_str:>5} {cons_str:>6} {pnl_str:>10} {c['buys']:>4} {vol_str:>7} {c['hace_min']:>4}min {mid_str:>5} {c['horas']:>5.1f}h  {c['addr'][:16]}...{ambas_tag} ({c['name'][:12]})")
        print(f"    📌 {c['slug'][:65]}")
        spread_str = f"spread={c['spread']:.4f}" if c.get('spread') is not None else "spread=?"
        mid_str2   = f"mid={c.get('midpoint','?')}" if c.get('midpoint') is not None else ""
        ratio_str  = f"{c['win_amount']/max(abs(c['loss_amount']),1):.1f}x"
        print(f"    📉 Pérdidas: {loss_str} | Ratio: {ratio_str} | {spread_str} {mid_str2}")
        if c["tags"]:
            print(f"    🏷  {c['tags'][:70]}")
        print()

    # Guardar resultados
    output = {
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "candidatos": candidatos[:10],
        "filtros": {
            "min_win_rate_a": MIN_WIN_RATE,
            "min_win_rate_b": MIN_WIN_RATE_B,
            "min_pnl":        MIN_PNL,
            "ventana_h":      VENTANA_H,
        }
    }
    with open("/root/granja-v2/scout_results.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"💾 Resultados guardados en scout_results.json")

    # ── Modo --auto: proposal para Claudio ───────────────
    if "--auto" in __import__("sys").argv:
        if candidatos:
            mejor = candidatos[0]
            proposal = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "mercado": {
                    "slug":  mejor["slug"],
                    "vol":   mejor["vol_market"],
                    "hours": mejor["horas"],
                    "token": mejor.get("token_id", ""),
                    "neg":   mejor.get("neg_risk", False),
                    "score": 3,
                },
                "wallet": {
                    "addr":        mejor["addr"],
                    "score":       3,
                    "score_final": mejor.get("score_final", 0),
                    "win_rate":    mejor["win_rate"],
                    "buys":        mejor["buys"],
                    "name":        mejor["name"],
                    "en_ambas":    mejor.get("en_ambas", False),
                },
                "estado": "pendiente",
                "fuente": "scout"
            }
            Path("/root/granja-v2/proposals.json").write_text(json.dumps(proposal, indent=2))
        return  # Claudio decide

    # ── Modo interactivo ──────────────────────────────────
    print()
    opciones = [str(i) for i in range(1, min(len(candidatos), 10) + 1)]
    sel = input(f"¿Qué candidato aplicar? (1-{len(opciones)}, Enter=salir): ").strip()
    if not sel or sel not in opciones:
        print("↩️  Sin cambios aplicados.")
        return

    elegido = candidatos[int(sel) - 1]

    workers_dir  = Path("/root/granja-v2/workers")
    worker_dirs  = sorted([w for w in workers_dir.iterdir() if w.is_dir()]) if workers_dir.exists() else []
    if not worker_dirs:
        print("❌ No se encontraron workers.")
        return

    worker_dir = worker_dirs[0]
    if len(worker_dirs) > 1:
        print("\nWorkers disponibles:")
        for i, w in enumerate(worker_dirs, 1):
            print(f"  {i}. {w.name}")
        sw = input(f"¿Qué worker? (Enter=1): ").strip() or "1"
        worker_dir = worker_dirs[int(sw) - 1]

    print(f"\n{'='*60}")
    print(f"✅ CANDIDATO SELECCIONADO")
    print(f"{'='*60}")
    print(f"   Wallet  : {elegido['addr']}")
    print(f"   Nombre  : {elegido['name']}")
    print(f"   WR      : {elegido['win_rate']*100:.0f}% | Consistencia: {elegido['consistencia']}/100")
    print(f"   En ambas fuentes: {'Sí ✨' if elegido.get('en_ambas') else 'No'}")
    print(f"   Mercado : {elegido['slug']}")
    print(f"   Horas   : {elegido['horas']:.1f}h restantes")
    print(f"   Worker  : {worker_dir.name}")

    confirma = input("\n¿Aplicar y arrancar? (s/n, Enter=s): ").strip().lower() or "s"
    if confirma != "s":
        print("↩️  Cancelado.")
        return

    cfg_path = worker_dir / "config.json"
    cfg      = json.loads(cfg_path.read_text())
    cfg["target_wallet"] = elegido["addr"]
    cfg["market"]        = elegido["slug"]
    cfg_path.write_text(json.dumps(cfg, indent=2))

    proposal = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mercado": {
            "slug":  elegido["slug"],
            "vol":   elegido["vol_market"],
            "hours": elegido["horas"],
            "token": elegido.get("token_id", ""),
            "neg":   elegido.get("neg_risk", False),
            "score": 3,
        },
        "wallet": {
            "addr":     elegido["addr"],
            "score":    3,
            "win_rate": elegido["win_rate"],
            "buys":     elegido["buys"],
            "en_ambas": elegido.get("en_ambas", False),
        },
        "estado": "aprobado",
        "fuente": "scout"
    }
    Path("/root/granja-v2/proposals.json").write_text(json.dumps(proposal, indent=2))
    print(f"\n✅ Config actualizado para [{worker_dir.name}]")

    import subprocess, os
    pidfile = Path("/root/granja-v2/claudio.pid")
    try:
        pid = int(pidfile.read_text().strip())
        claudio_vivo = os.path.exists(f"/proc/{pid}")
    except:
        claudio_vivo = False

    if claudio_vivo:
        subprocess.Popen(["/root/granja-v2/farm-restart.sh"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        print("🔄 farm-restart.sh ejecutado")
    else:
        subprocess.Popen(["/root/granja-v2/farm-start.sh"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        print("🚀 farm-start.sh ejecutado")

if __name__ == "__main__":
    main()
