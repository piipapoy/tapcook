import os
import string
import random
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Boolean, DateTime, Float, Integer, Text, select, func, desc
from typing import Optional

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///tapcook.db")

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine)


class Base(DeclarativeBase):
    pass


class Config(Base):
    __tablename__ = "config"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(String(256), nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: datetime.utcnow()
    )


class PendingRegistration(Base):
    __tablename__ = "pending_registrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(6), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------------------
# NEW: Usage session & anomaly models
# ---------------------------------------------------------------------------

class UsageSession(Base):
    """One relay ON→OFF cycle."""
    __tablename__ = "usage_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_uid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    total_energy_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    avg_power_w: Mapped[float] = mapped_column(Float, default=0.0)
    max_power_w: Mapped[float] = mapped_column(Float, default=0.0)


class AnomalyAlert(Base):
    """Detected anomaly for a usage session."""
    __tablename__ = "anomaly_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)   # "statistical" | "isolation_forest"
    severity: Mapped[str] = mapped_column(String(16), nullable=False)     # "warning" | "critical"
    score: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(String(512), nullable=False)
    details_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), default=lambda: datetime.utcnow()
    )


# ---------------------------------------------------------------------------
# Database init
# ---------------------------------------------------------------------------

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("[DB] Database siap (SQLite)")


# ---------------------------------------------------------------------------
# User / Pending helpers (unchanged)
# ---------------------------------------------------------------------------

async def get_user_by_uid(uid: str) -> Optional[User]:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.uid == uid))
        return result.scalar_one_or_none()


def _utcnow():
    return datetime.utcnow()

async def create_pending(uid: str) -> PendingRegistration:
    code = "".join(random.choices(string.digits, k=6))
    expires_at = _utcnow() + timedelta(minutes=10)
    async with async_session() as session:
        reg = PendingRegistration(uid=uid, code=code, expires_at=expires_at)
        session.add(reg)
        await session.commit()
        await session.refresh(reg)
        return reg


async def redeem_code(code: str, name: str) -> Optional[User]:
    async with async_session() as session:
        result = await session.execute(
            select(PendingRegistration).where(
                PendingRegistration.code == code,
                PendingRegistration.used == False,
                PendingRegistration.expires_at > _utcnow(),
            )
        )
        reg = result.scalar_one_or_none()
        if not reg:
            return None
        reg.used = True
        user = User(uid=reg.uid, name=name)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def get_active_pending_by_uid(uid: str) -> Optional[PendingRegistration]:
    async with async_session() as session:
        result = await session.execute(
            select(PendingRegistration).where(
                PendingRegistration.uid == uid,
                PendingRegistration.used == False,
                PendingRegistration.expires_at > _utcnow(),
            )
        )
        return result.scalar_one_or_none()


async def get_all_users():
    async with async_session() as session:
        result = await session.execute(select(User).order_by(User.created_at.desc()))
        return result.scalars().all()


async def get_pending_list():
    async with async_session() as session:
        result = await session.execute(
            select(PendingRegistration).where(
                PendingRegistration.used == False,
                PendingRegistration.expires_at > _utcnow(),
            )
        )
        return result.scalars().all()


async def get_config(key: str, default: str = "") -> str:
    async with async_session() as session:
        result = await session.execute(select(Config).where(Config.key == key))
        row = result.scalar_one_or_none()
        return row.value if row else default


async def set_config(key: str, value: str) -> None:
    async with async_session() as session:
        result = await session.execute(select(Config).where(Config.key == key))
        row = result.scalar_one_or_none()
        if row:
            row.value = value
        else:
            session.add(Config(key=key, value=value))
        await session.commit()


async def delete_user_by_uid(uid: str) -> bool:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.uid == uid))
        user = result.scalar_one_or_none()
        if not user:
            return False
        await session.delete(user)
        await session.commit()
        return True


# ---------------------------------------------------------------------------
# NEW: Usage session CRUD
# ---------------------------------------------------------------------------

async def create_usage_session(
    device_id: str, user_uid: Optional[str], user_name: Optional[str],
    start_time: datetime, end_time: datetime, duration_seconds: int,
    total_energy_kwh: float, avg_power_w: float, max_power_w: float,
) -> UsageSession:
    async with async_session() as session:
        s = UsageSession(
            device_id=device_id, user_uid=user_uid, user_name=user_name,
            start_time=start_time, end_time=end_time,
            duration_seconds=duration_seconds,
            total_energy_kwh=total_energy_kwh,
            avg_power_w=avg_power_w, max_power_w=max_power_w,
        )
        session.add(s)
        await session.commit()
        await session.refresh(s)
        return s


async def get_sessions_by_device(device_id: str, limit: Optional[int] = None):
    async with async_session() as session:
        q = select(UsageSession).where(UsageSession.device_id == device_id).order_by(UsageSession.start_time.asc())
        if limit:
            q = q.limit(limit)
        result = await session.execute(q)
        return result.scalars().all()


async def get_session_count(device_id: str) -> int:
    async with async_session() as session:
        result = await session.execute(
            select(func.count(UsageSession.id)).where(UsageSession.device_id == device_id)
        )
        return result.scalar() or 0


async def get_recent_sessions(device_id: str, limit: int = 20):
    async with async_session() as session:
        q = (select(UsageSession)
             .where(UsageSession.device_id == device_id)
             .order_by(desc(UsageSession.start_time))
             .limit(limit))
        result = await session.execute(q)
        return result.scalars().all()


# ---------------------------------------------------------------------------
# NEW: Anomaly alert CRUD
# ---------------------------------------------------------------------------

async def create_anomaly_alert(
    device_id: str, session_id: Optional[int], alert_type: str,
    severity: str, score: float, message: str, details_json: str = "{}",
) -> AnomalyAlert:
    async with async_session() as session:
        a = AnomalyAlert(
            device_id=device_id, session_id=session_id,
            alert_type=alert_type, severity=severity,
            score=score, message=message, details_json=details_json,
        )
        session.add(a)
        await session.commit()
        await session.refresh(a)
        return a


async def get_anomaly_alerts(device_id: Optional[str] = None, limit: int = 50):
    async with async_session() as session:
        q = select(AnomalyAlert).order_by(desc(AnomalyAlert.created_at))
        if device_id:
            q = q.where(AnomalyAlert.device_id == device_id)
        q = q.limit(limit)
        result = await session.execute(q)
        return result.scalars().all()


async def get_unacknowledged_count(device_id: Optional[str] = None) -> int:
    async with async_session() as session:
        q = select(func.count(AnomalyAlert.id)).where(AnomalyAlert.acknowledged == False)
        if device_id:
            q = q.where(AnomalyAlert.device_id == device_id)
        result = await session.execute(q)
        return result.scalar() or 0


async def acknowledge_alert(alert_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(select(AnomalyAlert).where(AnomalyAlert.id == alert_id))
        alert = result.scalar_one_or_none()
        if not alert:
            return False
        alert.acknowledged = True
        await session.commit()
        return True