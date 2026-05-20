#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

echo "=== TapCook — Instalasi & Setup ==="
echo ""

# ── Cek prerequisites ──
command -v python3 >/dev/null 2>&1 || { echo "ERROR: Python3 tidak ditemukan. Install dulu."; exit 1; }
command -v pip3 >/dev/null 2>&1 || { echo "ERROR: pip3 tidak ditemukan."; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "ERROR: Docker tidak ditemukan. Install dulu: https://docs.docker.com/engine/install/"; exit 1; }

echo "[1/4] Setup Python virtual environment..."
python3 -m venv backend/venv
source backend/venv/bin/activate
pip install --quiet -r backend/requirements.txt
echo "       OK — venv siap"

echo ""
echo "[2/4] Setup MQTT broker (EMQX)..."
if docker ps --format '{{.Names}}' | grep -q '^tapcook_emqx$'; then
  echo "       EMQX sudah berjalan"
elif docker ps -a --format '{{.Names}}' | grep -q '^tapcook_emqx$'; then
  docker start tapcook_emqx
  echo "       EMQX di-start dari container yang ada"
else
  docker run -d --name tapcook_emqx -p 1883:1883 -p 18083:18083 emqx:5.8
  echo "       EMQX container baru dibuat & dijalankan"
fi

echo ""
echo "[3/4] Update IP di config.h..."
HOST_IP=$(ip -4 addr show | grep -oP 'inet \K[\d.]+' | grep -v '^127\.' | grep -v '^172\.' | grep -v '^10\.' | head -1)
if [ -z "$HOST_IP" ]; then
  HOST_IP=$(ip -4 addr show | grep -oP 'inet \K[\d.]+' | grep '^192\.' | head -1)
fi
if [ -n "$HOST_IP" ]; then
  sed -i "s/#define MQTT_HOST.*/#define MQTT_HOST     \"$HOST_IP\"/" src/config.h
  echo "       config.h: MQTT_HOST = $HOST_IP"
else
  echo "       WARNING: Gak bisa deteksi IP. Edit manual di src/config.h"
fi

echo ""
echo "[4/4] Start backend..."
cd backend
nohup venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 > /tmp/tapcook-backend.log 2>&1 &
BACKEND_PID=$!
cd "$ROOT_DIR"

sleep 2
if kill -0 "$BACKEND_PID" 2>/dev/null; then
  echo "       Backend running di http://localhost:8000 (PID: $BACKEND_PID)"
else
  echo "       ERROR: Backend gagal start. Cek /tmp/tapcook-backend.log"
  exit 1
fi

echo ""
echo "=== SELESAI ==="
echo "Admin:     http://localhost:8000/admin"
echo "Registrasi: http://localhost:8000/"
echo ""
echo "Upload firmware ESP32:"
echo "  1. Buka folder ini di PlatformIO / VS Code"
echo "  2. src/config.h udah terisi IP laptop ($HOST_IP)"
echo "  3. Upload: pio run --target upload"
echo ""
echo "Matikan: docker stop tapcook_emqx && kill $BACKEND_PID"
