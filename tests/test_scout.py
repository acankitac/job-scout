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

    def test_salary_bounds(self):
        low = job(salary=Salary(50000, 60000, "EUR"))
        self.assertTrue(evaluate(low, self.crit(salary_min_eur=80000)).startswith("salary"))
        ok = job(salary=Salary(70000, 95000, "EUR"))
        self.assertIsNone(evaluate(ok, self.crit(salary_min_eur=80000)))

    def test_parsed_usd_on_european_job_does_not_filter(self):
        j = job(salary=Salary(60000, 70000, "USD", origin="parsed"))
        self.assertIsNone(evaluate(j, self.crit(salary_min_eur=90000)))

    def test_missing_salary(self):
        self.assertIsNone(evaluate(job(), self.crit(salary_min_eur=80000)))
        self.assertIsNotNone(evaluate(job(), self.crit(require_salary=True)))

    def test_years_cap(self):
        j = job(description="Java. 10+ years of experience.")
        self.assertTrue(evaluate(j, self.crit(max_years_required=7)).startswith("experience"))

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
