"""
TapCook — Anomaly Detection Module

Uses a 3-phase cold-start strategy:
  Phase 1 (0-19 sessions):  Learning only, no detection
  Phase 2 (20-49 sessions): Statistical Z-score detection
  Phase 3 (50+ sessions):   Isolation Forest ML detection

Each device (kosan) gets its own independent model.
"""

import os
import json
import math
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import joblib
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

log = logging.getLogger("tapcook.ml")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FEATURE_NAMES = [
    "duration_min", "energy_kwh", "avg_power_w", "max_power_w",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    "month_sin", "month_cos",
]

MIN_SESSIONS_STATS = 20   # minimum for statistical detection
MIN_SESSIONS_ML    = 50   # minimum for Isolation Forest
RETRAIN_EVERY      = 10   # retrain after N new sessions

# Z-score thresholds (statistical phase)
ZSCORE_WARNING  = 2.5
ZSCORE_CRITICAL = 3.5

# Isolation Forest score thresholds (negative = more anomalous)
IF_WARNING  = -0.05
IF_CRITICAL = -0.08


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------
def extract_features(session) -> list[float]:
    """Build feature vector from a UsageSession row."""
    st = session.start_time
    hour = st.hour + st.minute / 60.0
    dow  = st.weekday()
    month = st.month

    return [
        session.duration_seconds / 60.0,
        session.total_energy_kwh,
        session.avg_power_w,
        session.max_power_w,
        math.sin(2 * math.pi * hour / 24),
        math.cos(2 * math.pi * hour / 24),
        math.sin(2 * math.pi * dow / 7),
        math.cos(2 * math.pi * dow / 7),
        math.sin(2 * math.pi * month / 12),
        math.cos(2 * math.pi * month / 12),
    ]


def _build_matrix(sessions) -> np.ndarray:
    """Build feature matrix from list of sessions."""
    return np.array([extract_features(s) for s in sessions], dtype=np.float64)


# ---------------------------------------------------------------------------
# AnomalyDetector
# ---------------------------------------------------------------------------
class AnomalyResult:
    """Container for a single anomaly-check result."""
    def __init__(self, is_anomaly: bool, severity: str, score: float,
                 method: str, message: str, details: dict):
        self.is_anomaly = is_anomaly
        self.severity   = severity   # "warning" | "critical"
        self.score      = score
        self.method     = method     # "statistical" | "isolation_forest"
        self.message    = message
        self.details    = details


class AnomalyDetector:
    """Per-device anomaly detection manager."""

    def __init__(self, models_dir: str = "models"):
        self.models_dir = Path(models_dir)
        self.models_dir.mkdir(exist_ok=True)
        self._models:  dict[str, IsolationForest] = {}
        self._scalers: dict[str, StandardScaler]  = {}
        self._trained_counts: dict[str, int]       = {}
        self._load_all()

    # -- persistence --------------------------------------------------------

    def _model_path(self, did: str)  -> Path: return self.models_dir / f"{did}_model.pkl"
    def _scaler_path(self, did: str) -> Path: return self.models_dir / f"{did}_scaler.pkl"
    def _meta_path(self, did: str)   -> Path: return self.models_dir / f"{did}_meta.json"

    def _load_all(self):
        for p in self.models_dir.glob("*_meta.json"):
            did = p.stem.replace("_meta", "")
            try:
                self._models[did]  = joblib.load(self._model_path(did))
                self._scalers[did] = joblib.load(self._scaler_path(did))
                with open(p) as f:
                    self._trained_counts[did] = json.load(f).get("trained_count", 0)
                log.info("Model loaded: %s (%d sessions)", did, self._trained_counts[did])
            except Exception as e:
                log.warning("Failed to load model %s: %s", did, e)

    def _save(self, did: str, count: int):
        joblib.dump(self._models[did],  self._model_path(did))
        joblib.dump(self._scalers[did], self._scaler_path(did))
        with open(self._meta_path(did), "w") as f:
            json.dump({"trained_count": count, "trained_at": datetime.utcnow().isoformat(), "device_id": did}, f)

    # -- training -----------------------------------------------------------

    def train(self, device_id: str, sessions: list):
        """Train / retrain model on all sessions for a device."""
        n = len(sessions)
        if n < MIN_SESSIONS_ML:
            return

        X = _build_matrix(sessions)
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        model = IsolationForest(
            n_estimators=150,
            contamination=0.05,      # assume ~5 % anomaly rate
            max_samples="auto",
            random_state=42,
            n_jobs=-1,
        )
        model.fit(X_scaled)

        self._models[device_id]  = model
        self._scalers[device_id] = scaler
        self._trained_counts[device_id] = n
        self._save(device_id, n)
        log.info("Trained model for %s on %d sessions", device_id, n)

    def _needs_retrain(self, device_id: str, current_count: int) -> bool:
        last = self._trained_counts.get(device_id, 0)
        return (current_count - last) >= RETRAIN_EVERY

    # -- detection ----------------------------------------------------------

    def get_phase(self, session_count: int) -> str:
        if session_count < MIN_SESSIONS_STATS:
            return "learning"
        if session_count < MIN_SESSIONS_ML:
            return "statistical"
        return "ml"

    def check(self, device_id: str, session, all_sessions: list) -> Optional[AnomalyResult]:
        """
        Check a single session for anomalies.
        Returns AnomalyResult if anomalous, else None.
        """
        n = len(all_sessions)
        phase = self.get_phase(n)

        if phase == "learning":
            return None

        features = np.array([extract_features(session)], dtype=np.float64)

        if phase == "statistical":
            return self._check_statistical(features[0], all_sessions)

        # ML phase — retrain if needed
        if device_id not in self._models or self._needs_retrain(device_id, n):
            self.train(device_id, all_sessions)

        return self._check_ml(device_id, features)

    # -- statistical check (phase 2) ---------------------------------------

    def _check_statistical(self, feat: np.ndarray, all_sessions: list) -> Optional[AnomalyResult]:
        """Z-score based detection on first 4 features (duration, energy, avg_power, max_power)."""
        X = _build_matrix(all_sessions)
        numeric_cols = X[:, :4]  # duration, energy, avg, max
        means = numeric_cols.mean(axis=0)
        stds  = numeric_cols.std(axis=0)
        stds[stds == 0] = 1  # prevent division by zero

        z_scores = np.abs((feat[:4] - means) / stds)
        max_z = float(z_scores.max())
        max_idx = int(z_scores.argmax())
        feature_name = FEATURE_NAMES[max_idx]

        if max_z >= ZSCORE_CRITICAL:
            return AnomalyResult(
                is_anomaly=True, severity="critical", score=max_z,
                method="statistical",
                message=f"Penggunaan sangat tidak biasa! ({feature_name} z={max_z:.1f})",
                details={"z_scores": {FEATURE_NAMES[i]: round(float(z_scores[i]), 2) for i in range(4)},
                         "max_feature": feature_name},
            )
        if max_z >= ZSCORE_WARNING:
            return AnomalyResult(
                is_anomaly=True, severity="warning", score=max_z,
                method="statistical",
                message=f"Penggunaan agak tidak biasa ({feature_name} z={max_z:.1f})",
                details={"z_scores": {FEATURE_NAMES[i]: round(float(z_scores[i]), 2) for i in range(4)},
                         "max_feature": feature_name},
            )
        return None

    # -- ML check (phase 3) ------------------------------------------------

    def _check_ml(self, device_id: str, features: np.ndarray) -> Optional[AnomalyResult]:
        """Isolation Forest based detection."""
        model  = self._models[device_id]
        scaler = self._scalers[device_id]

        X_scaled = scaler.transform(features)
        raw_score = float(model.decision_function(X_scaled)[0])
        prediction = int(model.predict(X_scaled)[0])  # 1=normal, -1=anomaly

        if raw_score <= IF_CRITICAL:
            return AnomalyResult(
                is_anomaly=True, severity="critical", score=raw_score,
                method="isolation_forest",
                message="Penggunaan sangat tidak biasa terdeteksi!",
                details={"if_score": round(raw_score, 4), "prediction": prediction},
            )
        if raw_score <= IF_WARNING:
            return AnomalyResult(
                is_anomaly=True, severity="warning", score=raw_score,
                method="isolation_forest",
                message="Penggunaan agak tidak biasa terdeteksi",
                details={"if_score": round(raw_score, 4), "prediction": prediction},
            )
        return None

    # -- status info --------------------------------------------------------

    def status(self, device_id: str, session_count: int) -> dict:
        phase = self.get_phase(session_count)
        info = {
            "device_id": device_id,
            "phase": phase,
            "session_count": session_count,
            "min_stats": MIN_SESSIONS_STATS,
            "min_ml": MIN_SESSIONS_ML,
        }
        if phase == "learning":
            info["progress_pct"] = round(session_count / MIN_SESSIONS_STATS * 100, 1)
            info["remaining"] = MIN_SESSIONS_STATS - session_count
        elif phase == "statistical":
            info["progress_pct"] = round(session_count / MIN_SESSIONS_ML * 100, 1)
            info["remaining"] = MIN_SESSIONS_ML - session_count
        else:
            info["trained_on"] = self._trained_counts.get(device_id, 0)
            info["progress_pct"] = 100
        return info
