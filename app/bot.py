"""Telegram bot (aiogram 3). Users connect their own Eitaa token, add channels,
send/schedule posts, buy subscriptions. Admins manage via commands + the web panel."""
import asyncio
import contextlib
import logging
import re
from datetime import timedelta
from html import escape

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from . import eitaa
from .botutil import (extract_content, fmt_dt, norm_digits, normalize_chat_id, parse_when,
                      to_utc_naive)
from .crypto import enc
from .db import (Channel, Payment, Post, Session, User, get_settings, parse_ids, parse_plans,
                 utcnow)

log = logging.getLogger("bot")

# ------------------------------------------------------------------ menus
B_NEW, B_SCHED = "📝 پست جدید", "📅 زمان‌بندی‌شده‌ها"
B_CHAN, B_TOKEN = "📢 کانال‌ها", "🔑 توکن ایتایار"
B_BUY, B_ME = "💳 خرید اشتراک", "👤 حساب من"
B_SUP, B_HELP = "☎️ پشتیبانی", "📖 راهنما"
B_ADMIN = "🛠 مدیریت"
MENU_TEXTS = {B_NEW, B_SCHED, B_CHAN, B_TOKEN, B_BUY, B_ME, B_SUP, B_HELP, B_ADMIN}


def menu_kb(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=B_NEW), KeyboardButton(text=B_SCHED)],
            [KeyboardButton(text=B_CHAN), KeyboardButton(text=B_TOKEN)],
            [KeyboardButton(text=B_BUY), KeyboardButton(text=B_ME)],
            [KeyboardButton(text=B_SUP), KeyboardButton(text=B_HELP)]]
    if is_admin:
        rows.append([KeyboardButton(text=B_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def ikb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for row in rows:
        b.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    return b.as_markup()


# ------------------------------------------------------------------ states
class SetToken(StatesGroup):
    token = State()


class AddChannel(StatesGroup):
    chat_id = State()
    title = State()


class NewPost(StatesGroup):
    content = State()
    menu = State()
    title = State()
    view = State()
    when = State()


class Pay(StatesGroup):
    receipt = State()


# ------------------------------------------------------------------ guard
class Guard(BaseMiddleware):
    """Registers/updates the user, blocks banned users, injects `is_admin` and `cfg`."""

    async def __call__(self, handler, event, data):
        tg = data.get("event_from_user")
        if tg is None:
            return await handler(event, data)
        cfg = await get_settings()
        admins = parse_ids(cfg["admin_ids"])
        async with Session() as s:
            u = await s.get(User, tg.id)
            if not u:
                td = cfg["trial_days"]
                trial = int(td) if td.isdigit() else 0
                u = User(id=tg.id, expires_at=(utcnow() + timedelta(days=trial)) if trial else None)
                s.add(u)
            u.username = tg.username or ""
            u.name = tg.full_name or ""
            try:
                await s.commit()
            except IntegrityError:  # two updates of a brand-new user raced; the other one won
                await s.rollback()
                u = await s.get(User, tg.id)
        is_admin = tg.id in admins
        if u.banned and not is_admin:
            if isinstance(event, CallbackQuery):
                await event.answer("دسترسی شما مسدود شده است.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("⛔️ دسترسی شما مسدود شده است.")
            return None
        data["is_admin"] = is_admin
        data["cfg"] = cfg
        return await handler(event, data)


# ------------------------------------------------------------------ helpers
async def get_user(uid: int) -> User | None:
    async with Session() as s:
        return await s.get(User, uid)


def has_access(u: User | None, is_admin: bool) -> bool:
    return bool(is_admin or (u and u.expires_at and u.expires_at > utcnow()))


async def user_channels(uid: int) -> list[Channel]:
    async with Session() as s:
        return list((await s.execute(
            select(Channel).where(Channel.user_id == uid).order_by(Channel.id))).scalars())


def money(n: int) -> str:
    return f"{n:,}"


def support_line(cfg: dict) -> str:
    sup = cfg["support_username"].lstrip("@").strip()
    return f"@{sup}" if sup else "مدیر ربات"


async def safe_edit(msg: Message, text: str, kb=None) -> None:
    with contextlib.suppress(TelegramBadRequest):
        await msg.edit_text(text, reply_markup=kb)


# ------------------------------------------------------------------ basics
async def cmd_start(m: Message, state: FSMContext, is_admin: bool, cfg: dict):
    await state.clear()
    brand = escape(cfg["brand_name"])
    welcome = cfg["welcome_text"].strip() or (
        "با این ربات می‌توانید متن، عکس، فیلم و فایل را به کانال‌های ایتا ارسال یا زمان‌بندی کنید.")
    await m.answer(f"👋 به <b>{brand}</b> خوش آمدید!\n\n{escape(welcome)}\n\n"
                   "برای شروع: ۱) توکن ایتایار را ثبت کنید ۲) کانال را اضافه کنید ۳) «پست جدید» را بزنید.",
                   reply_markup=menu_kb(is_admin))


async def cmd_cancel(m: Message, state: FSMContext, is_admin: bool):
    await state.clear()
    await m.answer("لغو شد.", reply_markup=menu_kb(is_admin))


async def cmd_id(m: Message):
    await m.answer(f"آیدی عددی شما: <code>{m.from_user.id}</code>")


async def btn_help(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "📖 <b>راهنما</b>\n\n"
        "🔑 <b>توکن:</b> در پنل <b>eitaayar.ir</b> ثبت‌نام کنید و توکن API را از آن‌جا بردارید و در بخش «توکن ایتایار» بفرستید.\n"
        "📢 <b>کانال:</b> شناسه عددی یا یوزرنیم کانال (بدون @) را همان‌طور که در پنل ایتایار هست اضافه کنید. "
        "کانال باید در پنل ایتایار تعریف شده باشد.\n"
        "📝 <b>پست:</b> متن یا فایل را بفرستید، گزینه‌ها (عنوان، بی‌صدا، سنجاق، حذف با بازدید) را تنظیم و «ارسال» یا «زمان‌بندی» را بزنید.\n\n"
        "⏰ <b>فرمت زمان‌بندی (به وقت تهران):</b>\n"
        "<code>18:30</code> · <code>فردا 09:00</code> · <code>+45</code> (۴۵ دقیقه بعد) · "
        "<code>+2 ساعت</code> · <code>1405/07/20 18:30</code>\n\n"
        "/cancel لغو عملیات · /id آیدی عددی شما")


async def btn_support(m: Message, state: FSMContext, cfg: dict):
    await state.clear()
    await m.answer(f"☎️ پشتیبانی: {support_line(cfg)}")


async def btn_me(m: Message, state: FSMContext, is_admin: bool):
    await state.clear()
    u = await get_user(m.from_user.id)
    chans = await user_channels(m.from_user.id)
    async with Session() as s:
        pend = (await s.execute(select(func.count()).select_from(Post).where(
            Post.user_id == m.from_user.id, Post.status == "pending"))).scalar_one()
        sent = (await s.execute(select(func.count()).select_from(Post).where(
            Post.user_id == m.from_user.id, Post.status == "sent"))).scalar_one()
    if is_admin:
        sub = "♾ مدیر (نامحدود)"
    elif has_access(u, False):
        sub = f"✅ فعال تا {fmt_dt(u.expires_at)}"
    else:
        sub = "❌ منقضی / غیرفعال"
    tok = f"✅ متصل ({escape(u.eitaa_account)})" if u.eitaa_token_enc else "❌ ثبت نشده"
    await m.answer(f"👤 <b>حساب من</b>\n\nآیدی: <code>{u.id}</code>\nاشتراک: {sub}\nتوکن ایتایار: {tok}\n"
                   f"کانال‌ها: {len(chans)}\nدر صف ارسال: {pend}\nارسال‌شده: {sent}")


# ------------------------------------------------------------------ token
async def btn_token(m: Message, state: FSMContext):
    await state.clear()
    u = await get_user(m.from_user.id)
    if u.eitaa_token_enc:
        kb = ikb([[("🔄 تغییر توکن", "tk:set"), ("🗑 حذف توکن", "tk:del")]])
        await m.answer(f"🔑 توکن ایتایار متصل است: <b>{escape(u.eitaa_account)}</b>", reply_markup=kb)
    else:
        kb = ikb([[("➕ ثبت توکن", "tk:set")]])
        await m.answer("🔑 هنوز توکنی ثبت نکرده‌اید.\nتوکن API خود را از پنل eitaayar.ir بردارید و ثبت کنید.",
                       reply_markup=kb)


async def cb_token_set(c: CallbackQuery, state: FSMContext):
    await state.set_state(SetToken.token)
    await c.message.answer("توکن ایتایار را بفرستید (فقط خود توکن یا لینک کامل API).\n"
                           "بعد از بررسی، پیام شما برای امنیت حذف می‌شود. لغو: /cancel")
    await c.answer()


async def cb_token_del(c: CallbackQuery):
    async with Session() as s:
        u = await s.get(User, c.from_user.id)
        u.eitaa_token_enc, u.eitaa_account = "", ""
        await s.commit()
    await safe_edit(c.message, "🗑 توکن حذف شد.")
    await c.answer()


async def st_token(m: Message, state: FSMContext, is_admin: bool):
    raw = (m.text or "").strip()
    mm = re.search(r"/api/([^/\s?]+)", raw)
    token = mm.group(1) if mm else raw
    with contextlib.suppress(Exception):
        await m.delete()
    if not token or " " in token or len(token) < 8:
        await m.answer("❌ توکن نامعتبر است. دوباره بفرستید یا /cancel بزنید.")
        return
    try:
        info = await eitaa.get_me(token)
    except eitaa.EitaaNetworkError as e:
        await m.answer(f"⚠️ اتصال به ایتایار برقرار نشد:\n<code>{escape(str(e)[:300])}</code>")
        return
    except eitaa.EitaaError as e:
        await m.answer(f"❌ توکن پذیرفته نشد:\n<code>{escape(str(e)[:300])}</code>")
        return
    name = ""
    if isinstance(info, dict):
        name = info.get("username") or info.get("first_name") or ""
    name = name or "OK"
    async with Session() as s:
        u = await s.get(User, m.from_user.id)
        u.eitaa_token_enc, u.eitaa_account = enc(token), str(name)[:120]
        await s.commit()
    await state.clear()
    await m.answer(f"✅ توکن متصل شد: <b>{escape(str(name))}</b>", reply_markup=menu_kb(is_admin))


# ------------------------------------------------------------------ channels
def _chan_kb(chans: list[Channel]) -> InlineKeyboardMarkup:
    rows = [[(f"🗑 {c.title}", f"ch:del:{c.id}")] for c in chans]
    rows.append([("➕ افزودن کانال", "ch:add")])
    return ikb(rows)


async def btn_channels(m: Message, state: FSMContext):
    await state.clear()
    chans = await user_channels(m.from_user.id)
    if chans:
        lines = "\n".join(f"• <b>{escape(c.title)}</b> — <code>{escape(c.chat_id)}</code>" for c in chans)
        txt = f"📢 <b>کانال‌های شما</b>\n\n{lines}\n\nبرای حذف روی نام کانال بزنید."
    else:
        txt = "📢 هنوز کانالی اضافه نکرده‌اید."
    await m.answer(txt, reply_markup=_chan_kb(chans))


async def cb_chan_add(c: CallbackQuery, state: FSMContext):
    chans = await user_channels(c.from_user.id)
    if len(chans) >= 20:
        await c.answer("حداکثر ۲۰ کانال مجاز است.", show_alert=True)
        return
    await state.set_state(AddChannel.chat_id)
    await c.message.answer("شناسه کانال/گروه را بفرستید:\n• شناسه عددی (مثل <code>1404</code>)\n"
                           "• یا یوزرنیم بدون @ (مثل <code>eitaayar</code>)\n"
                           "• برای گروه: لینک دعوت\n\nلغو: /cancel")
    await c.answer()


KIND_FA = {"channel": "📢 کانال", "group": "👥 گروه", "private": "🔒 گروه/کانال خصوصی",
           "unknown": "❓ نوع نامشخص"}


async def _save_channel(uid: int, chat_id: str, title: str) -> None:
    async with Session() as s:
        s.add(Channel(user_id=uid, chat_id=chat_id, title=title[:100] or chat_id))
        await s.commit()


async def st_chan_id(m: Message, state: FSMContext):
    cid = normalize_chat_id(m.text or "")
    if not cid or len(cid) > 120:
        await m.answer("❌ شناسه نامعتبر است. دوباره بفرستید.")
        return
    wait = await m.answer("🔎 در حال بررسی...")
    info = await eitaa.lookup_chat(cid)
    await state.update_data(chat_id=cid, found_title=info["title"])
    safe_id = escape(cid)
    if info["exists"] is True:
        txt = (f"✅ پیدا شد\n\nنوع: {KIND_FA.get(info['kind'], KIND_FA['unknown'])}\n"
               f"نام: <b>{escape(info['title'])}</b>\nشناسه: <code>{safe_id}</code>")
        if info["desc"]:
            txt += f"\n\n{escape(info['desc'])}"
        txt += ("\n\n⚠️ برای ارسال، ایتایار باید در این کانال/گروه <b>ادمین</b> باشد؛ "
                "این بررسی فقط وجود آن را تأیید می‌کند.")
        kb = ikb([[("✅ افزودن", "ch:ok")], [("✏️ نام دلخواه", "ch:rename"), ("❌ لغو", "ch:cancel")]])
    elif info["exists"] is False:
        txt = (f"❌ کانال یا گروهی با شناسه <code>{safe_id}</code> پیدا نشد.\n\n"
               "شناسه/لینک را بررسی کنید و دوباره بفرستید، یا اگر مطمئن هستید با همین شناسه اضافه کنید.")
        kb = ikb([[("افزودن با همین شناسه", "ch:force")], [("❌ لغو", "ch:cancel")]])
    else:
        txt = (f"⚠️ نتوانستم این شناسه را بررسی کنم (<code>{safe_id}</code>). "
               "شناسه‌ی عددی یا خطای اتصال ممکن است دلیل باشد.\n"
               "اگر مطمئن هستید اضافه کنید، یا شناسه‌ی دیگری بفرستید.")
        kb = ikb([[("افزودن بدون بررسی", "ch:force")], [("❌ لغو", "ch:cancel")]])
    with contextlib.suppress(Exception):
        await wait.delete()
    await m.answer(txt, reply_markup=kb)


async def cb_chan_ok(c: CallbackQuery, state: FSMContext, is_admin: bool):
    d = await state.get_data()
    if not d.get("chat_id"):
        await c.answer("منقضی شد؛ دوباره شروع کنید.", show_alert=True)
        return
    title = d.get("found_title") or d["chat_id"]
    await _save_channel(c.from_user.id, d["chat_id"], title)
    await state.clear()
    await safe_edit(c.message, f"✅ «{escape(title)}» اضافه شد.")
    await c.message.answer("از منو ادامه دهید 👇", reply_markup=menu_kb(is_admin))
    await c.answer()


async def cb_chan_rename(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddChannel.title)
    await c.message.answer("یک نام دلخواه بفرستید (یا «-» برای نام پیش‌فرض):")
    await c.answer()


async def cb_chan_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c.message, "لغو شد.")
    await c.answer()


async def st_chan_title(m: Message, state: FSMContext, is_admin: bool):
    d = await state.get_data()
    t = (m.text or "").strip()
    default = d.get("found_title") or d["chat_id"]
    title = default if t in ("-", "") else t[:100]
    await _save_channel(m.from_user.id, d["chat_id"], title)
    await state.clear()
    await m.answer(f"✅ کانال «{escape(title)}» اضافه شد.", reply_markup=menu_kb(is_admin))


async def cb_chan_del(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    async with Session() as s:
        ch = await s.get(Channel, cid)
        if ch and ch.user_id == c.from_user.id:
            await s.execute(Post.__table__.update().where(
                Post.channel_id == cid, Post.status == "pending").values(status="cancelled"))
            await s.execute(delete(Channel).where(Channel.id == cid))
            await s.commit()
    chans = await user_channels(c.from_user.id)
    await safe_edit(c.message, "🗑 حذف شد (پست‌های زمان‌بندی‌شده‌ی آن هم لغو شدند).", _chan_kb(chans))
    await c.answer()


# ------------------------------------------------------------------ new post
def builder_view(d: dict) -> tuple[str, InlineKeyboardMarkup]:
    if d["kind"] == "text":
        prev = escape(d["text"][:200]) + ("…" if len(d["text"]) > 200 else "")
        what = f"📝 متن:\n{prev}"
    else:
        cap = escape(d["text"][:120])
        what = f"📎 فایل: <code>{escape(d['file_name'])}</code>" + (f"\nکپشن: {cap}" if cap else "")
    on = lambda v: "روشن ✅" if v else "خاموش"  # noqa: E731
    txt = (f"{what}\n\n📢 کانال: <b>{escape(d['channel_title'])}</b>\n"
           f"🏷 عنوان (فقط پنل ایتایار): {escape(d['title']) or '—'}\n"
           f"🔕 بی‌صدا: {on(d['silent'])}\n📌 سنجاق: {on(d['pin'])}\n"
           f"👁 حذف خودکار با بازدید: {d['view'] or 'غیرفعال'}")
    kb = ikb([[("🚀 ارسال الان", "np:now"), ("📅 زمان‌بندی", "np:sched")],
              [("🏷 عنوان", "np:title"), ("👁 حذف با بازدید", "np:view")],
              [(f"🔕 بی‌صدا: {on(d['silent'])}", "np:silent"), (f"📌 سنجاق: {on(d['pin'])}", "np:pin")],
              [("❌ انصراف", "np:cancel")]])
    return txt, kb


async def btn_new(m: Message, state: FSMContext, is_admin: bool, cfg: dict):
    await state.clear()
    u = await get_user(m.from_user.id)
    if not has_access(u, is_admin):
        await m.answer("⛔️ اشتراک شما فعال نیست. از «💳 خرید اشتراک» تمدید کنید.")
        return
    if not u.eitaa_token_enc:
        await m.answer("ابتدا از «🔑 توکن ایتایار» توکن خود را ثبت کنید.")
        return
    chans = await user_channels(m.from_user.id)
    if not chans:
        await m.answer("ابتدا از «📢 کانال‌ها» حداقل یک کانال اضافه کنید.")
        return
    if len(chans) == 1:
        await state.update_data(channel_id=chans[0].id, channel_title=chans[0].title)
        await state.set_state(NewPost.content)
        await m.answer(f"کانال: <b>{escape(chans[0].title)}</b>\n\nمتن یا فایل (عکس/فیلم/صوت/سند/گیف) را بفرستید.\nلغو: /cancel")
        return
    await m.answer("کدام کانال؟", reply_markup=ikb([[(c.title, f"np:ch:{c.id}")] for c in chans]))


async def cb_np_channel(c: CallbackQuery, state: FSMContext):
    cid = int(c.data.split(":")[2])
    async with Session() as s:
        ch = await s.get(Channel, cid)
    if not ch or ch.user_id != c.from_user.id:
        await c.answer("کانال پیدا نشد.", show_alert=True)
        return
    await state.update_data(channel_id=ch.id, channel_title=ch.title)
    await state.set_state(NewPost.content)
    await safe_edit(c.message, f"کانال: <b>{escape(ch.title)}</b>\n\nمتن یا فایل را بفرستید.\nلغو: /cancel")
    await c.answer()


async def st_np_content(m: Message, state: FSMContext):
    ex = extract_content(m)
    if ex is None:
        await m.answer("این نوع پیام پشتیبانی نمی‌شود. متن، عکس، فیلم، صوت، سند یا گیف بفرستید.")
        return
    if "error" in ex:
        await m.answer("❌ " + ex["error"])
        return
    await state.update_data(**ex, title="", silent=False, pin=False, view=0)
    await state.set_state(NewPost.menu)
    d = await state.get_data()
    txt, kb = builder_view(d)
    await m.answer(txt, reply_markup=kb)


async def cb_np_toggle(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[1]
    d = await state.get_data()
    await state.update_data(**{key: not d[key]})
    d = await state.get_data()
    txt, kb = builder_view(d)
    await safe_edit(c.message, txt, kb)
    await c.answer()


async def cb_np_title(c: CallbackQuery, state: FSMContext):
    await state.set_state(NewPost.title)
    await c.message.answer("عنوان را بفرستید (فقط برای جست‌وجو در پنل ایتایار؛ «-» برای حذف):")
    await c.answer()


async def st_np_title(m: Message, state: FSMContext):
    t = (m.text or "").strip()
    await state.update_data(title="" if t == "-" else t[:200])
    await state.set_state(NewPost.menu)
    txt, kb = builder_view(await state.get_data())
    await m.answer(txt, reply_markup=kb)


async def cb_np_view(c: CallbackQuery, state: FSMContext):
    await state.set_state(NewPost.view)
    await c.message.answer("با رسیدن تعداد بازدید به چه عددی پست حذف شود؟ (عدد بفرستید؛ ۰ = غیرفعال)")
    await c.answer()


async def st_np_view(m: Message, state: FSMContext):
    t = norm_digits(m.text or "").strip()
    if not t.isdigit() or int(t) > 100_000_000:
        await m.answer("❌ یک عدد معتبر بفرستید.")
        return
    await state.update_data(view=int(t))
    await state.set_state(NewPost.menu)
    txt, kb = builder_view(await state.get_data())
    await m.answer(txt, reply_markup=kb)


async def _create_post(uid: int, d: dict, run_at_utc) -> int:
    async with Session() as s:
        p = Post(user_id=uid, channel_id=d["channel_id"], kind=d["kind"], text=d["text"],
                 file_id=d["file_id"], file_name=d["file_name"], title=d["title"],
                 silent=d["silent"], pin=d["pin"], view_delete=d["view"], run_at=run_at_utc)
        s.add(p)
        await s.commit()
        return p.id


async def cb_np_now(c: CallbackQuery, state: FSMContext, is_admin: bool):
    d = await state.get_data()
    if not has_access(await get_user(c.from_user.id), is_admin):
        await c.answer("اشتراک شما منقضی شده است.", show_alert=True)
        return
    pid = await _create_post(c.from_user.id, d, utcnow())
    await state.clear()
    await safe_edit(c.message, f"🚀 پست #{pid} در صف ارسال قرار گرفت؛ نتیجه را همین‌جا اعلام می‌کنم.")
    await c.answer()


async def cb_np_sched(c: CallbackQuery, state: FSMContext):
    await state.set_state(NewPost.when)
    await c.message.answer("⏰ زمان ارسال را بفرستید (به وقت تهران):\n"
                           "<code>18:30</code> · <code>فردا 09:00</code> · <code>+45</code> · "
                           "<code>+2 ساعت</code> · <code>1405/07/20 18:30</code>")
    await c.answer()


async def st_np_when(m: Message, state: FSMContext, is_admin: bool):
    dt = parse_when(m.text or "")
    if dt is None:
        await m.answer("❌ زمان نامعتبر یا گذشته است. نمونه: <code>فردا 09:00</code> یا <code>1405/07/20 18:30</code>")
        return
    if not has_access(await get_user(m.from_user.id), is_admin):
        await state.clear()
        await m.answer("⛔️ اشتراک شما فعال نیست.")
        return
    d = await state.get_data()
    run_utc = to_utc_naive(dt)
    pid = await _create_post(m.from_user.id, d, run_utc)
    await state.clear()
    await m.answer(f"📅 پست #{pid} برای <b>{fmt_dt(run_utc)}</b> زمان‌بندی شد.", reply_markup=menu_kb(is_admin))


async def cb_np_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c.message, "لغو شد.")
    await c.answer()


# ------------------------------------------------------------------ scheduled list
async def btn_sched(m: Message, state: FSMContext):
    await state.clear()
    async with Session() as s:
        posts = (await s.execute(select(Post).where(
            Post.user_id == m.from_user.id, Post.status.in_(("pending", "sending")))
            .order_by(Post.run_at).limit(15))).scalars().all()
        chan = {c.id: c.title for c in (await s.execute(
            select(Channel).where(Channel.user_id == m.from_user.id))).scalars()}
    if not posts:
        await m.answer("📅 پست زمان‌بندی‌شده‌ای ندارید.")
        return
    lines, rows = [], []
    for p in posts:
        prev = escape((p.title or p.text or p.file_name)[:35])
        lines.append(f"#{p.id} · {fmt_dt(p.run_at)} · {escape(chan.get(p.channel_id, '؟'))}\n   {prev}")
        if p.status == "pending":
            rows.append([(f"🗑 لغو #{p.id}", f"sp:del:{p.id}")])
    await m.answer("📅 <b>پست‌های در صف</b>\n\n" + "\n".join(lines), reply_markup=ikb(rows) if rows else None)


async def cb_sp_del(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    async with Session() as s:
        p = await s.get(Post, pid)
        ok = bool(p and p.user_id == c.from_user.id and p.status == "pending")
        if ok:
            p.status = "cancelled"
            await s.commit()
    await c.answer("لغو شد." if ok else "قابل لغو نیست.", show_alert=not ok)
    if ok:
        await safe_edit(c.message, f"🗑 پست #{pid} لغو شد.")


# ------------------------------------------------------------------ subscription
async def btn_buy(m: Message, state: FSMContext, cfg: dict):
    await state.clear()
    plans = parse_plans(cfg["plans"])
    if not plans or not cfg["card_number"].strip():
        await m.answer(f"برای خرید اشتراک با {support_line(cfg)} در ارتباط باشید.")
        return
    rows = [[(f"{d} روزه — {money(p)} تومان", f"buy:{d}")] for d, p in plans]
    await m.answer("💳 <b>خرید / تمدید اشتراک</b>\nیکی از پلن‌ها را انتخاب کنید:", reply_markup=ikb(rows))


async def cb_buy(c: CallbackQuery, state: FSMContext, cfg: dict):
    days = int(c.data.split(":")[1])
    price = dict(parse_plans(cfg["plans"])).get(days)
    if price is None:
        await c.answer("پلن معتبر نیست.", show_alert=True)
        return
    await state.set_state(Pay.receipt)
    await state.update_data(days=days, amount=price)
    owner = f"\nبه نام: {escape(cfg['card_owner'])}" if cfg["card_owner"].strip() else ""
    await c.message.answer(
        f"مبلغ <b>{money(price)} تومان</b> را کارت‌به‌کارت کنید:\n\n"
        f"<code>{escape(cfg['card_number'].strip())}</code>{owner}\n\n"
        "سپس <b>عکس رسید</b> (یا کد پیگیری) را همین‌جا بفرستید. لغو: /cancel")
    await c.answer()


async def st_receipt(m: Message, state: FSMContext, is_admin: bool, cfg: dict):
    if not (m.photo or m.text):
        await m.answer("عکس رسید یا کد پیگیری را بفرستید.")
        return
    d = await state.get_data()
    async with Session() as s:
        pay = Payment(user_id=m.from_user.id, days=d["days"], amount=d["amount"],
                      receipt_file_id=m.photo[-1].file_id if m.photo else "",
                      receipt_text=(m.text or m.caption or "")[:500])
        s.add(pay)
        await s.commit()
        pid = pay.id
    await state.clear()
    await m.answer("✅ رسید ثبت شد؛ پس از تأیید مدیر اشتراک فعال می‌شود.", reply_markup=menu_kb(is_admin))
    who = f"{escape(m.from_user.full_name)}" + (f" @{m.from_user.username}" if m.from_user.username else "")
    cap = (f"💳 <b>رسید جدید #{pid}</b>\nکاربر: {who} (<code>{m.from_user.id}</code>)\n"
           f"پلن: {d['days']} روزه — {money(d['amount'])} تومان\n{escape(pay.receipt_text)}")
    kb = ikb([[("✅ تأیید", f"pay:ok:{pid}"), ("❌ رد", f"pay:no:{pid}")]])
    for aid in parse_ids(cfg["admin_ids"]):
        with contextlib.suppress(Exception):
            if pay.receipt_file_id:
                await m.bot.send_photo(aid, pay.receipt_file_id, caption=cap, reply_markup=kb)
            else:
                await m.bot.send_message(aid, cap, reply_markup=kb)


async def cb_pay(c: CallbackQuery, is_admin: bool):
    if not is_admin:
        await c.answer("فقط مدیر.", show_alert=True)
        return
    _, action, pid = c.data.split(":")
    async with Session() as s:
        pay = await s.get(Payment, int(pid))
        if not pay or pay.status != "pending":
            await c.answer("قبلاً بررسی شده.", show_alert=True)
            return
        u = await s.get(User, pay.user_id)
        if action == "ok":
            pay.status = "approved"
            base = u.expires_at if (u.expires_at and u.expires_at > utcnow()) else utcnow()
            u.expires_at = base + timedelta(days=pay.days)
            note = f"✅ پرداخت شما تأیید شد. اشتراک تا <b>{fmt_dt(u.expires_at)}</b> فعال است."
        else:
            pay.status = "rejected"
            note = "❌ رسید شما تأیید نشد. در صورت اشتباه با پشتیبانی تماس بگیرید."
        await s.commit()
        uid = u.id
    with contextlib.suppress(Exception):
        await c.bot.send_message(uid, note)
    with contextlib.suppress(TelegramBadRequest):
        await c.message.edit_reply_markup(reply_markup=None)
    await c.answer("انجام شد: " + ("تأیید" if action == "ok" else "رد"), show_alert=True)


# ------------------------------------------------------------------ admin
async def btn_admin(m: Message, state: FSMContext, is_admin: bool):
    await state.clear()
    if not is_admin:
        return
    async with Session() as s:
        users = (await s.execute(select(func.count()).select_from(User))).scalar_one()
        active = (await s.execute(select(func.count()).select_from(User).where(
            User.expires_at > utcnow()))).scalar_one()
        pend = (await s.execute(select(func.count()).select_from(Post).where(Post.status == "pending"))).scalar_one()
        sent = (await s.execute(select(func.count()).select_from(Post).where(Post.status == "sent"))).scalar_one()
        pays = (await s.execute(select(func.count()).select_from(Payment).where(Payment.status == "pending"))).scalar_one()
    await m.answer(
        f"🛠 <b>مدیریت</b>\n\nکاربران: {users} (اشتراک فعال: {active})\nپست در صف: {pend} · ارسال‌شده: {sent}\n"
        f"رسید در انتظار: {pays}\n\n"
        "<b>دستورها:</b>\n<code>/user ID</code> — مشخصات کاربر\n<code>/adddays ID DAYS</code> — افزودن روز (منفی = کم)\n"
        "<code>/ban ID</code> · <code>/unban ID</code>\n<code>/broadcast متن</code> — پیام همگانی\n\n"
        "تنظیمات کامل (قیمت، کارت، متن‌ها) از پنل وب.")


def _args(m: Message) -> list[str]:
    return norm_digits(m.text or "").split()[1:]


async def cmd_user(m: Message, is_admin: bool):
    a = _args(m)
    if not is_admin or not a or not a[0].isdigit():
        return
    u = await get_user(int(a[0]))
    if not u:
        await m.answer("کاربر پیدا نشد.")
        return
    await m.answer(f"👤 <code>{u.id}</code> {escape(u.name)} @{escape(u.username)}\n"
                   f"اشتراک تا: {fmt_dt(u.expires_at)}\nمسدود: {'بله' if u.banned else 'خیر'}\n"
                   f"توکن: {'دارد' if u.eitaa_token_enc else 'ندارد'}")


async def cmd_adddays(m: Message, is_admin: bool):
    a = _args(m)
    if not is_admin or len(a) != 2 or not a[0].isdigit() or not a[1].lstrip("-").isdigit():
        if is_admin:
            await m.answer("فرمت: <code>/adddays USER_ID DAYS</code>")
        return
    async with Session() as s:
        u = await s.get(User, int(a[0]))
        if not u:
            await m.answer("کاربر پیدا نشد.")
            return
        base = u.expires_at if (u.expires_at and u.expires_at > utcnow()) else utcnow()
        u.expires_at = base + timedelta(days=int(a[1]))
        await s.commit()
        exp = u.expires_at
    await m.answer(f"✅ اشتراک تا {fmt_dt(exp)}")
    with contextlib.suppress(Exception):
        await m.bot.send_message(int(a[0]), f"🎁 اشتراک شما تا <b>{fmt_dt(exp)}</b> تمدید شد.")


async def _set_ban(m: Message, is_admin: bool, value: bool):
    a = _args(m)
    if not is_admin or not a or not a[0].isdigit():
        return
    async with Session() as s:
        u = await s.get(User, int(a[0]))
        if not u:
            await m.answer("کاربر پیدا نشد.")
            return
        u.banned = value
        await s.commit()
    await m.answer("⛔️ مسدود شد." if value else "✅ آزاد شد.")


async def cmd_ban(m: Message, is_admin: bool):
    await _set_ban(m, is_admin, True)


async def cmd_unban(m: Message, is_admin: bool):
    await _set_ban(m, is_admin, False)


async def cmd_broadcast(m: Message, is_admin: bool):
    if not is_admin:
        return
    text = (m.text or "").split(None, 1)
    if len(text) < 2:
        await m.answer("فرمت: <code>/broadcast متن پیام</code>")
        return
    async with Session() as s:
        ids = [r for r in (await s.execute(select(User.id).where(User.banned == False))).scalars()]  # noqa: E712
    ok = 0
    for uid in ids:
        try:
            await m.bot.send_message(uid, escape(text[1]))
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"📣 ارسال شد به {ok} از {len(ids)} کاربر.")


async def fallback(m: Message, is_admin: bool):
    await m.answer("از منوی پایین استفاده کنید 👇", reply_markup=menu_kb(is_admin))


# ------------------------------------------------------------------ wiring
def make_router() -> Router:
    r = Router()
    g = Guard()
    r.message.outer_middleware(g)
    r.callback_query.outer_middleware(g)

    M, C = r.message.register, r.callback_query.register
    # commands + menu buttons first: they work from any state and reset it
    M(cmd_start, CommandStart())
    M(cmd_cancel, Command("cancel"))
    M(cmd_id, Command("id"))
    M(cmd_user, Command("user"))
    M(cmd_adddays, Command("adddays"))
    M(cmd_ban, Command("ban"))
    M(cmd_unban, Command("unban"))
    M(cmd_broadcast, Command("broadcast"))
    M(btn_new, F.text == B_NEW)
    M(btn_sched, F.text == B_SCHED)
    M(btn_channels, F.text == B_CHAN)
    M(btn_token, F.text == B_TOKEN)
    M(btn_buy, F.text == B_BUY)
    M(btn_me, F.text == B_ME)
    M(btn_support, F.text == B_SUP)
    M(btn_help, F.text == B_HELP)
    M(btn_admin, F.text == B_ADMIN)
    # state inputs
    M(st_token, SetToken.token, F.text)
    M(st_chan_id, AddChannel.chat_id, F.text)
    M(st_chan_title, AddChannel.title, F.text)
    M(st_np_content, NewPost.content)
    M(st_np_title, NewPost.title, F.text)
    M(st_np_view, NewPost.view, F.text)
    M(st_np_when, NewPost.when, F.text)
    M(st_receipt, Pay.receipt)
    M(fallback)
    # callbacks
    C(cb_token_set, F.data == "tk:set")
    C(cb_token_del, F.data == "tk:del")
    C(cb_chan_add, F.data == "ch:add")
    C(cb_chan_ok, F.data.in_({"ch:ok", "ch:force"}), AddChannel.chat_id)
    C(cb_chan_rename, F.data == "ch:rename", AddChannel.chat_id)
    C(cb_chan_cancel, F.data == "ch:cancel")
    C(cb_chan_del, F.data.startswith("ch:del:"))
    C(cb_np_channel, F.data.startswith("np:ch:"))
    C(cb_np_toggle, F.data.in_({"np:silent", "np:pin"}), NewPost.menu)
    C(cb_np_title, F.data == "np:title", NewPost.menu)
    C(cb_np_view, F.data == "np:view", NewPost.menu)
    C(cb_np_now, F.data == "np:now", NewPost.menu)
    C(cb_np_sched, F.data == "np:sched", NewPost.menu)
    C(cb_np_cancel, F.data == "np:cancel")
    C(cb_sp_del, F.data.startswith("sp:del:"))
    C(cb_buy, F.data.startswith("buy:"))
    C(cb_pay, F.data.startswith("pay:"))
    return r


# ------------------------------------------------------------------ manager
class BotManager:
    def __init__(self) -> None:
        self.bot: Bot | None = None
        self.dp: Dispatcher | None = None
        self.task: asyncio.Task | None = None
        self.username = ""
        self.error = ""

    @property
    def running(self) -> bool:
        return bool(self.task and not self.task.done())

    async def start(self) -> bool:
        await self.stop()
        token = (await get_settings())["bot_token"].strip()
        if not token:
            self.error = "توکن ربات تلگرام هنوز تنظیم نشده است."
            return False
        bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            me = await bot.get_me()
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception as e:
            self.error = str(e).replace(token, "***")[:300] or type(e).__name__
            with contextlib.suppress(Exception):
                await bot.session.close()
            return False
        self.bot, self.username, self.error = bot, me.username or "", ""
        self.dp = Dispatcher(storage=MemoryStorage())
        self.dp.include_router(make_router())
        self.task = asyncio.create_task(self._run(self.dp, bot))
        log.info("bot @%s started", self.username)
        return True

    async def _run(self, dp: Dispatcher, bot: Bot) -> None:
        try:
            await dp.start_polling(bot, handle_signals=False, close_bot_session=False)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"[:300]
            log.exception("polling crashed")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        if self.bot:
            with contextlib.suppress(Exception):
                await self.bot.session.close()
        self.bot = self.dp = self.task = None
        self.username = ""


manager = BotManager()
