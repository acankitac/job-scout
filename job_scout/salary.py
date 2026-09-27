"""Salary judgement: listed ranges, estimates for unlisted roles, and negotiation headroom.

For a role that lists no salary, an estimate comes from (in order of trust):
  1. the same company's own postings that do list a salary, for the same level and country;
  2. web research on trusted salary sources (see research.py), if an API key is configured.

Every range is judged by what is realistically reachable with negotiation, not by its
midpoint: a role passes the minimum if negotiating could get you there.
"""
import re
import statistics
from dataclasses import dataclass, field
from typing import Optional

from .country import currency_of, job_country
from .models import Job

# Base-salary uplift typically achievable by negotiating, by how negotiable the company is.
DEFAULT_ROOM_PCT = {"low": 3, "medium": 8, "high": 15}


@dataclass
class Profile:
    years_experience: int = 5
    city: str = ""
    country: str = "Germany"
    currency: str = "EUR"
    # Location words that count as "this country" when matching a company's other postings.
    country_aliases: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        return cls(**d)

    def in_country(self, location: str) -> bool:
        names = [self.country, self.city] + list(self.country_aliases)
        loc = location.lower()
        return any(n and re.search(r"(?<![a-z])" + re.escape(n.lower()) + r"(?![a-z])", loc) for n in names)


@dataclass
class Negotiation:
    default_headroom_pct: float = 10
    room_pct: dict = field(default_factory=lambda: dict(DEFAULT_ROOM_PCT))

    @classmethod
    def from_dict(cls, d: dict) -> "Negotiation":
        d = dict(d)
        rooms = {k: d.pop(k) for k in list(d) if k in ("low", "medium", "high")}
        n = cls(**d)
        n.room_pct.update(rooms)
        return n

    def headroom(self, room: str) -> float:
        return self.room_pct.get(room, self.default_headroom_pct)


@dataclass
class Estimate:
    low: float
    typical: float
    high: Optional[float]
    level: str
    basis: str                 # "company postings (n=3)" or "web research"
    confidence: str = "medium"  # low / medium / high
    room: str = "unknown"       # negotiability: low / medium / high / unknown
    notes: str = ""
    sources: list = field(default_factory=list)  # [{"title":..., "url":...}]


def infer_level(title: str) -> str:
    t = title.lower()
    if re.search(r"\b(principal|distinguished|fellow)\b", t):
        return "principal"
    if re.search(r"(?<!technical )\bstaff\b", t):  # "Member of Technical Staff" is a generic title
        return "staff"
    if re.search(r"\blead\b", t):
        return "lead"
    if re.search(r"\b(senior|sr)\b", t):
        return "senior"
    if re.search(r"\b(junior|jr|associate|graduate|entry)\b", t):
        return "junior"
    return "mid"


def reachable(typical: float, high: Optional[float], headroom_pct: float) -> float:
    """What negotiating from a typical offer can realistically reach: capped at the band top."""
    reach = typical * (1 + headroom_pct / 100)
    return min(reach, high) if high else reach


def sibling_estimate(job: Job, all_jobs: list, profile: Profile, fx: dict, is_role) -> Optional[Estimate]:
    """Estimate from the same company's salaried postings at the same level in the job's country."""
    level = infer_level(job.title)
    country = job.country or job_country(job.location, profile.country)
    lows, highs, srcs = [], [], []
    for s in all_jobs:
        if (s is job or s.company != job.company or not s.salary or infer_level(s.title) != level
                or not is_role(s.title) or job_country(s.location, profile.country) != country
                # Only bands in the country's own currency: a USD band on a posting that also
                # lists Germany is the US tier, not the German one.
                or s.salary.currency != currency_of(country)):
            continue
        lo, hi = s.salary.to_eur_year(fx)
        if lo is None or hi is None:
            continue
        lows.append(lo)
        highs.append(hi)
        srcs.append({"title": f"{s.company}: {s.title}", "url": s.url})
    if not lows:
        return None
    low, high = statistics.median(lows), statistics.median(highs)
    return Estimate(low=low, typical=(low + high) / 2, high=high, level=level,
                    basis=f"company postings (n={len(lows)})",
                    confidence="high" if len(lows) >= 2 else "medium",
                    # A published band is the band: expect to negotiate within it, not past it.
                    room="medium", sources=srcs[:3])


def listed_salary_usable(job: Job, profile: Profile) -> bool:
    """A figure parsed from the text in a currency other than the job country's is usually
    another region's pay band quoted elsewhere in the posting; treat it as 'not listed'."""
    s = job.salary
    if not s:
        return False
    country = job.country or job_country(job.location, profile.country)
    return not (s.origin == "parsed" and s.currency != currency_of(country))


def judge_listed(job: Job, min_eur: float, max_eur: float, fx: dict, neg: Negotiation) -> Optional[str]:
    lo, hi = job.salary.to_eur_year(fx)
    if lo is None and hi is None:
        return None
    if hi is None:  # a single figure: treat it as a typical offer with default headroom
        top = reachable(lo, None, neg.default_headroom_pct)
        mid = lo
    else:
        top = hi
        mid = (lo + hi) / 2 if lo is not None else hi
    bottom = lo if lo is not None else hi
    if min_eur and top < min_eur:
        return f"salary: listed range tops out at €{top:,.0f}"
    if max_eur and bottom > max_eur:
        return f"salary: listed range starts at €{bottom:,.0f}"
    job.negotiate = bool(min_eur and mid < min_eur <= top)
    return None


def judge_estimate(job: Job, est: Estimate, min_eur: float, max_eur: float, neg: Negotiation) -> Optional[str]:
    reach = reachable(est.typical, est.high, neg.headroom(est.room))
    job.reach = reach
    if min_eur and reach < min_eur:
        return (f"salary: estimated typical €{est.typical:,.0f}, about €{reach:,.0f} with negotiation "
                f"({est.basis})")
    if max_eur and est.low > max_eur:
        return f"salary: estimated range starts at €{est.low:,.0f} ({est.basis})"
    job.negotiate = bool(min_eur and est.typical < min_eur <= reach)
    return None
