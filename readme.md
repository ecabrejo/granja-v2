# La Granja V2 — Registro de Operaciones y Aprendizajes
**Última actualización:** 2026-04-04  
**Estado:** 🟢 Operativa  
**Servidor:** DigitalOcean Ubuntu — IP 64.23.187.205 (VPN Netherlands activa)

---

## El aprendizaje más importante

> *El bottleneck del copy-trading no es la ejecución — es la selección del objetivo a seguir.*

Aunque existe mucho "ruido" en GitHub, Reddit y foros de internet sobre copy-trading en Polymarket, la información realmente valiosa solo se encuentra con el método del ensayo y el error. Ese camino es largo pero fructífero.

El segundo aprendizaje igual de importante: **el mayor enemigo no fue la falta de información sino la sobre-iteración sin validación**. Cambiar parámetros constantemente buscando la solución perfecta estanca más que avanza. La disciplina de validar antes de cambiar es crítica.

---

## Arquitectura actual

```
/root/granja-v2/
├── claudio.py          ← Supervisor central, Telegram, orquesta todo
├── bot_granjav2.py     ← Módulo puro de copy-trading, sin Telegram
├── selector.py         ← Buscador de wallets candidatas (v2, market-first)
├── hunter.py           ← Legacy, mantener pero no usar
├── scout.py            ← Legacy, mantener pero no usar
├── wallet_pool.json    ← Repositorio de wallets evaluadas (en construcción)
├── selector_results.json ← Última corrida del selector
├── proposals.json      ← Propuesta pendiente de aprobación
├── workers/
│   └── worker_01/
│       ├── config.json ← target_wallets, market, trade_usd, poll_seconds
│       └── .env        ← credenciales API (no versionar)
└── venv/               ← Python environment
```

### Scripts de operación
```bash
./farm-start.sh     # Arrancar granja completa
./farm-stop.sh      # Detener granja
./farm-restart.sh   # Reiniciar
./farm-status.sh    # Estado rápido
./wallet-status.sh  # Posiciones abiertas con PnL y tiempo restante
```

### Comandos Telegram
```
/status    — Estado completo de la granja
/selector  — Buscar nueva wallet candidata (presenta top 3)
/start     — Arrancar worker
/stop      — Detener worker (Claudio sigue en standby)
/stop all  — Detener TODO incluyendo Claudio
/restart   — Reiniciar worker
/wallet    — Ver wallet target activa y su última actividad
/help      — Lista de comandos
```

---

## Estado financiero actual (2026-04-04)

| Concepto | Valor |
|---|---|
| Capital inicial | ~$50 |
| Portfolio total | ~$85 |
| Cash disponible | ~$25 |
| Posiciones abiertas | ~$60 |
| PnL all-time | -$7 (lastre de posiciones antiguas) |

El PnL negativo all-time refleja posiciones de las primeras semanas cuando operábamos sin filtros adecuados — mercados de 30-275 días que aún no resuelven. El capital efectivo creció de $50 a $85.

---

## El pipeline de selección de wallets (selector.py v2)

### Filosofía
**Calidad sobre cantidad. Confianza sobre incertidumbre.**

No existe la wallet perfecta ni el mercado perfecto. La tarea es encontrar wallets suficientemente buenas que estén activas ahora mismo, copiarlas mientras funcionen, y rotar cuando cambien.

### Pipeline técnico
```
1. Gamma API → mercados activos válidos (4h-168h, vol>$50k)
2. Data API /trades → wallets activas en esos mercados (últimas 24h)
3. Filtrar bots (>5 buys en un solo mercado en un día)
4. Para cada candidata → historial /activity → score propio
5. Score: 0.4×WR + 0.3×edge + 0.2×recency + 0.1×diversidad
6. Sistema de estrellas 1-4 según calidad de señales
7. Top 3 → Gerencia aprueba vía Telegram
```

### Filtros de mercado
- Duración: 4h a 168h restantes (no mercados casi resueltos ni demasiado largos)
- Volumen 24h: >$50,000
- Midpoint: entre 0.10 y 0.90 (mercado genuinamente incierto)
- Excluidos: updown, crypto-5m, crypto-15m

### Filtros de wallet
- Historial mínimo: 50 trades (filtra cuentas nuevas)
- WR aproximado: >50%
- Máximo 5 buys por día en un solo mercado (filtra bots)
- Diversidad: se valora positivamente operar en muchos mercados

### Sistema de estrellas
| Estrellas | Criterio |
|---|---|
| ⭐⭐ | Activa en mercado válido ahora (base) |
| ⭐⭐⭐ | Además en leaderboard reciente con PnL>0 |
| ⭐⭐⭐⭐ | Además 3+ BUYs en el mercado (convicción) |

---

## Lecciones aprendidas — cronología

### Lo que no funciona

**Polmarket CLI leaderboard como fuente de wallets**  
La CLI devuelve 0 wallets — fuente rota desde el inicio. No invertir tiempo en arreglarla.

**polymarketanalytics como fuente primaria**  
Rankea por historial histórico, no por actividad actual. Las top 100 wallets tienen posiciones en mercados ya cerrados hace meses. Son buenos traders históricamente pero no están operando en mercados que nos sirvan ahora.

**Filtro de 48h en el bot**  
Estuvimos semanas buscando wallets que operaran mercados <48h porque el bot rechazaba todo lo demás. El problema no era la duración — era la calidad de la wallet. Cambiamos el parámetro equivocado durante semanas. Ahora el límite es 168h.

**Copiar múltiples entradas al mismo mercado**  
Si la wallet target promedia hacia abajo (compra el mismo outcome varias veces), nosotros copiábamos cada señal individual. Una wallet perdedora en un partido generaba 4-5 posiciones perdedoras para nosotros. Solución: límite de una posición por mercado (clob_token).

**Buscar la wallet perfecta**  
Paralizó la operación durante 5 días. No existe. Good enough + inmediatez > perfecto + espera.

**Backtesting**  
No vale el esfuerzo dado el costo de ingeniería y las limitaciones del API. Preferible análisis semanal vía Telegram.

### Lo que sí funciona

**Pipeline invertido: mercados → wallets**  
En lugar de buscar wallets buenas y ver qué mercados operan (siempre cerrados), buscar mercados activos válidos y ver quién opera en ellos. Este cambio desbloqueó el selector.

**Score propio desde Data API**  
Sin dependencias de polymarketanalytics ni CLI rota. WR calculado comparando precio de entrada vs midpoint actual. Funciona y es suficientemente preciso para selección.

**MarketOrderArgs(amount=1.0)**  
El patrón correcto de ejecución. Cálculo manual de shares causó errores repetidos. Amount en USDC, el bot calcula los shares.

**proxyWallet como campo correcto**  
En Data API trades, `maker` y `taker` están vacíos. `proxyWallet` es el campo correcto para identificar la wallet.

**CLOB /midpoint no garantiza liquidez**  
200 OK no significa que haya órdenes. Verificar siempre con /book.

**Spread del orderbook en Polymarket siempre ~0.98**  
Los market makers ponen órdenes límite lejos del precio. El spread no es una métrica útil. El midpoint sí lo es.

---

## Perfiles de wallet — qué buscar y qué evitar

### ✅ Perfil ideal
- Historial >100 trades en mercados legítimos
- WR >55% sostenido (no spikes)
- Diversidad >20 mercados distintos
- Opera con $5-200 por posición (no whale, no micro)
- Activa en las últimas 24h en mercados válidos
- Emite tanto BUYs como SELLs (trader activo) — deseable pero no obligatorio

### ⚠️ Perfil aceptable
- Historial 50-100 trades
- WR >50%
- Opera solo con BUYs (espera resolución) — implica capital bloqueado más tiempo
- Activa en horarios específicos (ej: tarde UTC para deportes norteamericanos)

### ❌ Evitar
- Wallets con >30% de trades en mercados updown (bots de alta frecuencia)
- Whales con $10k-100k por posición (mueven el mercado, latencia los hace incopiables)
- Cuentas con <50 trades en historial (insuficiente para evaluar)
- Wallets en leaderboard público visible (edge arbitrado por followers)
- Concentración extrema: <5 mercados distintos en 50 trades

---

## Tipos de traders identificados en Polymarket

### Trader intradía (ej: Anon)
- Entra y sale del mismo mercado en minutos/horas
- Captura movimientos de precio durante el evento
- Emite BUYs y SELLs — nosotros podemos copiar ambos
- Capital rota rápido — ideal para liquidez
- Riesgo: puede concentrar múltiples entradas en un perdedor

### Trader de convicción (ej: 0xee613b3fc183ee)
- Entra en mercados y espera resolución
- Opera en múltiples mercados simultáneamente
- Solo emite BUYs — capital bloqueado hasta resolución
- Más predecible, menos frecuente en señales
- Riesgo: capital inmovilizado si el mercado tarda

### Whale institucional (ej: 0xe934f2d7d6358c)
- Posiciones de $10k-100k
- Mueve el mercado con cada entrada
- Incopiable por latencia — cuando detectamos la señal, el precio ya se movió
- Evitar siempre

### Bot de alta frecuencia
- Decenas de trades por día en el mismo mercado
- Opera principalmente updown (5min, 15min)
- Filtrado por MAX_BUYS_HOY=5 en el selector
- Evitar siempre

---

## Wallet pool — wallets evaluadas

*En construcción — se poblará automáticamente con cada corrida del selector*

### Wallets aplicadas
| Fecha | Addr | Score | WR | Resultado |
|---|---|---|---|---|
| 2026-04-03 | 0x6b31bd1b...57968f (Anon) | 0.842 | 64% | Mal día en tenis — reemplazada |
| 2026-04-03 | 0x5736ffb2...8bfdba | 0.832 | 60% | Inactiva en horario europeo — reemplazada |
| 2026-04-04 | 0xee613b3f...3debf | 0.863 | 91% | Activa ✅ |

### Wallets descartadas
| Addr | Razón |
|---|---|
| 0xe934f2d7...9640b | Whale — $6.8k promedio por trade, incopiable |
| Top 100 polymarketanalytics | Posiciones en mercados cerrados hace meses |

---

## Principios operativos

1. **Zero autonomía es permanente.** Claudio presenta, Gerencia decide. Sin excepciones.
2. **KISS.** Sin over-engineering. Si funciona con menos, no agregar más.
3. **Una posición por mercado máximo.** No promediar pérdidas copiando la misma señal múltiples veces.
4. **Rotación dinámica.** Cuando la wallet se inactiva o falla, /selector busca la siguiente.
5. **Todo en UTC.** Sin excepciones en logs y reportes.
6. **Backups en cada milestone importante.**

---

## Plan de crecimiento por hitos

### 🎯 Hito actual — $100 (en curso)
**Foco: estabilidad y selección de calidad**
- [x] Selector market-first con score propio
- [x] Filtro de whales (size promedio >$50 descartado)
- [x] Filtro de una posición por mercado
- [x] Notificaciones Telegram silenciosas
- [x] wallet_pool.json con historial de wallets usadas
- [ ] Daily report 08:00 UTC (solo si hubo actividad)
- [ ] Drawdown protection — pausa si balance cae 20% vs inicio del ciclo
- [ ] Fix TG_POLL_ERROR — Claudio no debe caer por timeouts de Telegram
- [ ] Log de decisiones cronológico

### 🎯 Hito $200 — Escalar con seguridad
**Foco: segundo worker + mejoras de infraestructura**
- [ ] **worker_02** — segunda wallet siguiendo mercado distinto simultáneamente
- [ ] **Sizing proporcional** — `trade_usd = max($1, cash_disponible * 0.03)` en lugar de $1 fijo. Nota: Polymarket tiene orden mínima de $1 — no implementar antes de tener capital suficiente para que el 3% supere ese mínimo consistentemente
- [ ] **Lifecycle GREEN/YELLOW/RED** — monitoreo WR del worker cada 5 min, alerta si cae <55%, pausa si cae <40%
- [ ] **Redeem semi-automático** — detectar posiciones ganadoras resueltas y alertar con botón de confirmación
- [ ] **Filtro de categoría en selector** — priorizar Politics/Finance/Weather sobre Sports/Entertainment

### 🎯 Hito $500 — Optimización avanzada
**Foco: edge más sofisticado**
- [ ] **Basket de wallets** — seguir 3-5 wallets simultáneamente, señal más fuerte cuando coinciden
- [ ] **Order aggregation** — agrupar señales en ventanas de 30s para reducir costos (relevante a mayor volumen)
- [ ] **Pool curado manual** — 10-20 wallets pre-validadas con criterios estrictos (WR>60%, >4 meses track record, <100 trades/mes)
- [ ] **Motor dual A+B** — Pool curado (monitoreo continuo) + Scout automático en ventanas horarias

### 🎯 Hito $1000 — Explorar nuevas estrategias
**Foco: diversificación de estrategias**
- [ ] **Arbitraje combinatorio** — explotar mispricings entre outcomes relacionados (ver paper Saguillo et al. 2025, $40M extraídos)
- [ ] **Granja Meteo en live** — si dry-run valida edge en NYC/Chicago/Seattle
- [ ] **worker_03+** — escalar número de workers con capital suficiente

---

## Notas técnicas importantes

- **NegRisk fix:** 90%+ de mercados Polymarket son NegRisk. Fix requiere `OrderArgs + CreateOrderOptions(tick_size, neg_risk=True)`
- **Latencia Data API:** 2-6 min de lag — incompatible con mercados <72h para copy-trading
- **WebSocket:** entrega cambios de orderbook, no eventos de wallet específicos
- **polymarketanalytics.com:** requiere `User-Agent: Mozilla/5.0 Macintosh`
- **Venv:** `/root/granja-v2/venv/` (symlink → `/root/backups/estebans-oldfarm/venv`)
- **NordVPN:** activa Netherlands — causa fallos SSH si se conecta por IP real

---

*Documento vivo — actualizar con cada aprendizaje significativo*
