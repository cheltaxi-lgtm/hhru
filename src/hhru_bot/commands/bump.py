"""Команда bump: поднять резюме в поиске (кулдаун 4ч, дневной лимит)."""

from __future__ import annotations

import argparse

from ._audit import action_status, record_resume_action
from ._common import ApplyProgress, resolve_resume, run_supervised_command


def register(subparsers) -> None:
    p = subparsers.add_parser("bump", help="Поднять резюме в поиске")
    p.add_argument(
        "--resume",
        action="append",
        help="Slug из конфига или resume_id HH.ru (можно несколько; по умолчанию — все из конфига)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать, что будет сделано, без реальных действий",
    )
    p.set_defaults(func=run)


def resumes_for_bump(config, args: argparse.Namespace):
    """Резюме для bump: slug/hash из --resume (в т.ч. bare-hash) или все из конфига.

    jobs передаёт hh-хэши опубликованных резюме; в account config.yaml их
    обычно нет, поэтому здесь resolve_resume, а не get_resume/resumes_from_args.
    """
    raw = getattr(args, "resume", None)
    if not raw:
        return list(config.resumes)
    keys = raw if isinstance(raw, list) else [raw]
    out = []
    for key in keys:
        text = str(key or "").strip()
        if text:
            out.append(resolve_resume(config, text))
    return out


def _reconcile_bump_progress(progress: ApplyProgress, _history, _run_id: str) -> None:
    """Classify an interrupted in-flight bump as failed for bump's summary.

    Bump has no ``uncertain`` *run* counter: its single remote operation is
    either successful or failed from the command's perspective.  The action
    audit still retains ``uncertain`` when a click may have reached hh.ru, so
    the cooldown and daily limit remain fail-closed.
    """
    completed = progress.applied_count + progress.failed_count
    if progress.attempted_count > completed:
        progress.failed_count += progress.attempted_count - completed


def _run(args: argparse.Namespace, config, history, progress: ApplyProgress) -> bool:
    from ..browser import launch_context
    from ..bump import bump_resume
    from ..throttle import LimitReached, Throttle

    resumes = resumes_for_bump(config, args)
    throttle = Throttle(config.throttle, history)
    failed = False

    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        for resume in resumes:
            print(f"\n=== Поднятие резюме: {resume.id} ===")

            try:
                throttle.check_bump_limit(resume.resume_id, args.dry_run)
            except LimitReached as e:
                print(f"Пропуск: {e}")
                continue

            can_bump, wait_left = throttle.can_bump_now(resume.resume_id)
            if not can_bump:
                print(f"Пропуск: рано поднимать, подождите ещё {wait_left}")
                continue

            progress.begin_attempt()
            result = bump_resume(page, resume, args.dry_run)

            # #163: actions — журнал реальных взаимодействий с hh.ru. Dry-run
            # ничего не кликает и не записывается в историю.
            if result.acted:
                # #176: uncertain — клик мог уйти, но результат неизвестен.
                # Такой статус видят кулдаун can_bump_now и дневной лимит
                # (count_today), поэтому «просто failed» не годится: он не
                # остановил бы повторное поднятие раньше 4ч. dry_run по
                # определению без клика — uncertain там невозможен.
                status = action_status(
                    dry_run=args.dry_run, success=result.success, uncertain=result.uncertain
                )
                # Для action='bump' нет естественного vacancy_id (поднятие резюме,
                # не отклик). actions.vacancy_id NOT NULL — заполняем resume.resume_id
                # как sentinel; UNIQUE-индекс idx_resume_vacancy_apply существует
                # только WHERE action='apply', так что коллизий нет.
                record_resume_action(
                    history,
                    resume.resume_id,
                    "bump",
                    status,
                    result.reason,
                    run_id=progress.run_id,
                )

            if result.success:
                progress.applied_count += 1
                print(f"  [OK] {resume.id} поднято")
            else:
                progress.failed_count += 1
                failed = True
                print(f"  [FAIL] {resume.id} — {result.reason}")

            # #163: анти-бан-пауза — только после реального действия на hh.ru
            # (клика по кнопке). Ранние выходы не оставляют на сайте следа,
            # пауза там не от чего не защищает; после реального поднятия
            # троттлинг обязателен (CLAUDE.md: «не убирай троттлинг/лимиты»).
            if result.acted:
                throttle.wait(f"после поднятия резюме '{resume.id}'")
    return failed


def run(args: argparse.Namespace):
    """Run bump under its own durable command ledger entry."""
    from ..config import load_config_or_exit
    from ..history import History

    config = load_config_or_exit(args.config)
    history = History(args.history)

    def _body(progress: ApplyProgress) -> bool:
        return _run(args, config, history, progress)

    return run_supervised_command(
        command="bump",
        history=history,
        requested_limit=None,
        body=_body,
        reconcile=_reconcile_bump_progress,
    )
