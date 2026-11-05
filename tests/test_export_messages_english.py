"""Messages of the snapshot and effort exports stay in English (#156).

Callers such as the mool-ops engine embed them in their own English output.
This reads the source, so it also covers the paths that are hard to trigger:
every message raised by the export modules, and what the two CLI commands print.
"""
import ast
import re
import unittest
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent / "hub"
RAISERS = {"SnapshotError", "EffortError", "DeviceError", "warn"}
# Spanish words seen in these messages. A word list as well as the ASCII check,
# because most Spanish text has no accent.
SPANISH = re.compile(
    r"\b(aviso|avisos|debe|ser|fecha|recib[ií]|registrado|registr[aá]|repos? no|"
    r"todav[ií]a|sin|nunca|obligatorio|falta|lista|pas[aá]|us[aá]|corr[eé]|"
    r"esper[aá]|export[aá]|pude|ninguno|se omite|existe|esta|m[aá]quina|"
    r"posterior|per[ií]odo|zona|archivo|ya|hay|crea|cubre[n]?|datos)\b",
    re.IGNORECASE)


def _strings(node):
    """Every string literal under a node, including those inside an f-string."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
    return out


def _reason_token(call, text):
    """The first argument of an error is its stable reason, not a message."""
    return (getattr(call.func, "id", "") != "warn" and call.args
            and isinstance(call.args[0], ast.Constant) and call.args[0].value == text)


def _messages(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") in RAISERS:
            yield from (s for s in _strings(n) if not _reason_token(n, s))


def _cli_messages():
    tree = ast.parse((HUB / "cli.py").read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name in ("cmd_snapshot_export", "cmd_effort_export"):
            yield from _messages(fn)
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "print":
                    yield from _strings(n)


def _all_messages():
    out = list(_cli_messages())
    for name in ("snapshot.py", "effort.py", "device.py"):
        out += _messages(ast.parse((HUB / name).read_text(encoding="utf-8")))
    return out


class ExportMessagesAreEnglish(unittest.TestCase):
    def test_the_scan_finds_the_messages(self):
        text = " ".join(_all_messages())
        self.assertGreater(len(_all_messages()), 25)
        for known in ("repo not registered", "--timezone is required", "no device identity",
                      "warning: ", "the last sync does not cover"):
            self.assertIn(known, text)

    def test_ascii_and_no_spanish_words(self):
        for msg in _all_messages():
            self.assertTrue(msg.isascii(), msg)
            self.assertIsNone(SPANISH.search(msg), msg)

    def test_the_word_list_catches_what_this_issue_fixed(self):
        for old in ("repo no registrado: x", "--org es obligatorio", "no pude leer github.db",
                    "falta la lista de repos", "aviso: x"):
            self.assertIsNotNone(SPANISH.search(old), old)
