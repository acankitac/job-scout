# Using job-scout

## 1. Setup

You need **Python 3.11 or newer** (check with `python3 --version`). Nothing else to install.

```bash
git clone https://github.com/acankitac/job-scout.git
cd job-scout
cp config.example.toml config.toml
./scout.py --help
```

`config.toml` is your personal copy and is git-ignored, so your criteria and any companies you
list are never committed.

## 2. Your first run

```bash
./scout.py
```

This searches the job market with the terms and places in `[generic]` in your `config.toml`
and writes two files:

- `reports/jobs-YYYY-MM-DD.md`: the report. Open it in any Markdown viewer, VS Code, or GitHub.
- `reports/jobs-YYYY-MM-DD.csv`: the same jobs, for sorting and tracking in a spreadsheet.

The report has four sections:

1. **Jobs**: every match, highest score first. 🆕 marks roles you haven't seen before, and
   "Yrs" is the experience the posting asks for.
2. **Salary estimates**: for roles that don't list pay, an estimated range, what's reachable
   with negotiation, and the sources (see step 5).
3. **Recruiter contacts**: for each company with matches, recruiter emails (if Hunter.io is
   set up, see step 6) and LinkedIn search links.
4. **Check by hand**: search links for companies with no public job feed, plus LinkedIn Jobs.

## 3. What to search

`[generic]` in `config.toml` controls the market-wide search:

```toml
[generic]
sources = ["arbeitsagentur", "arbeitnow", "remotive", "adzuna"]
queries = ["backend engineer", "software engineer", "softwareentwickler backend"]
where = ["Berlin", "München"]   # places to search around
radius_km = 30
```

- **Queries** are sent to every source. Include German terms: many German employers use them.
- **Where** sets the places searched by sources that support location search (Bundesagentur, Adzuna).
  Your `locations` filter (step 4) then decides which results you see.
- **Adzuna** needs a free key from [developer.adzuna.com](https://developer.adzuna.com):
  `export ADZUNA_APP_ID=...` and `export ADZUNA_APP_KEY=...`. Without them it's skipped.
- Override for one run: `./scout.py --query "golang" --where Hamburg`.

**Watch-list mode.** Some companies, especially larger tech firms, post only on their own careers
site. List them under `[[companies]]` (step 7) and run:

```bash
./scout.py --companies      # only your listed companies
./scout.py --all-sources    # the market plus your listed companies
```

## 4. Set your criteria

Open your `config.toml`. Everything under `[criteria]` is commented; these are the settings most
people change first:

```toml
[criteria]
locations = ["Berlin", "Germany"]          # a job passes if its location names any of these
remote_regions = ["Europe", "EMEA", "Germany"]  # remote roles must name one of these regions
salary_min_eur = 80000                      # annual, EUR; 0 = no minimum
salary_max_eur = 0                          # 0 = no maximum
max_years_required = 7                      # drop roles expecting more years than this
min_years_expected = 3                      # drop roles aimed below this (e.g. "0-2 years")
```

**Titles.** `title_include` and `title_exclude` are regular expressions matched against the
job title, and they do most of the filtering. A job must match at least one include pattern
and no exclude pattern. To target a different role type, change these first.

**Ranking.** `[criteria.keywords]` maps a keyword to a weight. Each keyword found in the job
description adds its weight to the score, doubled if it's in the title. Put your strongest
skills at the top weights. `min_score` drops anything below the threshold.

**Experience, not titles.** Seniority words in titles are unreliable: some companies call every
engineer "Member of Technical Staff", and "Lead" is often a hands-on engineer. So `title_exclude`
should list role *types* you don't want (manager, frontend, sales), not levels. Experience fit is
judged from the job description instead: "5+ years", "3-5 years", "at least five years",
"mindestens 5 Jahre" are all recognised. When the description doesn't say, which is common, the
typical experience for that company's level for the role is researched online (see step 5 for
the API key). Without research those roles are kept and shown as "?", never rejected on a guess.
The report's "Yrs" column shows what was found: `5+` from the description, `≈5–8*` researched.

**Salary.** Set `salary_min_eur` and/or `salary_max_eur` (annual base, EUR). See the next section
for how roles that don't list pay are handled.

**Tip:** after changing filters, run `./scout.py --explain` to see every rejected job and the
reason, so you can tell whether a filter is too tight.

## 5. Salary and experience research

Most European postings don't list pay. When you set a salary bound, job-scout doesn't drop
those roles. It estimates them and judges every role by **what you could reach by negotiating**.

**A minimum per country.** €120k is an ordinary senior salary in Zurich and an exceptional one in
Warsaw, so the minimum depends on the country the job would employ you in:

```toml
[criteria]
salary_min_eur = 120000          # countries without their own minimum
use_default_minimums = true      # fill gaps from the built-in defaults

[criteria.salary_min_by_country]  # annual base, EUR
Germany = 115000
Switzerland = 160000
Denmark = 110000
Netherlands = 115000
Ireland = 115000
Poland = 80000
```

A country missing from `salary_min_by_country` uses the built-in default above, if there is one,
then `salary_min_eur`. `use_default_minimums = false` uses only what you list. `--min-salary N` on
the command line applies one minimum to every country for that run.

The job's country comes from its location: country names in English or the local language, and
major cities ("Zürich", "Warszawa", "Aarhus"). A posting open in several countries is judged for
your home country (`[profile] country`) if it's one of them, otherwise the first listed. Remote
postings that name no country ("Remote - Europe") are judged for your home country. Research and
company-band estimates use that country and its currency, so a Zurich role is compared with
Zurich pay in CHF, not Berlin pay.

**How the bounds work**

| You set | Effect |
|---|---|
| a minimum (max = 0) | No upper limit. A role passes if its reachable pay meets your minimum for its country. |
| only `salary_max_eur` (min = 0) | Drops roles whose pay *floor* is above it. Rarely useful. |
| both | Both rules apply. |
| neither | No salary filtering, and no paid research (company-posting estimates are still shown). |

**How each role's pay is judged** (first match wins)

1. **Listed in the posting:** checked against your bounds.
2. **The company's own postings.** If the same company lists a salary on other roles at the same
   level in your country, their published band is used. Free and usually accurate.
3. **Web research.** Claude searches an allowlist of salary sources (levels.fyi, Glassdoor,
   kununu, StepStone, gehalt.de and others in `[research] trusted_domains`, plus the company's
   own site) for the company's base-salary range at the role's level in your country, and how
   negotiable it is. Best-scoring roles are researched first.
4. **In your `high_payers` list:** kept in the main list as "high payer", even without data.
5. **Market check.** Official pay statistics for that kind of role and region show how much of
   the market pays your minimum. Germany: Bundesagentur Entgeltatlas, by skill level and federal
   state. Other European countries: an approximation, marked ≈. Eurostat only publishes pay for
   "professionals" as a whole, so the German software-developer figures are scaled by each
   country's ratio to Germany (Switzerland about 1.6×, Poland about 0.3×). It understates tech pay
   where tech earns unusually more than other professions, notably Poland, so treat ≈ figures as
   rough and prefer research there. If a typical offer would have
   to sit above the `unlikely_above_percentile` (default 75th) to reach it with negotiation, the
   role is "unlikely"; otherwise "plausible" and it stays in the main list.
6. **No data at all** (countries outside Eurostat, e.g. the UK or US): "unverified".

"Unlikely" and "unverified" roles go to a separate **Salary not verified** section of the report,
with the reason and the market figures, so the main list only holds roles whose pay is known or
plausible. Set `[salary_check] unverified = "keep"` to leave them in the main list, or `"drop"`
to hide them.

For context on the market check: for experienced software developers in Berlin the official
median is about €83k and €120k is roughly the top 13% of the market. The official figures stop at
the social-insurance ceiling (about €97k/year), so the upper end is modelled from the median and
25th percentile, which are always published. The market check can't tell a well-paying company
from the rest; that's what research and `high_payers` are for.

```toml
[salary_check]
unverified = "separate"          # separate | keep | drop
market_data = true
unlikely_above_percentile = 75
high_payers = ["ExampleCorp", "SampleLabs"]
```

**How negotiation is counted.** Reachable pay = typical offer + headroom, capped at the top of the
band. Headroom depends on how negotiable the company is (`[negotiation]`: 3% for fixed-pay
companies, 8% typical, 15% for companies that routinely negotiate up, 10% when unknown). For a
listed range, reachable pay is the top of the range. Roles that meet your minimum **only** with
negotiation are marked 🤝 in the report. For those, open by asking for the top of the band.

**Set up web research** (used for both unlisted salaries and unstated experience)

Research runs on **Amazon Bedrock** and/or the **Anthropic API**, tried in the order set by
`providers` in `[research]` (default: Bedrock first, then Anthropic). If a backend's credentials
are missing or expired, the model isn't enabled, or it can't run the web-search tool, the tool
says so once and uses the next one for the rest of the run.

*Bedrock:* put credentials with `bedrock:InvokeModel` permission (and `bedrock:ListInferenceProfiles`
for `bedrock_model_id = "auto"`) under a profile in `~/.aws/credentials`, and set
`bedrock_profile` and `bedrock_region`. Claude must be enabled for the account in that region.
Use a personal AWS account: calls are billed to and logged against whichever account you use.

*Anthropic:* create an API key at [console.anthropic.com](https://console.anthropic.com) and set it
in your shell (add the line to `~/.zshrc` to make it permanent):

```bash
export ANTHROPIC_API_KEY=your_key_here
```

Either way, fill in `[profile]` in `config.toml` (years of experience, city, country, currency).

**Web search matters.** Estimates are only as good as their sources, so a backend that can't run
Claude's web-search tool is skipped by default. Set `allow_without_web_search = true` to accept
answers from model knowledge instead; they're labelled "model knowledge (no web search)", carry
low confidence and no sources, and are re-checked after 3 days rather than 30.

Each lookup costs roughly $0.05–0.20. Results are cached for 30 days: salary per company +
level + country, experience per company + title ("Senior Software Engineer - Payments" and
"Senior Software Engineer, Identity" share one lookup), so reruns are free. Research only runs
for roles that passed every other filter, best matches first, `concurrency` (default 4) at a time.

Lookups per run are capped **per backend** with `caps` (0 = unlimited). The default leaves Bedrock
unlimited, bounded only by your account's own quotas (throttling is retried), and caps the
Anthropic fallback at 20 per run to keep that bill predictable. When a cap is hit, the rest are
left for the next run, which picks up where this one stopped.

```toml
[research]
caps = { bedrock = 0, anthropic = 20 }
concurrency = 4
``` Use `--no-research` to turn it off for a run, or `--research` to estimate even without a
salary bound.

The report's **Salary estimates** section lists every estimate with its range, typical figure,
reachable figure, negotiation room, confidence and source links. Treat web-research figures as a
starting point for your own check, not a guarantee.

## 6. Recruiter emails (optional)

1. Create a free account at [hunter.io](https://hunter.io) and copy your API key.
2. Set it in your shell (add the line to `~/.zshrc` to make it permanent):

   ```bash
   export HUNTER_API_KEY=your_key_here
   ```

3. Run `./scout.py` as usual.

For each company with matches you'll get up to five contacts, recruiters first, with name, title,
email, Hunter's confidence score and often a LinkedIn URL. The report also shows the company's
email format (e.g. `{first}.{last}@company.com`), useful for reaching a specific person you found
on LinkedIn.

The free plan allows about 25 lookups a month. Results are cached for 30 days in `.cache/hunter/`,
so repeat runs are free; only a company appearing for the first time costs a lookup. Because a
market-wide search turns up hundreds of employers, each run looks up at most
`max_lookups_per_run` (default 10) new companies, best matches first; companies without a known
domain are looked up by name. Use `--no-contacts` to skip lookups entirely.

## 7. Adding companies (watch-list mode)

Open the company's careers page, click into any job, and look at the address bar:

| The job URL contains | Set `ats` to | Set `token` to |
|---|---|---|
| `job-boards.greenhouse.io/<name>/` or `gh_jid=` | `greenhouse` | `<name>` |
| `jobs.ashbyhq.com/<name>/` | `ashby` | `<name>` |
| `jobs.lever.co/<name>/` | `lever` | `<name>` |

Then add it to `config.toml`. For a job URL like `job-boards.greenhouse.io/examplecorp/jobs/123`:

```toml
[[companies]]
name = "ExampleCorp"
ats = "greenhouse"
token = "examplecorp"
domain = "examplecorp.com"     # used for the recruiter email lookup
```

If a careers page hides which system it uses, you can test a guess. This prints a job count if
the token is right:

```bash
curl -s https://boards-api.greenhouse.io/v1/boards/<guess>/jobs | python3 -c "import json,sys; print(len(json.load(sys.stdin)['jobs']))"
```

Companies with no ATS feed can go under `[[manual]]` as a search link. `{query}` and `{location}`
are filled in from the `[search]` section:

```toml
[[manual]]
name = "ExampleCorp"
url = "https://careers.examplecorp.com/search?q={query}&location={location}"
```

## 8. Command-line options

Every option overrides `config.toml` for that run only.

| Option | What it does |
|---|---|
| `--companies` | Search only the `[[companies]]` job boards, not the whole market |
| `--all-sources` | Search the market and the `[[companies]]` job boards |
| `--query TEXT` | Replace the `[generic]` search terms; repeat for several |
| `--where PLACE` | Replace the `[generic]` places; repeat for several |
| `--new-only` | Only roles not seen in a previous run |
| `--only NAME` | Keep only this employer (name match); repeat for several |
| `--location CITY` | Replace the configured locations; repeat for several |
| `--min-salary N` / `--max-salary N` | Annual salary bounds in EUR |
| `--require-salary` | Drop jobs that don't list a salary |
| `--max-age DAYS` | Only roles posted in the last N days |
| `--top N` | Keep only the N highest-scoring roles |
| `--no-contacts` | Skip Hunter.io lookups |
| `--no-research` | No paid salary or experience research this run (cached results still used) |
| `--research` | Research unlisted salaries even without a salary bound |
| `--explain` | Print every rejected job and why |
| `-c FILE` | Use a different config file |
| `--out DIR` | Write reports somewhere other than `reports/` |

## 9. Recipes

```bash
# Daily check: just what's new, top 20
./scout.py --new-only --top 20

# At least €90k, counting estimated and negotiated pay; unverified roles listed separately
./scout.py --min-salary 90000

# Only roles that actually publish a salary of at least €90k
./scout.py --require-salary --min-salary 90000

# One company, to see everything they have
./scout.py --only ExampleCorp --explain

# Different city, different search profile
./scout.py -c config-amsterdam.toml
```

**Run it automatically every morning** (macOS/Linux) with `crontab -e`:

```
0 8 * * * cd /path/to/job-scout && ./scout.py --new-only >> reports/cron.log 2>&1
```

## 10. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `! arbeitnow: ... HTTP 429` | Arbeitnow throttled paging. Results from earlier pages are kept; lower `arbeitnow_pages` if it recurs. |
| `N postings not opened, max_detail_fetches reached` | Raise `max_detail_fetches`, or narrow `queries`/`where`. Unopened postings are judged on title only. |
| `! Company: board not found (404)` | The token is wrong or the company moved to another ATS. Recheck the job URL (step 5). |
| A job you expected is missing | Run `--only Company --explain` and find it in the rejected list; the reason tells you which filter to loosen. |
| Everything is 🆕 again | The seen-jobs memory is in `.state/seen.json`. It was deleted or you're running from a different folder. |
| `hunter ...: HTTP 401` | The API key is wrong or not exported in this shell. |
| `hunter ...: HTTP 429 (quota exhausted?)` | Monthly free lookups are used up. Cached companies still work. |
| Salary looks wrong | If it's marked "(parsed)" it was read from the description and may be a different region's pay band. Check the posting. |
| `research: bedrock unavailable ... ExpiredToken` or `invalid security token` | The Bedrock profile's credentials have expired. Refresh them; the run falls back to Anthropic meanwhile. |
| `research: bedrock can't run web search` | Bedrock rejected the web-search tool in that region or for that model. Research falls back to Anthropic, or see `allow_without_web_search`. |
| `research: bedrock skipped: boto3 is not installed` | `pip install boto3`, or remove `"bedrock"` from `providers`. |
| `...anthropic: HTTP 401` | `ANTHROPIC_API_KEY` is wrong or not exported in this shell. |
| "N roles not researched, a per-run cap was reached" | Rerun; finished lookups are cached, so it continues where it stopped. Or raise `caps`. |
| A company you know pays well is in "Salary not verified" | Add it to `[salary_check] high_payers`, or enable research. |
| "N roles don't state years of experience; kept" | No research backend was available. Set one up (step 5), or check those roles by hand. |
| A role was rejected for experience you think is wrong | `--explain` shows whether it came from the description or research. Delete its file in `.cache/experience/` to research again. |
| An estimate looks off | Delete its file in `.cache/salary/` and rerun, or check the linked sources in the report. |

## 11. Running the tests

```bash
python3 -m unittest discover -s tests -t .
```
