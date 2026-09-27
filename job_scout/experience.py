"""Experience fit: judge a role by the years it expects, not by seniority words in its title.

Titles are unreliable signals of level ("Member of Technical Staff" is a generic title at some
companies, "Lead" is often a hands-on engineer), so the requirement comes from:
  1. the job description ("5+ years of experience", "3-5 years", "mindestens 5 Jahre");
  2. if the description doesn't say, web research on what the company's level for this role
     typically expects (see research.py);
  3. if neither is available, the role is kept and marked unknown, never rejected on a guess.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

from .models import Job


@dataclass
class YoeEstimate:
    min: int
    max: Optional[int]
    typical: Optional[int] = None
    level: str = ""
    basis: str = "job description"
    confidence: str = "high"
    notes: str = ""
    sources: list = field(default_factory=list)

    def display(self) -> str:
        rng = f"{self.min}–{self.max}" if self.max is not None else f"{self.min}+"
        return rng if self.basis == "job description" else f"≈{rng}*"


def title_stem(title: str) -> str:
    """Level-bearing part of a title, for caching: "Senior Software Engineer - Payments" and
    "Senior Software Engineer, Identity" share one lookup."""
    t = title.lower().strip()
    t = re.sub(r"^\((\w+)\)\s*", r"\1 ", t)  # "(Senior) Backend Engineer"
    t = re.split(r"\s+[-–|/]\s+|,|\(|:", t)[0]
    return re.sub(r"\s+", " ", t).strip() or title.lower()


def judge_experience(job: Job, yoe: Optional[YoeEstimate], max_required: int, min_expected: int) -> Optional[str]:
    """None if the role fits, else the rejection reason."""
    if yoe is None:
        return None
    src = "" if yoe.basis == "job description" else f" (researched: {yoe.level or 'this level'})"
    if max_required and yoe.min > max_required:
        return f"experience: expects {yoe.min}+ years{src}"
    if min_expected and yoe.max is not None and yoe.max < min_expected:
        return f"experience: aimed at up to {yoe.max} years{src}"
    return None
