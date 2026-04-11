"""
selector.py — Pipeline de selección de wallets v2
Enfoque: market-first + scoring propio desde Data API

Pipeline:
  1. Gamma API → mercados activos válidos (4h-168h, vol>$50k)
  2. Data API /trades → wallets activas en esos mercados (últimas 24h)
  3. Filtrar bots (buys_hoy > MAX_BUYS_HOY)
  4. Para cada candidata → historial /activity → calcular score propio
  5. Score: WR aproximado + consistencia + diversidad + recencia
  6. Top 3 → proposals.json → Gerencia aprueba vía Telegram

Sin dependencia de polymarketanalytics. Sin CLI. Solo Data API.
"""

import json, time, threading, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

# ── APIs ──────────────────────────────────────────────────
DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

PROPOSALS_PATH = Path("/root/granja-v2/proposals.json")

# ── Filtros de mercado ────────────────────────────────────
MIN_HORAS       = 4      # mercado debe tener al menos 4h restantes
MAX_HORAS       = 168    # máximo 7 días
MIN_MARKET_VOL  = 50000  # volumen 24h mínimo
MAX_SPREAD      = 0.15   # spread máximo
MID_MIN         = 0.10   # mercado no resuelto
MID_MAX         = 0.90

# ── Filtros de wallets ────────────────────────────────────
MAX_BUYS_HOY    = 5      # más de esto = probablemente bot
MIN_HIST_TRADES = 50     # mínimo trades en historial para calificar
MIN_WR_APROX    = 0.50   # WR aproximado mínimo
VENTANA_TRADES  = 24     # horas para buscar wallets activas en mercado

# ── Blacklist de slugs ────────────────────────────────────
SLUG_BLACKLIST = ["updown", "crypto-5m", "crypto-15m"]

# ─────────────────────────────────────────────────────────
#  UTILIDADES
# ─────────────────────────────────────────────────────────

def stars(n: int) -> str:
    return "⭐" * n + "·" * (4 - n)

def slug_es_valido(slug: str) -> bool:
    if not slug:
        return False
    s = slug.lower()
    return not any(kw in s for kw in SLUG_BLACKLIST)

def score_composite(wr: float, edge: float, recency: float, diversidad: float) -> float:
    """0.4×WR + 0.3×edge + 0.2×recency + 0.1×diversidad"""
    return round(0.4 * wr + 0.3 * edge + 0.2 * recency + 0.1 * diversidad, 4)

# ─────────────────────────────────────────────────────────
#  FASE 1 — MERCADOS ACTIVOS VÁLIDOS
# ─────────────────────────────────────────────────────────

def buscar_mercados_activos() -> list[dict]:
    """Gamma API → mercados activos con 4h-168h y vol>$50k."""
    print("📡 [1] Gamma API — mercados activos válidos...", end="", flush=True)
    try:
        r = requests.get(
            f"{GAMMA_API}/markets",
            params={"closed": "false", "active": "true",
                    "order": "volume24hr", "ascending": "false", "limit": 150},
            timeout=10
        )
        if not r.ok:
            print(f" ERROR {r.status_code}")
            return []
        markets = r.json()
    except Exception as e:
        print(f" ERROR: {e}")
        return []

    validos = []
    now_ts = time.time()
    for m in markets:
        slug = m.get("slug", "")
        if not slug_es_valido(slug):
            continue
        vol = float(m.get("volume24hr", 0) or 0)
        if vol < MIN_MARKET_VOL:
            continue
        end_str = m.get("endDate", "") or m.get("endDateIso", "")
        if not end_str:
            continue
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            horas = (end_dt.timestamp() - now_ts) / 3600
        except:
            continue
        if horas < MIN_HORAS or horas > MAX_HORAS:
            continue
        clob_raw = m.get("clobTokenIds", "[]")
        try:
            clob_ids = json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw
            token_id = str(clob_ids[0]) if clob_ids else ""
        except:
            token_id = ""
        if not token_id:
            continue
        validos.append({
            "slug":          slug,
            "vol":           vol,
            "horas":         horas,
            "token_id":      token_id,
            "all_token_ids": [str(c) for c in clob_ids[:2]],
            "neg_risk":      bool(m.get("negRisk", False)),
            "tick_size":     str(m.get("minimumTickSize", "0.01") or "0.01"),
            "cond_id":       m.get("conditionId", ""),
        })

    print(f" {len(validos)} mercados")
    return validos

# ─────────────────────────────────────────────────────────
#  FASE 2 — WALLETS ACTIVAS EN ESOS MERCADOS
# ─────────────────────────────────────────────────────────

def get_wallets_activas(mercados: list[dict]) -> dict:
    """
    Para cada mercado válido, obtiene wallets que compraron
    en las últimas VENTANA_TRADES horas.
    Retorna dict: addr → {buys_hoy, mercados_set, last_ts, slugs}
    Filtra bots (buys_hoy > MAX_BUYS_HOY en un solo mercado).
    """
    print(f"📡 [2] Data API — wallets activas en {len(mercados)} mercados...", end="", flush=True)
    cutoff = time.time() - VENTANA_TRADES * 3600
    pool = {}  # addr → datos agregados
    bots_filtrados = 0

    for mkt in mercados:
        if not mkt["cond_id"]:
            continue
        try:
            r = requests.get(
                f"{DATA_API}/trades",
                params={"conditionId": mkt["cond_id"], "limit": 500},
                timeout=10
            )
            if not r.ok:
                continue
            trades = r.json()
            if not isinstance(trades, list):
                continue

            # Contar buys por wallet en este mercado
            buys_en_este_mkt = defaultdict(int)
            for t in trades:
                if t.get("timestamp", 0) < cutoff:
                    continue
                if t.get("side", "").upper() != "BUY":
                    continue
                addr = (t.get("proxyWallet") or "").lower()
                if not addr:
                    continue
                buys_en_este_mkt[addr] += 1

            # Agregar al pool, filtrar bots
            for addr, buys in buys_en_este_mkt.items():
                if buys > MAX_BUYS_HOY:
                    bots_filtrados += 1
                    continue
                if addr not in pool:
                    pool[addr] = {
                        "buys_total": 0,
                        "mercados":   [],  # lista de dicts con info completa del mercado
                        "last_ts":    0,
                    }
                pool[addr]["buys_total"] += buys
                pool[addr]["mercados"].append(mkt)  # guardar dict completo
                # Actualizar last_ts
                for t in trades:
                    a = (t.get("proxyWallet") or "").lower()
                    if a == addr:
                        pool[addr]["last_ts"] = max(pool[addr]["last_ts"], t.get("timestamp", 0))
        except:
            continue

    print(f" {len(pool)} wallets candidatas ({bots_filtrados} bots filtrados)")
    return pool

# ─────────────────────────────────────────────────────────
#  FASE 3 — SCORE PROPIO DESDE HISTORIAL
# ─────────────────────────────────────────────────────────

def calcular_score_wallet(addr: str, datos_pool: dict) -> dict | None:
    """
    Obtiene historial de la wallet y calcula score propio.
    Retorna dict con score y métricas, o None si no califica.
    """
    try:
        r = requests.get(
            f"{DATA_API}/activity",
            params={"user": addr, "limit": 100},
            timeout=8
        )
        if not r.ok:
            return None
        acts = r.json()
        if not isinstance(acts, list) or len(acts) < MIN_HIST_TRADES:
            return None

        # Filtrar bots de alta frecuencia del historial
        updown_ratio = sum(1 for a in acts if "updown" in a.get("slug", "").lower()) / len(acts)
        if updown_ratio > 0.3:
            return None

        # Filtrar whales — size promedio > $50 por BUY indica capital incopiable
        buys_all = [a for a in acts if a.get("side", "").upper() == "BUY"]
        sizes = [float(a.get("usdcSize", 0) or 0) for a in buys_all if float(a.get("usdcSize", 0) or 0) > 0]
        if sizes:
            avg_size = sum(sizes) / len(sizes)
            if avg_size > 50:
                return None  # whale — posiciones grandes mueven el mercado antes de que copiemos

        # WR aproximado: para cada BUY, verificar si el midpoint actual > precio entrada
        # Solo en los últimos 20 trades para no hacer demasiadas llamadas
        buys = [a for a in acts if a.get("side", "").upper() == "BUY"][:20]
        total_check = ganando = 0
        seen_tokens = set()
        for b in buys:
            token = str(b.get("asset", ""))
            if not token or token in seen_tokens:
                continue
            seen_tokens.add(token)
            p_entrada = float(b.get("price", 0) or 0)
            if p_entrada <= 0 or p_entrada >= 1:
                continue
            try:
                rm = requests.get(f"{CLOB_API}/midpoint",
                    params={"token_id": token}, timeout=3)
                if rm.ok:
                    mid = float(rm.json().get("mid", 0))
                    if mid > 0:
                        total_check += 1
                        if mid > p_entrada:
                            ganando += 1
            except:
                pass

        wr_aprox = (ganando / total_check) if total_check >= 3 else 0.5
        if total_check < 3:
            # Sin datos suficientes para WR — usar 0.5 neutro pero no rechazar
            wr_aprox = 0.5

        if wr_aprox < MIN_WR_APROX:
            return None

        # Edge: ratio ganancia/pérdida aproximado por precio entrada
        # Proxy: promedio de (mid - precio_entrada) para BUYs en ganancia
        edges = []
        for b in buys[:10]:
            token = str(b.get("asset", ""))
            p = float(b.get("price", 0) or 0)
            if p <= 0 or p >= 1 or token not in seen_tokens:
                continue
            try:
                rm = requests.get(f"{CLOB_API}/midpoint",
                    params={"token_id": token}, timeout=3)
                if rm.ok:
                    mid = float(rm.json().get("mid", 0))
                    if mid > 0:
                        edges.append((mid - p) / p)
            except:
                pass
        edge = max(0, sum(edges) / len(edges)) if edges else 0.3

        # Diversidad: mercados distintos en historial (no concentrado en uno solo)
        slugs_hist = set(a.get("slug", "") for a in acts if a.get("slug"))
        diversidad = min(len(slugs_hist) / 10, 1.0)  # normalizado: 10+ mercados = 1.0

        # Recency: qué tan reciente fue su último trade
        hace_min = int((time.time() - datos_pool["last_ts"]) / 60) if datos_pool["last_ts"] else 9999
        recency = max(0, 1 - hace_min / 1440)  # decae a 0 en 24h

        score = score_composite(wr_aprox, min(edge + 0.5, 1.0), recency, diversidad)

        # Estrellas basadas en score y señales
        if score >= 0.65 and len(datos_pool["mercados"]) >= 2:
            estrellas = 4
        elif score >= 0.60:
            estrellas = 3
        elif score >= 0.55:
            estrellas = 2
        else:
            estrellas = 1

        return {
            "addr":       addr,
            "score":      score,
            "estrellas":  estrellas,
            "wr_aprox":   round(wr_aprox, 3),
            "edge":       round(edge, 3),
            "recency":    round(recency, 3),
            "diversidad": round(diversidad, 3),
            "hace_min":   hace_min,
            "buys_hoy":   datos_pool["buys_total"],
            "mercados_hoy": len(datos_pool["mercados"]),
            "hist_trades":  len(acts),
            "hist_slugs":   len(slugs_hist),
            "slugs_activos": [m["slug"] for m in datos_pool["mercados"]],
            "wr_checks":    total_check,
        }
    except Exception as e:
        return None

# ─────────────────────────────────────────────────────────
#  FASE 4 — SPREAD Y MIDPOINT DEL MERCADO
# ─────────────────────────────────────────────────────────

def get_spread_midpoint(token_id: str, all_token_ids: list = None) -> tuple:
    """
    Calcula spread y midpoint reales.
    Para mercados binarios, usa el token cuyo mid esté más cercano a 0.5.
    El spread real es best_ask - best_bid de ese token.
    """
    tokens = all_token_ids if all_token_ids else [token_id]
    best_token = token_id
    best_mid = None

    # Encontrar el token más cercano a 0.5
    for tid in tokens[:2]:
        try:
            rm = requests.get(f"{CLOB_API}/midpoint", params={"token_id": str(tid)}, timeout=3)
            if rm.ok:
                mid = float(rm.json().get("mid", 0))
                if mid > 0 and (best_mid is None or abs(mid - 0.5) < abs(best_mid - 0.5)):
                    best_mid = mid
                    best_token = str(tid)
        except:
            pass

    if best_mid is None:
        return None, None

    # Spread del token elegido
    try:
        rb = requests.get(f"{CLOB_API}/book", params={"token_id": best_token}, timeout=5)
        spread = None
        if rb.ok:
            book = rb.json()
            asks = book.get("asks", [])
            bids = book.get("bids", [])
            if asks and bids:
                best_ask = float(asks[0]["price"])
                best_bid = float(bids[0]["price"])
                # Spread real: solo válido si ask > bid (mercado líquido)
                if best_ask > best_bid and best_ask < 1.0 and best_bid > 0:
                    spread = round(best_ask - best_bid, 4)
        return spread, best_mid
    except:
        return None, best_mid

# ─────────────────────────────────────────────────────────
#  PIPELINE PRINCIPAL
# ─────────────────────────────────────────────────────────

def run(modo_auto: bool = False) -> list[dict]:
    print("=" * 65)
    print("🎯 SELECTOR v2 — Pipeline market-first + score propio")
    print(f"   Mercados: {MIN_HORAS}h–{MAX_HORAS}h | Vol≥${MIN_MARKET_VOL:,} | Spread≤{MAX_SPREAD}")
    print(f"   Wallets: max {MAX_BUYS_HOY} buys/día | WR≥{MIN_WR_APROX*100:.0f}% | hist≥{MIN_HIST_TRADES}")
    print(f"   Score: 0.4×WR + 0.3×edge + 0.2×recency + 0.1×diversidad")
    print("=" * 65)
    print()

    # Fase 1: mercados
    mercados = buscar_mercados_activos()
    if not mercados:
        print("❌ Sin mercados válidos ahora.")
        return []

    print(f"\n   Mercados encontrados:")
    for m in mercados:
        print(f"   {m['horas']:>5.1f}h  ${m['vol']:>10,.0f}  {m['slug'][:55]}")

    # Fase 2: wallets activas
    print()
    pool = get_wallets_activas(mercados)
    if not pool:
        print("❌ Sin wallets activas en esos mercados.")
        return []

    # Fase 3: score propio en paralelo
    print(f"\n📊 [3] Calculando score de {len(pool)} wallets candidatas...")
    candidatos_raw = []
    lock = threading.Lock()
    completados = [0]
    total = len(pool)

    def _score(item):
        addr, datos = item
        resultado = calcular_score_wallet(addr, datos)
        with lock:
            completados[0] += 1
            print(f"   [{completados[0]}/{total}] procesando... {len(candidatos_raw)} calificadas    ",
                  end="\r", flush=True)
            if resultado:
                candidatos_raw.append(resultado)

    with ThreadPoolExecutor(max_workers=20) as executor:
        list(executor.map(_score, pool.items()))

    print(f"\n\n✅ {len(candidatos_raw)} wallets calificadas\n")

    if not candidatos_raw:
        print("❌ Ninguna wallet pasó los filtros de score.")
        print(f"   — Wallets en pool: {len(pool)}")
        print(f"   — WR mínimo requerido: {MIN_WR_APROX*100:.0f}%")
        print(f"   — Historial mínimo: {MIN_HIST_TRADES} trades")
        return []

    # Ordenar por estrellas desc, score desc
    candidatos_raw.sort(key=lambda x: (-x["estrellas"], -x["score"]))
    top = candidatos_raw[:10]

    # Enriquecer con info del mejor mercado — viene directo del pool (no necesita match por slug)
    candidatos_finales = []
    for c in top:
        addr = c["addr"]
        wallet_pool_data = pool.get(addr, {})
        mercados_wallet = wallet_pool_data.get("mercados", [])
        if not mercados_wallet:
            continue
        # Elegir el mercado de mayor volumen donde opera esta wallet
        mkt = max(mercados_wallet, key=lambda m: m["vol"])

        spread, midpoint = get_spread_midpoint(mkt["token_id"], mkt.get("all_token_ids"))

        # Filtrar solo mercados casi resueltos (midpoint fuera de rango)
        if midpoint is not None and (midpoint < MID_MIN or midpoint > MID_MAX):
            # Intentar con otro mercado de la wallet
            encontrado = False
            for m2 in sorted(mercados_wallet, key=lambda m: -m["vol"]):
                if m2["slug"] == mkt["slug"]:
                    continue
                s2, mid2 = get_spread_midpoint(m2["token_id"], m2.get("all_token_ids"))
                if mid2 is not None and MID_MIN <= mid2 <= MID_MAX:
                    spread, midpoint, mkt = s2, mid2, m2
                    encontrado = True
                    break
            if not encontrado:
                continue  # todos los mercados de esta wallet están casi resueltos

        # Spread: en Polymarket el orderbook tiene órdenes límite lejos del mid
        # No usamos spread como filtro — el midpoint es la métrica correcta
        candidatos_finales.append({**c,
            "slug":      mkt["slug"],
            "horas":     mkt["horas"],
            "vol_market":mkt["vol"],
            "token_id":  mkt["token_id"],
            "neg_risk":  mkt["neg_risk"],
            "tick_size": mkt["tick_size"],
            "spread":    spread,
            "midpoint":  midpoint,
        })

    if not candidatos_finales:
        print("❌ Sin candidatos después de verificar liquidez de mercados.")
        return []

    # ── Diversificar top: máx 3 wallets por mercado ──
    # Garantiza variedad aunque un mercado domine por volumen
    MAX_POR_MERCADO = 3
    conteo_mercado = {}
    top_diverso = []
    for c in candidatos_finales:  # ya vienen ordenados por estrellas+score
        slug = c["slug"]
        if conteo_mercado.get(slug, 0) < MAX_POR_MERCADO:
            conteo_mercado[slug] = conteo_mercado.get(slug, 0) + 1
            top_diverso.append(c)
        if len(top_diverso) >= 20:
            break
    candidatos_finales = top_diverso

    # Contar mercados representados
    mercados_rep = len(set(c["slug"] for c in candidatos_finales))
    print(f"   (top diversificado: {len(candidatos_finales)} candidatos en {mercados_rep} mercados distintos)\n")

    # ── Imprimir tabla ──
    print(f"{'#':<3} {'STARS':<6} {'SCORE':>6} {'WR':>5} {'EDGE':>5} {'REC':>5} {'BUYs':>4} {'Hist':>5} {'Hace':>5} {'Mid':>5} {'Horas':>6}")
    print("─" * 90)
    for i, c in enumerate(candidatos_finales, 1):
        mid_str = f"{c['midpoint']:.2f}" if c.get("midpoint") is not None else "   ?"
        spr_str = f"{c['spread']:.3f}" if c.get("spread") is not None else "   ?"
        print(
            f"{i:<3} {stars(c['estrellas']):<6} {c['score']:>6.3f} "
            f"{c['wr_aprox']*100:>4.0f}% {c['edge']:>5.2f} {c['recency']:>5.2f} "
            f"{c['buys_hoy']:>4} {c['hist_trades']:>5} {c['hace_min']:>4}min "
            f"{mid_str:>5} {c['horas']:>5.1f}h"
        )
        print(f"    📌 {c['slug'][:65]}")
        print(f"    👛 {c['addr'][:42]}")
        print(f"    📊 Spread: {spr_str} | Vol: ${c['vol_market']:,.0f} | "
              f"Diversidad: {c['hist_slugs']} mercados hist | "
              f"WR checks: {c['wr_checks']}")
        print()

    # ── Guardar resultados ──
    Path("/root/granja-v2/selector_results.json").write_text(
        json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                    "candidatos": candidatos_finales}, indent=2)
    )
    print("💾 Resultados en selector_results.json")

    # ── Modo auto: proposals.json con top 3 ──
    if modo_auto:
        top3 = candidatos_finales[:3]
        proposals = []
        for c in top3:
            proposals.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "mercado": {
                    "slug":     c["slug"],
                    "vol":      c["vol_market"],
                    "hours":    c["horas"],
                    "token":    c["token_id"],
                    "neg":      c["neg_risk"],
                    "tick":     c["tick_size"],
                    "spread":   c["spread"],
                    "midpoint": c["midpoint"],
                },
                "wallet": {
                    "addr":      c["addr"],
                    "name":      c["addr"][:16] + "...",
                    "estrellas": c["estrellas"],
                    "score":     c["score"],
                    "win_rate":  c["wr_aprox"],
                    "edge":      c["edge"],
                    "pnl":       0,
                    "pnl_7d":   None,
                    "tags":      "",
                },
                "estado":  "pendiente",
                "fuente":  "selector",
            })
        PROPOSALS_PATH.write_text(json.dumps({
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "candidatos": proposals,
            "fuente":     "selector",
        }, indent=2))
        print(f"📋 Top {len(top3)} candidatos en proposals.json")
        return top3

    return candidatos_finales

# ─────────────────────────────────────────────────────────
#  MODO INTERACTIVO
# ─────────────────────────────────────────────────────────

def main():
    import sys
    modo_auto = "--auto" in sys.argv
    candidatos = run(modo_auto=modo_auto)

    if modo_auto or not candidatos:
        return

    opciones = [str(i) for i in range(1, min(len(candidatos), 10) + 1)]
    print()
    sel = input(f"¿Qué candidato aplicar? (1-{len(opciones)}, Enter=salir): ").strip()
    if not sel or sel not in opciones:
        print("↩️  Sin cambios aplicados.")
        return

    elegido = candidatos[int(sel) - 1]
    workers_dir = Path("/root/granja-v2/workers")
    worker_dirs = sorted([w for w in workers_dir.iterdir() if w.is_dir()]) if workers_dir.exists() else []
    if not worker_dirs:
        print("❌ No se encontraron workers.")
        return

    worker_dir = worker_dirs[0]
    if len(worker_dirs) > 1:
        print("\nWorkers disponibles:")
        for i, w in enumerate(worker_dirs, 1):
            print(f"  {i}. {w.name}")
        sw = input("¿Qué worker? (Enter=1): ").strip() or "1"
        worker_dir = worker_dirs[int(sw) - 1]

    print(f"\n{'='*60}")
    print(f"✅ CANDIDATO SELECCIONADO")
    print(f"   Wallet    : {elegido['addr']}")
    print(f"   Estrellas : {stars(elegido['estrellas'])} ({elegido['estrellas']}/4)")
    print(f"   Score     : {elegido['score']:.3f} | WR: {elegido['wr_aprox']*100:.0f}% | Edge: {elegido['edge']:.2f}")
    print(f"   Mercado   : {elegido['slug']}")
    print(f"   Horas     : {elegido['horas']:.1f}h restantes")
    print(f"   Worker    : {worker_dir.name}")

    confirma = input("\n¿Aplicar y arrancar? (s/n, Enter=s): ").strip().lower() or "s"
    if confirma != "s":
        print("↩️  Cancelado.")
        return

    cfg_path = worker_dir / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg.pop("target_wallet", None)
    cfg["target_wallets"] = [elegido["addr"]]
    cfg["market"] = elegido["slug"]
    cfg_path.write_text(json.dumps(cfg, indent=2))

    PROPOSALS_PATH.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "candidatos": [{
            "mercado": {"slug": elegido["slug"], "vol": elegido["vol_market"],
                        "hours": elegido["horas"], "token": elegido["token_id"],
                        "neg": elegido["neg_risk"], "tick": elegido["tick_size"]},
            "wallet":  {"addr": elegido["addr"], "estrellas": elegido["estrellas"],
                        "score": elegido["score"], "win_rate": elegido["wr_aprox"],
                        "edge": elegido["edge"], "name": elegido["addr"][:16]+"...",
                        "pnl": 0, "pnl_7d": None, "tags": ""},
            "estado": "aprobado",
        }],
        "fuente": "selector",
    }, indent=2))

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

