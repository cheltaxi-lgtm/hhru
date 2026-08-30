"""One-process login by an hh.ru email or SMS code."""

from __future__ import annotations

import logging
import re
import select
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .browser import (
    HH_BASE_URL,
    goto_hh,
    has_auth_cookie,
    has_login_form,
    launch_context,
    require_authenticated_page,
)
from .config import AppConfig
from .cookie_import import write_storage_state
from .selectors import (
    LOGIN_CODE_INPUT,
    LOGIN_CODE_REQUEST_BUTTON,
    LOGIN_EMAIL_INPUT,
    LOGIN_EMAIL_TYPE,
    LOGIN_PHONE_INPUT,
)
from .session_security import secure_storage_state_parent

logger = logging.getLogger("hhru_bot.auth_code")

_LOGIN_URL = f"{HH_BASE_URL}/account/login"
CODE_TIMEOUT_SECONDS = 300
CODE_FORM_TIMEOUT_MS = 15_000
CODE_FILE_POLL_SECONDS = 0.1
CAPTCHA_PICTURE = "[data-qa='account-captcha-picture']"
CAPTCHA_INPUT = "[data-qa='account-captcha-input']"
CAPTCHA_MODAL_SUBMIT = "[data-qa='modal-overlay'] [data-qa='submit-button']"


def mask_login(value: str) -> str:
    """Return a log-safe representation of an email address or phone number."""
    value = value.strip()
    if "@" in value:
        local, domain = value.split("@", 1)
        return f"{local[:1]}***@{domain}"
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 7:
        return f"+{digits[:2]}***{digits[-4:]}"
    return "***"


def _read_code(code_file: Path | None, timeout_seconds: int) -> str:
    if code_file is not None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                code = code_file.read_text(encoding="utf-8").strip()
            except OSError:
                code = ""
            if code:
                return code
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"--code-file не появился или остался пустым через {timeout_seconds} секунд"
                )
            time.sleep(min(CODE_FILE_POLL_SECONDS, remaining))
    else:
        ready, _, _ = select.select([sys.stdin], [], [], timeout_seconds)
        if not ready:
            raise RuntimeError(f"Ввод кода истёк через {timeout_seconds} секунд")
        code = sys.stdin.readline().strip()
    if not code:
        raise ValueError("Код не должен быть пустым")
    return code


def _locator_visible(page, selector: str) -> bool:
    locator = page.locator(selector)
    try:
        return locator.count() >= 1 and locator.first.is_visible()
    except (PlaywrightError, PlaywrightTimeoutError, AssertionError, AttributeError):
        return False


def _login_block_reason(page) -> str | None:
    try:
        text = page.locator("body").inner_text().casefold()
    except (PlaywrightError, PlaywrightTimeoutError):
        return None
    checks = (
        ("слишком много", "hh.ru временно не шлёт код — слишком много попыток"),
        ("попробуйте позже", "hh.ru просит повторить запрос кода позже"),
        ("повторите позже", "hh.ru просит повторить запрос кода позже"),
        ("неверный номер", "hh.ru не принял номер телефона"),
        ("неправильный номер", "hh.ru не принял номер телефона"),
        ("некорректный номер", "hh.ru не принял номер телефона"),
    )
    for needle, message in checks:
        if needle in text:
            return message
    return None


def _has_vendor_captcha(page) -> bool:
    return any(
        _locator_visible(page, selector)
        for selector in (
            'iframe[src*="recaptcha" i]',
            'iframe[src*="hcaptcha" i]',
            ".g-recaptcha",
            ".h-captcha",
        )
    )


def _has_text_captcha(page) -> bool:
    return _locator_visible(page, CAPTCHA_PICTURE) or _locator_visible(page, CAPTCHA_INPUT)


def _page_says_robot(page) -> bool:
    try:
        text = page.locator("body").inner_text().casefold()
    except (PlaywrightError, PlaywrightTimeoutError, AssertionError):
        return False
    return "не робот" in text or "подтвердите, что вы" in text


def _has_captcha_challenge(page) -> bool:
    return _has_text_captcha(page) or _page_says_robot(page)


def _click_robot_continue(page) -> None:
    get_by_role = getattr(page, "get_by_role", None)
    if callable(get_by_role):
        for name in ("Дальше", "Продолжить", "Я не робот"):
            locator = get_by_role("button", name=name)
            try:
                if locator.count() >= 1:
                    locator.first.click()
                    page.wait_for_timeout(800)
                    return
            except (PlaywrightError, PlaywrightTimeoutError, AssertionError, AttributeError):
                continue
    for selector in ("text=Дальше", 'button:has-text("Дальше")'):
        if _locator_visible(page, selector):
            page.locator(selector).first.click()
            page.wait_for_timeout(800)
            return
    raise RuntimeError("не удалось нажать «Дальше» на капче hh.ru")


def _submit_text_captcha_answer(page, answer: str) -> None:
    field = page.locator(CAPTCHA_INPUT)
    _wait_for_one_visible(field, "поле капчи")
    field.first.fill(answer)
    try:
        field.first.press("Enter")
        page.wait_for_timeout(400)
        if not _locator_visible(page, CAPTCHA_INPUT):
            return
    except (PlaywrightError, PlaywrightTimeoutError, AssertionError):
        pass
    get_by_role = getattr(page, "get_by_role", None)
    if callable(get_by_role):
        locator = get_by_role("button", name="Отправить")
        try:
            if locator.count() >= 1:
                locator.first.click()
                return
        except (PlaywrightError, PlaywrightTimeoutError, AssertionError, AttributeError):
            pass
    for selector in (
        CAPTCHA_MODAL_SUBMIT,
        "[data-qa='modal-overlay'] button[type='submit']",
        'button:has-text("Отправить")',
    ):
        if _locator_visible(page, selector):
            page.locator(selector).first.click()
            return
    raise RuntimeError("не удалось отправить текстовую капчу")


def _solve_text_captcha(
    page,
    *,
    image_path: Path,
    answer_file: Path | None,
    timeout_seconds: int,
) -> None:
    image_path.parent.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(1200)
    try:
        page.screenshot(path=str(image_path), full_page=True)
    except TypeError:
        page.screenshot(path=str(image_path))
    except (PlaywrightError, PlaywrightTimeoutError, OSError, AttributeError) as exc:
        raise RuntimeError("не удалось сохранить картинку капчи") from exc
    kind = "text" if _locator_visible(page, CAPTCHA_INPUT) else "click"
    image_path.with_name("captcha-kind.txt").write_text(kind, encoding="utf-8")
    if answer_file is not None:
        answer_file.write_text("", encoding="utf-8")
    if kind == "text":
        print("[CAPTCHA] Введите символы с картинки", flush=True)
    else:
        print("[CAPTCHA] Подтвердите, что вы не робот", flush=True)
    answer = _read_code(answer_file, timeout_seconds)
    if kind == "text":
        _submit_text_captcha_answer(page, answer)
    else:
        _click_robot_continue(page)
    page.wait_for_timeout(500)


def _enter_otp(code_field, code: str) -> None:
    """Magritte PIN ignores a single fill(); type digits, then Enter."""
    code_field.click()
    press_seq = getattr(code_field, "press_sequentially", None)
    if callable(press_seq):
        press_seq(code, delay=80)
    else:
        code_field.fill(code)
    try:
        code_field.press("Enter")
    except (PlaywrightError, PlaywrightTimeoutError):
        pass


def _wait_for_one_visible(locator, name: str, page=None) -> None:
    """Wait for SPA hydration, then require one unambiguous control."""
    try:
        locator.first.wait_for(state="visible", timeout=CODE_FORM_TIMEOUT_MS)
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        if page is not None:
            block = _login_block_reason(page)
            if block:
                raise RuntimeError(block) from exc
        raise RuntimeError(f"{name} не отрисовался") from exc
    if locator.count() != 1:
        raise RuntimeError(f"{name} не подтверждён")


def _wait_for_authenticated_page(page, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if has_auth_cookie(page) and not has_login_form(page):
                require_authenticated_page(page)
                return
            page.wait_for_timeout(250)
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            raise RuntimeError("Ошибка проверки входа; сессия не сохранена") from exc
    raise RuntimeError("hh.ru не подтвердил вход по коду; сессия не сохранена")


def _chrome_channel_unavailable(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "distribution 'chrome'" in text or (
        "chrome" in text and ("not found" in text or "doesn't exist" in text)
    )


def login_with_code(
    config: AppConfig,
    login: str,
    *,
    code_file: Path | None = None,
    captcha_file: Path | None = None,
    captcha_image: Path | None = None,
    timeout_seconds: int = CODE_TIMEOUT_SECONDS,
    account_dir: str | Path | None = None,
) -> None:
    """Complete login in one browser process and save only confirmed state."""
    if not login.strip():
        raise ValueError("Логин не должен быть пустым")
    if timeout_seconds <= 0:
        raise ValueError("Таймаут должен быть положительным")
    secure_storage_state_parent(config.storage_state_file, account_dir=account_dir)
    temporary_state = config.storage_state_file.with_name(
        config.storage_state_file.name + ".login-code.tmp.json"
    )
    image_path = captcha_image
    if image_path is None and captcha_file is not None:
        image_path = captcha_file.with_name("captcha.png")
    try:

        def _run(context) -> None:
            page = context.new_page()
            goto_hh(page, _LOGIN_URL)
            continue_button = page.locator(LOGIN_CODE_REQUEST_BUTTON)
            _wait_for_one_visible(continue_button, "кнопка продолжения login")
            continue_button.click()
            if "@" in login:
                email_type = page.locator(LOGIN_EMAIL_TYPE)
                _wait_for_one_visible(email_type, "переключатель email")
                email_type.check(force=True)
                field = page.locator(LOGIN_EMAIL_INPUT)
            else:
                field = page.locator(LOGIN_PHONE_INPUT)
            _wait_for_one_visible(field, "поле логина")
            field.fill(login)
            submit_button = page.locator(LOGIN_CODE_REQUEST_BUTTON)
            _wait_for_one_visible(submit_button, "кнопка отправки кода")
            submit_button.click()
            page.wait_for_timeout(400)
            for _ in range(3):
                if _locator_visible(page, LOGIN_CODE_INPUT):
                    break
                if _has_vendor_captcha(page):
                    raise RuntimeError("hh.ru показал интерактивную капчу без текстового поля")
                if _has_captcha_challenge(page):
                    if captcha_file is None or image_path is None:
                        raise RuntimeError("hh.ru требует капчу")
                    _solve_text_captcha(
                        page,
                        image_path=image_path,
                        answer_file=captcha_file,
                        timeout_seconds=timeout_seconds,
                    )
                    continue
                block = _login_block_reason(page)
                if block:
                    raise RuntimeError(block)
                break
            code_field = page.locator(LOGIN_CODE_INPUT)
            _wait_for_one_visible(code_field, "поле одноразового кода", page=page)
            print(
                f"[WAIT] Код отправлен на {mask_login(login)}. "
                f"Введите код (таймаут {timeout_seconds} сек):",
                flush=True,
            )
            code = _read_code(code_file, timeout_seconds)
            _enter_otp(code_field, code)
            _wait_for_authenticated_page(page, timeout_seconds)
            write_storage_state(
                context.storage_state(), config.storage_state_file, account_dir=account_dir
            )

        try:
            with launch_context(
                temporary_state,
                headless=True,
                user_agent=config.user_agent,
                channel="chrome",
            ) as context:
                _run(context)
        except PlaywrightError as exc:
            if not _chrome_channel_unavailable(exc):
                raise
            logger.warning("Chrome недоступен, fallback на Chromium")
            with launch_context(
                temporary_state, headless=True, user_agent=config.user_agent
            ) as context:
                _run(context)
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        raise RuntimeError("Ошибка браузера при входе; сессия не сохранена") from exc
    finally:
        try:
            temporary_state.unlink()
        except FileNotFoundError:
            pass
    logger.info("Вход по одноразовому коду подтверждён; сессия сохранена")
