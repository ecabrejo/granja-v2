#!/bin/bash
# farm-stop.sh — Detiene Claudio
DIR="/root/granja-v2"
PIDFILE="$DIR/claudio.pid"

# Matar proceso del PID file si existe
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill -TERM "$PID"
        sleep 2
        kill -0 "$PID" 2>/dev/null && kill -KILL "$PID"
        echo "Claudio detenido (PID $PID)"
    else
        echo "Proceso $PID no encontrado"
    fi
    rm -f "$PIDFILE"
fi

# Red de seguridad — matar cualquier proceso huerfano de claudio.py
ORPHANS=$(pgrep -f "python3.*claudio.py" 2>/dev/null)
if [ -n "$ORPHANS" ]; then
    echo "Matando procesos huerfanos: $ORPHANS"
    pkill -TERM -f "python3.*claudio.py"
    sleep 2
    pkill -KILL -f "python3.*claudio.py" 2>/dev/null
fi
