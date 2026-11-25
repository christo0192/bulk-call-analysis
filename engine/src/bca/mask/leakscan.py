"""Independent, configurable post-mask leak scanning."""
# ruff: noqa

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .fields import free_text_values
from .patterns import (
    _aadhaar,
    _card,
    _digits,
    _email,
    _money_spans,
    _number_word_spans,
    _pan,
    _phone,
    _spelled_pan,
    _upi,
    _url,
)

DOMAIN_STOPS = frozenset(
    "the a an this that these those any some every no my your our his her their its what which all least most first last present once home work night in on of for as with and or but one two three four five six seven eight nine ten".split()
)  # noqa: E501, SIM905
ENDING_STOPS = frozenset(
    "it is was were that this the so we he she they you if then also because but and i".split()
)  # noqa: E501, SIM905
FILE_ENDINGS = frozenset(
    "js jsx ts py pdf doc docx xls xlsx csv exe sql json html css txt jpg png zip".split()
)  # noqa: E501, SIM905
KNOWN_TLDS = frozenset(
    "com org net edu gov io info biz co in me ai us uk au ca sg ae de eu dev app tech".split()
)  # noqa: E501, SIM905
NUMBER_WORDS = frozenset(
    "zero oh one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety double triple".split()
)  # noqa: E501, SIM905
ORDINARY_BEFORE_AT = frozenset(
    "is id email mail your my address that sir it to on me send sent for this and or in please yes so okay ok its it's will you have has was be check same registered official contact at the a an your his her their our this am are were been being we us they he she him them look looks looking looked work works working worked job offer joined join visit visited visiting saw see seen checking checked available here there stuck good bad better best placed selected rejected interviewed employed studied study studying live living lived stay staying stayed reached arrived office home ad ads team people friend friends manager hr batches classes currently now also still even only just recently already both call applied applying enrolled enroll signed posted listed found read watched practiced practice practicing solved solving learning learned learnt certified course courses review reviews batch training trained internship interview interviews round rounds opening openings profile account hired hiring met meet meeting counsellor counselor advisor adviser mentor lead recruiter teacher trainer colleague colleagues senior seniors junior juniors boss cousin uncle aunt father mother brother sister wife husband".split()
)  # noqa: E501, SIM905
PROVIDERS = frozenset("gmail yahoo rediff outlook hotmail rediffmail icloud ymail protonmail".split())  # noqa: SIM905
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
    ("red", "gmail"),
}
UPI_HANDLES = frozenset("okaxis oksbi okhdfcbank ybl paytm upi ibl icici".split())  # noqa: SIM905


@dataclass(frozen=True)
class Hit:
    detector: str
    start: int
    end: int
    precision: str = "high"


@dataclass(frozen=True)
class LeakReport:
    hits: list[Hit]

    @property
    def held_back(self) -> bool:
        return any(hit.precision == "high" for hit in self.hits)


_detectors: dict[str, tuple[re.Pattern[str], str]] = {
    "PHONE_LIKE": (_phone, "high"),
    "EMAIL_LIKE": (_email, "high"),
    "PAN_LIKE": (_pan, "high"),
    "AADHAAR_LIKE": (_aadhaar, "high"),
    "CARD_LIKE": (_card, "high"),
    "DIGITS_LIKE": (_digits, "high"),
    "URL_LIKE": (_url, "high"),
    "UPI_LIKE": (_upi, "high"),
    "HANDLE": (re.compile(r"(?<!\w)@[A-Za-z0-9_]{3,}"), "medium"),
}
_placeholder = re.compile(r"\[[A-Z_]+\]")


def _scan_spoken_email_tokens_legacy(text: str) -> list[Hit]:
    scrubbed = _placeholder.sub(lambda match: " " * len(match.group()), text)
    tokens = list(re.finditer(r"\S+", scrubbed))
    hits: list[Hit] = []
    for i, match in enumerate(tokens):
        raw = match.group().strip(",.;:!?)]}" + "'")
        lower = raw.casefold()
        if "@" in raw:
            left, right = raw.split("@", 1)
            if (
                left
                and right
                and any(c.isalpha() for c in right)
                and (any(c.isalpha() for c in left) or left.isdigit())
                and right.casefold() not in UPI_HANDLES
            ):
                hits.append(Hit("EMAIL_LIKE", match.start(), match.end()))
                continue
            if not left and right and any(c.isalpha() for c in right) and right.casefold() not in UPI_HANDLES:
                start = i
                if i > 0 and tokens[i - 1].group().strip(",.;:!?)]}" + "'").casefold() not in DOMAIN_STOPS:
                    hits.append(Hit("EMAIL_LIKE", tokens[i - 1].start(), match.end()))
                continue
        anchor = lower in {"at", "@"}
        if lower == "at" and i > 0:
            previous = tokens[i - 1].group().strip(",.;:!?)]}" + "'").casefold()
            if (
                previous in ORDINARY_BEFORE_AT
                and not any(char.isdigit() or char in "._-" for char in previous)
                and not (len(previous) == 1 and previous.isalpha())
                and not (
                    i > 1
                    and (
                        tokens[i - 2].group().casefold() in {"dot", "underscore", "hyphen", "dash"}
                        or tokens[i - 2].group().endswith((".", "_"))
                    )
                )
            ):
                continue
        if (
            lower == "at"
            and i + 2 < len(tokens)
            and tokens[i + 1].group().casefold() in {"the", "d", "da"}
            and tokens[i + 2].group().casefold() == "rate"
        ):
            anchor = True
            i_end = i + 2
            if i_end + 1 < len(tokens) and tokens[i_end + 1].group().casefold() == "of":
                i_end += 1
        elif lower == "at" and i + 1 < len(tokens) and tokens[i + 1].group().casefold() == "rate":
            anchor = True
            i_end = i + 1
            if i_end + 1 < len(tokens) and tokens[i_end + 1].group().casefold() == "of":
                i_end += 1
        elif lower == "at" and i + 1 < len(tokens) and tokens[i + 1].group().casefold() == "sign":
            anchor, i_end = True, i + 1
        else:
            i_end = i
        if not anchor:
            continue
        start = i_end + 1
        if start >= len(tokens):
            continue
        provider_end = None
        first = tokens[start].group().strip(",.;:!?)]}" + "'").casefold()
        if first in {"asp", "vb", "ado", "sharp", "core"}:
            continue
        split_provider = (
            start + 1 < len(tokens) and (first, tokens[start + 1].group().casefold()) in SPLIT_PROVIDERS
        )
        first_base, first_dot, first_tld = first.partition(".")
        glued_domain = bool(
            first_dot
            and first_base.isalnum()
            and first_tld.split(".")[-1] in KNOWN_TLDS
            and all(part.isalnum() for part in first.split("."))
        )
        if first in PROVIDERS or split_provider or glued_domain:
            provider_end = start + 1 if split_provider else start
        end = provider_end
        if end is None:
            limit = 4
            for candidate in range(start + 1, min(len(tokens), start + limit + 1)):
                word = tokens[candidate - 1].group().strip(",.;:!?)]}" + "'").casefold()
                if word in DOMAIN_STOPS and word not in NUMBER_WORDS:
                    break
                marker = (
                    tokens[candidate].group().strip(",.;:!?)]}" + "'").casefold()
                    if candidate < len(tokens)
                    else ""
                )
                tld_word = (
                    tokens[candidate + 1].group().strip(",.;:!?)]}" + "'")
                    if candidate + 1 < len(tokens)
                    else ""
                )
                if marker in {"period", "point", "full"} and tld_word.casefold() not in KNOWN_TLDS:
                    continue
                raw_marker = tokens[candidate].group().strip(",.;:!?)]}" + "'")
                if marker in KNOWN_TLDS:
                    if marker != "com" and raw_marker != marker:
                        continue
                    if not (
                        raw_marker.startswith(".")
                        or tokens[candidate - 1].group().rstrip().endswith(".")
                        or tokens[candidate - 1].group().casefold() in {"dot", "period", "point"}
                    ):
                        continue
                    if marker != "com" and candidate + 1 < len(tokens):
                        following = tokens[candidate + 1].group().strip(",.;:!?)]}" + "'").casefold()
                        if (
                            following
                            and following not in KNOWN_TLDS
                            and not following.startswith((".", ",", ";", ":"))
                        ):
                            continue
                    if (
                        tokens[candidate - 1].group().casefold() in {"dot", "period", "point"}
                        and candidate > 1
                        and tokens[candidate - 2].group().casefold() in NUMBER_WORDS
                    ):
                        continue
                if marker == "dot" and (
                    tld_word.casefold() in FILE_ENDINGS
                    or tld_word.casefold() in ENDING_STOPS
                    or len(tld_word) == 1
                    or tld_word.casefold() == "dot"
                ):
                    continue
                if marker in {"dot", "period", "point", "full"} and candidate + 1 < len(tokens):
                    if not tld_word.isalpha() or tld_word.casefold() in NUMBER_WORDS:
                        continue
                    after = (
                        tokens[candidate + 2].group().strip(",.;:!?)]}" + "'").casefold()
                        if candidate + 2 < len(tokens)
                        else ""
                    )
                    if (
                        tld_word.casefold() != "com"
                        and not tokens[candidate + 1].group().startswith(".")
                        and not tokens[candidate].group().rstrip().endswith(".")
                        and after
                    ):
                        continue
                    if tld_word.casefold() != "com" and after in {
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
                    }:
                        continue
                    end = candidate + 1
                    break
                if marker.startswith("dot") and marker[3:] in KNOWN_TLDS:
                    end = candidate
                    break
                if marker in KNOWN_TLDS:
                    after = (
                        tokens[candidate + 1].group().strip(",.;:!?)]}" + "'").casefold()
                        if candidate + 1 < len(tokens)
                        else ""
                    )
                    if marker != "com" and after in {
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
                    }:
                        continue
                    end = candidate
                    break
        if end is not None:
            local_word = tokens[i - 1].group().strip(",.;:!?)]}" + "'").casefold() if i > 0 else ""
            has_local = i > 0 and (local_word not in DOMAIN_STOPS or local_word in NUMBER_WORDS)
            if has_local:
                hits.append(Hit("EMAIL_LIKE", tokens[i - 1].start(), tokens[end].end()))
    for i, match in enumerate(tokens):
        provider = match.group().strip(",.;:!?)]}" + "'").casefold()
        provider_end = i
        if i + 1 < len(tokens) and (provider, tokens[i + 1].group().casefold()) in SPLIT_PROVIDERS:
            provider_end = i + 1
        elif "." in provider:
            name, _, tld = provider.partition(".")
            if name not in PROVIDERS or tld not in KNOWN_TLDS:
                continue
        elif provider not in PROVIDERS:
            continue
        suffix_end = provider_end
        if (
            suffix_end + 2 < len(tokens)
            and tokens[suffix_end + 1].group().casefold()
            in {
                "dot",
                "period",
                "point",
            }
            and tokens[suffix_end + 2].group().strip(",.;:!?)]}" + "'").casefold() in KNOWN_TLDS
        ):
            suffix_end += 2
        elif "." not in provider:
            continue
        if i == 0:
            continue
        local_word = tokens[i - 1].group().strip(",.;:!?)]}" + "'").casefold()
        if (
            local_word in DOMAIN_STOPS and local_word not in NUMBER_WORDS
        ) or local_word in ORDINARY_BEFORE_AT:
            continue
        hits.append(Hit("EMAIL_LIKE", tokens[i - 1].start(), tokens[suffix_end].end()))
    return hits


def _scan_spoken_email_tokens(text: str) -> list[Hit]:
    return _scan_spoken_email_tokens_legacy(text)


def scan_text(
    text: str, allow_ids: frozenset[str] = frozenset(), precision: frozenset[str] | None = None
) -> list[Hit]:
    hits: list[Hit] = []
    money_spans = _money_spans(text)
    for name, (regex, detector_precision) in _detectors.items():
        if precision is not None and detector_precision not in precision:
            continue
        for match in regex.finditer(text):
            value = match.group()
            if value in allow_ids or _placeholder.fullmatch(value):
                continue
            if name == "DIGITS_LIKE" and any(
                match.start() < end and match.end() > start for start, end in money_spans
            ):
                continue
            hits.append(Hit(name, match.start(), match.end(), detector_precision))
    if precision is None or "high" in precision:
        for match in _spelled_pan.finditer(text):
            if match.group() not in allow_ids:
                hits.append(Hit("PAN_LIKE", match.start(), match.end()))
    if precision is None or "high" in precision:
        for start, end in _number_word_spans(text):
            hits.append(Hit("NUMBER_WORDS", start, end))
        hits.extend(_scan_spoken_email_tokens(text))
    return sorted(hits, key=lambda hit: (hit.start, hit.end, hit.detector))


def scan_lead(
    masked_file: Path, allow_ids: frozenset[str] = frozenset(), precision: frozenset[str] | None = None
) -> LeakReport:
    hits: list[Hit] = []
    with gzip.open(masked_file, "rt", encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            for text in free_text_values(record):
                hits.extend(scan_text(text, allow_ids, precision))
    return LeakReport(hits)


def calibrate(files: Iterable[Path], allow_ids: frozenset[str] = frozenset()) -> dict[str, int]:
    totals: dict[str, int] = {}
    for path in files:
        for hit in scan_lead(path, allow_ids).hits:
            totals[hit.detector] = totals.get(hit.detector, 0) + 1
    return totals
