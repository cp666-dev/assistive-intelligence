"""Fuzzy matching of "my usual string quartet setup" against saved recipes."""

from __future__ import annotations

import re

from rapidfuzz import fuzz

# Words that signal "a saved setup of mine" rather than a one-off request.
_RECALL = re.compile(
    r"\b(my|our|the)\s+(usual|typical|normal|regular|standard|default|go[\s\-]?to|favou?rite|saved)\b"
    r"|\b(load|recall|apply|use|bring up|set up|setup|do)\s+(my|our)\b",
    re.IGNORECASE,
)
_SAVE = re.compile(
    r"\b(save|store|remember|keep)\b.*\b(as|like)\b.*\b(setup|set up|template|recipe|preset|layout)\b"
    r"|\bsave\s+(this|that|it)\s+as\b",
    re.IGNORECASE,
)
_FILLER = re.compile(
    r"\b(my|our|the|a|usual|typical|normal|regular|standard|default|go[\s\-]?to|favou?rite|saved|"
    r"setup|set up|template|session|layout|recipe|preset|load|recall|apply|use|bring up|please|"
    r"can|could|you|we|get|give|me|us|for|of|up|do|let's|lets)\b",
    re.IGNORECASE,
)

MATCH_THRESHOLD = 72


def wants_recipe(text: str) -> bool:
    return bool(_RECALL.search(text or "")) and not wants_save(text)


def wants_save(text: str) -> bool:
    return bool(_SAVE.search(text or ""))


def _core(text: str) -> str:
    s = _FILLER.sub(" ", text.lower())
    s = re.sub(r"[^\w\s\-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_recipe(text: str, recipes: list[dict], threshold: int = MATCH_THRESHOLD) -> tuple[dict | None, float]:
    """Best recipe for the utterance, by name or alias. Returns (recipe, score 0..100)."""
    if not text or not recipes:
        return None, 0.0
    core = _core(text)
    best, best_score = None, 0.0
    for r in recipes:
        for label in [r.get("name", "")] + list(r.get("aliases") or []):
            if not label:
                continue
            lab = _core(label) or label.lower()
            # partial_ratio finds "string quartet" inside a longer sentence;
            # token_set_ratio forgives word order / extra words.
            score = max(fuzz.partial_ratio(lab, core), fuzz.token_set_ratio(lab, core))
            # don't let a one-word label match any sentence that happens to contain it weakly
            if len(lab) < 4:
                score = 100.0 if re.search(rf"\b{re.escape(lab)}\b", core) else 0.0
            if score > best_score:
                best, best_score = r, score
    if best_score >= threshold:
        return best, best_score
    return None, best_score
