"""Один отклик на указанную вакансию с готовым сопроводительным письмом."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..search import VacancyCard, _extract_vacancy_id
from ._common import _prepare_apply_resume, resolve_resume


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "respond",
        help="Откликнуться на одну вакансию и вставить готовое сопроводительное в форму",
    )
    p.add_argument("--resume", required=True, help="Slug из конфига или hash резюме hh.ru")
    p.add_argument("--vacancy-url", help="URL вакансии https://hh.ru/vacancy/<id>")
    p.add_argument("--vacancy-id", help="Числовой ID вакансии")
    p.add_argument("--letter-file", required=True, help="Файл с текстом сопроводительного")
    p.add_argument("--json", action="store_true", help="JSON для внешних клиентов")
    p.set_defaults(func=run)


def _emit(payload: dict, *, as_json: bool, ok: bool) -> bool:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), flush=True)
    else:
        status = "[OK]" if payload.get("success") else "[FAIL]"
        reason = str(payload.get("reason") or payload.get("error") or "")
        print(f"{status} {reason}".strip())
    return not ok


def _vacancy_from_args(args: argparse.Namespace) -> VacancyCard:
    url = str(getattr(args, "vacancy_url", None) or "").strip()
    vacancy_id = str(getattr(args, "vacancy_id", None) or "").strip()
    if url:
        extracted = _extract_vacancy_id(url)
        if not extracted:
            raise ValueError(f"не удалось извлечь id вакансии из URL: {url}")
        vacancy_id = extracted
    elif vacancy_id.isdigit():
        url = f"https://hh.ru/vacancy/{vacancy_id}"
    else:
        raise ValueError("укажите --vacancy-url или --vacancy-id")
    return VacancyCard(
        vacancy_id=vacancy_id,
        title=f"вакансия {vacancy_id}",
        company="",
        url=url,
    )


def run(args: argparse.Namespace) -> bool:
    from ..apply import apply_to_vacancy
    from ..apply.antibot import AntiBotChallengeDetected
    from ..apply.letter import FixedCoverLetterProvider
    from ..apply.verify import verify_response_in_negotiations
    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..history import History
    from ..throttle import LimitReached, Throttle

    as_json = bool(getattr(args, "json", False))

    def fail(message: str, **extra) -> bool:
        payload = {"ok": False, "success": False, "error": message, "reason": message, **extra}
        return _emit(payload, as_json=as_json, ok=False)

    letter_path = Path(str(args.letter_file))
    try:
        letter = letter_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return fail(f"не удалось прочитать письмо: {exc}")
    if len(letter) < 40:
        return fail("сопроводительное письмо слишком короткое")

    try:
        vacancy = _vacancy_from_args(args)
    except ValueError as exc:
        return fail(str(exc))

    try:
        config = load_config_or_exit(args.config)
    except SystemExit:
        return fail("конфиг hhru не найден или битый", error_code="config")
    try:
        history = History(args.history)
    except Exception as exc:
        return fail(f"history недоступна: {exc}", error_code="history")
    try:
        resume = resolve_resume(config, str(args.resume).strip())
    except Exception as exc:
        return fail(f"резюме не найдено: {exc}")

    throttle = Throttle(config.throttle, history)
    try:
        throttle.check_apply_limit(resume.resume_id, dry_run=False)
    except LimitReached as exc:
        return fail(str(exc), skipped=True)

    action_id: int | None = None

    def _before_submit() -> None:
        nonlocal action_id
        # Как #245 в run_apply_for_resume: резервируем uncertain-строку до
        # необратимого submit. Иначе kill процесса после клика не оставляет
        # следа — ретрай шлёт дубликат. Плюс без записи дневной лимит и
        # has_applied для пути Telegram-бота не работали вовсе.
        action_id = history.begin_action(resume.resume_id, vacancy.vacancy_id, "apply")

    try:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            identity = _prepare_apply_resume(page, resume, dry_run=False)
            if identity is None:
                return fail("резюме на hh.ru не готово к отклику")

            def _verifier(page, vacancy_id, _pipeline_resume_id):  # noqa: ANN001
                return verify_response_in_negotiations(
                    page,
                    vacancy_id,
                    identity.verify_resume_id,
                    account_resume_ids=identity.account_resume_ids,
                )

            # Форма отклика адресует опцию как magritte-select-option-{hash}
            # (хвост resume_url). identity.verify_resume_id — числовой id для
            # SSR /applicant/negotiations; подставлять его в форму нельзя:
            # опция не находится, отклик отменяется (живой прогон 2026-08-28,
            # vacancy 136143178, id 277041349). Тот же контракт, что у
            # run_apply_for_resume и probe: в pipeline идёт resume.resume_id.
            result = apply_to_vacancy(
                page,
                vacancy,
                resume.resume_id,
                letter,
                False,
                letter_provider=FixedCoverLetterProvider(letter),
                verifier=_verifier,
                before_submit=_before_submit,
            )
    except AntiBotChallengeDetected as exc:
        if action_id is not None:
            history.finalize_action(action_id, "uncertain", str(exc), reason_code="uncertain")
        return fail(str(exc)[:400], error_code="antibot")
    except Exception as exc:
        if action_id is not None:
            history.finalize_action(action_id, "uncertain", str(exc)[:400], reason_code="uncertain")
        return fail(str(exc)[:400], session_dead="форму входа" in str(exc))

    if result.skipped:
        history.record_skip(resume.resume_id, vacancy.vacancy_id, result.skip_reason)
    if action_id is not None:
        status = "uncertain" if result.uncertain else ("success" if result.success else "failed")
        history.finalize_action(
            action_id,
            status,
            result.reason,
            letter_variant=result.letter_variant,
            reason_code=result.outcome_code or status,
        )
    elif result.acted:
        # before_submit не успел отработать, но submit был (переходная страница
        # сразу показала post-click состояние) — пишем action напрямую.
        history.record_action(
            resume.resume_id,
            vacancy.vacancy_id,
            "apply",
            "uncertain" if result.uncertain else ("success" if result.success else "failed"),
            result.reason,
            letter_variant=result.letter_variant,
            reason_code=result.outcome_code or None,
        )

    reason = result.reason or ""
    session_dead = "Сессия недействительна" in reason or "не авторизован" in reason
    payload = {
        "ok": bool(result.success or result.skipped),
        "success": bool(result.success),
        "skipped": bool(result.skipped),
        "skip_reason": result.skip_reason if result.skipped else None,
        "acted": bool(result.acted),
        "uncertain": bool(result.uncertain),
        "stop_run": bool(getattr(result, "stop_run", False)),
        "outcome_code": getattr(result, "outcome_code", "") or "",
        "reason": reason,
        "error": None if (result.success or result.skipped) else (reason or "отклик не ушёл"),
        "session_dead": session_dead,
    }
    if result.acted:
        throttle.wait("после отклика через respond")
    return _emit(payload, as_json=as_json, ok=payload["ok"])
