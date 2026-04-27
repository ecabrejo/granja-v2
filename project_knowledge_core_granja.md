# La Granja V2 — Core Knowledge
**Proyecto:** Copy-trading automatizado en Polymarket
**Servidor:** DigitalOcean Ubuntu — 64.23.187.205
**Stack:** Python 3, py-clob-client, requests, threading, Telegram

## Arquitectura/root/granja-v2/
├── claudio.py        ← Supervisor, Telegram, orquesta todo
├── bot_granjav2.py   ← Copy-trading puro
├── selector.py       ← Buscador market-first de wallets
├── workers/worker_01/
│   ├── config.json   ← target_wallets, trade_usd, poll_seconds
│   └── .env          ← credenciales API
└── venv/             ← symlink → /root/shared/venv

## Scripts
```bash./farm-start.sh / stop / restart / status
./wallet-status.sh   # posiciones con PnL

## Comandos Telegram
/status /start /stop /stop all /restart /selector /wallet /help

## APIs
- gamma-api.polymarket.com — mercados, neg_risk, tick_size
- data-api.polymarket.com — actividad, trades (campo: proxyWallet)
- clob.polymarket.com — ejecución, balance, /book

## Wallet propia
- POLYMARKET_PROXY: 0x96e7C5cD27eCfe5Ce369Dc1EF59772f892eE7A9C
- Red: Polygon POS | Token: USDC nativo

## Principios
1. Zero autonomía — Claudio presenta, Gerencia decide
2. KISS
3. Una posición por mercado máximo
4. Todo en UTC
5. GitHub como fuente canónica (ecabrejo/granja-v2)
6. Consulta multi-modelo para decisiones estratégicas
7. Servidor = solo producción, nunca backtest ni experimentos

## Lecciones permanentes — las más valiosas

### Sobre copy-trading y latencia
- Latencia 2-6 min con REST polling hace incopiables mercados de timing puro
- Social media counts (tweet counts, Truth Social posts) son timing puro — el edge es de velocidad via xtracker, no de análisis
- Semántica Trump (will-trump-say-, will-trump-name-, -during-whca-) igual problema
- Politics/geo largo plazo son analysis edge — latencia irrelevante
- El edge de AJSV en social counts ya fue absorbido cuando copiamos — consenso Claude+Grok+Opus

### Sobre selección de wallets
- Pipeline invertido funciona: mercados activos válidos → wallets activas en esos mercados
- El mayor error: confiar en WR alto sin verificar categoría del historial
- Bots de deportes y temperatura tienen WR muy alto pero son incopiables
- Bots de temperatura: highest-temp en slug, 100% WR porque son bots de arbitraje
- Bots de deportes: mls-, mex-, kor-, bun-, aus- en historial
- Verificar manualmente los top 3 candidatos antes de aplicar — siempre
- AJSV ($58k profit): excelente pero vira entre ciclos (politics → crypto → semántica)
- Wallets públicas en leaderboard tienen edge arbitrado por followers

### Sobre el bot
- NegRisk es el default (90%+ de mercados) — requiere OrderArgs + neg_risk=True
- MarketOrderArgs(amount=1.0) es el patrón correcto — no calcular shares manualmente
- proxyWallet es el campo correcto en Data API (maker/taker están vacíos)
- farm-stop.sh necesita pkill como red de seguridad para matar procesos huérfanos
- Dos instancias de Claudio corriendo = spam de Telegram. Siempre verificar con ps aux
- selector.py correr con `python3 selector.py` — NO con `venv/bin/python3`

### Sobre capital y sizing
- $3 por trade con $50 capital = demasiado concentrado, 29 trades en un día agota todo
- $1 por trade es más conservador y permite más diversificación temporal
- Posiciones a >30 días inmovilizan capital innecesariamente en fase de crecimiento
- No promediar pérdidas — una posición por mercado máximo es crítico
- No vender posiciones ganadoras antes de resolución — esperar $1/share siempre

### Sobre filtros (BLOCKED_CATS en bot_granjav2.py)
```pythonBLOCKED_CATS = ['lol-', 'cs2-', 'ufc-', 'cbb-',
'temperature', 'highest-temp',
'nba-', 'nhl-', 'mlb-', 'nfl-', 'epl-',
'of-tweets', 'truth-social', '-posts-this-week',
'will-nyc-have', 'will-seattle-have', 'will-chicago-have',
'will-trump-say-', 'will-trump-name-', 'trumps-tie-',
'-during-whca-', 'will-trump-post-']
Justificación por grupo:
- Deportes US + esports: gap maker-taker alto (Becker 2026, >2.23pp)
- Temperatura/precipitación: WR propio 25%, gap 2.57pp, bots de arbitraje
- Social media counts: timing puro, latencia 2-6min mata el edge
- Semántica Trump: mismo problema que social counts, requiere velocidad sub-segundo
- Weather cities: precipitación mensual, sin edge estructural

### Sobre el selector (selector.py)
Pipeline completo:
Gamma API → mercados activos (4h-222h, vol>$50k, mid 0.10-0.90)
Data API /trades → wallets activas en esos mercados (últimas 24h)
Filtrar bots (>5 buys/día en un mercado)
Filtrar whales (avg_size >$50)
Para cada candidata → /activity → score propio
Score: 0.4×WR + 0.3×edge + 0.2×recency + 0.1×diversidad
Filtros adicionales en scoring:

updown_ratio >30% → descartar
sports_ratio >30% → descartar (mls-, nba-, kor-, aus-, bun-, etc.)
avg_size >$50 → descartar (whale)


Diversificación: máx 3 wallets por mercado en top 20
Top candidatos → verificación manual → Gerencia aprueba

**CRÍTICO:** Siempre verificar manualmente los candidatos con /activity antes de aplicar.
Los bots de temperatura y deportes tienen WR artificialmente alto.

### Sobre infraestructura
- Servidor necesita reinicio periódico (kernel updates pendientes causan pérdida de red)
- Power Cycle desde DigitalOcean panel cuando SSH no responde
- NordVPN Netherlands — reconectar después de reboot con `nordvpn connect Netherlands`
- apt update + apt upgrade antes de reiniciar para evitar problemas de red

### Sobre el dataset local (Mac)
- Ubicación: ~/Desktop/Proyectos/poly_data/poly_data/
- orderFilled.csv: 40GB, 164M eventos on-chain raw
- markets.csv: 20MB, 50K mercados
- Token IDs no coinciden entre datasets (77 vs 76 dígitos) — requiere procesador
- Procesador: update_utils/process_live.py genera processed/trades.csv
- Para backtest: correr `uv run python3 update_all.py` overnight

## Wallet pool histórico
| Addr | Resultado | Reactivar |
|---|---|---|
| 0x6b31bd1b...57968f | Malo — tenis, promedió pérdidas | No |
| 0x5736ffb2...8bfdba | Neutral — inactiva horario europeo | Sí |
| 0xee613b3f...3debf | Malo — whale $150-350/trade | No |
| 0x06dc5182...4524 | Bueno — crypto predictions, activa | Sí |
| 0x121785324...690a | Bueno — UCL/ATP/crypto, horario europeo | Sí |
| 0xae0797bd...059 | Bueno — temperatura/crypto, muy activa | Sí |
| 0xc1b5c7da...cf8 | Malo — whale | No |
| 0xfcc096cf...de4 | Bueno — golf/crypto, avg=$8 | Sí |
| 0xe4b5414d...6ab | Mixto — deportes mezclados | No |
| 0xa509ae94...46b | Malo — bot deportes MLS/Liga MX | No |
| 0xad5353af...ef24 (AJSV) | Referencia — $66k profit, vira entre ciclos | Monitorear |

## Roadmap
- ✅ Hito $100 (20 abril 2026) — superado brevemente
- 🎯 Hito $200 — worker_02, sizing proporcional, Pattern 3
- Hito $500 — basket wallets, Pattern 4 hedge, Midterms 2026
- Hito $1000 — arbitraje, on-chain monitoring

## ⚠️ Migración pendiente para granja-v3
**CLOB V2** — deadline 28 abril 11:00 UTC
- py-clob-client V1 deja de funcionar
- Instalar py-clob-client-v2
- Migrar imports y constructor en bot
- Wrap USDC.e → pUSD desde Rabby (una sola vez)
- Nuevo token: pUSD (1:1 USDC, transparente para el bot)# La Granja V2 — Core Knowledge
**Proyecto:** Copy-trading automatizado en Polymarket
**Servidor:** DigitalOcean Ubuntu — 64.23.187.205
**Stack:** Python 3, py-clob-client, requests, threading, Telegram

## Arquitectura/root/granja-v2/
├── claudio.py        ← Supervisor, Telegram, orquesta todo
├── bot_granjav2.py   ← Copy-trading puro
├── selector.py       ← Buscador market-first de wallets
├── workers/worker_01/
│   ├── config.json   ← target_wallets, trade_usd, poll_seconds
│   └── .env          ← credenciales API
└── venv/             ← symlink → /root/shared/venv

## Scripts
```bash./farm-start.sh / stop / restart / status
./wallet-status.sh   # posiciones con PnL

## Comandos Telegram
/status /start /stop /stop all /restart /selector /wallet /help

## APIs
- gamma-api.polymarket.com — mercados, neg_risk, tick_size
- data-api.polymarket.com — actividad, trades (campo: proxyWallet)
- clob.polymarket.com — ejecución, balance, /book

## Wallet propia
- POLYMARKET_PROXY: 0x96e7C5cD27eCfe5Ce369Dc1EF59772f892eE7A9C
- Red: Polygon POS | Token: USDC nativo

## Principios
1. Zero autonomía — Claudio presenta, Gerencia decide
2. KISS
3. Una posición por mercado máximo
4. Todo en UTC
5. GitHub como fuente canónica (ecabrejo/granja-v2)
6. Consulta multi-modelo para decisiones estratégicas
7. Servidor = solo producción, nunca backtest ni experimentos

## Lecciones permanentes — las más valiosas

### Sobre copy-trading y latencia
- Latencia 2-6 min con REST polling hace incopiables mercados de timing puro
- Social media counts (tweet counts, Truth Social posts) son timing puro — el edge es de velocidad via xtracker, no de análisis
- Semántica Trump (will-trump-say-, will-trump-name-, -during-whca-) igual problema
- Politics/geo largo plazo son analysis edge — latencia irrelevante
- El edge de AJSV en social counts ya fue absorbido cuando copiamos — consenso Claude+Grok+Opus

### Sobre selección de wallets
- Pipeline invertido funciona: mercados activos válidos → wallets activas en esos mercados
- El mayor error: confiar en WR alto sin verificar categoría del historial
- Bots de deportes y temperatura tienen WR muy alto pero son incopiables
- Bots de temperatura: highest-temp en slug, 100% WR porque son bots de arbitraje
- Bots de deportes: mls-, mex-, kor-, bun-, aus- en historial
- Verificar manualmente los top 3 candidatos antes de aplicar — siempre
- AJSV ($58k profit): excelente pero vira entre ciclos (politics → crypto → semántica)
- Wallets públicas en leaderboard tienen edge arbitrado por followers

### Sobre el bot
- NegRisk es el default (90%+ de mercados) — requiere OrderArgs + neg_risk=True
- MarketOrderArgs(amount=1.0) es el patrón correcto — no calcular shares manualmente
- proxyWallet es el campo correcto en Data API (maker/taker están vacíos)
- farm-stop.sh necesita pkill como red de seguridad para matar procesos huérfanos
- Dos instancias de Claudio corriendo = spam de Telegram. Siempre verificar con ps aux
- selector.py correr con `python3 selector.py` — NO con `venv/bin/python3`

### Sobre capital y sizing
- $3 por trade con $50 capital = demasiado concentrado, 29 trades en un día agota todo
- $1 por trade es más conservador y permite más diversificación temporal
- Posiciones a >30 días inmovilizan capital innecesariamente en fase de crecimiento
- No promediar pérdidas — una posición por mercado máximo es crítico
- No vender posiciones ganadoras antes de resolución — esperar $1/share siempre

### Sobre filtros (BLOCKED_CATS en bot_granjav2.py)
```pythonBLOCKED_CATS = ['lol-', 'cs2-', 'ufc-', 'cbb-',
'temperature', 'highest-temp',
'nba-', 'nhl-', 'mlb-', 'nfl-', 'epl-',
'of-tweets', 'truth-social', '-posts-this-week',
'will-nyc-have', 'will-seattle-have', 'will-chicago-have',
'will-trump-say-', 'will-trump-name-', 'trumps-tie-',
'-during-whca-', 'will-trump-post-']
Justificación por grupo:
- Deportes US + esports: gap maker-taker alto (Becker 2026, >2.23pp)
- Temperatura/precipitación: WR propio 25%, gap 2.57pp, bots de arbitraje
- Social media counts: timing puro, latencia 2-6min mata el edge
- Semántica Trump: mismo problema que social counts, requiere velocidad sub-segundo
- Weather cities: precipitación mensual, sin edge estructural

### Sobre el selector (selector.py)
Pipeline completo:
Gamma API → mercados activos (4h-222h, vol>$50k, mid 0.10-0.90)
Data API /trades → wallets activas en esos mercados (últimas 24h)
Filtrar bots (>5 buys/día en un mercado)
Filtrar whales (avg_size >$50)
Para cada candidata → /activity → score propio
Score: 0.4×WR + 0.3×edge + 0.2×recency + 0.1×diversidad
Filtros adicionales en scoring:

updown_ratio >30% → descartar
sports_ratio >30% → descartar (mls-, nba-, kor-, aus-, bun-, etc.)
avg_size >$50 → descartar (whale)


Diversificación: máx 3 wallets por mercado en top 20
Top candidatos → verificación manual → Gerencia aprueba

**CRÍTICO:** Siempre verificar manualmente los candidatos con /activity antes de aplicar.
Los bots de temperatura y deportes tienen WR artificialmente alto.

### Sobre infraestructura
- Servidor necesita reinicio periódico (kernel updates pendientes causan pérdida de red)
- Power Cycle desde DigitalOcean panel cuando SSH no responde
- NordVPN Netherlands — reconectar después de reboot con `nordvpn connect Netherlands`
- apt update + apt upgrade antes de reiniciar para evitar problemas de red

### Sobre el dataset local (Mac)
- Ubicación: ~/Desktop/Proyectos/poly_data/poly_data/
- orderFilled.csv: 40GB, 164M eventos on-chain raw
- markets.csv: 20MB, 50K mercados
- Token IDs no coinciden entre datasets (77 vs 76 dígitos) — requiere procesador
- Procesador: update_utils/process_live.py genera processed/trades.csv
- Para backtest: correr `uv run python3 update_all.py` overnight

## Wallet pool histórico
| Addr | Resultado | Reactivar |
|---|---|---|
| 0x6b31bd1b...57968f | Malo — tenis, promedió pérdidas | No |
| 0x5736ffb2...8bfdba | Neutral — inactiva horario europeo | Sí |
| 0xee613b3f...3debf | Malo — whale $150-350/trade | No |
| 0x06dc5182...4524 | Bueno — crypto predictions, activa | Sí |
| 0x121785324...690a | Bueno — UCL/ATP/crypto, horario europeo | Sí |
| 0xae0797bd...059 | Bueno — temperatura/crypto, muy activa | Sí |
| 0xc1b5c7da...cf8 | Malo — whale | No |
| 0xfcc096cf...de4 | Bueno — golf/crypto, avg=$8 | Sí |
| 0xe4b5414d...6ab | Mixto — deportes mezclados | No |
| 0xa509ae94...46b | Malo — bot deportes MLS/Liga MX | No |
| 0xad5353af...ef24 (AJSV) | Referencia — $66k profit, vira entre ciclos | Monitorear |

## Roadmap
- ✅ Hito $100 (20 abril 2026) — superado brevemente
- 🎯 Hito $200 — worker_02, sizing proporcional, Pattern 3
- Hito $500 — basket wallets, Pattern 4 hedge, Midterms 2026
- Hito $1000 — arbitraje, on-chain monitoring

## ⚠️ Migración pendiente para granja-v3
**CLOB V2** — deadline 28 abril 11:00 UTC
- py-clob-client V1 deja de funcionar
- Instalar py-clob-client-v2
- Migrar imports y constructor en bot
- Wrap USDC.e → pUSD desde Rabby (una sola vez)
- Nuevo token: pUSD (1:1 USDC, transparente para el bot)

