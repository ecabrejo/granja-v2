#!/bin/bash
# farm-start.sh — Arranca Claudio (supervisor central)
VENV="/root/granja-v2/venv"
DIR="/root/granja-v2"
PIDFILE="$DIR/claudio.pid"

if [ -f "$PIDFILE" ] && kill -0 $(cat "$PIDFILE") 2>/dev/null; then
    echo "Claudio ya está corriendo (PID $(cat $PIDFILE))"
    exit 0
fi

# Verificar NordVPN
VPN_STATUS=$(nordvpn status 2>/dev/null | grep "Status:" | awk '{print $2}')
if [ "$VPN_STATUS" != "Connected" ]; then
    echo "❌ NordVPN no está conectado. Conecta VPN antes de arrancar la granja."
    echo "   Usa: nordvpn connect"
    exit 1
fi
VPN_COUNTRY=$(nordvpn status 2>/dev/null | grep "Country:" | cut -d' ' -f2-)
echo "✅ VPN conectado: $VPN_COUNTRY"

cd "$DIR"
source "$VENV/bin/activate"
nohup /root/granja-v2/venv/bin/python3 claudio.py >> claudio.log 2>&1 &
echo $! > "$PIDFILE"
echo "Claudio arrancado (PID $!)"
