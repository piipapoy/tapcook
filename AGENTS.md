# TapCook — Agent Rules

## Handoff
- When context usage reaches **70%–80%**, create `HANDOFF.md` with:
  - Current progress / changes made
  - Last known state (server running, MQTT status, etc.)
  - Pending tasks / next steps

## Backend
- Kill existing python processes before re-run
- Start backend: `uvicorn main:app --reload --host 0.0.0.0 --port 8000`
- Use venv at `backend/venv/`
- EMQX MQTT broker via Docker (`emqx:5.8`)

## ESP32 Firmware
- Source in `src/main.cpp` + `src/config.h`
- PlatformIO project
