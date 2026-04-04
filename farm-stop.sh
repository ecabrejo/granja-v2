#!/bin/bash
# farm-stop.sh — Detiene Claudio
DIR="/root/granja-v2"
PIDFILE="$DIR/claudio.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "Claudio no está corriendo (no hay PID file)"
    exit 0
fi

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
