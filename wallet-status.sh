#!/bin/bash
# wallet-status.sh — Estado completo de la granja
VENV="/root/granja-v2/venv"

echo "═══════════════════════════════════════════════════════════════════"
echo "  🌱 GRANJA STATUS — $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "═══════════════════════════════════════════════════════════════════"

PIDFILE="/root/granja-v2/claudio.pid"
if [ -f "$PIDFILE" ] && kill -0 $(cat "$PIDFILE") 2>/dev/null; then
    echo "  🟢 Claudio corriendo (PID $(cat $PIDFILE))"
else
    echo "  🔴 Claudio detenido"
fi

VPN=$(nordvpn status 2>/dev/null | grep "Country:" | cut -d' ' -f2-)
echo "  🔒 VPN: ${VPN:-desconectado}"

CFG=$(python3 /root/granja-v2/get_worker_info.py 2>/dev/null)
echo "  🎯 Worker: $CFG"
echo ""

$VENV/bin/python3 << 'PYEOF'
import requests, json, os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

PROXY = "0x96e7C5cD27eCfe5Ce369Dc1EF59772f892eE7A9C"
load_dotenv('/root/granja-v2/workers/worker_01/.env')

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams
    client = ClobClient('https://clob.polymarket.com',
        key=os.getenv('WALLET_PRIVATE_KEY'), chain_id=137,
        signature_type=1, funder=os.getenv('POLYMARKET_PROXY_ADDRESS'))
    client.set_api_creds(ApiCreds(
        api_key=os.getenv('POLY_API_KEY'),
        api_secret=os.getenv('POLY_API_SECRET'),
        api_passphrase=os.getenv('POLY_API_PASSPHRASE')))
    bal = round(int(client.get_balance_allowance(
        BalanceAllowanceParams(asset_type='COLLATERAL')).get('balance',0)) / 1e6, 2)
except:
    bal = None

try:
    r = requests.get(f"https://data-api.polymarket.com/positions?user={PROXY}", timeout=10)
    positions = r.json()
except:
    positions = []

now       = datetime.now(timezone.utc)
total_val = sum(float(p.get('currentValue',0)) for p in positions)
total_pnl = sum(float(p.get('cashPnl',0)) for p in positions)
redeemable = [p for p in positions if p.get('redeemable')]

print(f"─── 💰 CAPITAL ──────────────────────────────────────────────────")
print(f"  💵 Cash disponible : ${bal:.2f}" if bal is not None else "  💵 Cash: N/A")
print(f"  📊 Posiciones      : {len(positions)} abiertas | {len(redeemable)} para REDEEM")
print(f"  💼 Valor total     : ${total_val:.2f} | PnL: {total_pnl:+.2f}")
print()
print(f"─── 📋 POSICIONES ───────────────────────────────────────────────")
print(f"  {'PnL':>7} | {'Valor':>6} | {'Cierra UTC':>14} | {'Resta':>12} | Mercado")
print(f"  {'─'*7}-+-{'─'*6}-+-{'─'*14}-+-{'─'*12}-+-{'─'*35}")

for p in sorted(positions, key=lambda x: x.get('endDate','9')):
    pnl    = float(p.get('cashPnl', 0))
    val    = float(p.get('currentValue', 0))
    redeem = p.get('redeemable', False)
    title  = p.get('title','?')[:35]
    end_str = p.get('endDate','')

    try:
        end_str_clean = end_str.replace('Z','+00:00')
        if '+' not in end_str_clean and 'T' in end_str_clean:
            end_str_clean += '+00:00'
        end_utc = datetime.fromisoformat(end_str_clean)
        if end_utc.tzinfo is None:
            end_utc = end_utc.replace(tzinfo=timezone.utc)
        diff = end_utc - now

        if redeem:
            resta = "🔴 REDEEM"
        elif diff.total_seconds() < 0:
            resta = "⏰ CERRADO"
        else:
            total_s = diff.total_seconds()
            d = int(total_s // 86400)
            h = int((total_s % 86400) // 3600)
            m = int((total_s % 3600) // 60)
            resta = f"{d}d {h}h {m:02d}m" if d > 0 else f"{h}h {m:02d}m"

        utc_str = end_utc.strftime('%m/%d %H:%M UTC')
    except:
        utc_str = resta = "?"

    signo = "📈" if pnl >= 0 else "📉"
    print(f"  {signo}{pnl:>+6.2f} | ${val:>5.2f} | {utc_str:>14} | {resta:>12} | {title}")

if redeemable:
    print()
    print(f"─── 🔴 PARA REDEEM ──────────────────────────────────────────────")
    for p in redeemable:
        pnl  = float(p.get('cashPnl',0))
        val  = float(p.get('currentValue',0))
        slug = p.get('eventSlug') or p.get('slug','')
        estado = "WON ✅" if pnl > 0 else "LOST"
        print(f"  {estado} | pnl={pnl:+.2f} | val=${val:.2f}")
        print(f"  → https://polymarket.com/event/{slug}")
PYEOF
echo "═══════════════════════════════════════════════════════════════════"
