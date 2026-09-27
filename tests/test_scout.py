import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from job_scout import contacts
from job_scout.filters import Criteria, dedupe, evaluate
from job_scout.models import Job, Salary
from job_scout import experience as xp, research, salary as sal
from job_scout.textutil import emails_in, html_to_text, parse_salary, years_required


def job(**kw):
    base = dict(company="Acme", title="Senior Backend Engineer", location="Berlin, Germany",
                url="https://x", source="greenhouse", job_id="1",
                description="We use Java and Kafka. 5+ years of experience required.")
    base.update(kw)
    return Job(**base)


class SalaryParsing(unittest.TestCase):
    def test_euro_range_with_separators(self):
        s = parse_salary("The range is €70,000 - €90,000 per year.")
        self.assertEqual((s.min, s.max, s.currency, s.interval), (70000, 90000, "EUR", "year"))

    def test_german_format_and_code(self):
        s = parse_salary("Gehalt: 75.000 – 95.000 EUR brutto")
        self.assertEqual((s.min, s.max, s.currency), (75000, 95000, "EUR"))

    def test_k_suffix(self):
        s = parse_salary("Base salary $150K to $200K")
        self.assertEqual((s.min, s.max, s.currency), (150000, 200000, "USD"))

    def test_monthly(self):
        s = parse_salary("€5,000 - €6,500 per month")
        self.assertEqual(s.interval, "month")
        self.assertEqual(s.to_eur_year({"EUR": 1}), (60000, 78000))

    def test_requires_currency(self):
        self.assertIsNone(parse_salary("We have 2000 - 3000 customers"))

    def test_ignores_funding_amounts(self):
        self.assertIsNone(parse_salary("We raised $100M - $200M from investors"))


class TextHelpers(unittest.TestCase):
    def test_years_takes_headline_requirement(self):
        text = "5+ years of experience in backend. 2+ years experience with Kubernetes."
        self.assertEqual(years_required(text), 5)

    def test_years_needs_experience_context(self):
        self.assertIsNone(years_required("Founded 10 years ago."))

    def test_html_double_escaped(self):
        self.assertEqual(html_to_text("&lt;p&gt;Hello &amp;amp; bye&lt;/p&gt;"), "Hello & bye")

    def test_emails_skip_noise(self):
        text = "Contact jane@acme.com or privacy@acme.com, apply via noreply@acme.com."
        self.assertEqual(emails_in(text), ["jane@acme.com"])


class Filtering(unittest.TestCase):
    def crit(self, **kw):
        base = dict(locations=["Berlin"], remote_regions=["EMEA"], title_include=["backend"],
                    title_exclude=["staff"], keywords={"java": 3, "kafka": 2})
        base.update(kw)
        return Criteria(**base)

    def test_passes_and_scores(self):
        j = job()
        self.assertIsNone(evaluate(j, self.crit()))
        self.assertEqual(j.score, 5)
        self.assertEqual(j.years_required, 5)

    def test_title_exclude(self):
        self.assertTrue(evaluate(job(title="Staff Backend Engineer"), self.crit()).startswith("title"))

    def test_location_region_and_word_boundary(self):
        self.assertIsNone(evaluate(job(location="EMEA"), self.crit()))
        self.assertTrue(evaluate(job(location="Leuven, Belgium"), self.crit(remote_regions=["EU"]))
                        .startswith("location"))

    def test_bare_remote_is_opt_in(self):
        self.assertIsNotNone(evaluate(job(location="Remote", remote=True), self.crit()))
        self.assertIsNone(evaluate(job(location="Remote", remote=True), self.crit(include_unscoped_remote=True)))

    def test_title_seniority_no_longer_filters(self):
        j = job(title="Staff Backend Engineer", description="Java. 5+ years of experience.")
        self.assertIsNone(evaluate(j, self.crit(title_exclude=[])))
        self.assertEqual((j.yoe.min, j.yoe.max, j.yoe.basis), (5, None, "job description"))

    def test_age(self):
        old = job(published=datetime.now(timezone.utc) - timedelta(days=90))
        self.assertTrue(evaluate(old, self.crit(max_age_days=30)).startswith("age"))

    def test_dedupe_per_country_postings(self):
        a = job(title="Backend Engineer / Spain / Remote", job_id="1", location_reason="remote (EMEA)")
        b = job(title="Backend Engineer / Germany / Remote", job_id="2", location_reason="Germany")
        out = dedupe([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].job_id, "2")
        self.assertEqual(out[0].variants, 2)

    def test_unknown_config_key_rejected(self):
        with self.assertRaises(ValueError):
            Criteria.from_dict({"locatons": ["Berlin"]})


class ExperienceFit(unittest.TestCase):
    def test_jd_parsing_variants(self):
        from job_scout.textutil import experience_required as er
        cases = {
            "3-5 years of backend experience": (3, 5),
            "Have 10 or more years of engineering experience": (10, None),
            "at least five years of professional experience": (5, None),
            "mindestens 5 Jahre Berufserfahrung": (5, None),
            "0–2 years experience in software": (0, 2),
            "2+ years with Go. 6+ years of industry experience": (6, None),
        }
        for text, want in cases.items():
            self.assertEqual(er(text), want, text)
        self.assertIsNone(er("Founded 10 years ago in Berlin."))

    def test_judge_bounds(self):
        Y = xp.YoeEstimate
        self.assertIsNone(xp.judge_experience(job(), Y(5, None), 7, 3))
        self.assertTrue(xp.judge_experience(job(), Y(8, None), 7, 3).startswith("experience: expects 8+"))
        self.assertTrue(xp.judge_experience(job(), Y(0, 2), 7, 3).startswith("experience: aimed at up to 2"))
        self.assertIsNone(xp.judge_experience(job(), Y(2, None), 7, 3))  # "2+ years" doesn't cap
        self.assertIsNone(xp.judge_experience(job(), None, 7, 3))        # unknown: kept

    def test_title_stem(self):
        self.assertEqual(xp.title_stem("Senior Software Engineer - Payments"), "senior software engineer")
        self.assertEqual(xp.title_stem("Senior Software Engineer, Identity"), "senior software engineer")
        self.assertEqual(xp.title_stem("(Senior) Backend Engineer (Java)"), "senior backend engineer")
        self.assertEqual(xp.title_stem("Member of Technical Staff (Backend)"), "member of technical staff")

    def test_research_fallback(self):
        answer = {"level": "L3", "years_min": 0, "years_typical": 1, "years_max": 2, "confidence": "high",
                  "notes": "Entry level.", "sources": [{"title": "levels", "url": "https://levels.fyi/z"}]}
        reply = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(answer)}]}
        body = io.BytesIO(json.dumps(reply).encode())
        calls = []

        def urlopen(req, timeout=None):
            calls.append(json.loads(req.data))
            return mock.MagicMock(__enter__=lambda s: body, __exit__=lambda *a: False)
        with tempfile.TemporaryDirectory() as d, mock.patch("urllib.request.urlopen", urlopen):
            r = research.ExperienceResearch({}, sal.Profile(), Path(d), api_key="k")
            y = r.expected(job(title="Software Engineer (Early Careers)"), "acme.com")
            again = r.expected(job(job_id="9", title="Software Engineer - Platform"), "acme.com")
        self.assertEqual(len(calls), 1)  # same company + title stem: cached
        self.assertEqual((y.min, y.max, y.basis, y.level), (0, 2, "web research", "L3"))
        self.assertEqual(again.max, 2)
        self.assertTrue(xp.judge_experience(job(), y, 7, 3).startswith("experience: aimed at up to 2"))
        self.assertEqual(y.display(), "≈0–2*")

    def test_separate_caps(self):
        with tempfile.TemporaryDirectory() as d:
            r = research.ExperienceResearch({"max_lookups_per_run": 5, "max_experience_lookups_per_run": 0},
                                            sal.Profile(), Path(d), api_key="k")
            self.assertIsNone(r.expected(job()))
            self.assertEqual(r.skipped, 1)


class SalaryJudgement(unittest.TestCase):
    FX = {"EUR": 1.0, "USD": 0.86}
    NEG = sal.Negotiation()
    PROFILE = sal.Profile(city="Berlin", country="Germany", country_aliases=["Munich"])

    def test_listed_range_passes_if_top_reaches_minimum(self):
        j = job(salary=Salary(90000, 160000, "EUR"))
        self.assertIsNone(sal.judge_listed(j, 130000, 0, self.FX, self.NEG))
        self.assertTrue(j.negotiate)  # midpoint 125k < 130k <= 160k

    def test_listed_range_below_minimum(self):
        j = job(salary=Salary(50000, 60000, "EUR"))
        self.assertTrue(sal.judge_listed(j, 80000, 0, self.FX, self.NEG).startswith("salary"))

    def test_min_only_means_no_upper_bound(self):
        j = job(salary=Salary(200000, 250000, "EUR"))
        self.assertIsNone(sal.judge_listed(j, 80000, 0, self.FX, self.NEG))
        self.assertFalse(j.negotiate)

    def test_max_drops_roles_whose_floor_is_above_it(self):
        j = job(salary=Salary(200000, 250000, "EUR"))
        self.assertIsNotNone(sal.judge_listed(j, 0, 150000, self.FX, self.NEG))

    def test_estimate_passes_only_with_negotiation(self):
        e = sal.Estimate(low=90000, typical=100000, high=120000, level="senior", basis="x", room="medium")
        j = job()
        self.assertIsNone(sal.judge_estimate(j, e, 105000, 0, self.NEG))   # reach 108k
        self.assertTrue(j.negotiate)
        self.assertAlmostEqual(j.reach, 108000)
        self.assertIsNotNone(sal.judge_estimate(job(), e, 110000, 0, self.NEG))

    def test_reach_capped_at_band_top(self):
        self.assertEqual(sal.reachable(100000, 104000, 15), 104000)
        self.assertAlmostEqual(sal.reachable(100000, None, 10), 110000)

    def test_low_room_company_gets_less_headroom(self):
        e = sal.Estimate(low=90000, typical=100000, high=130000, level="senior", basis="x", room="low")
        self.assertIsNotNone(sal.judge_estimate(job(), e, 105000, 0, self.NEG))  # reach 103k

    def test_infer_level(self):
        self.assertEqual(sal.infer_level("Senior Backend Engineer"), "senior")
        self.assertEqual(sal.infer_level("Member of Technical Staff (Backend)"), "mid")
        self.assertEqual(sal.infer_level("Staff Engineer"), "staff")
        self.assertEqual(sal.infer_level("(Senior) Software Engineer"), "senior")

    def test_sibling_estimate_same_level_and_country(self):
        target = job(job_id="t", title="Senior Backend Engineer, Payments")
        same = job(job_id="a", title="Senior Software Engineer", location="Munich",
                   salary=Salary(100000, 130000, "EUR"))
        other_level = job(job_id="b", title="Backend Engineer", salary=Salary(60000, 80000, "EUR"))
        other_country = job(job_id="c", title="Senior Backend Engineer", location="London",
                            salary=Salary(90000, 120000, "GBP"))
        other_company = job(job_id="d", company="Other", salary=Salary(1, 2, "EUR"))
        e = sal.sibling_estimate(target, [target, same, other_level, other_country, other_company],
                                 self.PROFILE, self.FX, lambda t: True)
        self.assertEqual((e.low, e.high, e.typical), (100000, 130000, 115000))
        self.assertEqual(e.basis, "company postings (n=1)")

    def test_parsed_foreign_salary_on_local_posting_is_unusable(self):
        j = job(salary=Salary(60000, 70000, "USD", origin="parsed"))
        self.assertFalse(sal.listed_salary_usable(j, self.PROFILE))
        self.assertTrue(sal.listed_salary_usable(job(salary=Salary(1, 2, "USD")), self.PROFILE))


class WebResearch(unittest.TestCase):
    ANSWER = {"currency": "EUR", "level": "L4", "base_low": 85000, "base_typical": 95000,
              "base_high": 110000, "negotiation_room": "high", "confidence": "medium",
              "notes": "Based on 12 levels.fyi entries.", "sources": [{"title": "levels", "url": "https://levels.fyi/x"}]}

    def _fake(self, payloads):
        calls = []

        def urlopen(req, timeout=None):
            calls.append(json.loads(req.data))
            body = io.BytesIO(json.dumps(payloads[len(calls) - 1]).encode())
            return mock.MagicMock(__enter__=lambda s: body, __exit__=lambda *a: False)
        return urlopen, calls

    def test_lookup_parses_caches_and_restricts_domains(self):
        reply = {"stop_reason": "end_turn", "content": [
            {"type": "web_search_tool_result", "content": [{"url": "https://glassdoor.de/y", "title": "gd"}]},
            {"type": "text", "text": "```json\n" + json.dumps(self.ANSWER) + "\n```"}]}
        urlopen, calls = self._fake([reply])
        with tempfile.TemporaryDirectory() as d, mock.patch("urllib.request.urlopen", urlopen):
            r = research.SalaryResearch({}, sal.Profile(), {"EUR": 1.0}, Path(d), api_key="k")
            e = r.estimate(job(), "acme.com")
            e2 = r.estimate(job(job_id="2"), "acme.com")  # same company + level: cache hit
        self.assertEqual(len(calls), 1)
        self.assertEqual((e.low, e.typical, e.high, e.room, e.level), (85000, 95000, 110000, "high", "L4"))
        self.assertEqual(e2.typical, 95000)
        tool = calls[0]["tools"][0]
        self.assertIn("acme.com", tool["allowed_domains"])
        self.assertIn("levels.fyi", tool["allowed_domains"])

    def test_pause_turn_is_resumed(self):
        pause = {"stop_reason": "pause_turn", "content": [{"type": "text", "text": "searching"}]}
        done = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(self.ANSWER)}]}
        urlopen, calls = self._fake([pause, done])
        with tempfile.TemporaryDirectory() as d, mock.patch("urllib.request.urlopen", urlopen):
            e = research.SalaryResearch({}, sal.Profile(), {"EUR": 1.0}, Path(d), api_key="k").estimate(job())
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["messages"][-1]["role"], "assistant")
        self.assertEqual(e.typical, 95000)

    def test_lookup_cap_and_no_key(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {}, clear=True):
            r = research.SalaryResearch({}, sal.Profile(), {"EUR": 1.0}, Path(d))
            self.assertIsNone(r.estimate(job()))
            capped = research.SalaryResearch({"max_lookups_per_run": 0}, sal.Profile(), {"EUR": 1.0}, Path(d), api_key="k")
            self.assertIsNone(capped.estimate(job()))
            self.assertEqual(capped.skipped, 1)

    def test_implausible_estimate_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            r = research.SalaryResearch({}, sal.Profile(), {"EUR": 1.0}, Path(d), api_key="k")
            self.assertIsNone(r._to_estimate({"currency": "EUR", "base_typical": 95}, "senior"))
            self.assertIsNone(r._to_estimate({"currency": "XYZ", "base_typical": 95000}, "senior"))


class HunterLookup(unittest.TestCase):
    RESPONSE = {"data": {"pattern": "{first}.{last}", "emails": [
        {"value": "sam@acme.com", "first_name": "Sam", "last_name": "Lee", "position": "Office Manager",
         "confidence": 99, "linkedin": None},
        {"value": "ana@acme.com", "first_name": "Ana", "last_name": "Ruiz", "position": "Technical Recruiter",
         "confidence": 90, "linkedin": "https://www.linkedin.com/in/anaruiz"},
    ]}}

    def test_recruiters_ranked_first_and_cached(self):
        with tempfile.TemporaryDirectory() as d:
            h = contacts.Hunter(Path(d), api_key="k")
            fake = io.BytesIO(json.dumps(self.RESPONSE).encode())
            with mock.patch("urllib.request.urlopen", return_value=mock.MagicMock(
                    __enter__=lambda s: fake, __exit__=lambda *a: False)) as m:
                people = h.recruiters("acme.com")
                again = h.recruiters("acme.com")
            self.assertEqual(m.call_count, 1)  # second call served from cache
            self.assertEqual(people[0]["email"], "ana@acme.com")
            self.assertTrue(people[0]["recruiting"])
            self.assertEqual(people, again)
            self.assertEqual(contacts.Hunter.pattern("acme.com", Path(d)), "{first}.{last}")

    def test_disabled_without_key(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {}, clear=True):
            h = contacts.Hunter(Path(d))
            self.assertFalse(h.enabled)
            self.assertEqual(h.recruiters("acme.com"), [])

    def test_linkedin_links_are_search_urls(self):
        links = contacts.linkedin_people_links("Acme", "Platform")
        self.assertTrue(all(u.startswith("https://www.linkedin.com/search/results/people/") for u in links.values()))


if __name__ == "__main__":
    unittest.main()
