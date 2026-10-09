"""Client for the eitaayar.ir API: https://eitaayar.ir/api/TOKEN/METHOD"""
import httpx

from . import config


class EitaaError(Exception):
    """API answered with ok=false (or garbage)."""


class EitaaNetworkError(EitaaError):
    """Could not reach the API (retry-able)."""


def _clean(msg: str, token: str) -> str:
    return msg.replace(token, "***") if token else msg


async def _call(token: str, method: str, data: dict | None = None, files: dict | None = None):
    url = f"{config.EITAA_API_BASE}/{token}/{method}"
    kw = {"proxy": config.EITAA_PROXY} if config.EITAA_PROXY else {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=20), **kw) as c:
            r = await c.post(url, data=data or None, files=files or None)
    except httpx.HTTPError as e:
        raise EitaaNetworkError(_clean(f"{type(e).__name__}: {e}", token)) from None
    try:
        j = r.json()
    except ValueError:
        raise EitaaError(f"پاسخ نامعتبر از ایتایار (HTTP {r.status_code})") from None
    if not isinstance(j, dict) or not j.get("ok"):
        desc = ""
        if isinstance(j, dict):
            desc = str(j.get("description") or j.get("message") or j.get("error") or j)
        raise EitaaError(_clean(desc or f"HTTP {r.status_code}", token))
    return j.get("result", j)


def _opts(title, silent, reply_to, date, pin, view_delete) -> dict:
    d: dict = {}
    if title:
        d["title"] = title
    if silent:
        d["disable_notification"] = 1
    if reply_to:
        d["reply_to_message_id"] = reply_to
    if date:
        d["date"] = int(date)
    if pin:
        d["pin"] = 1
    if view_delete:
        d["viewCountForDelete"] = int(view_delete)
    return d


async def get_me(token: str) -> dict:
    return await _call(token, "getMe")


async def send_message(token: str, chat_id: str, text: str, *, title=None, silent=False,
                       reply_to=None, date=None, pin=False, view_delete=None) -> dict:
    data = {"chat_id": chat_id, "text": text, **_opts(title, silent, reply_to, date, pin, view_delete)}
    return await _call(token, "sendMessage", data=data)


async def send_file(token: str, chat_id: str, filename: str, content: bytes, *, caption=None,
                    title=None, silent=False, reply_to=None, date=None, pin=False,
                    view_delete=None) -> dict:
    data = {"chat_id": chat_id, **_opts(title, silent, reply_to, date, pin, view_delete)}
    if caption:
        data["caption"] = caption
    return await _call(token, "sendFile", data=data, files={"file": (filename, content)})
