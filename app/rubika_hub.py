"""Connects Rubika channels to users without any manual IDs.

The user adds the platform's Rubika bot to their channel as admin and posts a one-time code
there. We read it from getUpdates, remember the channel's chat_id and delete the code message.
Polling only runs while at least one code is waiting.
"""
import asyncio
import logging
import re
import secrets
import time

from sqlalchemy import select

from . import rubika
from .db import Channel, Session, get_settings

log = logging.getLogger("rubika_hub")

CODE_TTL = 15 * 60
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_RE = re.compile(r"RBK-([A-Z0-9]{5})", re.I)
_pending: dict[str, tuple[int, float]] = {}  # code -> (telegram user id, expires_at)


def new_code(uid: int) -> str:
    for k in [k for k, (u, _) in _pending.items() if u == uid]:
        del _pending[k]
    code = "RBK-" + "".join(secrets.choice(_ALPHABET) for _ in range(5))
    _pending[code] = (uid, time.time() + CODE_TTL)
    return code


def cancel(uid: int) -> None:
    for k in [k for k, (u, _) in _pending.items() if u == uid]:
        del _pending[k]


async def _register(manager, token: str, chat_id: str, message_id: str, uid: int, code: str) -> None:
    info = await rubika.lookup_chat(token, chat_id)
    title = info["title"] or chat_id
    async with Session() as s:
        dup = (await s.execute(select(Channel).where(
            Channel.user_id == uid, Channel.chat_id == chat_id, Channel.platform == "rubika"))).scalar_one_or_none()
        if not dup:
            s.add(Channel(user_id=uid, chat_id=chat_id, title=title[:100], platform="rubika"))
            await s.commit()
    _pending.pop(code, None)
    try:
        await rubika.delete_message(token, chat_id, message_id)
    except rubika.RubikaError:
        pass
    if manager.bot:
        try:
            note = "کانال از قبل متصل بود." if dup else "کانال روبیکا متصل شد."
            await manager.bot.send_message(uid, f"✅ {note}\n<b>{_esc(title)}</b>")
        except Exception:
            pass


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _handle(manager, token: str, upd: dict) -> None:
    msg = upd.get("new_message") or upd.get("updated_message") or {}
    text = msg.get("text") or ""
    m = _CODE_RE.search(text)
    if not m:
        return
    code = "RBK-" + m.group(1).upper()
    owner = _pending.get(code)
    chat_id = str(upd.get("chat_id") or "")
    if not owner or owner[1] < time.time() or chat_id[:1] not in ("c", "g"):
        return  # unknown/expired code, or posted somewhere that is not a channel/group
    await _register(manager, token, chat_id, str(msg.get("message_id") or ""), owner[0], code)


async def hub_loop(manager) -> None:
    offset: str | None = None
    log.info("rubika hub started")
    while True:
        try:
            now = time.time()
            for k in [k for k, (_, exp) in _pending.items() if exp < now]:
                del _pending[k]
            token = (await get_settings())["rubika_bot_token"].strip()
            if not token or not _pending:
                await asyncio.sleep(3)
                continue
            data = await rubika.get_updates(token, offset)
            for upd in data.get("updates") or []:
                if isinstance(upd, dict):
                    await _handle(manager, token, upd)
            if data.get("next_offset_id"):
                offset = str(data["next_offset_id"])
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except rubika.RubikaError as e:
            log.warning("rubika poll: %s", e)
            await asyncio.sleep(8)
        except Exception:
            log.exception("rubika hub error")
            await asyncio.sleep(8)
