"""
build_pool.py — Constructor de wallet_pool.json
Lógica: track record histórico, NO actividad reciente.
Fuentes: polymarketanalytics (WR + PnL + consistencia) + leaderboard CLI (PnL 7d)
Output: /root/granja-v2/wallet_pool.json listo para pool_monitor

Uso:
  python3 build_pool.py              # interactivo, muestra top y pregunta
  python3 build_pool.py --auto       # toma todos los >=2 estrellas sin preguntar
  python3 build_pool.py --tag crypto # filtrar por categoría (politics/crypto/all)
"""
import json, sys, os, subprocess, time, requests
from pathlib import Path
from datetime import datetime, timezone

ANALYTICS = "https://polymarketanalytics.com/api"
DATA_API  = "https://data-api.polymarket.com"
HEADERS   = {"User-Agent": "Mozilla/5.0"}
OUTPUT    = Path("/root/granja-v2/wallet_pool.json")

# ── Criterios de calidad (conservadores) ─────────────────
TIERS = {
    3: {"wr": 0.65, "pnl": 10_000, "pos": 50,  "ratio": 2.0, "label": "⭐⭐⭐ Elite"},
    2: {"wr": 0.58, "pnl":  3_000, "pos": 25,  "ratio": 1.5, "label": "⭐⭐  Sólida"},
    1: {"wr": 0.55, "pnl":  1_000, "pos": 10,  "ratio": 1.0, "label": "⭐   Base"},
}

TAGS_DISPONIBLES = ["Overall", "Politics", "Crypto", "Sports", "Science"]

# ── Fuente 1: polymarketanalytics ─────────────────────────
def cargar_analytics(tag="Overall", limit=500):
    print(f"📡 [1] polymarketanalytics tag={tag} limit={limit}...", end="", flush=True)
    try:
        r = requests.get(
            f"{ANALYTICS}/traders-tag-performance",
            params={"tag": tag, "sortBy": "win_rate", "order": "desc", "limit": limit},
            headers=HEADERS, timeout=15
        )
        resp = r.json()
        data = resp.get("data", resp) if isinstance(resp, dict) else resp
        print(f" {len(data)} wallets brutas")
        return data
    except Exception as e:
        print(f" ERROR: {e}")
        return []

# ── Fuente 2: leaderboard CLI ─────────────────────────────
def cargar_leaderboard():
    print("📡 [2] Leaderboard CLI (PnL 7d)...", end="", flush=True)
    try:
        env = os.environ.copy()
        env["PATH"] = "/root/granja-v2/venv/bin:" + env.get("PATH", "")
        r = subprocess.run(
            ["polymarket", "data", "leaderboard", "--limit", "200", "-o", "json"],
            capture_output=True, text=True, timeout=25,
            cwd="/root/granja-v2", env=env
        )
        data = json.loads(r.stdout)
        lb = {}
        for e in data:
            if not isinstance(e, dict):
                continue
            addr = e.get("proxy_wallet", "").lower()
            if addr:
                lb[addr] = {
                    "pnl_7d":  float(e.get("pnl", 0)),
                    "rank_7d": int(e.get("rank", 9999)),
                    "name_lb": e.get("user_name", ""),
                }
        print(f" {len(lb)} wallets")
        return lb
    except Exception as e:
        print(f" ERROR: {e}")
        return {}

# ── Verificación rápida de wallet ─────────────────────────
def verificar_wallet(addr: str) -> dict:
    """
    Una sola llamada API — últimos 20 trades.
    Retorna: avg_price, sell_ratio, cat_dominante, age_days, activa_30d
    """
    try:
        r = requests.get(
            f"{DATA_API}/activity",
            params={"user": addr.lower(), "limit": 20},
            headers=HEADERS, timeout=(4, 7)
        )
        if not r.ok:
            return {}
        trades = r.json()
        if not isinstance(trades, list) or not trades:
            return {}

        buys  = [t for t in trades if t.get("side","").upper() == "BUY"]
        sells = [t for t in trades if t.get("side","").upper() == "SELL"]

        if not buys:
            return {}

        avg_price  = sum(float(t.get("price", 0.5)) for t in buys) / len(buys)
        sell_ratio = len(sells) / len(buys)

        # Categoría dominante
        cats = {}
        for t in trades:
            slug = t.get("slug", "").lower()
            if any(x in slug for x in ["nba","nfl","mlb","nhl","lol","cs2","ufc","epl","bundesliga","serie","laliga","champions"]):
                c = "sports"
            elif any(x in slug for x in ["trump","iran","election","president","congress","senate","gov","war","ceasefire","tariff","fed","rate"]):
                c = "politics"
            elif any(x in slug for x in ["bitcoin","eth","crypto","btc","sol","doge","xrp"]):
                c = "crypto"
            elif any(x in slug for x in ["temperature","weather","rain","snow","storm","hurricane"]):
                c = "weather"
            else:
                c = "other"
            cats[c] = cats.get(c, 0) + 1
        cat = max(cats, key=cats.get) if cats else "unknown"

        # Antigüedad y actividad reciente
        oldest_ts  = min(t.get("timestamp", time.time()) for t in trades)
        newest_ts  = max(t.get("timestamp", 0) for t in trades)
        age_days   = (time.time() - oldest_ts) / 86400
        activa_30d = (time.time() - newest_ts) < 30 * 86400

        return {
            "avg_price":  round(avg_price, 3),
            "sell_ratio": round(sell_ratio, 2),
            "cat":        cat,
            "age_days":   round(age_days, 0),
            "activa_30d": activa_30d,
        }
    except:
        return {}

# ── Calcular tier ─────────────────────────────────────────
def calcular_tier(wr, pnl, pos, win_amount, loss_amount):
    ratio = win_amount / max(abs(loss_amount), 1)
    for stars in [3, 2, 1]:
        t = TIERS[stars]
        if wr >= t["wr"] and pnl >= t["pnl"] and pos >= t["pos"] and ratio >= t["ratio"]:
            return stars, round(ratio, 2)
    return 0, round(ratio, 2)

# ── Main ──────────────────────────────────────────────────
def main():
    auto_mode  = "--auto"  in sys.argv
    tag_arg    = None
    if "--tag" in sys.argv:
        idx = sys.argv.index("--tag")
        if idx + 1 < len(sys.argv):
            tag_arg = sys.argv[idx + 1].capitalize()

    print("=" * 65)
    print("🏊 BUILD_POOL — Constructor de wallet_pool.json")
    print("   Criterio: track record histórico (WR + PnL + consistencia)")
    print("=" * 65)
    print()

    # Elegir tag si no viene por arg
    tag = tag_arg or "Overall"
    if not auto_mode and not tag_arg:
        print("Tags disponibles:", " | ".join(TAGS_DISPONIBLES))
        inp = input("¿Tag? (Enter=Overall): ").strip().capitalize() or "Overall"
        if inp in TAGS_DISPONIBLES:
            tag = inp

    # Cargar fuentes
    raw        = cargar_analytics(tag=tag, limit=500)
    leaderboard = cargar_leaderboard()

    if not raw:
        print("❌ Sin datos de analytics. Verifica conexión.")
        return

    # Filtrar y calcular tier
    print(f"\n🔬 Calculando tiers para {len(raw)} wallets...", end="", flush=True)
    candidatos = []
    for e in raw:
        wr  = float(e.get("win_rate", 0))
        pnl = float(e.get("overall_gain", 0))
        pos = int(e.get("total_positions", 0))
        win = float(e.get("win_amount", 0))
        loss= float(e.get("loss_amount", 0))
        addr= e.get("trader", "").lower()

        if not addr or wr < 0.55 or pnl < 1000 or pos < 10:
            continue

        tier, ratio = calcular_tier(wr, pnl, pos, win, loss)
        if tier == 0:
            continue

        lb = leaderboard.get(addr, {})
        candidatos.append({
            "addr":      addr,
            "name":      e.get("trader_name", "?"),
            "tier":      tier,
            "win_rate":  round(wr, 4),
            "pnl":       round(pnl, 2),
            "positions": pos,
            "ratio":     ratio,
            "win_amount":  round(win, 2),
            "loss_amount": round(loss, 2),
            "pnl_7d":    lb.get("pnl_7d"),
            "rank_7d":   lb.get("rank_7d"),
            "tags":      e.get("trader_tags", ""),
        })

    candidatos.sort(key=lambda x: (-x["tier"], -x["win_rate"], -x["pnl"]))
    print(f" {len(candidatos)} candidatos calificados")

    if not candidatos:
        print("❌ Ninguna wallet pasa los filtros base (WR≥55%, PnL≥$1k, Pos≥10).")
        return

    # Imprimir tabla
    print(f"\n{'#':<4} {'TIER':<14} {'WR':>5} {'PnL hist':>12} {'Ratio':>6} {'Pos':>5} {'PnL 7d':>10}  WALLET")
    print("─" * 100)
    for i, c in enumerate(candidatos[:20], 1):
        tier_label = TIERS[c["tier"]]["label"]
        wr_str     = f"{c['win_rate']*100:.1f}%"
        pnl_str    = f"${c['pnl']:>,.0f}"
        pnl7_str   = f"${c['pnl_7d']:>,.0f}" if c["pnl_7d"] is not None else "  N/A"
        ratio_str  = f"{c['ratio']:.1f}x"
        print(f"{i:<4} {tier_label:<14} {wr_str:>5} {pnl_str:>12} {ratio_str:>6} {c['positions']:>5} {pnl7_str:>10}  {c['addr'][:20]}... ({c['name'][:14]})")
        if c["tags"]:
            print(f"     🏷  {c['tags'][:80]}")

    print(f"\nResumen: ⭐⭐⭐ {sum(1 for c in candidatos if c['tier']==3)} | "
          f"⭐⭐ {sum(1 for c in candidatos if c['tier']==2)} | "
          f"⭐ {sum(1 for c in candidatos if c['tier']==1)}")

    # Selección
    if auto_mode:
        # Auto: tomar todos los tier >= 2
        seleccionados = [c for c in candidatos if c["tier"] >= 2]
        print(f"\n✅ Auto-modo: {len(seleccionados)} wallets tier≥2 seleccionadas")
    else:
        print()
        print("Opciones:")
        print("  [1] Incluir solo ⭐⭐⭐ Elite")
        print("  [2] Incluir ⭐⭐⭐ + ⭐⭐ (recomendado)")
        print("  [3] Incluir todas (⭐⭐⭐ + ⭐⭐ + ⭐)")
        print("  [n] Seleccionar manualmente por número")
        print()
        opcion = input("¿Qué incluir en el pool? (Enter=2): ").strip() or "2"

        if opcion == "1":
            seleccionados = [c for c in candidatos if c["tier"] == 3]
        elif opcion == "3":
            seleccionados = candidatos[:]
        elif opcion == "2":
            seleccionados = [c for c in candidatos if c["tier"] >= 2]
        else:
            # Selección manual
            raw_sel = input("Números separados por coma (ej: 1,3,5): ").strip()
            indices = [int(x.strip()) - 1 for x in raw_sel.split(",") if x.strip().isdigit()]
            seleccionados = [candidatos[i] for i in indices if 0 <= i < len(candidatos)]

    if not seleccionados:
        print("❌ Nada seleccionado. Sin cambios.")
        return

    # Verificación rápida de las wallets seleccionadas
    print(f"\n🔍 Verificando actividad de {len(seleccionados)} wallets...")
    verificadas = []
    for i, c in enumerate(seleccionados):
        print(f"   [{i+1}/{len(seleccionados)}] {c['addr'][:20]}...", end="\r", flush=True)
        perfil = verificar_wallet(c["addr"])
        c["perfil"] = perfil
        c["activa_30d"] = perfil.get("activa_30d", False)
        c["cat"]        = perfil.get("cat", "unknown")
        c["avg_price"]  = perfil.get("avg_price")
        c["age_days"]   = perfil.get("age_days")
        verificadas.append(c)

    activas = sum(1 for c in verificadas if c["activa_30d"])
    print(f"\n✅ Verificadas: {activas}/{len(verificadas)} activas en últimos 30 días")

    # Guardar pool
    pool = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "tag_fuente":   tag,
        "total":        len(verificadas),
        "activas_30d":  activas,
        "wallets": [
            {
                "addr":       c["addr"],
                "name":       c["name"],
                "tier":       c["tier"],
                "win_rate":   c["win_rate"],
                "pnl":        c["pnl"],
                "positions":  c["positions"],
                "ratio":      c["ratio"],
                "pnl_7d":     c["pnl_7d"],
                "cat":        c.get("cat", "unknown"),
                "avg_price":  c.get("avg_price"),
                "age_days":   c.get("age_days"),
                "activa_30d": c.get("activa_30d", False),
                "tags":       c["tags"],
            }
            for c in verificadas
        ]
    }

    OUTPUT.write_text(json.dumps(pool, indent=2))
    print(f"\n💾 Pool guardado en {OUTPUT}")
    print(f"   Total: {len(verificadas)} wallets")
    print(f"   ⭐⭐⭐ {sum(1 for c in verificadas if c['tier']==3)} | "
          f"⭐⭐ {sum(1 for c in verificadas if c['tier']==2)} | "
          f"⭐ {sum(1 for c in verificadas if c['tier']==1)}")

    # Preview del pool
    print("\n📋 Pool final:")
    for c in verificadas:
        activa = "✅" if c["activa_30d"] else "💤"
        tier_s = "⭐" * c["tier"]
        print(f"   {activa} {tier_s} {c['addr'][:20]}... ({c['name'][:14]}) WR={c['win_rate']*100:.0f}% PnL=${c['pnl']:,.0f}")

if __name__ == "__main__":
    main()
