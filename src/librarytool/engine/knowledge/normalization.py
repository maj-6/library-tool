"""Versioned deterministic normalization for historical-text search."""

from __future__ import annotations

import unicodedata

from ..errors import ValidationError
from ._json import derived_revision


_LIGATURES = {
    "\u017f": "s",
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "st",
    "\ufb06": "st",
    "\u00e6": "ae",
    "\u0153": "oe",
}

# JavaScript's ``\s`` set.  Keeping this explicit makes the normalization
# stable across Python/Unicode releases and preserves parity with current web
# search behavior.
_SEARCH_SPACE = frozenset(
    "\t\n\v\f\r \u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def _fold_character(character: str) -> str:
    lowered = character.lower()
    expanded = _LIGATURES.get(lowered, lowered)
    return "".join(
        value
        for value in unicodedata.normalize("NFD", expanded)
        if not "\u0300" <= value <= "\u036f"
    )


class HistoricalSearchNormalizer:
    """The built-in normalization profile used by the lexical baseline."""

    normalizer_id = "historical-search"
    version = 1

    @property
    def revision(self) -> str:
        return derived_revision(
            "nr",
            {
                "id": self.normalizer_id,
                "version": self.version,
                # ``lower`` and NFD are supplied by Python's Unicode database.
                # Pinning that data version makes runtime-driven changes visible
                # to persisted passage and retrieval revisions.
                "unicode_data_version": unicodedata.unidata_version,
            },
        )

    def normalize(self, text: str) -> str:
        if not isinstance(text, str):
            raise ValidationError(
                "search text must be a string",
                code="invalid_search_text",
                details={"value_type": type(text).__name__},
            )

        output: list[str] = []
        index = 0
        while index < len(text):
            character = text[index]
            if character in "-\u00ad\u2010":
                cursor = index + 1
                while cursor < len(text) and text[cursor] in " \t\r":
                    cursor += 1
                if cursor < len(text) and text[cursor] == "\n":
                    cursor += 1
                    while cursor < len(text) and text[cursor] in _SEARCH_SPACE:
                        cursor += 1
                    index = cursor
                    continue
                if character == "\u00ad":
                    index += 1
                    continue
            if character in _SEARCH_SPACE:
                cursor = index + 1
                while cursor < len(text) and text[cursor] in _SEARCH_SPACE:
                    cursor += 1
                if output and output[-1] != " ":
                    output.append(" ")
                index = cursor
                continue
            folded = _fold_character(character)
            if folded:
                output.append(folded)
            index += 1
        if output and output[-1] == " ":
            output.pop()
        return "".join(output)


DEFAULT_NORMALIZER = HistoricalSearchNormalizer()


def normalize_search_text(text: str) -> str:
    return DEFAULT_NORMALIZER.normalize(text)


__all__ = [
    "DEFAULT_NORMALIZER",
    "HistoricalSearchNormalizer",
    "normalize_search_text",
]
