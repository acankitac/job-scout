"""Text helpers: HTML stripping, salary parsing, years-of-experience extraction."""
import html
import re
from typing import Optional

from .models import Salary

_BLOCK = re.compile(r"<\s*(br|/p|/li|/div|/h[1-6]|/tr|/ul|/ol)[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t ]+")


def html_to_text(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)  # Greenhouse double-escapes its content
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    s = _WS.sub(" ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


# ---------- salary ----------

_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP"}
_CUR = r"(?:€|\$|£|EUR|USD|GBP|CHF|SEK|DKK|NOK|PLN)"
_NUM = r"\d{1,3}(?:[.,  ]\d{3})+(?:[.,]\d{2})?|\d+(?:[.,]\d+)?\s?[kK]\b|\d{4,7}"
_SEP = r"\s*(?:-|–|—|to|bis|and)\s*"
_RANGE = re.compile(
    rf"(?P<c1>{_CUR})?\s?(?P<a>{_NUM})\s?(?P<c2>{_CUR})?{_SEP}(?P<c3>{_CUR})?\s?(?P<b>{_NUM})\s?(?P<c4>{_CUR})?",
    re.I,
)
_MONTH = re.compile(r"per\s+month|/\s*month|monthly|pro\s+monat|/\s*mo\b|a\s+month", re.I)
_HOUR = re.compile(r"per\s+hour|/\s*h(ou)?r\b|hourly|an\s+hour", re.I)


def _to_number(tok: str) -> float:
    tok = tok.strip().replace(" ", "").replace(" ", "")
    if tok.lower().endswith("k"):
        return float(tok[:-1].replace(",", ".")) * 1000
    # Thousands separators are "," (EN) or "." (DE); a trailing 2-digit group is decimals.
    m = re.fullmatch(r"(.+?)[.,](\d{2})", tok)
    if m and re.search(r"[.,]\d{3}", m.group(1) + "x"):  # e.g. 70.000,00
        tok = m.group(1)
    return float(re.sub(r"[.,]", "", tok))


def parse_salary(text: str) -> Optional[Salary]:
    """Find the first plausible salary range in free text. Requires a currency marker."""
    if not text:
        return None
    for m in _RANGE.finditer(text):
        cur = next((m.group(g) for g in ("c1", "c2", "c3", "c4") if m.group(g)), None)
        if not cur:
            continue
        try:
            lo, hi = _to_number(m.group("a")), _to_number(m.group("b"))
        except ValueError:
            continue
        if lo > hi:
            lo, hi = hi, lo
        tail = text[m.end(): m.end() + 40]
        if _MONTH.search(tail):
            interval, floor, ceil = "month", 1_500, 40_000
        elif _HOUR.search(tail):
            interval, floor, ceil = "hour", 15, 500
        else:
            interval, floor, ceil = "year", 20_000, 1_500_000
        if not (floor <= lo <= ceil and floor <= hi <= ceil):
            continue
        currency = _SYMBOLS.get(cur, cur.upper())
        return Salary(lo, hi, currency, interval, origin="parsed")
    return None


# ---------- years of experience ----------

_NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
             "ein": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8,
             "zehn": 10}
_N = r"(\d{1,2}|" + "|".join(_NUMWORDS) + r")"
_YEARS = re.compile(
    rf"(?:at\s+least|minimum(?:\s+of)?|min\.?|mindestens|over|more\s+than)?\s*"
    rf"\b{_N}\s*(?:\+|plus|\s+or\s+more)?\s*(?:(?:-|–|—|to|bis)\s*{_N}\s*)?\+?\s*"
    rf"(?:years?|yrs?|jahren?)\b",
    re.I,
)
_EXP_WORDS = ("experience", "erfahrung", "berufserfahrung", "working as", "industry", "professional")


def _num(tok: Optional[str]) -> Optional[int]:
    if tok is None:
        return None
    return int(tok) if tok.isdigit() else _NUMWORDS.get(tok.lower())


def experience_required(text: str) -> Optional[tuple[int, Optional[int]]]:
    """The headline years-of-experience requirement as (min, max-or-None), or None.

    Only counts figures tied to experience ("5+ years of experience", "3-5 years in backend
    development", "mindestens 5 Jahre Berufserfahrung"). Takes the mention with the largest
    minimum, because smaller figures are usually per-technology sub-requirements.
    """
    best = None
    for m in _YEARS.finditer(text or ""):
        window = text[m.end(): m.end() + 80].lower()
        before = text[max(0, m.start() - 40): m.start()].lower()
        if not any(w in window or w in before for w in _EXP_WORDS):
            continue
        lo, hi = _num(m.group(1)), _num(m.group(2))
        if lo is None or not 0 <= lo <= 20:
            continue
        if hi is not None and not lo <= hi <= 25:
            hi = None
        if best is None or lo > best[0]:
            best = (lo, hi)
    return best


def years_required(text: str) -> Optional[int]:
    found = experience_required(text)
    return found[0] if found else None


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_EMAIL_NOISE = re.compile(
    r"noreply|no-reply|privacy|gdpr|dataprotection|datenschutz|accommodat|accessib|"
    r"legal|security|abuse|\.(png|jpg|jpeg|gif|svg)$",
    re.I,
)


def emails_in(text: str) -> list[str]:
    return sorted({e.rstrip(".") for e in _EMAIL.findall(text or "") if not _EMAIL_NOISE.search(e)})
