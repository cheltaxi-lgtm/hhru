"""Send a hh.ru response reminder for one negotiation topic."""

from __future__ import annotations

import argparse
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .copy_resume import confirm_write

REMIND_NAME = re.compile(r"напомн", re.IGNORECASE)
REMIND_TEXT = re.compile(r"напомнить об отклике", re.IGNORECASE)
MENU_NAME = re.compile(
    r"ещё|меню|действи|more|options|открыть меню|дополнительн",
    re.IGNORECASE,
)
CHAT_ID_RE = re.compile(r"/chat/(\d+)")
# Bare chatik.hh.ru/chat/<id> redirects to {city}.hh.ru/chat/<id>, a chrome
# shell without the widget.  The dest=iframe query is the route hh.ru itself
# uses when embedding chatik after «Перейти в чат».
CHATIK_EMBED_QUERY = (
    ("without_list", "1"),
    ("platform", "xhh"),
    ("theme", "hh-day"),
    ("dest", "iframe"),
)


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "remind",
        help="Отправить напоминание работодателю по одной переписке",
        description=(
            "WRITE-команда: открывает переписку и кликает «Напомнить», "
            "если кнопка есть на странице. Боевой запуск требует --force."
        ),
    )
    p.add_argument("--topic", required=True, help="Уникальный ID переписки")
    p.add_argument(
        "--chat-url",
        default="",
        help="Прямая ссылка на чат, если уже известна",
    )
    p.add_argument("--force", action="store_true", help="Подтвердить боевой запуск")
    p.add_argument("--json", action="store_true", help="JSON для внешних клиентов")
    p.add_argument(
        "--max-pages",
        type=int,
        default=5,
        help="Максимум страниц списка откликов при проверке разрешения",
    )
    p.set_defaults(func=run)


def _emit(payload: dict, *, as_json: bool, ok: bool) -> bool:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), flush=True)
    else:
        status = "[OK]" if payload.get("success") or payload.get("skipped") else "[FAIL]"
        reason = str(payload.get("reason") or payload.get("error") or "")
        print(f"{status} {reason}".strip())
    if not ok:
        raise SystemExit(1)
    return False


def _click_visible(locator, timeout_ms: int) -> bool:
    try:
        target = locator.first
        target.wait_for(state="visible", timeout=timeout_ms)
        target.click(timeout=8000)
        return True
    except Exception:
        return False


def _iter_scopes(page):
    """Page first, then chat frames, then the rest — skip long waits on ads."""
    yield page
    frames = getattr(page, "frames", None) or ()
    rest = []
    for frame in frames:
        url = str(getattr(frame, "url", "") or "").casefold()
        if "chatik" in url or "/chat/" in url:
            yield frame
        else:
            rest.append(frame)
    for frame in rest:
        yield frame


def _remind_locators(scope):
    locators = []
    get_by_text = getattr(scope, "get_by_text", None)
    if callable(get_by_text):
        locators.append(get_by_text(REMIND_TEXT))
    get_by_role = getattr(scope, "get_by_role", None)
    if callable(get_by_role):
        for role in ("button", "link", "menuitem"):
            locators.append(scope.get_by_role(role, name=REMIND_NAME))
    return locators


def _wait_attached(locator, timeout_ms: int) -> bool:
    try:
        target = locator.first
        target.wait_for(state="attached", timeout=timeout_ms)
        return True
    except Exception:
        return False


def _wait_chat_ready(page, *, timeout_ms: int = 8000) -> bool:
    """Wait for the confirmed chatik composer before hunting the remind control."""
    from ..selector_groups.negotiations import CHAT_MESSAGE_INPUT

    for scope in _iter_scopes(page):
        locator_fn = getattr(scope, "locator", None)
        if not callable(locator_fn):
            continue
        if _wait_attached(locator_fn(CHAT_MESSAGE_INPUT), min(timeout_ms, 4000)):
            return True
    return False


def _click_remind_in_scopes(page, *, timeout_ms: int) -> bool:
    first = True
    for scope in _iter_scopes(page):
        for locator in _remind_locators(scope):
            wait = timeout_ms if first else min(400, timeout_ms)
            first = False
            if _click_visible(locator, wait):
                return True
    return False


def _open_overflow_menus(page) -> None:
    """Viewed chats often hide «Напомнить об отклике» behind the header menu."""
    for scope in _iter_scopes(page):
        get_by_role = getattr(scope, "get_by_role", None)
        if not callable(get_by_role):
            continue
        _click_visible(get_by_role("button", name=MENU_NAME), 800)


def click_remind_control(page, *, timeout_ms: int = 4000) -> tuple[bool, str, bool]:
    """Click the reminder control, including after opening the chat overflow menu.

    hh.ru shows the button on unseen chats and on viewed ones with no recent
    messages; in the latter case it is frequently inside «Ещё» as a menuitem.
    """
    if _host(getattr(page, "url", "") or "") == "chatik.hh.ru":
        _wait_chat_ready(page)
    if _click_remind_in_scopes(page, timeout_ms=timeout_ms):
        return True, "напоминание отправлено", True
    _open_overflow_menus(page)
    if _click_remind_in_scopes(page, timeout_ms=min(3000, timeout_ms)):
        return True, "напоминание отправлено", True
    return False, "кнопка напоминания не найдена", False


def chat_id_from_url(raw: str) -> str | None:
    match = CHAT_ID_RE.search(str(raw or ""))
    return match.group(1) if match else None


def chatik_embed_url(raw: str) -> str:
    parts = urlsplit(str(raw or "").strip())
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in CHATIK_EMBED_QUERY:
        query.setdefault(key, value)
    return urlunsplit(parts._replace(query=urlencode(query)))


def safe_chat_url(raw: str) -> str | None:
    text = str(raw or "").strip()
    if text.startswith("https://chatik.hh.ru/"):
        return chatik_embed_url(text)
    if text.startswith("https://hh.ru/"):
        return text
    return None


def candidate_remind_urls(topic: str, chat_url_arg: str | None = None) -> list[str]:
    from ..responses import NEGOTIATIONS_URL

    urls: list[str] = []
    chat = safe_chat_url(chat_url_arg or "")
    if chat:
        urls.append(chat)
    urls.append(f"{NEGOTIATIONS_URL}?topic={topic}")
    return urls


def open_listed_conversation(page, chat_id: str, *, timeout_ms: int = 4000) -> bool:
    """Open the selected chat from the chatik list via its href, not a data-qa."""
    if not chat_id:
        return False
    return _click_visible(page.locator(f'a[href*="/chat/{chat_id}"]'), timeout_ms)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").casefold()


def run(args: argparse.Namespace) -> bool:
    from ..browser import goto_hh, launch_context, require_authenticated_page
    from ..config import load_config_or_exit
    from ..responses import NotAuthenticated

    as_json = bool(getattr(args, "json", False))
    topic = str(getattr(args, "topic", "") or "").strip()

    def fail(message: str, **extra) -> bool:
        session_dead = bool(extra.pop("session_dead", False))
        payload = {
            "ok": False,
            "success": False,
            "skipped": False,
            "error": message,
            "reason": message,
            "session_dead": session_dead,
            "topic": topic,
            **extra,
        }
        return _emit(payload, as_json=as_json, ok=False)

    if not topic or not topic.isdigit():
        return fail("укажите числовой --topic")
    if args.max_pages < 1:
        return fail("--max-pages должен быть >= 1")
    if not confirm_write(
        bool(getattr(args, "force", False)),
        prompt=f"Отправить напоминание по переписке topic={topic}?",
    ):
        return fail("нужно --force")

    config = load_config_or_exit(args.config)
    try:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            last_reason = "кнопка напоминания не найдена"
            for url in candidate_remind_urls(topic, getattr(args, "chat_url", "")):
                goto_hh(page, url)
                # chatik is a separate app: the hh.ru login-form marker is not
                # a valid session signal there, and bouncing back to
                # /applicant/negotiations after chatik can false-trip it.
                if _host(page.url) != "chatik.hh.ru":
                    require_authenticated_page(page)
                success, last_reason, _acted = click_remind_control(page)
                if success:
                    payload = {
                        "ok": True,
                        "success": True,
                        "skipped": False,
                        "error": None,
                        "reason": last_reason,
                        "session_dead": False,
                        "topic": topic,
                    }
                    return _emit(payload, as_json=as_json, ok=True)
                chat_id = chat_id_from_url(url) or chat_id_from_url(getattr(page, "url", ""))
                if chat_id and open_listed_conversation(page, chat_id):
                    success, last_reason, _acted = click_remind_control(page)
                    if success:
                        payload = {
                            "ok": True,
                            "success": True,
                            "skipped": False,
                            "error": None,
                            "reason": last_reason,
                            "session_dead": False,
                            "topic": topic,
                        }
                        return _emit(payload, as_json=as_json, ok=True)
            payload = {
                "ok": True,
                "success": False,
                "skipped": True,
                "error": None,
                "reason": last_reason,
                "session_dead": False,
                "topic": topic,
            }
            return _emit(payload, as_json=as_json, ok=True)
    except NotAuthenticated as exc:
        text = str(exc)
        return fail(text, session_dead=True)
    except Exception as exc:
        text = str(exc)[:400]
        session_dead = "Сессия недействительна" in text or "форму входа" in text
        return fail(text, session_dead=session_dead)
