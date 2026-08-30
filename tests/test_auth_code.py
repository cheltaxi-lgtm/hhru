from __future__ import annotations

import logging
import os
import stat

import pytest
from playwright.sync_api import Error as PlaywrightError

from hhru_bot.auth_code import _read_code, login_with_code, mask_login

pytestmark = pytest.mark.integration


def test_mask_login():
    assert mask_login("+79991234567") == "+79***4567"
    assert mask_login("person@example.com") == "p***@example.com"


class _Locator:
    def __init__(self, page, count=1, *, kind="field"):
        self.page = page
        self._count = count
        self.kind = kind

    def count(self):
        return self._count() if callable(self._count) else self._count

    @property
    def first(self):
        return self

    def wait_for(self, *, timeout: float | None = None, state: str | None = None):  # noqa: ARG002
        if self.kind == "email-type" and self.page.show_credentials_on_wait:
            self.page.stage = "credentials"
        if self.kind == "code" and self.page.show_code_on_wait:
            self.page.stage = "code"
        if self.count() != 1:
            raise PlaywrightError("not visible")

    def click(self):
        if self.kind == "continue":
            if not self.page.show_credentials_on_wait:
                self.page.stage = "credentials"
        elif self.kind == "submit":
            if (
                self.page.need_captcha or self.page.need_click_captcha
            ) and not self.page.captcha_ok:
                self.page.stage = "captcha"
            else:
                self.page.stage = "code"
        elif self.kind == "dalshe":
            self.page.captcha_ok = True
            self.page.stage = "code"
        elif self.kind == "captcha-send":
            self.page.stage = "code"

    def check(
        self,
        *,
        position=None,
        timeout: float | None = None,
        force: bool | None = None,
        no_wait_after: bool | None = None,
        trial: bool | None = None,
    ):  # noqa: ARG002
        self.page.email_selected = True

    def fill(self, value):
        if self.kind == "code":
            self.page.code = value
            if value == "1234":
                self.page.context._cookies = [{"name": "hhtoken"}]
        elif self.kind == "captcha":
            self.page.captcha_ok = True
            self.page.captcha_value = value

    def press_sequentially(self, value, delay=0):  # noqa: ARG002
        self.fill(value)

    def press(self, key):
        if self.kind == "captcha" and key == "Enter":
            self.page.stage = "code"
        return None

    def is_visible(self):
        return self.count() == 1

    def screenshot(self, *, path=None, timeout=None):  # noqa: ARG002
        from pathlib import Path

        if path:
            Path(path).write_bytes(b"\x89PNG")

    def inner_text(self):
        return self.page.body


class _Context:
    def __init__(self, page):
        self.page = page
        self._cookies = []
        self.saved = None
        page.context = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def new_page(self):
        return self.page

    def cookies(self):
        return self._cookies

    def storage_state(self):
        self.saved = True
        return {"cookies": self._cookies, "origins": []}


class _Page:
    def __init__(self, body=""):
        self.body = body
        self.stage = "start"
        self.email_selected = False
        self.code = None
        self.context = None
        self.show_code_on_wait = False
        self.show_credentials_on_wait = False
        self.need_captcha = False
        self.need_click_captcha = False
        self.captcha_ok = False
        self.captcha_value = None

    def get_by_role(self, role, *, name=None):
        if role == "button" and name == "Отправить":
            return _Locator(
                self,
                count=lambda: int(self.stage == "captcha" and self.need_captcha),
                kind="captcha-send",
            )
        if role == "button" and name in ("Дальше", "Продолжить", "Я не робот"):
            return _Locator(
                self,
                count=lambda: int(self.stage == "captcha" and self.need_click_captcha),
                kind="dalshe",
            )
        return _Locator(self, count=0)

    def locator(self, selector):
        if selector == "[data-qa='submit-button']":
            return _Locator(self, kind="continue" if self.stage == "start" else "submit")
        if selector == "input[data-qa='credential-type-email']":
            return _Locator(self, count=lambda: int(self.stage == "credentials"), kind="email-type")
        if selector == "[data-qa='applicant-login-input-email']":
            return _Locator(self, count=lambda: int(self.stage == "credentials"))
        if selector == "[data-qa='magritte-pincode-input-field']":
            return _Locator(self, count=lambda: int(self.stage == "code"), kind="code")
        if selector == "[data-qa='account-captcha-picture']":
            return _Locator(
                self,
                count=lambda: int(self.stage == "captcha" and self.need_captcha),
                kind="captcha-pic",
            )
        if selector == "[data-qa='account-captcha-input']":
            return _Locator(
                self,
                count=lambda: int(self.stage == "captcha" and self.need_captcha),
                kind="captcha",
            )
        if selector in ("text=Дальше", 'button:has-text("Дальше")'):
            return _Locator(
                self,
                count=lambda: int(self.stage == "captcha" and self.need_click_captcha),
                kind="dalshe",
            )
        if selector in (
            "[data-qa='modal-overlay'] [data-qa='submit-button']",
            "[data-qa='modal-overlay'] button[type='submit']",
            'button:has-text("Отправить")',
        ):
            return _Locator(
                self,
                count=lambda: int(self.stage == "captcha" and self.need_captcha),
                kind="captcha-send",
            )
        if selector == "[data-qa='account-login-form']":
            return _Locator(self, count=0 if self.context and self.context._cookies else 1)
        if selector == "body":
            return _Locator(self)
        if (
            "recaptcha" in selector
            or "hcaptcha" in selector
            or selector in {".g-recaptcha", ".h-captcha"}
        ):
            return _Locator(self, count=0)
        raise AssertionError(f"unexpected selector: {selector}")

    def wait_for_timeout(self, _milliseconds):
        return None

    def screenshot(self, *, path=None, timeout=None):  # noqa: ARG002
        from pathlib import Path

        if path:
            Path(path).write_bytes(b"\x89PNG")


def _config(tmp_path):
    return type("Config", (), {"storage_state_file": tmp_path / "state.json", "user_agent": None})()


def test_login_with_code_keeps_one_context_and_saves_after_auth(
    monkeypatch, tmp_path, caplog, capsys
):
    page = _Page()
    context = _Context(page)
    monkeypatch.setattr("hhru_bot.auth_code.launch_context", lambda *args, **kwargs: context)
    monkeypatch.setattr("hhru_bot.auth_code.goto_hh", lambda *_args: None)

    code_file = tmp_path / "code.txt"
    code_file.write_text("1234\n", encoding="utf-8")
    caplog.set_level(logging.INFO, logger="hhru_bot.auth_code")
    login_with_code(_config(tmp_path), "person@example.com", code_file=code_file, timeout_seconds=1)

    assert page.email_selected
    assert page.code == "1234"
    assert context.saved is True
    assert (tmp_path / "state.json").exists()
    if os.name != "nt":
        # POSIX-режим 0o600; на Windows chmod выставляет только read-only
        # флаг, а защиту файла даёт ACL профиля пользователя.
        assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o600
    assert "person@example.com" not in caplog.text
    assert "1234" not in caplog.text
    assert "person@example.com" not in capsys.readouterr().out


def test_login_with_code_waits_for_delayed_code_form(monkeypatch, tmp_path):
    page = _Page()
    page.show_code_on_wait = True
    context = _Context(page)
    monkeypatch.setattr("hhru_bot.auth_code.launch_context", lambda *args, **kwargs: context)
    monkeypatch.setattr("hhru_bot.auth_code.goto_hh", lambda *_args: None)
    code_file = tmp_path / "code.txt"
    code_file.write_text("1234", encoding="utf-8")

    login_with_code(_config(tmp_path), "person@example.com", code_file=code_file)

    assert page.code == "1234"
    assert context.saved is True
    assert (tmp_path / "state.json").exists()


def test_login_with_code_waits_for_delayed_credentials_pane(monkeypatch, tmp_path):
    page = _Page()
    page.show_credentials_on_wait = True
    context = _Context(page)
    monkeypatch.setattr("hhru_bot.auth_code.launch_context", lambda *args, **kwargs: context)
    monkeypatch.setattr("hhru_bot.auth_code.goto_hh", lambda *_args: None)
    code_file = tmp_path / "code.txt"
    code_file.write_text("1234", encoding="utf-8")

    login_with_code(_config(tmp_path), "person@example.com", code_file=code_file)

    assert page.email_selected
    assert context.saved is True


def test_login_with_code_wrong_code_is_fail_closed(monkeypatch, tmp_path):
    page = _Page()
    context = _Context(page)
    monkeypatch.setattr("hhru_bot.auth_code.launch_context", lambda *args, **kwargs: context)
    monkeypatch.setattr("hhru_bot.auth_code.goto_hh", lambda *_args: None)
    code_file = tmp_path / "code.txt"
    code_file.write_text("9999", encoding="utf-8")

    with pytest.raises(RuntimeError, match="не подтвердил вход"):
        login_with_code(
            _config(tmp_path), "person@example.com", code_file=code_file, timeout_seconds=0.01
        )
    assert context.saved is None


def test_login_with_code_browser_error_is_fail_closed(monkeypatch, tmp_path):
    page = _Page()
    context = _Context(page)
    monkeypatch.setattr("hhru_bot.auth_code.launch_context", lambda *args, **kwargs: context)
    monkeypatch.setattr("hhru_bot.auth_code.goto_hh", lambda *_args: None)
    monkeypatch.setattr(
        _Locator, "fill", lambda *_args: (_ for _ in ()).throw(PlaywrightError("timeout"))
    )
    code_file = tmp_path / "code.txt"
    code_file.write_text("1234", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Ошибка браузера"):
        login_with_code(_config(tmp_path), "person@example.com", code_file=code_file)
    assert context.saved is None


def test_read_code_stdin_timeout(monkeypatch):
    monkeypatch.setattr("hhru_bot.auth_code.select.select", lambda *args: ([], [], []))
    with pytest.raises(RuntimeError, match="истёк"):
        _read_code(None, 300)


def test_read_code_waits_for_code_file_to_be_populated(monkeypatch, tmp_path):
    code_file = tmp_path / "code.txt"

    def populate_file(_seconds):
        code_file.write_text("1234\n", encoding="utf-8")

    monkeypatch.setattr("hhru_bot.auth_code.time.sleep", populate_file)

    assert _read_code(code_file, 1) == "1234"


def test_credentials_are_not_logged(caplog):
    caplog.set_level(logging.INFO, logger="hhru_bot.auth_code")
    logger = logging.getLogger("hhru_bot.auth_code")
    logger.info("login %s", mask_login("person@example.com"))
    assert "person@example.com" not in caplog.text
    assert "1234" not in caplog.text


def test_login_block_reason_rate_limit():
    from hhru_bot.auth_code import _login_block_reason

    page = _Page(body="Слишком много запросов, попробуйте позже")
    assert "слишком много" in (_login_block_reason(page) or "").casefold()


def test_login_with_code_solves_text_captcha_via_file(monkeypatch, tmp_path, capsys):
    page = _Page()
    page.need_captcha = True
    context = _Context(page)
    monkeypatch.setattr("hhru_bot.auth_code.launch_context", lambda *args, **kwargs: context)
    monkeypatch.setattr("hhru_bot.auth_code.goto_hh", lambda *_args: None)

    def populate(_seconds):
        captcha_file.write_text("Q7K2", encoding="utf-8")

    monkeypatch.setattr("hhru_bot.auth_code.time.sleep", populate)
    code_file = tmp_path / "code.txt"
    code_file.write_text("1234", encoding="utf-8")
    captcha_file = tmp_path / "captcha.txt"
    captcha_image = tmp_path / "captcha.png"

    login_with_code(
        _config(tmp_path),
        "person@example.com",
        code_file=code_file,
        captcha_file=captcha_file,
        captcha_image=captcha_image,
        timeout_seconds=1,
    )

    assert page.captcha_value == "Q7K2"
    assert page.code == "1234"
    assert captcha_image.exists()
    assert captcha_image.with_name("captcha-kind.txt").read_text(encoding="utf-8") == "text"
    assert "[CAPTCHA]" in capsys.readouterr().out
    assert context.saved is True


def test_login_with_code_clicks_robot_gate_via_file(monkeypatch, tmp_path, capsys):
    page = _Page(body="Пожалуйста, подтвердите, что вы не робот")
    page.need_click_captcha = True
    context = _Context(page)
    monkeypatch.setattr("hhru_bot.auth_code.launch_context", lambda *args, **kwargs: context)
    monkeypatch.setattr("hhru_bot.auth_code.goto_hh", lambda *_args: None)

    def populate(_seconds):
        captcha_file.write_text("continue", encoding="utf-8")

    monkeypatch.setattr("hhru_bot.auth_code.time.sleep", populate)
    code_file = tmp_path / "code.txt"
    code_file.write_text("1234", encoding="utf-8")
    captcha_file = tmp_path / "captcha.txt"
    captcha_image = tmp_path / "captcha.png"

    login_with_code(
        _config(tmp_path),
        "person@example.com",
        code_file=code_file,
        captcha_file=captcha_file,
        captcha_image=captcha_image,
        timeout_seconds=1,
    )

    assert page.captcha_ok is True
    assert page.captcha_value is None
    assert page.code == "1234"
    assert captcha_image.exists()
    assert captcha_image.with_name("captcha-kind.txt").read_text(encoding="utf-8") == "click"
    assert "не робот" in capsys.readouterr().out
    assert context.saved is True
