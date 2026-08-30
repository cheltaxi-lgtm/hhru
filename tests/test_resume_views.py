from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from hhru_bot.history import History
from hhru_bot.resume_views import (
    _canonicalize_viewed_at,
    has_next_page,
    parse_resume_view_history,
)

pytestmark = pytest.mark.unit


def _html(state: dict) -> str:
    return '<template id="HH-Lux-InitialState">' + json.dumps(state) + "</template>"


def test_parse_resume_view_history_reads_ssr_and_limit():
    html = _html(
        {
            "applicantResumeViewHistory": {
                "historyViews": [
                    {"date": "2026-08-20", "employerId": 7, "employerName": "Acme"},
                    {"viewedAt": "2026-08-19", "companyId": 8, "companyName": "Beta"},
                ]
            }
        }
    )
    assert parse_resume_view_history(html, "r1", limit=1) == [
        {
            "resume_id": "r1",
            "employer_id": "7",
            "employer": "Acme",
            "source_id": None,
            "viewed_at": "2026-08-20T00:00:00+00:00",
            "area": None,
            "vacancies": [],
        }
    ]


def test_parse_resume_view_history_preserves_hidden_same_date_events():
    html = _html(
        {
            "applicantResumeViewHistory": {
                "historyViews": [
                    {"date": "2026-08-20", "id": "v1"},
                    {"date": "2026-08-20", "id": "v2"},
                ]
            }
        }
    )
    rows = parse_resume_view_history(html, "r1")
    # employer stays empty for hidden rows — the source_id carries identity for
    # dedup (history.py's view_key), never leaking into the display name (#428).
    assert [row["employer"] for row in rows] == [None, None]
    assert [row["source_id"] for row in rows] == ["v1", "v2"]


def test_canonicalize_viewed_at_treats_naive_as_utc():
    """A naive timestamp and the equivalent Z-suffixed one must canonicalize
    identically — otherwise the same view can dedup as two rows depending on
    which spelling SSR returned (#428 review)."""
    assert _canonicalize_viewed_at("2026-08-20T10:00:00") == _canonicalize_viewed_at(
        "2026-08-20T10:00:00Z"
    )


def test_canonicalize_viewed_at_normalizes_offsets_to_utc():
    """Same instant in different UTC offsets must canonicalize identically."""
    assert _canonicalize_viewed_at("2026-08-20T13:00:00+03:00") == _canonicalize_viewed_at(
        "2026-08-20T10:00:00Z"
    )


def test_parse_resume_view_history_reads_area_and_vacancies():
    html = _html(
        {
            "applicantResumeViewHistory": {
                "historyViews": [
                    {
                        "date": "2026-08-20",
                        "employerId": 9,
                        "employerName": "Автомир",
                        "area": {"name": "Екатеринбург"},
                        "vacancies": [
                            {
                                "id": 55,
                                "name": "Директор дилерского центра",
                                "area": {"name": "Екатеринбург"},
                            }
                        ],
                    }
                ]
            }
        }
    )
    rows = parse_resume_view_history(html, "r1")
    assert rows[0]["area"] == "Екатеринбург"
    assert rows[0]["vacancies"][0]["title"] == "Директор дилерского центра"
    assert rows[0]["vacancies"][0]["url"] == "https://hh.ru/vacancy/55"


def test_parse_resume_view_history_fails_closed_on_schema_drift():
    with pytest.raises(ValueError):
        parse_resume_view_history(_html({"applicantResumeViewHistory": {}}), "r1")


def test_parse_resume_view_history_fails_closed_on_name_only_entry():
    """An entry with an employer name but neither source_id nor employer_id
    must be rejected, not silently accepted with an empty view_key — two
    such entries on the same date-only viewed_at would otherwise collapse
    into one row in history.py (#428 review, round 12)."""
    html = _html(
        {
            "applicantResumeViewHistory": {
                "historyViews": [{"date": "2026-08-20", "employerName": "Acme"}]
            }
        }
    )
    with pytest.raises(ValueError):
        parse_resume_view_history(html, "r1")


def test_has_next_page_uses_confirmed_numeric_pager():
    class Locator:
        def __init__(self, values):
            self.values = values

        def count(self):
            return len(self.values)

        def nth(self, index):
            return self.values[index]

    class Text:
        def __init__(self, value):
            self.value = value

        def inner_text(self):
            return self.value

    class Page:
        def locator(self, selector):
            if (
                "pager-next" in selector
                or "pagination-next" in selector
                or "rel='next'" in selector
            ):
                return Locator([])
            return Locator([Text("1"), Text("2")])

    assert has_next_page(Page(), 0)
    assert not has_next_page(Page(), 1)


def test_history_page_url_uses_numeric_resume_id() -> None:
    from hhru_bot.resume_views import history_page_url

    assert history_page_url(277045174, 0).endswith("resumeId=277045174&page=0")
    assert "resume=" not in history_page_url("277045174")


def test_parse_resume_view_history_reads_year_day_companies() -> None:
    ms = int(datetime(2026, 8, 27, 6, 0, tzinfo=UTC).timestamp() * 1000)
    html = _html(
        {
            "applicantResumeViewHistory": {
                "historyViews": {
                    "years": [
                        {
                            "year": 2026,
                            "days": [
                                {
                                    "day": 27,
                                    "month": 8,
                                    "companies": [
                                        {
                                            "id": 9,
                                            "name": "Автомир",
                                            "views": [ms, ms + 1000],
                                            "viewed": True,
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                    "total": 2,
                    "new": 0,
                },
                "paging": {"itemsNumber": 2, "currentPage": 0, "itemsOnPage": 100},
            }
        }
    )
    rows = parse_resume_view_history(html, "r1")
    assert len(rows) == 2
    assert rows[0]["employer"] == "Автомир"
    assert rows[0]["employer_id"] == "9"
    assert rows[0]["viewed_at"] == datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()
    assert rows[0]["source_id"] == f"9:{ms}"
    assert rows[1]["source_id"] == f"9:{ms + 1000}"


def test_ssr_history_has_more_from_paging() -> None:
    from hhru_bot.resume_views import ssr_history_has_more

    more = _html(
        {
            "applicantResumeViewHistory": {
                "historyViews": {"years": [], "total": 0, "new": 0},
                "paging": {"itemsNumber": 150, "currentPage": 0, "itemsOnPage": 100},
            }
        }
    )
    done = _html(
        {
            "applicantResumeViewHistory": {
                "historyViews": {"years": [], "total": 0, "new": 0},
                "paging": {"itemsNumber": 54, "currentPage": 0, "itemsOnPage": 100},
            }
        }
    )
    assert ssr_history_has_more(more) is True
    assert ssr_history_has_more(done) is False


def test_history_deduplicates_resume_view_snapshots(tmp_path):
    history = History(tmp_path / "history.db")
    row = {"resume_id": "r1", "employer_id": "7", "employer": "Acme", "viewed_at": "2026-08-20"}
    assert history.record_resume_views([row, row]) == 1
    assert history.record_resume_views([row]) == 0
    assert len(history.resume_views()) == 1


def test_history_dedup_ignores_mutable_employer_name(tmp_path):
    """Same employer_id + viewed_at dedups even if the display name differs
    (renamed employer, or formatting drift) — #428 review."""
    history = History(tmp_path / "history.db")
    row_a = {"resume_id": "r1", "employer_id": "7", "employer": "Acme", "viewed_at": "2026-08-20"}
    row_b = {
        "resume_id": "r1",
        "employer_id": "7",
        "employer": "Acme Corp",
        "viewed_at": "2026-08-20",
    }
    assert history.record_resume_views([row_a]) == 1
    assert history.record_resume_views([row_b]) == 0
    assert len(history.resume_views()) == 1


def test_history_preserves_distinct_hidden_events_same_date(tmp_path):
    history = History(tmp_path / "history.db")
    row_a = {"resume_id": "r1", "source_id": "v1", "viewed_at": "2026-08-20"}
    row_b = {"resume_id": "r1", "source_id": "v2", "viewed_at": "2026-08-20"}
    assert history.record_resume_views([row_a, row_b]) == 2
    assert len(history.resume_views()) == 2


def test_history_view_key_prefers_source_id_over_employer_id(tmp_path):
    """view_key = source_id or employer_id (never the reverse): two SSR
    views of the SAME employer on the SAME date-only viewed_at must stay
    distinct — employer_id + date-only viewed_at alone can't tell them
    apart (#428 review, round 9). Pinned so a future change doesn't
    silently flip the priority and reintroduce that data loss.
    """
    history = History(tmp_path / "history.db")
    row_a = {"resume_id": "r1", "employer_id": "7", "source_id": "v1", "viewed_at": "2026-08-20"}
    row_b = {"resume_id": "r1", "employer_id": "7", "source_id": "v2", "viewed_at": "2026-08-20"}
    assert history.record_resume_views([row_a, row_b]) == 2
    assert len(history.resume_views()) == 2
