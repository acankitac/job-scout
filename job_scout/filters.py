"""Criteria matching and relevance scoring."""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import Job
from .experience import YoeEstimate
from .textutil import experience_required


@dataclass
class Criteria:
    locations: list = field(default_factory=list)
    exclude_locations: list = field(default_factory=list)
    allow_remote: bool = True
    remote_regions: list = field(default_factory=list)
    include_unscoped_remote: bool = False
    title_include: list = field(default_factory=list)
    title_exclude: list = field(default_factory=list)
    salary_min_eur: float = 0
    salary_max_eur: float = 0
    require_salary: bool = False
    max_age_days: int = 0
    max_years_required: int = 0
    min_years_expected: int = 0
    min_score: int = 0
    keywords: dict = field(default_factory=dict)
    fx_to_eur: dict = field(default_factory=lambda: {"EUR": 1.0})

    @classmethod
    def from_dict(cls, d: dict) -> "Criteria":
        known = set(cls.__dataclass_fields__)
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown criteria keys in config: {', '.join(sorted(unknown))}")
        return cls(**d)


def _any(patterns, text) -> Optional[str]:
    for p in patterns:
        if re.search(p, text, re.I):
            return p
    return None


def _contains(needles, haystack) -> Optional[str]:
    """Whole-word, case-insensitive match, so "EU" doesn't hit "Leuven"."""
    for n in needles:
        if re.search(r"(?<![a-z])" + re.escape(n.lower()) + r"(?![a-z])", haystack.lower()):
            return n
    return None


def check_location(job: Job, c: Criteria) -> tuple[bool, str]:
    loc = job.location or ""
    bad = _contains(c.exclude_locations, loc)
    if bad:
        return False, f"location: excluded '{bad}'"
    hit = _contains(c.locations, loc)
    if hit:
        return True, hit
    if c.allow_remote:
        # Region-level locations ("EMEA", "Europe") are hireable from anywhere in the region,
        # whether or not the ATS flags them as remote.
        region = _contains(c.remote_regions, loc)
        if region:
            return True, f"{'remote' if job.remote else 'region'} ({region})"
        # "Remote" with no region attached is usually US-only at US companies.
        if c.include_unscoped_remote and re.fullmatch(r"\s*remote\s*", loc, re.I):
            return True, "remote (region unspecified)"
    if not c.locations and not c.remote_regions:
        return True, "any"
    return False, f"location: '{loc or 'unknown'}'"


def score(job: Job, c: Criteria) -> tuple[int, list]:
    title, body = job.title.lower(), job.description.lower()
    total, matched = 0, []
    for kw, weight in c.keywords.items():
        pat = r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])"
        in_title = re.search(pat, title)
        in_body = re.search(pat, body)
        if in_title or in_body:
            total += weight * (2 if in_title else 1)
            matched.append(kw)
    return total, matched


def prefilter(job: Job, c: Criteria) -> Optional[str]:
    """The checks that need no description: used before fetching one."""
    if c.title_include and not _any(c.title_include, job.title):
        return "title: no include pattern matched"
    bad = _any(c.title_exclude, job.title)
    if bad:
        return f"title: matches exclude '{bad}'"
    ok, why = check_location(job, c)
    return None if ok else why


def evaluate(job: Job, c: Criteria, now: Optional[datetime] = None) -> Optional[str]:
    """Annotate the job in place. Returns None if it passes, else "category: detail".

    Experience and salary are judged separately (experience.py, salary.py), after
    de-duplication, because filling in what a posting doesn't say can cost a web lookup.
    """
    now = now or datetime.now(timezone.utc)
    if c.title_include and not _any(c.title_include, job.title):
        return "title: no include pattern matched"
    bad = _any(c.title_exclude, job.title)
    if bad:
        return f"title: matches exclude '{bad}'"
    ok, why = check_location(job, c)
    if not ok:
        return why
    job.location_reason = why
    if c.max_age_days and job.published and job.published < now - timedelta(days=c.max_age_days):
        return f"age: posted {job.published:%Y-%m-%d}, older than {c.max_age_days} days"
    found = experience_required(job.description)
    if found:
        job.yoe = YoeEstimate(min=found[0], max=found[1])
        job.years_required = found[0]
    job.score, job.matched = score(job, c)
    if job.score < c.min_score:
        return f"score: {job.score} below {c.min_score}"
    return None


_LEGAL = r"\b(gmbh|ag|se|kg|kgaa|co|inc|ltd|llc|plc|bv|b\.v|sarl|sas|oy|ab|group|gruppe|deutschland|germany)\b"


def company_key(name: str) -> str:
    """"Examplecorp SE" and "Examplecorp" are the same employer across sources."""
    n = re.sub(r"\.(com|io|ai|de)\b", "", name.lower())
    n = re.sub(_LEGAL, " ", n)
    return re.sub(r"[^a-z0-9]+", "", n)


def dedupe(jobs: list) -> list:
    """Collapse one role posted several times (per country, as parallel requisitions, or on
    several job boards).

    Same company and same title stem ("Backend Engineer / Spain / Remote" -> "backend engineer")
    means same role. The copy whose location matched most directly is kept.
    """
    groups = {}
    for j in jobs:
        stem = re.split(r"\s+[/|]\s+", j.title)[0].strip().lower()
        stem = re.sub(r"\s*\((m/w/d|w/m/d|m/f/d|f/m/d|all genders|gn\*?|d/f/m)\)", "", stem)
        k = (company_key(j.company), re.sub(r"\s+", " ", stem).strip())
        groups.setdefault(k, []).append(j)
    out = []
    for group in groups.values():
        group.sort(key=lambda j: (not j.location_reason or j.location_reason.startswith(("remote", "region")), -j.score))
        keep = group[0]
        if len(group) > 1:
            keep.variants = len(group)
        out.append(keep)
    return out
