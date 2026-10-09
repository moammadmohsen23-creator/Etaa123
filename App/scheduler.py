"""Background loop: sends due posts to Eitaa and notifies the owner."""
import asyncio
import logging
from html import escape

from sqlalchemy import select, update

from . import eitaa
from .crypto import dec
from .db import Channel, Post, Session, User, utcnow

log = logging.getLogger("scheduler")
POLL_SECONDS = 10
RETRIES = 3


async def recover() -> None:
    """Posts stuck in 'sending' after a crash/redeploy go back to the queue."""
    async with Session() as s:
        await s.execute(update(Post).where(Post.status == "sending").values(status="pending"))
        await s.commit()


async def _deliver(post: Post, token: str, chat_id: str, bot) -> dict:
    kw = dict(title=post.title or None, silent=post.silent, pin=post.pin,
              view_delete=post.view_delete or None)
    if post.kind == "text":
        return await eitaa.send_message(token, chat_id, post.text, **kw)
    if bot is None:
        raise eitaa.EitaaError("ربات تلگرام فعال نیست؛ دانلود فایل ممکن نشد")
    buf = await bot.download(post.file_id)
    if buf is None:
        raise eitaa.EitaaError("دانلود فایل از تلگرام ناموفق بود")
    return await eitaa.send_file(token, chat_id, post.file_name or "file", buf.read(),
                                 caption=post.text or None, **kw)


async def process_post(post_id: int, manager) -> None:
    async with Session() as s:
        post = await s.get(Post, post_id)
        user = await s.get(User, post.user_id)
        chan = await s.get(Channel, post.channel_id)
    if not (user and chan):
        await _finish(post_id, "failed", "کاربر یا کانال حذف شده است")
        return
    token = dec(user.eitaa_token_enc)
    if not token:
        await _finish(post_id, "failed", "توکن ایتایار تنظیم نشده است")
        await _notify(manager, post, chan, ok=False, err="توکن ایتایار تنظیم نشده است")
        return

    error = ""
    result: dict = {}
    for attempt in range(1, RETRIES + 1):
        try:
            result = await _deliver(post, token, chan.chat_id, manager.bot)
            error = ""
            break
        except eitaa.EitaaNetworkError as e:
            error = f"خطای شبکه: {e}"
            await asyncio.sleep(5 * attempt)
        except Exception as e:  # EitaaError or anything else: no retry
            error = str(e) or type(e).__name__
            break

    if error:
        await _finish(post_id, "failed", error[:900])
        await _notify(manager, post, chan, ok=False, err=error)
    else:
        mid = ""
        if isinstance(result, dict):
            mid = str(result.get("message_id", ""))
        await _finish(post_id, "sent", "", mid)
        await _notify(manager, post, chan, ok=True)


async def _finish(post_id: int, status: str, error: str, mid: str = "") -> None:
    async with Session() as s:
        p = await s.get(Post, post_id)
        p.status, p.error, p.eitaa_message_id = status, error, mid
        p.sent_at = utcnow() if status == "sent" else None
        await s.commit()


async def _notify(manager, post: Post, chan: Channel, ok: bool, err: str = "") -> None:
    if manager.bot is None:
        return
    label = escape(post.title or (post.text[:40] if post.text else post.file_name) or f"#{post.id}")
    if ok:
        msg = f"✅ پست «{label}» در کانال <b>{escape(chan.title)}</b> ارسال شد."
    else:
        msg = (f"❌ ارسال پست «{label}» به کانال <b>{escape(chan.title)}</b> ناموفق بود.\n"
               f"<code>{escape(err[:500])}</code>")
    try:
        await manager.bot.send_message(post.user_id, msg)
    except Exception:
        pass


async def scheduler_loop(manager) -> None:
    await recover()
    log.info("scheduler started")
    while True:
        try:
            async with Session() as s:
                due = (await s.execute(
                    select(Post).where(Post.status == "pending", Post.run_at <= utcnow())
                    .order_by(Post.run_at).limit(20))).scalars().all()
                ids = [p.id for p in due]
                for p in due:
                    p.status = "sending"
                await s.commit()
            if ids:
                await asyncio.gather(*(process_post(i, manager) for i in ids), return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler error")
        await asyncio.sleep(POLL_SECONDS)
