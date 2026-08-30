"""respond must pass the resume hash into the apply form, not the numeric id."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hhru_bot.commands import respond as respond_cmd
from hhru_bot.config import ThrottleConfig, bare_resume

pytestmark = pytest.mark.unit


def test_respond_passes_resume_hash_not_numeric_verify_id(monkeypatch, tmp_path):
    """Форма отклика ищет magritte-select-option-{hash}, не числовой resumeId.

    _prepare_apply_resume отдаёт numeric verify_resume_id для SSR переговоров.
    Подставлять его в apply_to_vacancy — отказ «резюме не найдено среди опций»
    (живой прогон 2026-08-28, vacancy 136143178).
    """
    real_resume_id = "1e5303d2ff10835fb60039ed1f633566493437"
    resume = bare_resume(real_resume_id)
    captured: dict[str, str] = {}

    class _NullContext:
        def __enter__(self):
            return SimpleNamespace(new_page=lambda: object())

        def __exit__(self, *_exc):
            return False

    def _fake_apply(_page, _vacancy, resume_id, *_a, **kwargs):
        captured["resume_id"] = resume_id
        captured["has_verifier"] = kwargs.get("verifier") is not None
        return SimpleNamespace(
            success=True,
            skipped=False,
            skip_reason=None,
            acted=False,
            uncertain=False,
            stop_run=False,
            outcome_code="success",
            reason="ok",
        )

    letter = tmp_path / "letter.txt"
    letter.write_text("Добрый день. " * 8, encoding="utf-8")
    config = SimpleNamespace(
        storage_state_file=tmp_path / "state.json",
        user_agent=None,
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0, daily_apply_limit=40),
    )

    monkeypatch.setattr(respond_cmd, "resolve_resume", lambda *_a, **_k: resume)
    monkeypatch.setattr(respond_cmd, "_prepare_apply_resume", lambda *_a, **_k: SimpleNamespace(verify_resume_id="277041349"))
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _p: config)
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *_a, **_k: _NullContext())
    monkeypatch.setattr("hhru_bot.apply.apply_to_vacancy", _fake_apply)

    args = SimpleNamespace(
        resume=real_resume_id,
        vacancy_id="136143178",
        vacancy_url=None,
        letter_file=str(letter),
        json=True,
        headless=True,
        config=None,
        history=str(tmp_path / "history.db"),
    )
    assert respond_cmd.run(args) is False
    assert captured["resume_id"] == real_resume_id
    assert captured["resume_id"] != "277041349"
    assert captured.get("has_verifier") is True


def test_respond_writes_apply_to_history(monkeypatch, tmp_path):
    """respond записывает отклик в history — иначе дневной лимит и has_applied
    для пути Telegram-бота мертвы, а kill процесса после submit не оставляет
    следа (аудит 2026-08-30)."""
    real_resume_id = "1e5303d2ff10835fb60039ed1f633566493437"
    resume = bare_resume(real_resume_id)

    class _NullContext:
        def __enter__(self):
            return SimpleNamespace(new_page=lambda: object())

        def __exit__(self, *_exc):
            return False

    def _fake_apply(_page, _vacancy, resume_id, *_a, **kwargs):
        before_submit = kwargs.get("before_submit")
        assert before_submit is not None
        before_submit()
        return SimpleNamespace(
            success=True,
            skipped=False,
            skip_reason=None,
            acted=True,
            uncertain=False,
            stop_run=False,
            outcome_code="success",
            letter_variant="template",
            reason="ok",
        )

    letter = tmp_path / "letter.txt"
    letter.write_text("Добрый день. " * 8, encoding="utf-8")
    config = SimpleNamespace(
        storage_state_file=tmp_path / "state.json",
        user_agent=None,
        throttle=ThrottleConfig(min_delay_seconds=0, max_delay_seconds=0, daily_apply_limit=40),
    )

    monkeypatch.setattr(respond_cmd, "resolve_resume", lambda *_a, **_k: resume)
    monkeypatch.setattr(
        respond_cmd,
        "_prepare_apply_resume",
        lambda *_a, **_k: SimpleNamespace(verify_resume_id="277041349", account_resume_ids=None),
    )
    monkeypatch.setattr("hhru_bot.config.load_config_or_exit", lambda _p: config)
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *_a, **_k: _NullContext())
    monkeypatch.setattr("hhru_bot.apply.apply_to_vacancy", _fake_apply)
    # Пауза троттлинга после acted — ноль по конфигу, но не зависим от неё.
    monkeypatch.setattr("hhru_bot.throttle.Throttle.wait", lambda *_a, **_k: None)

    history_path = tmp_path / "history.db"
    args = SimpleNamespace(
        resume=real_resume_id,
        vacancy_id="136143178",
        vacancy_url=None,
        letter_file=str(letter),
        json=True,
        headless=True,
        config=None,
        history=str(history_path),
    )
    assert respond_cmd.run(args) is False

    from hhru_bot.history import History

    history = History(str(history_path))
    assert history.has_applied(real_resume_id, "136143178") is True
