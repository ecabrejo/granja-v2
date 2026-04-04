import requests, json, time
from pathlib import Path

wallets_polysmart = [
    "0xfd22b8843ae03a33a8a4c5e39ef1e5ff33ebad91",
    "0x75d1b199a96801338e67a6efaa35c0028ea87a5f",
    "0x46d3d62ab3dd01fb8dc3b16f0141462c82df62e0",
    "0xdfe3fedc5c7679be42c3d393e99d4b55247b73c4",
    "0xeface9902242b0399856f981600b907dcc9bc9a1",
    "0x54520a0661afd0c6580ce45773097c0f0c2f6f80",
    "0x3d1ecf16942939b3603c2539a406514a40b504d0",
    "0x7177a7f5c216809c577c50c77b12aae81f81ddef",
    "0xa61ef8773ec2e821962306ca87d4b57e39ff0abd",
    "0x2d4bf8f846bf68f43b9157bf30810d334ac6ca7a",
]

# Cargar pool existente
pool = json.loads(Path('/home/claude/wallet_pool.json').read_text()) if Path('/home/claude/wallet_pool.json').exists() else {"wallets": []}
pool_addrs = {w['addr'].lower(): w for w in pool.get('wallets', [])}

DATA_API = "https://data-api.polymarket.com"
HEADERS  = {"User-Agent": "Mozilla/5.0"}

print(f"{'ADDR':<20} {'EN_POOL':<8} {'TIER':<6} {'ACTIV':<8} {'ULTIMO_TRADE':<15} {'SLUG'}")
print("─" * 100)

for addr in wallets_polysmart:
    addr_low = addr.lower()
    en_pool  = addr_low in pool_addrs
    tier_str = f"⭐x{pool_addrs[addr_low]['tier']}" if en_pool else "NUEVO"

    try:
        r = requests.get(f"{DATA_API}/activity",
            params={"user": addr_low, "limit": 5},
            headers=HEADERS, timeout=(4, 7))
        trades = r.json() if r.ok else []
        if not isinstance(trades, list) or not trades:
            print(f"{addr[:18]}  {str(en_pool):<8} {tier_str:<6} {'sin datos'}")
            continue

        # Filtrar updown/5m/15m
        validos = [t for t in trades if not any(
            x in t.get('slug','').lower() for x in ['updown','5m','15m','1h']
        )]

        if not validos:
            print(f"{addr[:18]}  {str(en_pool):<8} {tier_str:<6} {'solo bots'}")
            continue

        t = validos[0]
        hace_h = (time.time() - t.get('timestamp', 0)) / 3600
        side   = t.get('side','?').upper()
        slug   = t.get('slug','')[:45]
        price  = float(t.get('price', 0))
        hace_str = f"{hace_h:.1f}h" if hace_h < 48 else f"{hace_h/24:.0f}d"

        print(f"{addr[:18]}  {str(en_pool):<8} {tier_str:<6} ✅       {hace_str:<8} {side}@{price:.2f} {slug}")
    except Exception as e:
        print(f"{addr[:18]}  ERROR: {e}")
    time.sleep(0.3)

