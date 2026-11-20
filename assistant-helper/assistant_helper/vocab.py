"""Domain vocabulary correction for STT output (editable JSON, hot-reloaded)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


def _phrase_pattern(phrase: str) -> str:
    tokens = [re.escape(t) for t in re.split(r"[\s\-]+", phrase.strip()) if t]
    return r"[\s\-]+".join(tokens)


class VocabCorrector:
    def __init__(self, path: Path | None = None, replacements: list[dict] | None = None):
        self.path = Path(path) if path else None
        self._mtime: float | None = None
        self._rules: list[tuple[re.Pattern, str]] = []
        if replacements is not None:
            self._compile(replacements)
        else:
            self._reload_if_changed()

    def _compile(self, replacements: list[dict]) -> None:
        pairs: list[tuple[str, str]] = []
        for entry in replacements:
            to = entry.get("to")
            if not to:
                continue
            for src in entry.get("from") or []:
                if src and src.strip():
                    pairs.append((src, to))
        pairs.sort(key=lambda p: len(p[0]), reverse=True)  # longest first
        self._rules = [
            (re.compile(r"(?<![\w])" + _phrase_pattern(src) + r"(?![\w])", re.IGNORECASE), to)
            for src, to in pairs
        ]

    def _reload_if_changed(self) -> None:
        if not self.path or not self.path.exists():
            return
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self.path.read_text())
            self._compile(data.get("replacements", []))
            self._mtime = mtime
        except Exception as e:
            log.error("vocabulary.json invalid (%s); keeping previous rules", e)
            self._mtime = mtime

    def correct(self, text: str) -> str:
        self._reload_if_changed()
        if not text:
            return text
        # One left-to-right pass over all rules combined would be faster, but per-rule
        # passes with longest-first ordering are simple and plenty fast for utterances.
        # Placeholders stop a later (shorter) rule from rewriting an earlier result.
        out = text
        holds: list[str] = []
        for pat, to in self._rules:
            def _sub(_m, to=to):
                holds.append(to)
                return f"\x00{len(holds) - 1}\x00"

            out = pat.sub(_sub, out)
        out = re.sub(r"\x00(\d+)\x00", lambda m: holds[int(m.group(1))], out)
        return re.sub(r"\s{2,}", " ", out).strip()
