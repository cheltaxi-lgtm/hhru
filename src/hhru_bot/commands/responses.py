"""Команда responses: мониторинг ответов работодателей (#12, Этап 2).

Top-level команда ``hhru_bot responses ...`` — регистрируется автоматически через
pkgutil.iter_modules в cli.register_commands (cli.py не трогается).

Поток: открыть /applicant/negotiations → responses.fetch_responses собирает
карточки → history.upsert_response по каждому (account-scope, без клонирования
по резюме) → печать ASCII-сводки «что нового» (status_changed_at с последней
отметки).

Read-only по hh.ru: страница откликов только читается, кликов действий нет.
Вывод только текст/ASCII — НИКАКИХ эмодзи (правило проекта: CLI чистый).
"""

from __future__ import annotations

import argparse
import json
import sys


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("должно быть положительным целым числом")
    return parsed


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "responses",
        help="Проверить ответы работодателей (приглашения/отказы/сообщения)",
    )
    p.add_argument("--resume", help="ID резюме из конфига (по умолчанию — все)")
    p.add_argument(
        "--max-pages",
        type=_positive_int,
        default=5,
        help="Максимум страниц списка откликов (по умолчанию 5)",
    )
    p.add_argument(
        "--since-hours",
        type=float,
        default=24.0,
        help="Показать ответы, сменившие статус за последние N часов (по умолчанию 24). "
        "0 — выполнить живой обход hh.ru и показать синхронизацию/историю.",
    )
    p.add_argument(
        "--detect-external-tests",
        action="store_true",
        help="Прочитать последние сообщения работодателей и записать внешние тесты (#180)",
    )
    p.add_argument(
        "--remindable",
        action="store_true",
        help="Показать переписки, для которых hh.ru явно разрешает напоминание",
    )
    p.add_argument(
        "--sync-applied",
        action="store_true",
        help="Импортировать однозначные ручные/внешние отклики в dedup ledger",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="JSON для внешних клиентов (Koplife Jobs): живой снимок откликов",
    )
    p.add_argument(
        "--with-messages",
        action="store_true",
        help=(
            "Прочитать последнее сообщение в чатах со статусом "
            "response/invitation (лимит 8) и добавить last_message в JSON"
        ),
    )
    p.set_defaults(func=run)


def _emit_json(payload: dict, *, ok: bool = True) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    if not ok:
        raise SystemExit(1)


def _card_payload(card, remindable_ids: set[str], previews: dict | None = None) -> dict:
    topic = card.topic
    payload = {
        "vacancy_id": card.vacancy_id,
        "title": card.title or "",
        "employer": card.employer or "",
        "status": card.status,
        "raw_status": card.raw_status or "",
        "topic": topic,
        "chat_url": card.chat_url,
        "date": card.date or "",
        "resume_id": card.resume_id,
        "topic_ambiguous": bool(card.topic_ambiguous),
        "remindable": bool(topic) and str(topic) in remindable_ids,
    }
    preview = (previews or {}).get(str(topic or ""))
    if preview:
        payload["last_message"] = preview
    return payload


def _remindable_payload(refs) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for ref in refs:
        topic_id = str(ref.topic_id)
        if topic_id in seen:
            continue
        seen.add(topic_id)
        rows.append(
            {
                "topic": topic_id,
                "chat_id": ref.chat_id,
                "vacancy_id": ref.vacancy_id,
                "employer": ref.employer or "",
                "title": ref.vacancy or "",
            }
        )
    return rows


def _print_responses_table(rows: list[dict], title: str) -> None:
    """ASCII-таблица ответов. rows — dict'и из history.new_responses_since."""
    print(f"\n{title}: {len(rows)}")
    if not rows:
        print("  (нет новых ответов за период)")
        return

    # Колонки фиксированной ширины для читаемого выравнивания (чистый ASCII).
    headers = ("Вакансия", "Работодатель", "Статус", "Дата", "Изменён")
    # Статус-ключ → человекочитаемая метка для вывода (storage хранит ключ).
    status_label = {
        "invitation": "Приглашение",
        "response": "Ответ",
        "discard": "Отказ",
        "read": "Прочитано",
        "unknown": "?",
    }
    body = []
    for r in rows:
        vac = r.get("vacancy_id", "")
        emp = (r.get("employer") or "").strip() or "(скрыт)"
        st = status_label.get(r.get("status", ""), r.get("status", "") or "?")
        # Дата ответа с hh.ru как есть (текстовый блок карточки); «-» если hh.ru
        # не отдал блок даты.
        date = (r.get("response_date") or "").strip() or "-"
        # Обрезаем ISO-время до минут: «2026-07-27 14:05» (полная секунда избыточна).
        changed = (r.get("status_changed_at") or "")[:16].replace("T", " ")
        body.append((vac, emp, st, date, changed))

    cols = list(zip(headers, *body, strict=False))
    widths = [max(len(str(c)) for c in col) for col in cols]

    def border() -> str:
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def line(cells) -> str:
        return "| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    print(border())
    print(line(headers))
    print(border())
    for row in body:
        print(line(row))
    print(border())


def run(args: argparse.Namespace) -> None:
    from datetime import datetime, timedelta

    from ..browser import launch_context
    from ..config import load_config_or_exit
    from ..history import History
    from ..responses import NotAuthenticated, ResponsesIndeterminate, fetch_responses

    config = load_config_or_exit(args.config)
    history = History(args.history)

    # «Что нового» меряется по status_changed_at. since = now - since-hours.
    # --sync-applied всегда выполняет живой read, включая --since-hours 0.
    as_json = bool(getattr(args, "json", False))
    remindable_only = getattr(args, "remindable", False)
    sync_applied = getattr(args, "sync_applied", False)
    detect_external = getattr(args, "detect_external_tests", False)
    if as_json and (remindable_only or sync_applied or detect_external):
        _emit_json(
            {
                "ok": False,
                "error": (
                    "--json нельзя совмещать с --remindable, "
                    "--sync-applied или --detect-external-tests"
                ),
                "items": [],
                "remindable": [],
            },
            ok=False,
        )
    if sync_applied and (remindable_only or detect_external):
        print(
            "Ошибка: --sync-applied нельзя совмещать с --remindable или --detect-external-tests",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.max_pages < 1:
        if as_json:
            _emit_json(
                {
                    "ok": False,
                    "error": "--max-pages должен быть положительным",
                    "items": [],
                    "remindable": [],
                },
                ok=False,
            )
        print("Ошибка: --max-pages должен быть положительным", file=sys.stderr)
        sys.exit(2)
    fresh_only = (
        args.since_hours <= 0 and not remindable_only and not sync_applied and not as_json
    )
    since_fetch = datetime.now() - timedelta(hours=args.since_hours)
    # Для сводки «что нового»: в режиме history-only берём вообще всё (min), иначе —
    # окно since-fetch. datetime.min — «любая status_changed_at подходит».
    since_summary = datetime.min if fresh_only else since_fetch

    if not as_json:
        if fresh_only:
            print("\n=== Ответы работодателей (вся история, без обхода hh.ru) ===")
        else:
            print(f"\n=== Ответы работодателей (новое за {args.since_hours:g}ч) ===")

    # Responses — account-scope: страница /applicant/negotiations общая и НЕ несёт
    # достоверного признака принадлежности ответа конкретному резюме. Поэтому
    # карточки persist'ятся ОДИН РАЗ (одна строка на vacancy_id), БЕЗ клонирования
    # под все resume_id из конфига — клонирование фабриковало бы данные (ответ
    # резюме A приписывался бы и резюме B). --resume здесь warn+ignore: фильтр по
    # резюме для ответов работодателя невозможен без достоверной атрибуции.
    if args.resume is not None and not as_json:
        print(
            "Внимание: --resume игнорируется как фильтр обхода — /applicant/negotiations "
            "сканируется на уровне аккаунта; подтверждённая SSR-атрибуция выводится "
            "отдельно как vacancy/topic/resume."
        )

    inserted = updated = unchanged = 0

    if not fresh_only:
        with launch_context(
            config.storage_state_file, headless=args.headless, user_agent=config.user_agent
        ) as context:
            page = context.new_page()
            try:
                if remindable_only:
                    from ..negotiations_probe import paginated_remindable_topic_refs

                    refs = paginated_remindable_topic_refs(page, max_pages=args.max_pages)
                    print(f"Переписки с разрешённым напоминанием: {len(refs)}")
                    for ref in refs:
                        print(
                            f"topic={ref.topic_id} chat={ref.chat_id} "
                            f"работодатель={ref.employer or '-'} "
                            f"вакансия={ref.vacancy} vacancy_id={ref.vacancy_id}"
                        )
                    return
                remindable_refs: list = []
                fetch_kwargs = {
                    "max_pages": args.max_pages,
                    "strict_empty": sync_applied,
                }
                if as_json:
                    fetch_kwargs["remindable_out"] = remindable_refs
                cards = fetch_responses(page, **fetch_kwargs)
                message_previews: dict = {}
                if getattr(args, "with_messages", False):
                    from ..negotiations_chat import (
                        read_chat_previews,
                        topics_for_chat_preview,
                    )
                    from ..negotiations_probe import paginated_topic_refs

                    try:
                        chat_refs = {
                            ref.topic_id: ref.chat_id
                            for ref in paginated_topic_refs(page, max_pages=args.max_pages)
                        }
                        message_previews = read_chat_previews(
                            page, chat_refs, topics_for_chat_preview(cards)
                        )
                    except (NotAuthenticated, ResponsesIndeterminate, ValueError) as exc:
                        print(
                            f"Внимание: не удалось прочитать тексты чатов: {exc}",
                            file=sys.stderr,
                        )
            except (NotAuthenticated, ResponsesIndeterminate, ValueError) as e:
                # Истёкшая сессия или не подтверждённый DOM: НЕ затираем
                # историю и НЕ выдаём неопределённость за «нет новых ответов».
                if as_json:
                    _emit_json(
                        {"ok": False, "error": str(e), "items": [], "remindable": []},
                        ok=False,
                    )
                print(f"Ошибка: {e}", file=sys.stderr)
                sys.exit(1)
                return
            if sync_applied:
                try:
                    synced = history.sync_external_applied(cards)
                except ValueError as e:
                    print(f"Ошибка: {e}", file=sys.stderr)
                    sys.exit(1)
                    return
                print(
                    "Синхронизация внешних откликов: "
                    f"добавлено {synced['imported']}, "
                    f"ambiguous {synced['ambiguous']}, "
                    f"пропущено {synced['skipped']}"
                )
                return

            if args.detect_external_tests:
                # fetch_responses performs its own navigation. Re-open the
                # list read-only so SSR topicList is captured from the actual
                # negotiations page, then use the confirmed chatId route.
                from ..negotiations_chat import (
                    extract_external_test_link,
                    read_employer_messages,
                )
                from ..negotiations_probe import paginated_topic_refs

                refs = {
                    ref.topic_id: ref.chat_id
                    for ref in paginated_topic_refs(page, max_pages=args.max_pages)
                }
                detected = 0
                for card in cards:
                    if not card.topic or card.topic not in refs:
                        continue
                    for message_text in read_employer_messages(page, refs[card.topic]):
                        test_url = extract_external_test_link(message_text)
                        if test_url is None:
                            continue
                        # resume_id=None: как и responses (см. warn выше),
                        # args.resume здесь ничем не подтверждён.
                        # NB (#200): формулировка «привязки не существует»
                        # опровергнута — SSR topicList[] отдаёт resumeId, и
                        # topic_refs() его читает (TopicRef.resume_id). Здесь
                        # он пока не проброшен: это зона #180 (внешние тесты),
                        # менять её в рамках #200 не стали.
                        history.record_test_assigned(
                            card.resume_id,
                            card.vacancy_id,
                            card.topic,
                            card.employer,
                            test_url,
                            message_text,
                        )
                        detected += 1
                print(f"Назначений внешнего теста обнаружено: {detected}")

        if not as_json:
            print(f"Собрано карточек переписки: {len(cards)}")

        skipped_ambiguous = 0
        for card in cards:
            if not as_json:
                print(
                    "[CORRELATION] "
                    f"vacancy_id={card.vacancy_id} topic={card.topic or '-'} "
                    f"resume_id={card.resume_id or '-'}"
                )
            if card.topic_ambiguous:
                # Несколько SSR-topic кандидатов на одну вакансию — fetch_responses
                # намеренно оставил topic=None (см. ResponseItem.topic_ambiguous).
                # history.upsert_response матчит существующую строку по
                # (vacancy_id, topic IS NULL): персистить такую карточку наравне с
                # легитимными без-чата ответами слило бы разные переписки одной
                # вакансии в одну строку истории. Пропускаем запись, не гадаем.
                skipped_ambiguous += 1
                continue
            outcome = history.upsert_response(
                vacancy_id=card.vacancy_id,
                employer=card.employer or None,
                status=card.status,
                chat_url=card.chat_url,
                topic=card.topic,
                response_date=card.date or None,
                resume_id=card.resume_id,
            )
            if outcome == "inserted":
                inserted += 1
            elif outcome == "updated":
                updated += 1
            else:
                unchanged += 1

        if as_json:
            remindable_ids = {str(ref.topic_id) for ref in remindable_refs}
            _emit_json(
                {
                    "ok": True,
                    "error": None,
                    "items": [
                        _card_payload(card, remindable_ids, message_previews)
                        for card in cards
                    ],
                    "remindable": _remindable_payload(remindable_refs),
                }
            )
            return

        print(
            f"Новых ответов: {inserted + updated} "
            f"(новых записей: {inserted}, смен статуса: {updated})"
        )
        if skipped_ambiguous:
            print(
                f"[WARN] Пропущено записей с неоднозначным topic: {skipped_ambiguous} "
                "(несколько переписок на одну вакансию, сопоставление с чатом не "
                "подтверждено — см. лог warning)"
            )
    else:
        print("Режим --since-hours 0: обход hh.ru пропущен, вывожу всю историю ответов.")

    # Сводка «что нового» по истории (account-scope — без фильтра по resume_id).
    rows = history.new_responses_since(since_summary)
    _print_responses_table(rows, "Новые ответы работодателей")
