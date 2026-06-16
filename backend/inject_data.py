"""
Inject synthetic cooking session data for anomaly detection training.
Generates ~120 realistic sessions mimicking daily cooking patterns.
"""
import sqlite3
import random
import math
from datetime import datetime, timedelta

random.seed(42)

DB_PATH = "backend/tapcook.db"
DEVICE_ID = "esp32_1"
UID = "C2 A7 8C 02"
NAME = "Rafi"

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Ensure table exists
c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='usage_sessions'")
if not c.fetchone():
    print("ERROR: usage_sessions table not found")
    conn.close()
    exit(1)

# Delete old sessions
c.execute("DELETE FROM usage_sessions")
c.execute("DELETE FROM anomaly_alerts")
print("Old data cleared")

# Generate sessions over the past 30-60 days
now = datetime.utcnow()
sessions = []

# Cooking patterns: meal times with realistic power/energy
MEAL_TIMES = [
    (7, 0, 0.8), (7, 15, 0.85), (7, 30, 0.9), (7, 45, 0.85),  # breakfast
    (12, 0, 0.9), (12, 15, 0.95), (12, 30, 0.9), (12, 45, 0.85),  # lunch
    (18, 0, 0.95), (18, 15, 1.0), (18, 30, 0.95), (18, 45, 0.9),  # dinner
    (20, 0, 0.7), (20, 15, 0.7),  # evening snack
]

for day_offset in range(60):
    day = now - timedelta(days=day_offset)
    # Skip some days (weekend pattern: more cooking)
    if day.weekday() >= 5:
        # Weekend: 3-5 meals
        num_meals = random.choices([2, 3, 4, 5], weights=[1, 2, 3, 2])[0]
    else:
        # Weekday: 1-3 meals
        num_meals = random.choices([0, 1, 2, 3], weights=[1, 3, 4, 2])[0]

    chosen = random.sample(MEAL_TIMES, min(num_meals, len(MEAL_TIMES)))

    for hour, minute, intensity in chosen:
        # Randomize start time within meal window
        start_h = hour + random.uniform(-0.3, 0.3)
        start_min = int(start_h * 60)
        h = start_min // 60
        m = start_min % 60

        start = day.replace(hour=h % 24, minute=m, second=random.randint(0, 59))

        # Duration: most cooking 5-40 min, occasional long session
        dur = random.choices([
            random.randint(60, 300),     # 1-5 min (quick boil)
            random.randint(300, 900),     # 5-15 min
            random.randint(900, 2400),    # 15-40 min
            random.randint(2400, 3600),   # 40-60 min (slow cook)
        ], weights=[15, 40, 35, 10])[0]

        end = start + timedelta(seconds=dur)

        # Power: 150-900W typical for cooking
        base_power = random.choices([
            random.uniform(150, 350),    # low (rice cooker warm)
            random.uniform(350, 600),    # medium
            random.uniform(600, 900),    # high (frying)
        ], weights=[20, 50, 30])[0]

        avg_power = base_power * intensity * random.uniform(0.85, 1.0)
        max_power = avg_power * random.uniform(1.1, 1.4)
        # Don't let max_power exceed reasonable limit
        max_power = min(max_power, 1100)

        # Energy: avg_power * duration / 3600000 to get kWh
        # But add some variation
        energy = (avg_power * dur) / 3600000.0 * random.uniform(0.9, 1.05)

        # Round values
        dur_s = int(dur)
        energy_kwh = round(energy, 4)
        avg_p = round(avg_power, 1)
        max_p = round(max_power, 1)

        sessions.append((
            DEVICE_ID, UID, NAME,
            start.isoformat(), end.isoformat(),
            dur_s, energy_kwh, avg_p, max_p,
        ))

# Add a few anomaly sessions (very long, very high power)
for i in range(3):
    day = now - timedelta(days=random.randint(0, 20))
    h = random.choice([2, 3, 14, 15])  # unusual hours
    start = day.replace(hour=h, minute=random.randint(0, 59))
    dur = random.randint(3600, 7200)  # 1-2 hours
    end = start + timedelta(seconds=dur)
    energy = round(random.uniform(0.8, 2.0), 4)
    sessions.append((
        DEVICE_ID, "ANOMALY_01", "Unknown",
        start.isoformat(), end.isoformat(), dur, energy, 950.0, 1200.0,
    ))

# Insert all sessions
c.executemany(
    "INSERT INTO usage_sessions (device_id, user_uid, user_name, start_time, end_time, duration_seconds, total_energy_kwh, avg_power_w, max_power_w) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
    sessions
)
conn.commit()

total = c.execute("SELECT COUNT(*) FROM usage_sessions").fetchone()[0]
print(f"Inserted {len(sessions)} sessions. Total: {total}")

# Show sample
print("\nSample sessions:")
for row in c.execute("SELECT id, start_time, duration_seconds, total_energy_kwh, avg_power_w, max_power_w FROM usage_sessions ORDER BY id DESC LIMIT 8").fetchall():
    print(f"  #{row[0]} | {row[1][:16]} | {row[2]}s | {row[3]:.4f}kWh | {row[4]:.0f}W avg | {row[5]:.0f}W max")

conn.close()
