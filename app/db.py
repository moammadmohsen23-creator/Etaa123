from datetime import datetime, timezone

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, inspect,
                        select, text)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from . import config
from .crypto import dec, enc


def utcnow() -> datetime:
    """Naive UTC datetime (all DB datetimes are naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(128), default="")
    eitaa_token_enc: Mapped[str] = mapped_column(Text, default="")
    eitaa_account: Mapped[str] = mapped_column(String(128), default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    chat_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(128))
    platform: Mapped[str] = mapped_column(String(10), default="eitaa", server_default="eitaa")  # eitaa | rubika


class Post(Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    channel_id: Mapped[int] = mapped_column(Integer, index=True)  # no FK: channels can be deleted
    kind: Mapped[str] = mapped_column(String(8), default="text")  # text | file
    text: Mapped[str] = mapped_column(Text, default="")  # message text or file caption
    file_id: Mapped[str] = mapped_column(String(256), default="")  # telegram file_id
    file_name: Mapped[str] = mapped_column(String(256), default="")
    title: Mapped[str] = mapped_column(String(256), default="")
    silent: Mapped[bool] = mapped_column(Boolean, default=False)
    pin: Mapped[bool] = mapped_column(Boolean, default=False)
    view_delete: Mapped[int] = mapped_column(Integer, default=0)
    run_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    # pending | sending | sent | failed | cancelled
    error: Mapped[str] = mapped_column(Text, default="")
    eitaa_message_id: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    days: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending|approved|rejected
    receipt_file_id: Mapped[str] = mapped_column(String(256), default="")
    receipt_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


engine = create_async_engine(config.DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # tiny migration: databases created before Rubika support lack channels.platform
        cols = await conn.run_sync(lambda c: [x["name"] for x in inspect(c).get_columns("channels")])
        if "platform" not in cols:
            await conn.execute(text("ALTER TABLE channels ADD COLUMN platform VARCHAR(10) DEFAULT 'eitaa'"))


# ---------------------------------------------------------------- settings
DEFAULTS = {
    "bot_token": "",
    "rubika_bot_token": "",
    "admin_ids": "",
    "brand_name": "ربات انتشار",
    "welcome_text": "",
    "support_username": "",
    "card_number": "",
    "card_owner": "",
    "trial_days": "3",
    "plans": "30:150000\n90:400000\n365:1400000",
}
SECRET_KEYS = {"bot_token", "rubika_bot_token"}


async def get_settings(s: AsyncSession | None = None) -> dict[str, str]:
    async def _load(sess: AsyncSession) -> dict[str, str]:
        rows = (await sess.execute(select(Setting))).scalars().all()
        out = dict(DEFAULTS)
        for r in rows:
            out[r.key] = dec(r.value) if r.key in SECRET_KEYS else r.value
        if not out["bot_token"] and config.ENV_BOT_TOKEN:
            out["bot_token"] = config.ENV_BOT_TOKEN
        if not out["rubika_bot_token"] and config.ENV_RUBIKA_TOKEN:
            out["rubika_bot_token"] = config.ENV_RUBIKA_TOKEN
        if not out["admin_ids"] and config.ENV_ADMIN_IDS:
            out["admin_ids"] = config.ENV_ADMIN_IDS
        return out

    if s is not None:
        return await _load(s)
    async with Session() as sess:
        return await _load(sess)


async def save_settings(values: dict[str, str]) -> None:
    async with Session() as s:
        for k, v in values.items():
            if k not in DEFAULTS:
                continue
            v = enc(v) if k in SECRET_KEYS else v
            row = await s.get(Setting, k)
            if row:
                row.value = v
            else:
                s.add(Setting(key=k, value=v))
        await s.commit()


def parse_ids(raw: str) -> set[int]:
    out = set()
    for part in raw.replace("،", ",").replace("\n", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def parse_plans(raw: str) -> list[tuple[int, int]]:
    """'30:150000' per line -> [(days, price_toman)]"""
    plans = []
    for line in raw.splitlines():
        if ":" not in line:
            continue
        d, p = line.split(":", 1)
        d, p = d.strip(), p.strip().replace(",", "")
        if d.isdigit() and p.isdigit() and int(d) > 0:
            plans.append((int(d), int(p)))
    return plans
