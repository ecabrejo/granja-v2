# La Granja V2 — Estado Final
**Actualizado:** 2026-04-27
**Estado:** 🔴 Detenida — migración a granja-v3

## Capital al cierre
| Concepto | Valor |
|---|---|
| Portfolio | ~$47 |
| Cash | ~$0.23 |
| Trade size | $1 flat |
| PnL total | -$24.92 |

## Causa del drawdown
Posiciones abiertas antes de implementar filtros completos:
- Trump say Pope/Leo → -$5.51
- Mercados de deportes (nueva wallet mal seleccionada) → -$3 aprox
- Social media counts pre-filtro → -$8 aprox
- Posiciones largas inmovilizadas (Kevin Warsh, Iran June, Graham Platner)

## Lección operativa más importante
El selector encontraba wallets con WR alto que eran bots de temperatura/deportes.
Fix implementado: filtro sports_ratio >30% en selector.py.
Pero la verificación manual sigue siendo obligatoria.

## Último commit granja-v2
fix: filtro deportes en selector + wallet crypto-predictions

## Todo listo para granja-v3
- Filtros BLOCKED_CATS completos ✅
- Selector con filtro de deportes ✅
- farm-stop.sh con pkill ✅
- Lecciones documentadas ✅
- CLOB V2 migration pendiente → implementar desde día 1 en v3
Ahora el git final:

bash
cd /root/granja-v2 && git add -A && git commit -m "docs: project knowledge final + cierre granja-v2" && git push


