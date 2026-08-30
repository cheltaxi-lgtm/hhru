"""READ: dump account resumes as JSON for Koplife Jobs (list + plaintext)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .list_resumes import _format_status


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "resumes-dump",
        help="JSON-дамп резюме аккаунта (READ: id, статус, текст)",
    )
    parser.set_defaults(func=run)


def _fail(message: str) -> None:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), flush=True)
    raise SystemExit(1)


def read_resume_plain_text(page) -> str:
    for selector in ("[data-qa='resume']", "main", "[data-qa='resume-content']"):
        locator = page.locator(selector)
        try:
            if locator.count() < 1:
                continue
            text = locator.first.inner_text().strip()
        except (PlaywrightError, PlaywrightTimeoutError, AssertionError, AttributeError):
            continue
        if len(text) > 80:
            return text[:15000]
    try:
        return page.locator("body").inner_text().strip()[:15000]
    except (PlaywrightError, PlaywrightTimeoutError, AssertionError, AttributeError):
        return ""


def run(args: argparse.Namespace) -> None:
    from ..browser import (
        RESUMES_FULL_LIST_URL,
        goto_hh,
        has_auth_cookie,
        has_login_form,
        launch_context,
        open_confirmed_resume,
    )
    from ..config import load_config_or_exit
    from ..copy_resume import ResumeListIndeterminate, list_resume_cards
    from .whoami import _check_storage_state

    config = load_config_or_exit(args.config)
    ok, detail, _ = _check_storage_state(Path(config.storage_state_file))
    if not ok:
        _fail(f"сессия недействительна: {detail}")

    payload: list[dict[str, object]] = []
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        if not has_auth_cookie(page):
            _fail("сессия недействительна (cookie hhtoken не найден)")
        goto_hh(page, RESUMES_FULL_LIST_URL)
        if has_login_form(page):
            _fail("сессия недействительна (форма входа на hh.ru)")
        try:
            cards = list_resume_cards(page, navigate=False)
        except ResumeListIndeterminate as exc:
            _fail(str(exc))

        for card in cards:
            item: dict[str, object] = {
                "hh_resume_id": card.resume_id,
                "title": card.title or "",
                "status": _format_status(card.status),
                "is_searchable": card.is_searchable,
                "body": "",
            }
            try:
                open_confirmed_resume(page, card.resume_id)
                item["body"] = read_resume_plain_text(page)
            except (PlaywrightError, PlaywrightTimeoutError, ValueError) as exc:
                item["error"] = str(exc)
            payload.append(item)

    print(json.dumps({"ok": True, "resumes": payload}, ensure_ascii=False), flush=True)
