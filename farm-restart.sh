#!/bin/bash
# farm-restart.sh — Reinicia Claudio
DIR="/root/granja-v2"
bash "$DIR/farm-stop.sh"
sleep 2
bash "$DIR/farm-start.sh"
