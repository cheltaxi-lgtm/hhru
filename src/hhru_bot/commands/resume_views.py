"""Real employer views of resumes (read-only, #415)."""

from __future__ import annotations

import argparse
import json
import sys


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "resume-views", help="Показать реальные просмотры резюме работодателями"
    )
    p.add_argument(
        "--resume",
        action="append",
        help="ID резюме (можно несколько; по умолчанию — все из конфига)",
    )
    p.add_argument(
        "--limit", type=int, default=100, help="Максимум snapshots на резюме (по умолчанию 100)"
    )
    p.add_argument(
        "--max-pages", type=int, default=5, help="Максимум страниц истории (по умолчанию 5)"
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="JSON для внешних клиентов (Koplife Jobs)",
    )
    p.set_defaults(func=run)


def _emit_json(payload: dict, *, ok: bool = True) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    if not ok:
        raise SystemExit(1)


def _fail(message: str, *, as_json: bool) -> None:
    if as_json:
        _emit_json({"ok": False, "error": message, "views": []}, ok=False)
    print(f"[FAIL] {message}", file=sys.stderr)
    raise SystemExit(1)


def _table(rows: list[dict]) -> None:
    headers = ("Дата", "Работодатель", "Резюме")
    body = [(r["viewed_at"], r.get("employer") or "(скрыт)", r["resume_id"]) for r in rows]
    widths = [max([len(h)] + [len(str(x[i])) for x in body]) for i, h in enumerate(headers)]
    border = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    print(border)
    print("| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |")
    print(border)
    for row in body:
        print("| " + " | ".join(str(x).ljust(widths[i]) for i, x in enumerate(row)) + " |")
    print(border)


def run(args: argparse.Namespace) -> None:
    from ..browser import goto_hh, launch_context, require_authenticated_page
    from ..config import load_config_or_exit
    from ..copy_resume import resolve_numeric_resume_ids
    from ..history import History
    from ..resume_views import (
        has_next_page,
        history_page_url,
        parse_resume_view_history,
        ssr_history_has_more,
    )

    as_json = bool(getattr(args, "json", False))
    if args.limit < 1 or args.max_pages < 1:
        _fail("limit и max-pages должны быть >= 1", as_json=as_json)
    config = load_config_or_exit(args.config)
    history = History(args.history)
    resume_keys = (
        args.resume if isinstance(args.resume, list) else ([args.resume] if args.resume else None)
    )
    if not resume_keys:
        resumes = config.resumes
    else:
        from ._common import resolve_resume

        resumes = []
        for key in resume_keys:
            try:
                resumes.append(resolve_resume(config, key))
            except Exception as exc:
                _fail(f"резюме не найдено: {exc}", as_json=as_json)
    if not resumes:
        _fail("резюме не найдено", as_json=as_json)

    fetched: list[dict] = []
    with launch_context(
        config.storage_state_file, headless=args.headless, user_agent=config.user_agent
    ) as context:
        page = context.new_page()
        mapping = resolve_numeric_resume_ids(page)
        for resume in resumes:
            numeric = str(resume.resume_id) if str(resume.resume_id).isdigit() else None
            if numeric is None and mapping is not None:
                numeric = mapping.get(resume.resume_id)
            if not numeric:
                _fail(
                    f"не удалось сопоставить числовой id резюме {resume.resume_id}",
                    as_json=as_json,
                )
            resume_fetched = 0
            truncated = False
            for page_num in range(args.max_pages):
                goto_hh(page, history_page_url(numeric, page_num))
                require_authenticated_page(page)
                html = page.content()
                try:
                    rows = parse_resume_view_history(
                        html,
                        resume.resume_id,
                        limit=args.limit - resume_fetched,
                    )
                except (ValueError, TypeError) as exc:
                    # No DOM fallback (#428 review, round 11): the DOM path's
                    # employer_id/link-based identity was structurally
                    # incompatible with SSR's source_id-based identity — any
                    # view_key priority between the two either lost data or
                    # duplicated it when the source switched between runs.
                    # SSR is the confirmed, curl-verified source (selectors.py);
                    # a parse failure fails closed instead of falling back to
                    # an unconfirmed DOM scrape.
                    _fail(f"история просмотров не подтверждена: {exc}", as_json=as_json)
                fetched.extend(rows)
                resume_fetched += len(rows)
                if not rows or resume_fetched >= args.limit:
                    break
                more_pages = ssr_history_has_more(html)
                if more_pages is None:
                    more_pages = has_next_page(page, page_num)
                if not more_pages:
                    break
                if page_num == args.max_pages - 1:
                    # --max-pages exhausted while the pager still confirms
                    # another page — the fetched history is a partial prefix,
                    # not the complete history. Silently persisting it as
                    # "the" history would mislead the daily trend and
                    # employer aggregation (#428 review, round 11: CLAUDE.md
                    # decision #5 — an unconfirmed-complete result must not
                    # be presented as confirmed).
                    truncated = True
            if truncated:
                print(
                    f"[WARN] история резюме {resume.resume_id} обрезана по --max-pages "
                    f"{args.max_pages}: на сайте есть ещё страницы, увеличьте --max-pages",
                    file=sys.stderr,
                )

    inserted = history.record_resume_views(fetched)
    if as_json:
        _emit_json({"ok": True, "views": fetched, "inserted": inserted})
        return
    stored = history.resume_views(resumes[0].resume_id if resume_keys else None)
    print(f"Просмотры резюме: всего {len(stored)}, новых {inserted}")
    if not stored:
        print("(нет подтверждённых просмотров)")
        return
    print("Тренд по дням:")
    by_day = {}
    for row in stored:
        day = str(row["viewed_at"])[:10]
        by_day[day] = by_day.get(day, 0) + 1
    for day in sorted(by_day, reverse=True):
        print(f"  {day}: {by_day[day]}")
    print("Топ работодателей:")
    by_employer = {}
    for row in stored:
        name = row.get("employer") or "(скрыт)"
        by_employer[name] = by_employer.get(name, 0) + 1
    for name, count in sorted(by_employer.items(), key=lambda item: (-item[1], item[0]))[:10]:
        print(f"  {count}  {name}")
    # --limit is documented as "max snapshots per resume"; a flat slice of the
    # combined multi-resume `stored` list would show up to `limit` rows total
    # (most-recent-first across all resumes) and silently omit later resumes'
    # rows entirely (#428 review). Cap each resume's rows independently instead.
    per_resume: dict[str, list[dict]] = {}
    for row in stored:
        per_resume.setdefault(str(row["resume_id"]), []).append(row)
    table_rows = [row for rows in per_resume.values() for row in rows[: args.limit]]
    _table(table_rows)
