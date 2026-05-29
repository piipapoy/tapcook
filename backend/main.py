import json
import os
import time
import asyncio
import logging
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tapcook")

import paho.mqtt.client as mqtt
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from database import init_db, get_user_by_uid, create_pending, redeem_code, get_pending_list, get_active_pending_by_uid, get_all_users, delete_user_by_uid, get_config, set_config

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
TOPIC_CARD = "tapcook/+/card"
TOPIC_AUTH = "tapcook/{}/auth"
TOPIC_CMD = "tapcook/{}/cmd"
TOPIC_STATUS = "tapcook/+/status"

device_relay: dict[str, bool] = {}
pending_queue: asyncio.Queue = asyncio.Queue()
sse_clients: list = []
loop: asyncio.AbstractEventLoop = None

mqttc = mqtt.Client(client_id="tapcook-backend")


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


def mqtt_connect_with_retry():
    for i in range(15):
        try:
            mqttc.connect(MQTT_HOST, MQTT_PORT, 60)
            mqttc.subscribe(TOPIC_CARD, qos=1)
            mqttc.subscribe(TOPIC_STATUS, qos=1)
            log.info("MQTT terhubung ke %s:%s", MQTT_HOST, MQTT_PORT)
            return
        except Exception as e:
            log.warning("MQTT percobaan %d/15 gagal: %s", i + 1, e)
            time.sleep(2)
    log.error("MQTT gagal setelah 15 percobaan — lanjut tanpa MQTT")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop
    loop = asyncio.get_running_loop()
    await init_db()
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
