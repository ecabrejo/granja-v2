"""
hunter.py v3 — Análisis multi-fuente de mercados y wallets
Fuentes: Gamma + CLOB + DataAPI + CLI Leaderboard + PolymarketAnalytics
Responsabilidad: proponer, no decidir. Guarda en proposals.json.
"""
import json, time, subprocess, requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

GAMMA_API    = "https://gamma-api.polymarket.com"
CLOB_API     = "https://clob.polymarket.com"
DATA_API     = "https://data-api.polymarket.com"
ANALYTICS    = "https://polymarketanalytics.com/api"
VENV_PYTHON  = "/root/granja-v2/venv/bin/python3"
PROPOSALS    = Path("/root/granja-v2/proposals.json")

# ── Helpers ───────────────────────────────────────────────
def score_bar(score, max_score=4):
    return "[" + "⭐" * (score or 0) + "·" * (max_score - (score or 0)) + f"] {score}/{max_score}"

def ask(prompt, default="1", valid=None):
    while True:
        print(f"{prompt} (Enter={default}): ", end="", flush=True)
        raw = input().strip() or default
        if valid is None or raw in valid:
            return raw
        print(f"  Opciones válidas: {', '.join(valid)}")

# ══════════════════════════════════════════════════════════
#  FASE 0 — CARGA DE FUENTES EXTERNAS (una vez al inicio)
# ══════════════════════════════════════════════════════════

def cargar_leaderboard_cli():
    """Polymarket CLI: top 200 wallets por PnL oficial."""
    print("  [B] Cargando leaderboard oficial (CLI)...", end="", flush=True)
    try:
        env = __import__("os").environ.copy()
        env["PATH"] = "/root/granja-v2/venv/bin:" + env.get("PATH", "")
        result = subprocess.run(
            ["polymarket", "data", "leaderboard", "--limit", "200", "-o", "json"],
            capture_output=True, text=True, timeout=30,
            cwd="/root/granja-v2", env=env
        )
        data = json.loads(result.stdout)
        lb = {}
        for entry in data:
            if not isinstance(entry, dict): continue
            w = entry.get("proxy_wallet", "").lower()
            if w:
                lb[w] = {
                    "pnl":    float(entry.get("pnl", 0)),
                    "volume": float(entry.get("volume", 0)),
                    "rank":   int(entry.get("rank", 9999)),
                    "name":   entry.get("user_name", "?"),
                }
        print(f" {len(lb)} wallets")
        return lb
    except Exception as e:
        print(f" ERROR: {e}")
        return {}

def cargar_analytics():
    """PolymarketAnalytics: top 500 con win rate real y tags de calidad."""
    print("  [D] Cargando polymarketanalytics.com...", end="", flush=True)
    try:
        r = requests.get(
            f"{ANALYTICS}/traders-tag-performance",
            params={"tag": "Overall", "sortBy": "pnl", "order": "desc", "limit": 500},
            timeout=15
        )
        data = r.json().get("data", [])
        analytics = {}
        for entry in data:
            if not isinstance(entry, dict): continue
            w = entry.get("trader", "").lower()
            if w:
                analytics[w] = {
                    "pnl":       float(entry.get("overall_gain", 0)),
                    "win_rate":  float(entry.get("win_rate", 0)),
                    "positions": int(entry.get("total_positions", 0)),
                    "rank":      int(entry.get("rank", 9999)),
                    "name":      entry.get("trader_name", "?"),
                    "tags":      entry.get("trader_tags", ""),
                }
        print(f" {len(analytics)} wallets")
        return analytics
    except Exception as e:
        print(f" ERROR: {e}")
        return {}

# ══════════════════════════════════════════════════════════
#  FASE 1 — MERCADOS
# ══════════════════════════════════════════════════════════

def buscar_mercados():
    print("\n🔍 FASE 1 — Analizando mercados en 3 fuentes\n")
    now = datetime.now(timezone.utc)
    min_close = now + timedelta(hours=2)
    max_close = now + timedelta(hours=72)

    # Fuente A: Gamma
    print("  [A] Consultando Gamma API...", end="", flush=True)
    try:
        r = requests.get(f"{GAMMA_API}/markets",
            params={"closed": "false", "active": "true",
                    "order": "volume24hr", "ascending": "false", "limit": 100},
            timeout=10)
        markets = r.json()
    except Exception as e:
        print(f" ERROR: {e}")
        return []

    candidatos = []
    for m in markets:
        slug = m.get("slug", "")
        if "updown" in slug: continue
        # Filtrar sports/entertainment — misma lógica que scout
        BLOCKED = ["nba","nfl","mlb","nhl","lol","cs2","ufc","nascar",
                   "epl","laliga","bundesliga","serie-a","champions","uef",
                   "entertainment","celebrity","oscar","grammy","emmy"]
        if any(b in slug.lower() for b in BLOCKED): continue
        vol = float(m.get("volume24hr", 0))
        if vol < 100000: continue
        end_str = m.get("endDate") or m.get("endDateIso", "")
        if not end_str: continue
        try:
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            if end.tzinfo is None: end = end.replace(tzinfo=timezone.utc)
            hours = (end - now).total_seconds() / 3600
            if not (min_close.timestamp() <= end.timestamp() <= max_close.timestamp()): continue
        except: continue
        clob_raw = m.get("clobTokenIds", "[]")
        try:
            clob_ids = json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw
        except: continue
        if not clob_ids: continue
        candidatos.append({
            "slug":  slug,
            "vol":   vol,
            "hours": hours,
            "cond":  m.get("conditionId", ""),
            "token": str(clob_ids[0]),
            "neg":   m.get("negRisk", False),
            "score": 1,
            "fuentes": "A",
        })
    print(f" {len(candidatos)} candidatos")

    # Fuente B: CLOB midpoint
    print("  [B] Verificando orderbook en CLOB...", end="", flush=True)
    validos_clob = 0
    for m in candidatos:
        try:
            r = requests.get(f"{CLOB_API}/midpoint",
                params={"token_id": m["token"]}, timeout=5)
            if r.ok:
                m["score"] += 1
                m["fuentes"] += "B"
                validos_clob += 1
            else:
                m["fuentes"] += "·"
        except:
            m["fuentes"] += "·"
    print(f" {validos_clob} activos")

    # Fuente C: trades recientes en Data API (últimas 4h)
    print("  [C] Verificando actividad reciente...", end="", flush=True)
    now_ts = time.time()
    activos_data = 0
    for m in candidatos:
        if not m.get("cond"): 
            m["fuentes"] += "·"
            continue
        try:
            r = requests.get(f"{DATA_API}/trades",
                params={"conditionId": m["cond"], "limit": 20}, timeout=8)
            trades = r.json()
            if not isinstance(trades, list):
                m["fuentes"] += "·"
                continue
            reciente = any(
                t.get("timestamp", 0) > now_ts - 14400 and
                t.get("side", "").upper() == "BUY" and
                "updown" not in t.get("slug", "")
                for t in trades
            )
            if reciente:
                m["score"] += 1
                m["fuentes"] += "C"
                activos_data += 1
            else:
                m["fuentes"] += "·"
        except:
            m["fuentes"] += "·"
    print(f" {activos_data} con actividad reciente")

    candidatos.sort(key=lambda x: (-x["score"], x["hours"], -x["vol"]))
    top = candidatos[:5]

    print(f"\n{'#':<3} {'SCORE':<12} {'VOL 24H':>12} {'HORAS':>6}  MERCADO")
    print("─" * 72)
    for i, m in enumerate(top, 1):
        print(f"{i:<3} {score_bar(m['score'], 3):<14} ${m['vol']:>11,.0f} {m['hours']:>5.1f}h  {m['slug'][:45]}")
        print(f"    Fuentes: {m['fuentes']} | cond: {m['cond'][:20]}...")

    return top

# ══════════════════════════════════════════════════════════
#  FASE 2 — WALLETS
# ══════════════════════════════════════════════════════════

def buscar_wallets(mercado, leaderboard_B, analytics_D):
    print(f"\n🔍 FASE 2 — Analizando wallets en 4 fuentes\n")
    print(f"  Mercado: {mercado['slug']}\n")

    now_ts = time.time()

    # Fuente A: wallets activas en el mercado — ventana dinámica
    # Primero 6h, si hay menos de 3 wallets ampliar a 24h, luego 7 días
    def buscar_wallets_ventana(trades, ventana_h, min_buys=2):
        w = defaultdict(lambda: {"buys": 0, "last_ts": 0, "size_total": 0.0})
        cutoff = now_ts - ventana_h * 3600
        for t in trades:
            ts   = t.get("timestamp", 0)
            side = t.get("side", "").upper()
            slug = t.get("slug", "")
            if side != "BUY" or "updown" in slug: continue
            if ts < cutoff: continue
            addr = t.get("proxyWallet", "")
            if not addr: continue
            w[addr]["buys"] += 1
            w[addr]["last_ts"] = max(w[addr]["last_ts"], ts)
            w[addr]["size_total"] += float(t.get("size", 0) or 0)
        return {a: d for a, d in w.items() if d["buys"] >= min_buys}

    ventana_usada = 6
    print(f"  [A] Buscando wallets posicionadas (últimas 6h)...", end="", flush=True)
    wallets_A = {}
    try:
        r = requests.get(f"{DATA_API}/trades",
            params={"conditionId": mercado["cond"], "limit": 500}, timeout=15)
        trades_raw = r.json() if r.ok else []
        if isinstance(trades_raw, list):
            wallets_A = buscar_wallets_ventana(trades_raw, 6)
            if len(wallets_A) < 3:
                print(f" solo {len(wallets_A)} — ampliando a 24h...", end="", flush=True)
                wallets_A = buscar_wallets_ventana(trades_raw, 24)
                ventana_usada = 24
            if len(wallets_A) < 3:
                print(f" solo {len(wallets_A)} — ampliando a 7 días...", end="", flush=True)
                wallets_A = buscar_wallets_ventana(trades_raw, 168)
                ventana_usada = 168
    except Exception as e:
        print(f" ERROR: {e}")
    print(f" {len(wallets_A)} wallets con 2+ BUYs (ventana: {ventana_usada}h)")

    if not wallets_A:
        print("\n❌ Sin candidatos en este mercado.")
        return []

    # Fuente C: historial limpio + win rate aproximado
    print("  [C] Verificando historial limpio + win rate...", end="", flush=True)
    limpias_C  = set()
    win_rates_C = {}
    for addr in list(wallets_A.keys())[:30]:
        try:
            r = requests.get(f"{DATA_API}/activity",
                params={"user": addr, "limit": 50}, timeout=8)
            trades = r.json()
            if not isinstance(trades, list): continue
            activos = [t for t in trades if t.get("side", "").upper() in ("BUY", "SELL")]
            if not activos: continue
            updown_ratio = sum(1 for t in activos if "updown" in t.get("slug", "")) / len(activos)
            if updown_ratio >= 0.3: continue
            limpias_C.add(addr.lower())
            # Win rate aproximado: precio entrada vs midpoint actual
            ganando = total = 0
            for t in activos[:15]:
                if t.get("side", "").upper() != "BUY": continue
                token = str(t.get("asset", ""))
                p_entrada = float(t.get("price", 0) or 0)
                if not token or p_entrada <= 0: continue
                try:
                    chk = requests.get(f"{CLOB_API}/midpoint",
                        params={"token_id": token}, timeout=3)
                    if chk.ok:
                        mid = float(chk.json().get("mid", 0))
                        if mid > 0:
                            total += 1
                            if mid > p_entrada: ganando += 1
                except: pass
            if total > 0:
                win_rates_C[addr.lower()] = round(ganando / total * 100, 1)
        except: pass
    print(f" {len(limpias_C)} limpias")

    # Construir candidatos con score 0-4
    candidatos = []
    for addr, data_A in wallets_A.items():
        addr_low = addr.lower()
        score    = 1  # pasó fuente A
        fuentes  = "A"

        # Fuente B: leaderboard CLI
        lb = leaderboard_B.get(addr_low, {})
        if lb and lb.get("pnl", 0) > 0:
            score += 1; fuentes += "B"
        else:
            fuentes += "·"

        # Fuente C: historial limpio
        if addr_low in limpias_C:
            score += 1; fuentes += "C"
        else:
            fuentes += "·"

        # Fuente D: polymarketanalytics
        ana = analytics_D.get(addr_low, {})
        if ana and ana.get("pnl", 0) > 0 and ana.get("win_rate", 0) >= 0.55:
            score += 1; fuentes += "D"
        else:
            fuentes += "·"

        hace = int((now_ts - data_A["last_ts"]) / 60)
        wr   = win_rates_C.get(addr_low)
        candidatos.append({
            "addr":      addr,
            "score":     score,
            "fuentes":   fuentes,
            "buys":      data_A["buys"],
            "size_total":data_A["size_total"],
            "hace":      hace,
            "pnl":       ana.get("pnl") or lb.get("pnl"),
            "win_rate":  ana.get("win_rate") or (wr / 100 if wr else None),
            "positions": ana.get("positions"),
            "rank":      ana.get("rank") or lb.get("rank"),
            "name":      ana.get("name") or lb.get("name") or "?",
            "tags":      ana.get("tags", ""),
        })

    candidatos.sort(key=lambda x: (-x["score"], -x["buys"]))
    top = candidatos[:5]

    print(f"\n{'#':<3} {'SCORE':<14} {'BUYs':>4} {'$TOTAL':>8} {'HACE':>6} {'WR':>6} {'PNL':>12}  WALLET")
    print("─" * 85)
    for i, c in enumerate(top, 1):
        pnl_str = f"${c['pnl']:>,.0f}" if c["pnl"] else "  N/A"
        wr_str  = f"{c['win_rate']*100:.0f}%" if c["win_rate"] else "  ?"
        sz_str  = f"${c['size_total']:,.0f}"
        name    = f" ({c['name']})" if c["name"] != "?" else ""
        print(f"{i:<3} {score_bar(c['score']):<16} {c['buys']:>4} {sz_str:>8} {c['hace']:>4}min {wr_str:>5} {pnl_str:>12}  {c['addr'][:42]}")
        if c["tags"]:
            print(f"    🏷  {c['tags'][:70]}")
        else:
            print(f"    Fuentes: {c['fuentes']}{name}")

    return top

# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════

def main():
    print("=" * 65)
    print("🦅 HUNTER v3 — Análisis multi-fuente (4 fuentes)")
    print("=" * 65)

    # Carga fuentes externas una sola vez
    print("\n📡 Cargando fuentes externas...\n")
    leaderboard_B = cargar_leaderboard_cli()
    analytics_D   = cargar_analytics()

    # Fase 1: Mercados
    mercados = buscar_mercados()
    if not mercados:
        print("\n❌ Sin mercados válidos.")
        return

    opciones = [str(i) for i in range(1, len(mercados) + 1)]
    sel      = int(ask(f"\n¿Qué mercado analizar?", "1", opciones)) - 1
    mercado  = mercados[sel]
    print(f"\n✅ Mercado: {mercado['slug']}")

    # Fase 2: Wallets
    wallets = buscar_wallets(mercado, leaderboard_B, analytics_D)
    if not wallets:
        print("\n❌ Sin wallets candidatas. Intenta más tarde.")
        return

    opciones_w = [str(i) for i in range(1, len(wallets) + 1)]
    sel_w      = int(ask(f"\n¿Qué wallet proponer?", "1", opciones_w)) - 1
    wallet     = wallets[sel_w]

    # Guardar propuesta
    propuesta = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mercado":   mercado,
        "wallet":    wallet,
        "estado":    "pendiente"
    }
    PROPOSALS.write_text(json.dumps(propuesta, indent=2))

    print(f"\n{'='*65}")
    print(f"✅ PROPUESTA LISTA")
    print(f"{'='*65}")
    print(f"   Mercado : {mercado['slug']}")
    print(f"   Score   : {score_bar(mercado['score'], 3)} | Vol: ${mercado['vol']:,.0f} | {mercado['hours']:.1f}h restantes")
    print(f"   Wallet  : {wallet['addr'][:42]}")
    wr_str = f"{wallet['win_rate']*100:.0f}%" if wallet.get('win_rate') else "?"
    print(f"   Score   : {score_bar(wallet['score'])} | WR: {wr_str} | BUYs: {wallet['buys']}")
    if wallet.get("tags"):
        print(f"   Tags    : {wallet['tags'][:70]}")
    print()

    # Preguntar si aplicar
    resp = ask("¿Aplicar esta configuración y arrancar el bot?", "s", ["s", "n", "S", "N"])
    if resp.lower() == "s":
        # Buscar worker_id del config
        workers_dir = Path("/root/granja-v2/workers")
        worker_dirs = sorted([w for w in workers_dir.iterdir() if w.is_dir()]) if workers_dir.exists() else []

        if not worker_dirs:
            print("❌ No se encontraron workers en /root/granja-v2/workers/")
            return

        # Si hay múltiples workers, preguntar cuál
        if len(worker_dirs) > 1:
            print("\nWorkers disponibles:")
            for i, w in enumerate(worker_dirs, 1):
                print(f"  {i}. {w.name}")
            sel_w = int(ask("¿Qué worker actualizar?", "1", [str(i) for i in range(1, len(worker_dirs)+1)])) - 1
            worker_dir = worker_dirs[sel_w]
        else:
            worker_dir = worker_dirs[0]

        # Actualizar config.json
        cfg_path = worker_dir / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["target_wallet"] = wallet["addr"]
        cfg["market"]        = mercado["slug"]
        cfg_path.write_text(json.dumps(cfg, indent=2))
        propuesta["estado"] = "aprobado"
        PROPOSALS.write_text(json.dumps(propuesta, indent=2))

        print(f"\n✅ Config actualizado para [{worker_dir.name}]")
        print(f"   target_wallet: {wallet['addr'][:42]}")
        print(f"   market: {mercado['slug']}")

        # Arrancar Claudio si no está corriendo
        import subprocess, os
        pidfile = Path("/root/granja-v2/claudio.pid")
        claudio_running = pidfile.exists() and __import__("os").path.exists(f"/proc/{pidfile.read_text().strip()}")

        if claudio_running:
            # Reiniciar solo el worker
            print(f"\n🔄 Claudio ya corre — reiniciando [{worker_dir.name}]...")
            result = subprocess.run(
                ["/root/granja-v2/venv/bin/python3", "-c",
                 f"import requests; requests.post('https://api.telegram.org/bot' + __import__('os').getenv('TELEGRAM_TOKEN','') + '/sendMessage')"],
                capture_output=True
            )
            # Enviar comando restart via Telegram interno no es viable — usar farm-restart
            subprocess.Popen(
                ["/root/granja-v2/farm-restart.sh"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True
            )
            print("✅ farm-restart.sh ejecutado")
        else:
            print(f"\n🚀 Arrancando Claudio...")
            subprocess.Popen(
                ["/root/granja-v2/farm-start.sh"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True
            )
            print("✅ farm-start.sh ejecutado")
    else:
        print("\n↩️  Propuesta rechazada. Puedes buscar de nuevo.")

import sys

def main_auto(worker_id=None):
    """Modo automatico para Claudio - sin interaccion, toma el mejor candidato."""
    print("=" * 65)
    print("HUNTER v3 - Modo AUTO")
    print("=" * 65)
    print("Cargando fuentes externas...")
    leaderboard_B = cargar_leaderboard_cli()
    analytics_D   = cargar_analytics()
    mercados = buscar_mercados()
    if not mercados:
        print("Sin mercados validos.")
        return
    mercado = None
    for m in mercados:
        if m["hours"] > 4:
            mercado = m
            break
    if not mercado:
        mercado = mercados[0]
    print("Auto-seleccionado: " + mercado["slug"])
    wallets = buscar_wallets(mercado, leaderboard_B, analytics_D)
    if not wallets:
        print("Sin wallets candidatas.")
        return
    wallet = None
    for w in wallets:
        wr = w.get("win_rate") or 0
        if w["score"] >= 2 and wr >= 0.4:
            wallet = w
            break
    if not wallet:
        wallet = wallets[0]
    from datetime import datetime, timezone
    propuesta = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mercado":   mercado,
        "wallet":    wallet,
        "estado":    "pendiente"
    }
    PROPOSALS.write_text(json.dumps(propuesta, indent=2))
    print("Propuesta guardada - mercado score=" + str(mercado["score"]) + " wallet score=" + str(wallet["score"]))

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--auto":
        main_auto(sys.argv[2] if len(sys.argv) > 2 else None)
        sys.exit(0)
    while True:
        main()
        print()
        print("-" * 65)
        try:
            input("Presiona Enter para buscar de nuevo o Ctrl+C para salir...")
            print()
        except KeyboardInterrupt:
            print()
            print("👋 Hunter cerrado.")
            break
