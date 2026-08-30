"""Unit tests for the remind command (no live hh.ru)."""

from __future__ import annotations

import argparse
import json

import pytest

from hhru_bot.commands import remind as remind_cmd

pytestmark = pytest.mark.unit


def test_remind_cli_flags():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    remind_cmd.register(sub)
    args = parser.parse_args(["remind", "--topic", "77", "--force", "--json"])
    assert args.topic == "77"
    assert args.force is True
    assert args.json is True
    args = parser.parse_args(
        ["remind", "--topic", "77", "--force", "--json", "--chat-url", "https://chatik.hh.ru/chat/1"]
    )
    assert args.chat_url == "https://chatik.hh.ru/chat/1"


def test_candidate_remind_urls_always_include_topic():
    urls = remind_cmd.candidate_remind_urls("77", "https://chatik.hh.ru/chat/9")
    assert urls[0].startswith("https://chatik.hh.ru/chat/9?")
    assert "dest=iframe" in urls[0]
    assert "without_list=1" in urls[0]
    assert urls[1].endswith("?topic=77")
    assert remind_cmd.safe_chat_url("https://evil.example/x") is None
    assert remind_cmd.chat_id_from_url("https://chatik.hh.ru/chat/5567733740") == "5567733740"


def test_chatik_embed_url_keeps_existing_query():
    url = remind_cmd.chatik_embed_url(
        "https://chatik.hh.ru/chat/9?dest=iframe&theme=hh-night"
    )
    assert "dest=iframe" in url
    assert "theme=hh-night" in url
    assert url.count("dest=") == 1


def test_remind_without_force_fails_closed(capsys, monkeypatch):
    monkeypatch.setattr(remind_cmd, "confirm_write", lambda *a, **k: False)
    with pytest.raises(SystemExit) as exc:
        remind_cmd.run(
            argparse.Namespace(
                topic="77",
                force=False,
                json=True,
                max_pages=5,
                config="missing.yaml",
                headless=True,
            )
        )
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "force" in payload["error"]


def test_click_remind_control_uses_accessible_name():
    class _Control:
        def __init__(self, n: int):
            self._n = n
            self.clicked = False

        def count(self):
            return self._n

        @property
        def first(self):
            return self

        def wait_for(self, *, state="visible", timeout=0):
            if self._n <= 0:
                raise RuntimeError("not visible")

        def click(self, *, timeout=0):
            self.clicked = True

    class _Page:
        def __init__(self):
            self.button = _Control(1)
            self.link = _Control(0)
            self.text = _Control(0)

        def get_by_role(self, role, *, name=None):
            return self.button if role == "button" else self.link

        def get_by_text(self, name=None):
            return self.text

    page = _Page()
    ok, reason, acted = remind_cmd.click_remind_control(page)
    assert ok is True
    assert acted is True
    assert page.button.clicked is True
    assert "отправлено" in reason


def test_click_remind_control_searches_frames():
    class _Control:
        def __init__(self, n: int):
            self._n = n
            self.clicked = False

        def count(self):
            return self._n

        @property
        def first(self):
            return self

        def wait_for(self, *, state="visible", timeout=0):
            if self._n <= 0:
                raise RuntimeError("not visible")

        def click(self, *, timeout=0):
            self.clicked = True

    class _Scope:
        def __init__(self, n: int):
            self.button = _Control(n)

        def get_by_role(self, role, *, name=None):
            return self.button if role == "button" else _Control(0)

        def get_by_text(self, name=None):
            return _Control(0)

    outer = _Scope(0)
    inner = _Scope(1)
    outer.frames = [inner]
    ok, reason, acted = remind_cmd.click_remind_control(outer)
    assert ok is True
    assert acted is True
    assert inner.button.clicked is True
    assert outer.button.clicked is False
    assert "отправлено" in reason


def test_click_remind_opens_overflow_menu_on_viewed_chat():
    class _Control:
        def __init__(self, n: int):
            self._n = n
            self.clicked = False
            self.on_click = None

        @property
        def first(self):
            return self

        def wait_for(self, *, state="visible", timeout=0):
            if self._n <= 0:
                raise RuntimeError("not visible")

        def click(self, *, timeout=0):
            self.clicked = True
            if self.on_click:
                self.on_click()

    class _Page:
        def __init__(self):
            self.remind = _Control(0)
            self.menu = _Control(1)
            self.menu.on_click = lambda: setattr(self.remind, "_n", 1)

        def get_by_role(self, role, *, name=None):
            blob = getattr(name, "pattern", str(name or ""))
            if role == "button" and "напомн" in blob:
                return self.remind
            if role == "button" and any(
                token in blob for token in ("ещё", "меню", "действи")
            ):
                return self.menu
            return _Control(0)

        def get_by_text(self, name=None):
            return _Control(0)

    page = _Page()
    ok, reason, acted = remind_cmd.click_remind_control(page, timeout_ms=50)
    assert ok is True
    assert acted is True
    assert page.menu.clicked is True
    assert page.remind.clicked is True
    assert "отправлено" in reason


def test_click_remind_finds_menuitem():
    class _Control:
        def __init__(self, n: int):
            self._n = n
            self.clicked = False

        @property
        def first(self):
            return self

        def wait_for(self, *, state="visible", timeout=0):
            if self._n <= 0:
                raise RuntimeError("not visible")

        def click(self, *, timeout=0):
            self.clicked = True

    class _Page:
        def __init__(self):
            self.item = _Control(1)

        def get_by_role(self, role, *, name=None):
            blob = getattr(name, "pattern", str(name or ""))
            if role == "menuitem" and "напомн" in blob:
                return self.item
            return _Control(0)

        def get_by_text(self, name=None):
            return _Control(0)

    page = _Page()
    ok, reason, acted = remind_cmd.click_remind_control(page, timeout_ms=50)
    assert ok is True
    assert acted is True
    assert page.item.clicked is True
    assert "отправлено" in reason
