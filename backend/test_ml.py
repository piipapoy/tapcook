import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

"""
TapCook ML Demo — Simulate ESP32 sessions via MQTT

Generates realistic cooking data, then injects anomalies to prove
the ML pipeline works end-to-end:

  MQTT publish → Backend receives → DB save → Anomaly check → Alert

Usage:
  1. Start backend:  uvicorn main:app --host 0.0.0.0 --port 8000
  2. Run this:        python test_ml.py
  3. Open dashboard:  http://localhost:8000/admin  → watch 🔔 Anomali tab
"""

import json
import time
import random
import math
import requests
import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MQTT_HOST = "localhost"
MQTT_PORT = 1883
DEVICE_ID = "esp32_1"
TOPIC_SESSION = f"tapcook/{DEVICE_ID}/session"
API_BASE = "http://localhost:8000"

# ---------------------------------------------------------------------------
# Realistic cooking session generator
# ---------------------------------------------------------------------------

def generate_normal_sessions(n=55):
    """
    Generate n realistic cooking sessions.
    Patterns:
      - Breakfast (06:00-09:00): 5-20 min, low-medium power
      - Lunch (11:00-14:00): 10-30 min, medium power
      - Dinner (17:00-20:00): 15-45 min, medium-high power
      - Snack/boil water: 3-10 min, low power
    """
    sessions = []
    users = [
        ("AB CD EF 01", "Andi"),
        ("AB CD EF 02", "Budi"),
        ("AB CD EF 03", "Citra"),
        ("AB CD EF 04", "Dewi"),
    ]

    for i in range(n):
        # Pick a meal type with weighted probability
        meal = random.choices(
            ["breakfast", "lunch", "dinner", "snack"],
            weights=[20, 25, 35, 20],
            k=1
        )[0]

        if meal == "breakfast":
            hour = random.uniform(6, 9)
            duration_min = random.uniform(5, 20)
            power_base = random.uniform(300, 600)
        elif meal == "lunch":
            hour = random.uniform(11, 14)
            duration_min = random.uniform(10, 30)
            power_base = random.uniform(400, 800)
        elif meal == "dinner":
            hour = random.uniform(17, 20)
            duration_min = random.uniform(15, 45)
            power_base = random.uniform(500, 1000)
        else:  # snack / boil water
            hour = random.uniform(8, 22)
            duration_min = random.uniform(3, 10)
            power_base = random.uniform(200, 500)

        duration_s = int(duration_min * 60)
        avg_power = int(power_base + random.gauss(0, 50))
        max_power = int(avg_power * random.uniform(1.1, 1.4))
        energy_kwh = round((avg_power * duration_s) / 3_600_000, 4)

        uid, name = random.choice(users)

        # Vary month for seasonal diversity (simulate over several months)
        month = random.choice([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
        dow = random.randint(0, 6)

        sessions.append({
            "uid": uid,
            "name": name,
            "duration_s": duration_s,
            "energy_kwh": energy_kwh,
            "avg_power_w": avg_power,
            "max_power_w": max_power,
            "_label": f"normal-{meal}",
        })

    return sessions


def generate_anomalous_sessions():
    """Generate clearly anomalous sessions."""
    return [
        {
            "uid": "AB CD EF 01",
            "name": "Andi",
            "duration_s": 7200,          # 2 HOURS! Way too long
            "energy_kwh": 2.5,
            "avg_power_w": 1250,
            "max_power_w": 1800,
            "_label": "ANOMALY: extremely long duration (2 hours)",
        },
        {
            "uid": "AB CD EF 02",
            "name": "Budi",
            "duration_s": 300,           # 5 min but insane power
            "energy_kwh": 0.8,
            "avg_power_w": 5500,         # Way too high!
            "max_power_w": 7000,
            "_label": "ANOMALY: extremely high power (5500W)",
        },
        {
            "uid": "AB CD EF 03",
            "name": "Citra",
            "duration_s": 5400,          # 1.5 hours
            "energy_kwh": 3.0,
            "avg_power_w": 2000,
            "max_power_w": 2500,
            "_label": "ANOMALY: long duration + high power combo",
        },
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  TapCook ML Demo — Simulating ESP32 Sessions")
    print("=" * 60)

    # Check backend is running
    try:
        r = requests.get(f"{API_BASE}/api/ml/status/{DEVICE_ID}", timeout=3)
        status = r.json()
        print(f"\n✅ Backend online — Phase: {status['phase']}, Sessions: {status['session_count']}")
    except Exception as e:
        print(f"\n❌ Backend tidak bisa dihubungi: {e}")
        print("   Pastikan: uvicorn main:app --host 0.0.0.0 --port 8000")
        return

    # Connect MQTT
    print(f"\n📡 Connecting to MQTT broker ({MQTT_HOST}:{MQTT_PORT})...")
    client = mqtt.Client(client_id="tapcook-ml-test")
    try:
        client.connect(MQTT_HOST, MQTT_PORT, 60)
        client.loop_start()
        print("   ✅ MQTT connected")
    except Exception as e:
        print(f"   ❌ MQTT error: {e}")
        return

    # Phase 1: Generate normal sessions
    normal = generate_normal_sessions(55)
    anomalous = generate_anomalous_sessions()

    print(f"\n{'─' * 60}")
    print(f"📊 Akan mengirim {len(normal)} sesi normal + {len(anomalous)} sesi anomali")
    print(f"{'─' * 60}")

    # Send normal sessions
    for i, session in enumerate(normal):
        label = session.pop("_label")
        payload = json.dumps(session)
        client.publish(TOPIC_SESSION, payload)

        # Check phase transitions
        if (i + 1) in [1, 10, 20, 30, 40, 50, 55]:
            time.sleep(1.5)  # Extra wait for DB processing
            r = requests.get(f"{API_BASE}/api/ml/status/{DEVICE_ID}")
            st = r.json()
            phase_icon = {"learning": "🟡", "statistical": "🟠", "ml": "🟢"}.get(st["phase"], "⚪")
            print(f"  [{i+1:3d}/{len(normal)}] {phase_icon} Phase: {st['phase']:12s} | "
                  f"Sessions: {st['session_count']:3d} | {label}")
        else:
            if (i + 1) % 5 == 0:
                print(f"  [{i+1:3d}/{len(normal)}] ✓ {label}")

        time.sleep(0.3)  # Small delay between sessions

    print(f"\n{'─' * 60}")
    print("⚠️  Sekarang mengirim sesi ANOMALI...")
    print(f"{'─' * 60}")
    time.sleep(2)

    # Send anomalous sessions
    for i, session in enumerate(anomalous):
        label = session.pop("_label")
        payload = json.dumps(session)
        print(f"\n  🚨 Sending: {label}")
        print(f"     Payload: duration={session['duration_s']}s, "
              f"avg_power={session['avg_power_w']}W, max={session['max_power_w']}W")
        client.publish(TOPIC_SESSION, payload)
        time.sleep(2)  # Wait for ML processing

    # Check results
    time.sleep(3)
    print(f"\n{'=' * 60}")
    print("📋 HASIL AKHIR")
    print(f"{'=' * 60}")

    # ML Status
    r = requests.get(f"{API_BASE}/api/ml/status/{DEVICE_ID}")
    ml = r.json()
    phase_icon = {"learning": "🟡", "statistical": "🟠", "ml": "🟢"}.get(ml["phase"], "⚪")
    print(f"\n{phase_icon} ML Phase: {ml['phase']}")
    print(f"   Total sessions: {ml['session_count']}")
    if ml.get("trained_on"):
        print(f"   Model trained on: {ml['trained_on']} sessions")

    # Anomalies detected
    r = requests.get(f"{API_BASE}/api/anomalies")
    alerts = r.json()
    print(f"\n🔔 Anomali terdeteksi: {len(alerts)}")
    for a in alerts:
        icon = "🚨" if a["severity"] == "critical" else "⚠️"
        print(f"   {icon} [{a['severity']:8s}] {a['message']}")
        print(f"      Method: {a['alert_type']}, Score: {a['score']}")

    # Session stats
    r = requests.get(f"{API_BASE}/api/sessions/{DEVICE_ID}?limit=5")
    recent = r.json()
    print(f"\n📊 5 Sesi terakhir:")
    for s in recent:
        dur_min = s["duration_s"] / 60
        print(f"   {s['user_name']:8s} | {dur_min:5.1f} min | "
              f"{s['avg_power_w']:6.0f}W avg | {s['energy_kwh']:.4f} kWh")

    # Cleanup
    client.loop_stop()
    client.disconnect()

    print(f"\n{'=' * 60}")
    print("✅ Demo selesai! Buka http://localhost:8000/admin")
    print("   → Tab 🔔 Anomali untuk lihat alert")
    print("   → ML Status bar harus 🟢 ML Active")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
