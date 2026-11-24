"""Token-based detection of spoken email addresses."""

# The compact tables and parser predicates are intentionally kept close to their rules.

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

KNOWN_TLDS = frozenset(
    {
        "com",
        "org",
        "net",
        "edu",
        "gov",
        "io",
        "info",
        "biz",
        "co",
        "in",
        "me",
        "ai",
        "us",
        "uk",
        "au",
        "ca",
        "sg",
        "ae",
        "de",
        "eu",
        "dev",
        "app",
        "tech",
    }
)
PROVIDERS = frozenset({"gmail", "yahoo", "rediff", "outlook", "zoho", "msn", "aol", "live"})
SPLIT_PROVIDERS = {
    ("g", "mail"),
    ("hot", "mail"),
    ("out", "look"),
    ("i", "cloud"),
    ("y", "mail"),
    ("proton", "mail"),
    ("zoho", "mail"),
    ("rediff", "mail"),
    ("yahoo", "mail"),
}
UPI_HANDLES = frozenset({"okaxis", "oksbi", "okhdfcbank", "ybl", "paytm", "upi", "ibl", "icici"})
NUMBER_WORDS = frozenset(
    [
        "zero",
        "oh",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
        "thirty",
        "forty",
        "fifty",
        "sixty",
        "seventy",
        "eighty",
        "ninety",
        "double",
        "triple",
    ]
)
DOMAIN_STOPS = frozenset(
    [
        "the",
        "a",
        "an",
        "this",
        "that",
        "these",
        "those",
        "any",
        "some",
        "every",
        "no",
        "my",
        "your",
        "our",
        "his",
        "her",
        "their",
        "its",
        "what",
        "which",
        "all",
        "least",
        "most",
        "first",
        "last",
        "present",
        "once",
        "home",
        "work",
        "night",
        "in",
        "on",
        "of",
        "for",
        "as",
        "with",
        "and",
        "or",
        "but",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
    ]
)
LOCAL_STOPS = frozenset(
    [
        "is",
        "id",
        "email",
        "mail",
        "your",
        "my",
        "address",
        "that",
        "sir",
        "it",
        "to",
        "on",
        "me",
        "send",
        "sent",
        "for",
        "this",
        "and",
        "or",
        "in",
        "please",
        "yes",
        "so",
        "okay",
        "ok",
        "its",
        "it's",
        "will",
        "you",
        "have",
        "has",
        "was",
        "be",
        "check",
        "same",
        "registered",
        "official",
        "contact",
        "at",
    ]
)
FILE_ENDINGS = frozenset(
    {
        "js",
        "jsx",
        "ts",
        "py",
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "csv",
        "exe",
        "sql",
        "json",
        "html",
        "css",
        "txt",
        "jpg",
        "png",
        "zip",
    }
)
SPEAKING_LABELS = frozenset({"lead", "advisor"})
_CORE_LEFT = "([{\"'"
_CORE_RIGHT = ",.;:!?)]}\"'"
_DOMAIN_WORD = re.compile(r"[A-Za-z0-9](?:-?[A-Za-z0-9])*$")
_LOCAL_WORD = re.compile(r"[\w+.'-]+$", re.UNICODE)


@dataclass(frozen=True)
class Token:
    raw_start: int
    raw_end: int
    core_start: int
    core_end: int
    value: str


@lru_cache(maxsize=8)
def tokenize(text: str) -> list[Token]:
    result: list[Token] = []
    for match in re.finditer(r"\S+", text):
        raw = match.group()
        left = len(raw) - len(raw.lstrip(_CORE_LEFT))
        right = len(raw.rstrip(_CORE_RIGHT))
        if raw[left:right].casefold().endswith("'s"):
            right -= 2
        result.append(
            Token(match.start(), match.end(), match.start() + left, match.start() + right, raw[left:right])
        )
    return result


def _label(value: str) -> bool:
    return bool(_DOMAIN_WORD.fullmatch(value)) and any(char.isalpha() for char in value)


def _tld(value: str) -> bool:
    return value.casefold() in KNOWN_TLDS


def _provider(tokens: list[Token], index: int) -> int | None:
    if index >= len(tokens):
        return None
    one = tokens[index].value.casefold().strip("-")
    if index + 1 < len(tokens) and (one, tokens[index + 1].value.casefold()) in SPLIT_PROVIDERS:
        return index + 1
    if one in PROVIDERS or one in {"g-mail", "hotmail", "rediffmail"}:
        return index
    return None


def _ending(tokens: list[Token], index: int, domain_end: int, strong: bool) -> int | None:
    if index >= len(tokens):
        return None
    value = tokens[index].value
    lower = value.casefold()
    # A complete label in one token, including acme.com and dotcom.
    if lower.startswith("dot") and _tld(lower[3:]):
        return index
    if "." in value:
        labels = value.split(".")
        if len(labels) >= 2 and _tld(labels[-1]):
            tld = labels[-1]
            if tld == "com" or (tld.islower() and index + 1 == len(tokens)):
                return index
    if index + 1 < len(tokens) and tokens[index + 1].value.casefold() in KNOWN_TLDS:
        tld = tokens[index + 1].value
        if tld == "com" or (
            tld.islower()
            and (
                index + 2 == len(tokens)
                or (
                    index + 3 < len(tokens)
                    and tokens[index + 2].value.casefold() == "dot"
                    and tokens[index + 3].value.islower()
                )
            )
        ):
            return index + 1
    if lower in {"dot", "period", "point", "full", "full-stop"}:
        next_index = index + 1
        if lower == "full" and next_index < len(tokens) and tokens[next_index].value.casefold() == "stop":
            next_index += 1
        if next_index >= len(tokens):
            return None
        tld = tokens[next_index].value
        if _tld(tld) and (
            tld.casefold() == "com"
            or (
                tld.islower()
                and (
                    next_index + 1 == len(tokens)
                    or (
                        next_index + 2 < len(tokens)
                        and tokens[next_index + 1].value.casefold() == "dot"
                        and tokens[next_index + 2].value.islower()
                    )
                )
            )
        ):
            return next_index
        if (
            lower == "dot"
            and 2 <= len(tld) <= 24
            and tld.casefold() not in NUMBER_WORDS
            and tld.casefold() not in DOMAIN_STOPS
        ):
            if not strong and tld.casefold() in FILE_ENDINGS:
                return None
            if tld.isalpha():
                return next_index
            if next_index + 9 >= len(tokens):
                return next_index
        if lower == "dot" and next_index + 2 < len(tokens):
            letters = "".join(tokens[pos].value for pos in range(next_index, next_index + 3))
            if letters.casefold() in KNOWN_TLDS and all(
                len(tokens[pos].value) == 1 and tokens[pos].value.isalpha()
                for pos in range(next_index, next_index + 3)
            ):
                return next_index + 2
    return None


def _walk_local(tokens: list[Token], before: int, text: str) -> int | None:
    accepted = 0
    words = singles = 0
    start = before
    for index in range(before - 1, max(0, before - 24) - 1, -1):
        token = tokens[index]
        value = token.value
        lower = value.casefold()
        if not value or (lower in SPEAKING_LABELS and index + 1 < len(tokens)):
            break
        if lower in LOCAL_STOPS and not (
            index > 0 and tokens[index - 1].value.casefold() in {"dot", "underscore", "hyphen", "dash"}
        ):
            break
        if not _LOCAL_WORD.fullmatch(value):
            break
        is_single = len(value) == 1 or lower in NUMBER_WORDS or value.isdigit()
        if is_single:
            if singles >= 20:
                break
            singles += 1
        else:
            if words >= 6:
                break
            words += 1
        accepted += 1
        start = token.core_start
    return start if accepted else None


def _domain_start(tokens: list[Token], index: int, strong: bool) -> int | None:
    limit = 4 if strong else 2
    count = 0
    if index < len(tokens) and "." in tokens[index].value:
        labels = tokens[index].value.casefold().split(".")
        if len(labels) > 1 and _tld(labels[-1]):
            return index + 1
    while index + count < len(tokens) and count < limit and _label(tokens[index + count].value):
        value = tokens[index + count].value.casefold()
        if value in {"dot", "period", "point", "full", "stop"} or (
            value.startswith("dot") and _tld(value[3:])
        ):
            break
        if not strong and (value in DOMAIN_STOPS or value.isdigit()):
            break
        count += 1
    return index + count if count else None


def find_spoken_emails(text: str) -> list[tuple[int, int]]:
    if not re.search(r"(?i)(?:@|\bat\b|\b(?:gmail|yahoo|rediff|outlook|zoho|hot|out|proton|i|y|g)\b)", text):
        return []
    tokens = tokenize(text)
    spans: list[tuple[int, int]] = []
    for i, token in enumerate(tokens):
        core = token.value
        if "@" in core:
            left, right = core.split("@", 1)
            if left and right and any(c.isalpha() for c in left) and any(c.isalpha() for c in right):
                if right.casefold() not in UPI_HANDLES and (
                    "." in right
                    or right.casefold() in PROVIDERS
                    or (
                        token.raw_end > token.core_end
                        and text[token.core_end : token.raw_end].startswith(".")
                    )
                ):
                    end = token.core_end
                    if token.raw_end > token.core_end and i + 1 < len(tokens) and _tld(tokens[i + 1].value):
                        end = tokens[i + 1].core_end
                    spans.append((token.core_start, end))
                continue
            if not left and right and ("." in right or _label(right)):
                end = token.core_end
                if token.raw_end > token.core_end and i + 1 < len(tokens) and _tld(tokens[i + 1].value):
                    end = tokens[i + 1].core_end
                local = _walk_local(tokens, i, text)
                if local is not None and right.casefold() not in UPI_HANDLES:
                    spans.append((local, end))
                continue
        anchor_end = i
        strong = False
        if core.casefold() in {"@", "at"}:
            strong = core == "@"
            if (
                core.casefold() == "at"
                and i + 2 < len(tokens)
                and tokens[i + 1].value.casefold() == "the"
                and tokens[i + 2].value.casefold() == "rate"
            ):
                anchor_end, strong = i + 2, True
                if anchor_end + 1 < len(tokens) and tokens[anchor_end + 1].value.casefold() == "of":
                    anchor_end += 1
            elif (
                core.casefold() == "at"
                and i + 2 < len(tokens)
                and tokens[i + 1].value.casefold() == "d"
                and tokens[i + 2].value.casefold() == "rate"
            ):
                anchor_end, strong = i + 2, True
            elif core.casefold() == "at" and i + 1 < len(tokens) and tokens[i + 1].value.casefold() == "sign":
                anchor_end, strong = i + 1, True
        else:
            continue
        domain_at = anchor_end + 1
        provider_end = _provider(tokens, domain_at)
        if provider_end is not None:
            end = provider_end
            if end + 1 < len(tokens) and (
                tokens[end + 1].value.casefold() in {"dot", "period", "point"}
                or tokens[end + 1].value.casefold().startswith("dot")
            ):
                ending = _ending(tokens, end + 1, end + 1, True)
                if ending is not None:
                    end = ending
                    while (
                        end + 2 < len(tokens)
                        and tokens[end + 1].value.casefold() == "dot"
                        and _tld(tokens[end + 2].value)
                    ):
                        end += 2
            elif (
                end + 1 < len(tokens)
                and tokens[end + 1].value.casefold() in KNOWN_TLDS
                and text[tokens[end].core_end : tokens[end].raw_end].startswith(".")
            ):
                end += 1
            local = _walk_local(tokens, i, text)
            if local is not None:
                spans.append((local, tokens[end].core_end))
            continue
        domain_end = _domain_start(tokens, domain_at, strong)
        if domain_end is None:
            continue
        ending = (
            domain_end - 1
            if domain_at < len(tokens) and "." in tokens[domain_at].value
            else _ending(tokens, domain_end, domain_end, strong)
        )
        if ending is None:
            continue
        while (
            ending + 2 < len(tokens)
            and tokens[ending + 1].value.casefold() == "dot"
            and _tld(tokens[ending + 2].value)
        ):
            ending += 2
        if not strong and tokens[domain_at].value.casefold() in {"asp", "vb", "ado", "sharp", "core"}:
            continue
        if (
            not strong
            and ending + 1 < len(tokens)
            and tokens[ending + 1].value.casefold()
            in {
                "developer",
                "framework",
                "core",
                "technology",
                "technologies",
                "stack",
                "project",
                "projects",
                "team",
                "engineer",
                "programmer",
                "jobs",
                "job",
                "role",
            }
        ):
            continue
        local = _walk_local(tokens, i, text)
        if local is not None:
            spans.append((local, tokens[ending].core_end))
    return sorted(set(spans))


def provider_replacements(text: str) -> list[tuple[int, int]]:
    # The provider-only rule is deliberately implemented through the same token table.
    if not re.search(r"(?i)\b(?:gmail|yahoo|rediff|outlook|zoho|hot|out|proton|i|y|g)\b", text):
        return []
    tokens = tokenize(text)
    result: list[tuple[int, int]] = []
    for i in range(len(tokens)):
        end = _provider(tokens, i)
        if end is None:
            continue
        local = _walk_local(tokens, i, text)
        if local is None:
            continue
        if end + 1 < len(tokens):
            ending = _ending(tokens, end + 1, end + 1, True)
            if ending is not None:
                end = ending
            elif (
                tokens[end].raw_end > tokens[end].core_end and tokens[end + 1].value.casefold() in KNOWN_TLDS
            ):
                end += 1
            elif tokens[i].value.casefold() not in {"gmail", "yahoo", "rediff", "outlook", "zoho"}:
                continue
        result.append((local, tokens[end].core_end))
    return result
