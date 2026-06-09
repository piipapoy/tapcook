import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from anomaly_detector import AnomalyDetector, extract_features
import numpy as np
from datetime import datetime

d = AnomalyDetector(models_dir='models')
model = d._models.get('esp32_1')
scaler = d._scalers.get('esp32_1')
print(f"Model loaded: {model is not None}")

class FakeSess:
    def __init__(self, dur, energy, avg_p, max_p):
        self.start_time = datetime(2026, 5, 31, 14, 0)
        self.duration_seconds = dur
        self.total_energy_kwh = energy
        self.avg_power_w = avg_p
        self.max_power_w = max_p

tests = [
    ("Normal (15min, 500W)", FakeSess(900, 0.125, 500, 650)),
    ("Anomaly: 2 hours",     FakeSess(7200, 2.5, 1250, 1800)),
    ("Anomaly: 5500W power", FakeSess(300, 0.8, 5500, 7000)),
    ("Anomaly: 1.5h+2000W",  FakeSess(5400, 3.0, 2000, 2500)),
]

for label, sess in tests:
    feat = np.array([extract_features(sess)])
    scaled = scaler.transform(feat)
    score = model.decision_function(scaled)[0]
    pred = model.predict(scaled)[0]
    print(f"  {label:30s} → score={score:+.4f}  pred={pred:+d}  {'⚠️ ANOMALY' if pred == -1 else '✅ Normal'}")

print(f"\nCurrent thresholds: IF_WARNING=-0.15, IF_CRITICAL=-0.30")
print(f"Recommendation: adjust thresholds based on scores above")
