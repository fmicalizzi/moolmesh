"""Minimal ANSI color utilities for CLI output."""

from __future__ import annotations

import os
import sys


def _supported() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    return os.getenv("TERM", "") != "dumb"


_OK = _supported()


def green(text: str) -> str:
    return f"\033[32m{text}\033[0m" if _OK else text


def yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m" if _OK else text


def red(text: str) -> str:
    return f"\033[31m{text}\033[0m" if _OK else text


def dim(text: str) -> str:
    return f"\033[2m{text}\033[0m" if _OK else text


def bold(text: str) -> str:
    return f"\033[1m{text}\033[0m" if _OK else text
