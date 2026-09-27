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
from job_scout import experience as xp, llm, research, salary as sal
from job_scout.textutil import emails_in, html_to_text, parse_salary, years_required


ANTH = {"providers": ["anthropic"]}  # keep tests off AWS


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
            r = research.ExperienceResearch(ANTH, sal.Profile(), Path(d), api_key="k")
            y = r.expected(job(title="Software Engineer (Early Careers)"), "acme.com")
            again = r.expected(job(job_id="9", title="Software Engineer - Platform"), "acme.com")
        self.assertEqual(len(calls), 1)  # same company + title stem: cached
        self.assertEqual((y.min, y.max, y.basis, y.level), (0, 2, "web research", "L3"))
        self.assertEqual(again.max, 2)
        self.assertTrue(xp.judge_experience(job(), y, 7, 3).startswith("experience: aimed at up to 2"))
        self.assertEqual(y.display(), "≈0–2*")

    def test_separate_caps(self):
        with tempfile.TemporaryDirectory() as d:
            r = research.ExperienceResearch(dict(ANTH, max_lookups_per_run=5, max_experience_lookups_per_run=0),
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
            r = research.SalaryResearch(ANTH, sal.Profile(), {"EUR": 1.0}, Path(d), api_key="k")
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
            e = research.SalaryResearch(ANTH, sal.Profile(), {"EUR": 1.0}, Path(d), api_key="k").estimate(job())
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["messages"][-1]["role"], "assistant")
        self.assertEqual(e.typical, 95000)

    def test_lookup_cap_and_no_key(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {}, clear=True):
            r = research.SalaryResearch(ANTH, sal.Profile(), {"EUR": 1.0}, Path(d))
            self.assertIsNone(r.estimate(job()))
            capped = research.SalaryResearch(dict(ANTH, max_lookups_per_run=0), sal.Profile(), {"EUR": 1.0}, Path(d), api_key="k")
            self.assertIsNone(capped.estimate(job()))
            self.assertEqual(capped.skipped, 1)

    def test_implausible_estimate_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            r = research.SalaryResearch(ANTH, sal.Profile(), {"EUR": 1.0}, Path(d), api_key="k")
            self.assertIsNone(r._to_estimate({"currency": "EUR", "base_typical": 95}, "senior"))
            self.assertIsNone(r._to_estimate({"currency": "XYZ", "base_typical": 95000}, "senior"))


class FakeBackend:
    def __init__(self, name, behaviour):
        self.name, self.behaviour, self.calls = name, behaviour, []

    def create(self, body):
        self.calls.append(body)
        b = self.behaviour
        if isinstance(b, Exception):
            raise b
        if callable(b):
            return b(body)
        return b


ANSWER_TEXT = json.dumps({"currency": "EUR", "level": "L4", "base_low": 80000, "base_typical": 90000,
                          "base_high": 100000, "negotiation_room": "medium", "confidence": "high"})
OK = {"stop_reason": "end_turn", "content": [{"type": "text", "text": ANSWER_TEXT}]}


class BackendRouting(unittest.TestCase):
    def test_bedrock_first_when_it_works(self):
        bed, anth = FakeBackend("bedrock", OK), FakeBackend("anthropic", OK)
        r = llm.Router([bed, anth])
        _, name, searched = r.create({"messages": [{"role": "user", "content": "q"}], "tools": [1]})
        self.assertEqual((name, searched, len(anth.calls)), ("bedrock", True, 0))

    def test_expired_bedrock_falls_back_and_stays_down(self):
        bed = FakeBackend("bedrock", llm.Unavailable("ExpiredTokenException"))
        anth = FakeBackend("anthropic", OK)
        r = llm.Router([bed, anth])
        body = {"messages": [{"role": "user", "content": "q"}], "tools": [1]}
        self.assertEqual(r.create(body)[1], "anthropic")
        r.create(body)
        self.assertEqual(len(bed.calls), 1)  # not retried after going down
        self.assertIn("bedrock", r.down)

    def test_bedrock_without_web_search_falls_back_to_anthropic(self):
        bed = FakeBackend("bedrock", llm.SearchUnsupported("tool type web_search not supported"))
        anth = FakeBackend("anthropic", OK)
        _, name, searched = llm.Router([bed, anth]).create({"messages": [{"role": "user", "content": "q"}], "tools": [1]})
        self.assertEqual((name, searched), ("anthropic", True))

    def test_no_search_mode_only_when_allowed(self):
        def bedrock(body):
            if "tools" in body:
                raise llm.SearchUnsupported("web_search not supported")
            return OK
        body = {"messages": [{"role": "user", "content": "q"}], "tools": [1]}
        with self.assertRaises(llm.CallFailed):
            llm.Router([FakeBackend("bedrock", bedrock)]).create(body)
        bed = FakeBackend("bedrock", bedrock)
        _, name, searched = llm.Router([bed], allow_without_search=True).create(body)
        self.assertEqual((name, searched), ("bedrock", False))
        self.assertNotIn("tools", bed.calls[-1])
        self.assertIn("Web search is not available", bed.calls[-1]["messages"][0]["content"])

    def test_unsearched_estimate_is_low_confidence_and_labelled(self):
        def bedrock(body):
            if "tools" in body:
                raise llm.SearchUnsupported("web_search not supported")
            return OK
        router = llm.Router([FakeBackend("bedrock", bedrock)], allow_without_search=True)
        with tempfile.TemporaryDirectory() as d:
            e = research.SalaryResearch({}, sal.Profile(), {"EUR": 1.0}, Path(d), router=router).estimate(job())
        self.assertEqual((e.basis, e.confidence), ("model knowledge (no web search)", "low"))

    def test_nothing_configured(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            r = llm.build_router({"providers": ["anthropic"]})
        self.assertFalse(r.available)


class MarketSearch(unittest.TestCase):
    def _serve(self, routes):
        def urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else req
            for key, payload in routes.items():
                if key in url:
                    body = io.BytesIO(json.dumps(payload).encode())
                    return mock.MagicMock(__enter__=lambda s: body, __exit__=lambda *a: False)
            raise AssertionError(f"unexpected url {url}")
        return urlopen

    def test_arbeitsagentur_maps_salary_location_and_loads_details(self):
        from job_scout import aggregators as ag
        listing = {"ergebnisliste": [{
            "referenznummer": "123-X", "stellenangebotsTitel": "Backend Engineer (m/w/d)", "firma": "Acme GmbH",
            "stellenlokationen": [{"adresse": {"ort": "Berlin", "land": "DEUTSCHLAND"}}],
            "homeofficemoeglich": True, "verguetungsangabe": "JAHRESGEHALT",
            "gehaltsspanneVon": 70000.0, "gehaltsspanneBis": 85000.0,
            "veroeffentlichungszeitraum": {"von": "2026-09-01"}}]}
        detail = {"stellenangebotsBeschreibung": "Mindestens 5 Jahre Berufserfahrung mit Java."}
        with mock.patch("urllib.request.urlopen", self._serve({"pc/v6/jobs": listing, "pc/v4/jobdetails": detail})):
            jobs = ag.arbeitsagentur(["backend"], ["Berlin"], max_pages=1)
            self.assertEqual(len(jobs), 1)
            j = jobs[0]
            self.assertEqual(j.location, "Berlin, Germany (home office possible)")
            self.assertEqual((j.salary.min, j.salary.max, j.salary.currency), (70000.0, 85000.0, "EUR"))
            self.assertEqual(j.description, "")
            j.loader()
        self.assertIn("5 Jahre", j.description)
        self.assertEqual(j.url, "https://www.arbeitsagentur.de/jobsuche/jobdetail/123-X")

    def test_arbeitnow_epoch_seconds(self):
        from job_scout import aggregators as ag
        page = {"data": [{"slug": "a", "company_name": "Acme", "title": "Backend Engineer", "description": "<p>x</p>",
                          "remote": True, "url": "https://x", "tags": [], "location": "Berlin", "created_at": 1790541603}],
                "links": {"next": None}}
        with mock.patch("urllib.request.urlopen", self._serve({"arbeitnow.com": page})):
            j = ag.arbeitnow(max_pages=1)[0]
        self.assertEqual(j.published.year, 2026)
        self.assertEqual(j.location, "Berlin (remote)")

    def test_same_role_on_two_boards_collapses(self):
        from job_scout.filters import company_key
        self.assertEqual(company_key("Examplecorp SE"), company_key("Examplecorp"))
        self.assertEqual(company_key("Sample Logistics SE & Co. KG"), company_key("Sample Logistics"))
        a = job(company="Acme GmbH", title="Backend Engineer (m/w/d)", source="arbeitsagentur", location_reason="Berlin")
        b = job(company="Acme", title="Backend Engineer", source="arbeitnow", job_id="2", location_reason="Berlin")
        self.assertEqual(len(dedupe([a, b])), 1)

    def test_prefilter_skips_description_fetch_for_obvious_misses(self):
        from job_scout.filters import prefilter
        c = Criteria(locations=["Berlin"], title_include=["backend"], title_exclude=["manager"])
        self.assertIsNone(prefilter(job(), c))
        self.assertIsNotNone(prefilter(job(title="Backend Engineering Manager"), c))
        self.assertIsNotNone(prefilter(job(location="Paris"), c))


class MarketCheck(unittest.TestCase):
    # Berlin, Experte level, as published: 75th percentile withheld above the ceiling.
    ROWS = [{"ageCategory": {"id": 1}, "gender": {"id": 1}, "branche": {"id": 1}, "entgelt": 6913,
             "entgeltQ25": 5540, "entgeltQ75": -2, "region": {"beitragsBemessungsGrenze": 8050}}]

    def _market(self, d):
        from job_scout import market as mk
        m = mk.MarketData(Path(d))
        m._fetch = lambda kldb, region: self.ROWS
        return m

    def test_withheld_q75_uses_lognormal_fit(self):
        with tempfile.TemporaryDirectory() as d:
            st = self._market(d).stat(job(title="Senior Backend Engineer", location="Berlin, Germany"), sal.Profile())
        self.assertEqual((st.region, st.level, st.q75), ("Berlin", "Experte", None))
        self.assertAlmostEqual(st.percentile(st.median), 50, places=5)
        self.assertAlmostEqual(st.percentile(st.q25), 25, delta=0.5)
        self.assertAlmostEqual(100 - st.percentile(120000), 13, delta=1.5)
        self.assertAlmostEqual(st.value_at(st.percentile(100000)), 100000, delta=50)

    def test_region_mapping(self):
        from job_scout.market import region_for
        p = sal.Profile(city="Berlin", country="Germany", country_aliases=["Deutschland"])
        self.assertEqual(region_for(job(location="München, Germany"), p), "Bayern")
        self.assertEqual(region_for(job(location="Hamburg or Berlin"), p), "Berlin")
        self.assertEqual(region_for(job(location="Somewhere, Germany"), p), "Deutschland")
        self.assertIsNone(region_for(job(location="Zurich, Switzerland"), p))

    def test_unverified_status(self):
        from job_scout.cli import _unverified_status
        c, neg, prof = Criteria(), sal.Negotiation(), sal.Profile()
        with tempfile.TemporaryDirectory() as d:
            m = self._market(d)
            startup = job(company="Tiny GmbH", location="Berlin, Germany", country="Germany", min_eur=120000)
            self.assertEqual(_unverified_status(startup, c, neg, prof, m, set(), 75), "market unlikely")
            self.assertAlmostEqual(startup.market_share_above_min, 13, delta=1.5)
            big = job(company="Bigco SE", location="Berlin, Germany", country="Germany", min_eur=120000)
            self.assertEqual(_unverified_status(big, c, neg, prof, m, {"bigco"}, 75), "high payer")
            low = job(location="Berlin", country="Germany", min_eur=80000)
            self.assertEqual(_unverified_status(low, c, neg, prof, m, set(), 75), "market plausible")
            nowhere = job(location="Tokyo", country="Japan", min_eur=120000)
            self.assertEqual(_unverified_status(nowhere, c, neg, prof, m, set(), 75), "unverified")

    def test_other_countries_scaled_by_eurostat(self):
        with tempfile.TemporaryDirectory() as d:
            m = self._market(d)
            m._ses = {"DE": 70000, "CH": 112000, "PL": 21000}
            de = m.stat(job(title="Senior Engineer", location="Germany", country="Germany"), sal.Profile())
            ch = m.stat(job(title="Senior Engineer", location="Zurich", country="Switzerland"), sal.Profile())
            pl = m.stat(job(title="Senior Engineer", location="Warsaw", country="Poland"), sal.Profile())
            self.assertIsNone(m.stat(job(location="London", country="United Kingdom"), sal.Profile()))
        self.assertAlmostEqual(ch.median / de.median, 1.6)
        self.assertAlmostEqual(pl.median / de.median, 0.3)
        self.assertTrue(ch.approx and not de.approx)
        # Scaling keeps the shape: the same minimum-to-median ratio sits at the same percentile.
        self.assertAlmostEqual(ch.percentile(ch.median * 1.2), de.percentile(de.median * 1.2))


class Countries(unittest.TestCase):
    def test_detection(self):
        from job_scout.country import countries_in, job_country
        self.assertEqual(countries_in("Zürich, Switzerland"), ["Switzerland"])
        self.assertEqual(countries_in("Amsterdam; Dublin"), ["Netherlands", "Ireland"])
        self.assertEqual(job_country("Amsterdam; Dublin", "Germany"), "Netherlands")
        self.assertEqual(job_country("Germany; Portugal; France", "Germany"), "Germany")  # home listed
        self.assertEqual(job_country("Remote - Europe", "Germany"), "Germany")          # no country
        self.assertEqual(job_country("Warszawa", "Germany"), "Poland")
        self.assertEqual(countries_in("Leuven"), [])  # no false "EU"/"UK" hits

    def test_minimums(self):
        from job_scout.country import minimum_for
        self.assertEqual(minimum_for("Switzerland", {}, 90000), 160000)            # built-in default
        self.assertEqual(minimum_for("Switzerland", {"Switzerland": 150000}, 0), 150000)  # config wins
        self.assertEqual(minimum_for("Austria", {}, 90000), 90000)                 # neither: fallback
        self.assertEqual(minimum_for("Poland", {}, 0), 80000)

    def test_currency_and_parsed_band_usability(self):
        prof = sal.Profile(country="Germany")
        chf = job(location="Zurich", country="Switzerland", salary=Salary(150000, 180000, "CHF", origin="parsed"))
        usd = job(location="Zurich", country="Switzerland", salary=Salary(150000, 180000, "USD", origin="parsed"))
        self.assertTrue(sal.listed_salary_usable(chf, prof))
        self.assertFalse(sal.listed_salary_usable(usd, prof))

    def test_sibling_estimate_uses_the_jobs_country(self):
        prof = sal.Profile(country="Germany")
        target = job(job_id="t", title="Senior Engineer", location="Zurich", country="Switzerland")
        berlin = job(job_id="a", title="Senior Engineer", location="Berlin", salary=Salary(90000, 110000, "EUR"))
        zurich = job(job_id="b", title="Senior Engineer", location="Zürich, Switzerland",
                     salary=Salary(150000, 170000, "CHF"))
        us_tier = job(job_id="c", title="Senior Engineer", location="Zurich; New York",
                      salary=Salary(250000, 300000, "USD"))
        e = sal.sibling_estimate(target, [target, berlin, zurich, us_tier], prof,
                                 {"EUR": 1.0, "CHF": 1.0, "USD": 1.0}, lambda t: True)
        self.assertEqual((e.low, e.high), (150000, 170000))

    def test_research_asks_about_the_jobs_country(self):
        router = llm.Router([FakeBackend("anthropic", OK)])
        with tempfile.TemporaryDirectory() as d:
            r = research.SalaryResearch({}, sal.Profile(city="Berlin", country="Germany"), {"EUR": 1.0},
                                        Path(d), router=router)
            r.estimate(job(location="Copenhagen", country="Denmark"))
            prompt = router.backends[0].calls[0]["messages"][0]["content"]
        self.assertIn("Estimate for employment in Denmark, paid in DKK", prompt)
        self.assertIn("based in Berlin, Germany", prompt)


class ResearchCaps(unittest.TestCase):
    def test_per_backend_caps(self):
        bed, anth = FakeBackend("bedrock", OK), FakeBackend("anthropic", OK)
        r = llm.Router([bed, anth], caps={"bedrock": 0, "anthropic": 1})
        body = {"messages": [{"role": "user", "content": "q"}], "tools": [1]}
        for _ in range(5):
            self.assertEqual(r.create(body)[1], "bedrock")  # 0 = unlimited
        self.assertFalse(r.exhausted)

    def test_fallback_backend_cap_stops_lookups(self):
        bed = FakeBackend("bedrock", llm.Unavailable("expired"))
        anth = FakeBackend("anthropic", OK)
        router = llm.Router([bed, anth], caps={"anthropic": 1})
        with tempfile.TemporaryDirectory() as d:
            res = research.SalaryResearch({}, sal.Profile(), {"EUR": 1.0}, Path(d), router=router)
            self.assertIsNotNone(res.estimate(job(company="A")))
            self.assertIsNone(res.estimate(job(company="B")))
        self.assertTrue(router.exhausted)
        self.assertEqual((res.lookups, res.skipped, res.errors), (1, 1, []))
        self.assertEqual(len(anth.calls), 1)


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
