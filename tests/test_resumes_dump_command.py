from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest

from hhru_bot.commands import resumes_dump as dump_cmd

pytestmark = pytest.mark.integration


class _FakeContext:
    def __init__(self, page):
        self.page = page

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def new_page(self):
        return self.page


class _FakeCard:
    def __init__(self, resume_id, title, status="approved", is_searchable=True):
        self.resume_id = resume_id
        self.title = title
        self.status = status
        self.is_searchable = is_searchable
        self.ssr_unavailable = False


def _config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "account:\n  storage_state_file: data/storage_state/hh_session.json\n",
        encoding="utf-8",
    )
    state = tmp_path / "data" / "storage_state" / "hh_session.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"cookies": [{"name": "hhtoken", "value": "x"}]}', encoding="utf-8")
    return path


def test_read_resume_plain_text_prefers_resume_block():
    page = SimpleNamespace(
        locator=lambda selector: SimpleNamespace(
            count=lambda: 1 if selector == "[data-qa='resume']" else 0,
            first=SimpleNamespace(inner_text=lambda: "  опыт python django  " * 4),
        )
    )
    text = dump_cmd.read_resume_plain_text(page)
    assert "python" in text


def test_resumes_dump_prints_json(monkeypatch, tmp_path, capsys):
    page = SimpleNamespace()
    config = _config(tmp_path)
    monkeypatch.setattr(
        "hhru_bot.commands.whoami._check_storage_state",
        lambda _path: (True, "", None),
    )
    monkeypatch.setattr(
        "hhru_bot.config.load_config_or_exit",
        lambda _path: SimpleNamespace(
            storage_state_file=str(tmp_path / "data/storage_state/hh_session.json"),
            user_agent=None,
        ),
    )
    monkeypatch.setattr("hhru_bot.browser.launch_context", lambda *a, **k: _FakeContext(page))
    monkeypatch.setattr("hhru_bot.browser.has_auth_cookie", lambda _p: True)
    monkeypatch.setattr("hhru_bot.browser.goto_hh", lambda *_a, **_k: None)
    monkeypatch.setattr("hhru_bot.browser.has_login_form", lambda _p: False)
    monkeypatch.setattr(
        "hhru_bot.copy_resume.list_resume_cards",
        lambda *_a, **_k: [_FakeCard("abc123", "Python-разработчик")],
    )
    monkeypatch.setattr("hhru_bot.browser.open_confirmed_resume", lambda *_a, **_k: None)
    monkeypatch.setattr(
        dump_cmd,
        "read_resume_plain_text",
        lambda _p: "Python, Django, 5 лет",
    )

    dump_cmd.run(argparse.Namespace(config=str(config), headless=True))
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["resumes"][0]["hh_resume_id"] == "abc123"
    assert data["resumes"][0]["status"] == "опубликовано"
    assert "Django" in data["resumes"][0]["body"]
