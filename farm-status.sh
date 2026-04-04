#!/bin/bash
# farm-status.sh — Estado de Claudio y workers
DIR="/root/granja-v2"
PIDFILE="$DIR/claudio.pid"

echo "=== CLAUDIO STATUS ==="
if [ -f "$PIDFILE" ] && kill -0 $(cat "$PIDFILE") 2>/dev/null; then
    echo "✅ Claudio corriendo (PID $(cat $PIDFILE))"
else
    echo "🔴 Claudio detenido"
fi

echo ""
echo "=== WORKERS ==="
for w in "$DIR/workers"/*/; do
    wid=$(basename "$w")
    cfg="$w/config.json"
    if [ -f "$cfg" ]; then
        target=$(python3 -c "import json; c=json.load(open('$cfg')); print(c.get('target_wallet','?')[:16]+'...')" 2>/dev/null)
        market=$(python3 -c "import json; c=json.load(open('$cfg')); print(c.get('market','?'))" 2>/dev/null)
        echo "  [$wid] target=$target market=$market"
    fi
done

echo ""
echo "=== ÚLTIMAS LÍNEAS LOG ==="
tail -20 "$DIR/claudio.log" 2>/dev/null || echo "(sin log)"
