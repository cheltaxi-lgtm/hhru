"""Command for one-process login by an hh.ru email or SMS code."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(subparsers) -> None:
    p = subparsers.add_parser("login-code", help="Войти по коду hh.ru в одном процессе")
    p.add_argument("--login", required=True, help="Email или телефон")
    p.add_argument(
        "--code-file",
        type=Path,
        help="Файл с одноразовым кодом; без него код читается из stdin",
    )
    p.add_argument(
        "--captcha-file",
        type=Path,
        help="Файл с ответом на текстовую капчу; без него капча читается из stdin",
    )
    p.add_argument(
        "--captcha-image",
        type=Path,
        help="Куда сохранить картинку капчи для ручного ввода",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from ..auth_code import login_with_code
    from ..config import load_config_or_exit

    config = load_config_or_exit(args.config)
    login_with_code(
        config,
        args.login,
        code_file=args.code_file,
        captcha_file=args.captcha_file,
        captcha_image=args.captcha_image,
    )
    print("[OK] Сессия сохранена")
