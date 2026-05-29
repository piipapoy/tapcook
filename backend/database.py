import os
import string
import random
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Boolean, DateTime, select
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


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("[DB] Database siap (SQLite)")


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