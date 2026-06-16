import json
import os
import time
import asyncio
import logging
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tapcook")

import paho.mqtt.client as mqtt
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from database import (
    init_db, get_user_by_uid, create_pending, redeem_code,
    get_pending_list, get_active_pending_by_uid, get_all_users,
    delete_user_by_uid, get_config, set_config,
    # NEW: session & anomaly
    create_usage_session, get_sessions_by_device, get_session_count,
    get_recent_sessions, create_anomaly_alert, get_anomaly_alerts,
    get_unacknowledged_count, acknowledge_alert,
)
from anomaly_detector import AnomalyDetector

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
TOPIC_CARD = "tapcook/+/card"
TOPIC_AUTH = "tapcook/{}/auth"
TOPIC_CMD = "tapcook/{}/cmd"
TOPIC_STATUS = "tapcook/+/status"
TOPIC_SESSION = "tapcook/+/session"
TOPIC_POWER = "tapcook/+/power"
TOPIC_ALERT = "tapcook/{}/alert"

device_relay: dict[str, bool] = {}
pending_queue: asyncio.Queue = asyncio.Queue()
sse_clients: list = []
loop: asyncio.AbstractEventLoop = None

mqttc = mqtt.Client(client_id="tapcook-backend")
detector: AnomalyDetector = None


def on_mqtt_message(client, userdata, msg):
    topic = msg.topic
    try:
        payload = json.loads(msg.payload)
    except json.JSONDecodeError:
        return

    if topic.endswith("/card"):
        uid = payload.get("uid", "")
        device_id = topic.split("/")[1]
        asyncio.run_coroutine_threadsafe(
            handle_card_scan(device_id, uid), loop
        )
    elif topic.endswith("/status"):
        device_id = topic.split("/")[1]
        asyncio.run_coroutine_threadsafe(
            handle_status(device_id, payload), loop
        )
    elif topic.endswith("/session"):
        device_id = topic.split("/")[1]
        asyncio.run_coroutine_threadsafe(
            handle_session(device_id, payload), loop
        )
    elif topic.endswith("/power"):
        device_id = topic.split("/")[1]
        asyncio.run_coroutine_threadsafe(
            handle_power(device_id, payload), loop
        )


async def handle_status(device_id: str, payload: dict):
    relay = payload.get("relay")
    if relay is not None:
        device_relay[device_id] = relay
        event = {"type": "relay", "device_id": device_id, "relay": relay}
        for q in sse_clients:
            await q.put(json.dumps(event))


async def handle_card_scan(device_id: str, uid: str):
    user = await get_user_by_uid(uid)

    if user:
        resp = {"status": "ok", "uid": uid, "name": user.name}
        mqttc.publish(TOPIC_AUTH.format(device_id), json.dumps(resp))
        return

    existing = await get_active_pending_by_uid(uid)
    if existing:
        log.info("UID %s sudah punya pending code %s — skip", uid, existing.code)
        resp = {"status": "unknown", "uid": uid}
        mqttc.publish(TOPIC_AUTH.format(device_id), json.dumps(resp))
        return

    reg = await create_pending(uid)
    resp = {"status": "unknown", "uid": uid}
    mqttc.publish(TOPIC_AUTH.format(device_id), json.dumps(resp))

    expires_str = _ensure_tz(reg.expires_at.isoformat())
    event = {
        "type": "pending",
        "uid": uid,
        "code": reg.code,
        "expires_at": expires_str,
    }
    await pending_queue.put(event)
    for q in sse_clients:
        await q.put(json.dumps(event))


# ---------------------------------------------------------------------------
# NEW: Session & power handlers
# ---------------------------------------------------------------------------

async def handle_session(device_id: str, payload: dict):
    """ESP32 sends session data when relay turns OFF."""
    uid       = payload.get("uid", "")
    name      = payload.get("name", "")
    duration  = int(payload.get("duration_s", 0))
    energy    = float(payload.get("energy_kwh", 0))
    avg_power = float(payload.get("avg_power_w", 0))
    max_power = float(payload.get("max_power_w", 0))

    if duration <= 0:
        return

    end_time   = datetime.utcnow()
    start_time = end_time - timedelta(seconds=duration)

    # Save session to DB
    session_row = await create_usage_session(
        device_id=device_id, user_uid=uid if uid else None,
        user_name=name if name else None,
        start_time=start_time, end_time=end_time,
        duration_seconds=duration, total_energy_kwh=energy,
        avg_power_w=avg_power, max_power_w=max_power,
    )
    log.info("Session saved: device=%s uid=%s dur=%ds energy=%.4fkWh",
             device_id, uid, duration, energy)

    # Notify admin via SSE
    session_event = {
        "type": "session",
        "device_id": device_id,
        "user_name": name,
        "duration_s": duration,
        "energy_kwh": round(energy, 4),
    }
    for q in sse_clients:
        await q.put(json.dumps(session_event))

    # Run anomaly detection
    await run_anomaly_check(device_id, session_row)


async def run_anomaly_check(device_id: str, session_row):
    """Run anomaly detector and handle result."""
    global detector
    all_sessions = await get_sessions_by_device(device_id)
    result = await asyncio.to_thread(detector.check, device_id, session_row, all_sessions)

    if result is None:
        return

    # Save alert to DB
    alert = await create_anomaly_alert(
        device_id=device_id,
        session_id=session_row.id,
        alert_type=result.method,
        severity=result.severity,
        score=result.score,
        message=result.message,
        details_json=json.dumps(result.details),
    )
    log.warning("ANOMALY [%s] %s: %s (score=%.3f)",
                result.severity, device_id, result.message, result.score)

    # Send alert to admin via SSE
    alert_event = {
        "type": "anomaly",
        "id": alert.id,
        "device_id": device_id,
        "severity": result.severity,
        "message": result.message,
        "method": result.method,
        "score": round(result.score, 3),
        "created_at": _ensure_tz(alert.created_at.isoformat()),
    }
    for q in sse_clients:
        await q.put(json.dumps(alert_event))

    # Send alert to ESP32 via MQTT
    mqtt_alert = {"cmd": "alert", "msg": result.message[:32], "severity": result.severity}
    mqttc.publish(TOPIC_ALERT.format(device_id), json.dumps(mqtt_alert))


async def handle_power(device_id: str, payload: dict):
    """Forward real-time power reading to admin dashboard via SSE."""
    power_event = {
        "type": "power",
        "device_id": device_id,
        "power_w": payload.get("power_w", 0),
        "current_a": payload.get("current_a", 0),
    }
    for q in sse_clients:
        await q.put(json.dumps(power_event))


# ---------------------------------------------------------------------------
# MQTT setup
# ---------------------------------------------------------------------------

def mqtt_connect_with_retry():
    for i in range(15):
        try:
            mqttc.connect(MQTT_HOST, MQTT_PORT, 60)
            mqttc.subscribe(TOPIC_CARD, qos=1)
            mqttc.subscribe(TOPIC_STATUS, qos=1)
            mqttc.subscribe(TOPIC_SESSION, qos=1)
            mqttc.subscribe(TOPIC_POWER, qos=0)
            log.info("MQTT terhubung ke %s:%s", MQTT_HOST, MQTT_PORT)
            return
        except Exception as e:
            log.warning("MQTT percobaan %d/15 gagal: %s", i + 1, e)
            time.sleep(2)
    log.error("MQTT gagal setelah 15 percobaan — lanjut tanpa MQTT")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop, detector
    loop = asyncio.get_running_loop()
    await init_db()
    detector = AnomalyDetector(models_dir="models")
    mqttc.on_message = on_mqtt_message
    mqtt_connect_with_retry()
    mqttc.loop_start()
    yield
    mqttc.loop_stop()
    mqttc.disconnect()


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    return templates.TemplateResponse("admin.html", {"request": request})


def _ensure_tz(s: str) -> str:
    if "T" in s and not s.endswith("+00:00") and not s.endswith("Z") and "+" not in s:
        return s + "+00:00"
    return s

@app.get("/api/pending")
async def api_pending():
    items = await get_pending_list()
    return [
        {"uid": r.uid, "code": r.code, "expires_at": _ensure_tz(r.expires_at.isoformat())}
        for r in items
    ]


@app.get("/api/users")
async def api_users():
    items = await get_all_users()
    return [
        {"uid": u.uid, "name": u.name, "created_at": u.created_at.isoformat()}
        for u in items
    ]


@app.post("/api/register")
async def api_register(code: str = Form(...), name: str = Form(...)):
    user = await redeem_code(code.strip(), name.strip())
    if not user:
        return JSONResponse(
            {"ok": False, "error": "Kode tidak valid atau sudah kedaluwarsa."},
            status_code=400,
        )

    event = {"type": "redeemed", "uid": user.uid, "name": user.name}
    for q in sse_clients:
        await q.put(json.dumps(event))

    return {"ok": True, "uid": user.uid, "name": user.name}


@app.get("/api/events")
async def sse_events(request: Request):
    queue: asyncio.Queue = asyncio.Queue()
    sse_clients.append(queue)
    try:

        async def event_gen():
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30)
                    evt_type = "pending"
                    try:
                        parsed = json.loads(data)
                        evt_type = parsed.get("type", "pending")
                    except json.JSONDecodeError:
                        pass
                    yield {"event": evt_type, "data": data}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}

        return EventSourceResponse(event_gen())
    finally:
        sse_clients.remove(queue)


@app.post("/api/device/{device_id}/reset-wifi")
async def reset_wifi(device_id: str):
    topic = TOPIC_CMD.format(device_id)
    mqttc.publish(topic, json.dumps({"cmd": "reset_wifi"}))
    log.info("Reset WiFi dikirim ke %s", topic)
    return {"ok": True, "device_id": device_id}


@app.get("/api/device/{device_id}/relay")
async def get_relay_state(device_id: str):
    return {"ok": True, "device_id": device_id, "relay": device_relay.get(device_id, False)}


@app.post("/api/device/{device_id}/relay")
async def relay_control(device_id: str, state: bool = True):
    cmd = "relay_on" if state else "relay_off"
    topic = TOPIC_CMD.format(device_id)
    mqttc.publish(topic, json.dumps({"cmd": cmd}))
    log.info("Relay %s dikirim ke %s", cmd, topic)
    return {"ok": True, "device_id": device_id, "state": state}


@app.get("/api/tariff")
async def get_tariff():
    val = await get_config("tariff", "1444.7")
    return {"ok": True, "tariff": float(val)}


@app.post("/api/tariff")
async def set_tariff(tariff: float = Form(...)):
    await set_config("tariff", str(tariff))
    # Publish to all known devices via cmd topic
    topic = TOPIC_CMD.format("+")
    mqttc.publish(topic.replace("/+", "/esp32_1"), json.dumps({"cmd": "set_tariff", "value": tariff}))
    log.info("Tarif diubah ke Rp %s/kWh dan dikirim ke ESP32", tariff)
    return {"ok": True, "tariff": tariff}


@app.delete("/api/users/{uid}")
async def delete_user(uid: str):
    ok = await delete_user_by_uid(uid)
    if not ok:
        return JSONResponse({"ok": False, "error": "User tidak ditemukan"}, status_code=404)
    return {"ok": True, "uid": uid}


# ---------------------------------------------------------------------------
# NEW: Anomaly & ML API endpoints
# ---------------------------------------------------------------------------

@app.get("/api/anomalies")
async def api_anomalies(device_id: str = None):
    alerts = await get_anomaly_alerts(device_id=device_id, limit=50)
    return [
        {
            "id": a.id,
            "device_id": a.device_id,
            "session_id": a.session_id,
            "alert_type": a.alert_type,
            "severity": a.severity,
            "score": round(a.score, 3),
            "message": a.message,
            "details": json.loads(a.details_json) if a.details_json else {},
            "acknowledged": a.acknowledged,
            "created_at": _ensure_tz(a.created_at.isoformat()),
        }
        for a in alerts
    ]


@app.post("/api/anomalies/{alert_id}/ack")
async def api_ack_alert(alert_id: int):
    ok = await acknowledge_alert(alert_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "Alert tidak ditemukan"}, status_code=404)

    event = {"type": "anomaly_ack", "id": alert_id}
    for q in sse_clients:
        await q.put(json.dumps(event))

    return {"ok": True}


@app.get("/api/ml/status/{device_id}")
async def api_ml_status(device_id: str):
    count = await get_session_count(device_id)
    return detector.status(device_id, count)


@app.get("/api/sessions/{device_id}")
async def api_sessions(device_id: str, limit: int = 20):
    sessions = await get_recent_sessions(device_id, limit=limit)
    return [
        {
            "id": s.id,
            "user_name": s.user_name or "Admin",
            "start_time": _ensure_tz(s.start_time.isoformat()),
            "duration_s": s.duration_seconds,
            "energy_kwh": round(s.total_energy_kwh, 4),
            "avg_power_w": round(s.avg_power_w, 1),
            "max_power_w": round(s.max_power_w, 1),
        }
        for s in sessions
    ]
