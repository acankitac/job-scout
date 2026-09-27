# Using job-scout

## 1. Setup

You need **Python 3.11 or newer** (check with `python3 --version`). Nothing else to install.

```bash
git clone https://github.com/acankitac/job-scout.git
cd job-scout
cp config.example.toml config.toml
./scout.py --help
```

`config.toml` is your personal copy and is git-ignored, so the companies you're targeting are
never committed. Add your companies to it before the first run (step 6).

## 2. Your first run

```bash
./scout.py
```

This checks every company in your `config.toml` and writes two files:

- `reports/jobs-YYYY-MM-DD.md`: the report. Open it in any Markdown viewer, VS Code, or GitHub.
- `reports/jobs-YYYY-MM-DD.csv`: the same jobs, for sorting and tracking in a spreadsheet.

The report has four sections:

1. **Jobs**: every match, highest score first. 🆕 marks roles you haven't seen before, and
   "Yrs" is the experience the posting asks for.
2. **Salary estimates**: for roles that don't list pay, an estimated range, what's reachable
   with negotiation, and the sources (see step 4).
3. **Recruiter contacts**: for each company with matches, recruiter emails (if Hunter.io is
   set up, see step 5) and LinkedIn search links.
4. **Check by hand**: search links for companies with no public job feed, plus LinkedIn Jobs.

## 3. Set your criteria

Open your `config.toml`. Everything under `[criteria]` is commented; these are the settings most
people change first:

```toml
[criteria]
locations = ["Berlin", "Germany"]          # a job passes if its location names any of these
remote_regions = ["Europe", "EMEA", "Germany"]  # remote roles must name one of these regions
salary_min_eur = 80000                      # annual, EUR; 0 = no minimum
salary_max_eur = 0                          # 0 = no maximum
max_years_required = 7                      # drop roles asking for more than this
```

**Titles.** `title_include` and `title_exclude` are regular expressions matched against the
job title, and they do most of the filtering. A job must match at least one include pattern
and no exclude pattern. To target a different role type, change these first.

**Ranking.** `[criteria.keywords]` maps a keyword to a weight. Each keyword found in the job
description adds its weight to the score, doubled if it's in the title. Put your strongest
skills at the top weights. `min_score` drops anything below the threshold.

**Salary.** Set `salary_min_eur` and/or `salary_max_eur` (annual base, EUR). See the next section
for how roles that don't list pay are handled.

**Tip:** after changing filters, run `./scout.py --explain` to see every rejected job and the
reason, so you can tell whether a filter is too tight.

## 4. Salary: listed, estimated and negotiated

Most European postings don't list pay. When you set a salary bound, job-scout doesn't drop
those roles. It estimates them and judges every role by **what you could reach by negotiating**.

**How the bounds work**

| You set | Effect |
|---|---|
| only `salary_min_eur` (max = 0) | No upper limit. A role passes if its reachable pay meets your minimum. |
| only `salary_max_eur` (min = 0) | Drops roles whose pay *floor* is above it. Rarely useful. |
| both | Both rules apply. |
| neither | No salary filtering, and no paid research (company-posting estimates are still shown). |

**Where an estimate comes from** (most trusted first)

1. **The company's own postings.** If the same company lists a salary on other roles at the same
   level in your country, their published band is used. Free and usually accurate.
2. **Web research.** Claude searches an allowlist of salary sources (levels.fyi, Glassdoor,
   kununu, StepStone, gehalt.de and others in `[research] trusted_domains`, plus the company's
   own site). It works out the company's level for the role and your years of experience,
   and returns a base-salary range for your country, how negotiable the company is, and its sources.
3. **Neither available:** the role is kept and shown as "not listed".

**How negotiation is counted.** Reachable pay = typical offer + headroom, capped at the top of the
band. Headroom depends on how negotiable the company is (`[negotiation]`: 3% for fixed-pay
companies, 8% typical, 15% for companies that routinely negotiate up, 10% when unknown). For a
listed range, reachable pay is the top of the range. Roles that meet your minimum **only** with
negotiation are marked 🤝 in the report. For those, open by asking for the top of the band.

**Set up web research**

1. Create an API key at [console.anthropic.com](https://console.anthropic.com).
2. Set it in your shell (add the line to `~/.zshrc` to make it permanent):

   ```bash
   export ANTHROPIC_API_KEY=your_key_here
   ```

3. Fill in `[profile]` in `config.toml` (years of experience, city, country, currency).

Each lookup costs roughly $0.05–0.20. Results are cached for 30 days per company + level +
country, so one lookup covers every senior role at that company, and reruns are free. No run
makes more than `max_lookups_per_run` (default 20) paid lookups; if the cap is hit, rerun to
continue. Use `--no-research` to turn it off for a run, or `--research` to estimate even without a
salary bound.

The report's **Salary estimates** section lists every estimate with its range, typical figure,
reachable figure, negotiation room, confidence and source links. Treat web-research figures as a
starting point for your own check, not a guarantee.

## 5. Recruiter emails (optional)

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
so repeat runs are free; only a company appearing for the first time costs a lookup. Use
`--no-contacts` to skip lookups entirely.

## 6. Adding companies

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

## 7. Command-line options

Every option overrides `config.toml` for that run only.

| Option | What it does |
|---|---|
| `--new-only` | Only roles not seen in a previous run |
| `--only NAME` | Check only this company; repeat for several |
| `--location CITY` | Replace the configured locations; repeat for several |
| `--min-salary N` / `--max-salary N` | Annual salary bounds in EUR |
| `--require-salary` | Drop jobs that don't list a salary |
| `--max-age DAYS` | Only roles posted in the last N days |
| `--top N` | Keep only the N highest-scoring roles |
| `--no-contacts` | Skip Hunter.io lookups |
| `--no-research` | No paid salary research this run (cached estimates still used) |
| `--research` | Research unlisted salaries even without a salary bound |
| `--explain` | Print every rejected job and why |
| `-c FILE` | Use a different config file |
| `--out DIR` | Write reports somewhere other than `reports/` |

## 8. Recipes

```bash
# Daily check: just what's new, top 20
./scout.py --new-only --top 20

# At least €90k, counting estimated and negotiated pay
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

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `! Company: board not found (404)` | The token is wrong or the company moved to another ATS. Recheck the job URL (step 5). |
| A job you expected is missing | Run `--only Company --explain` and find it in the rejected list; the reason tells you which filter to loosen. |
| Everything is 🆕 again | The seen-jobs memory is in `.state/seen.json`. It was deleted or you're running from a different folder. |
| `hunter ...: HTTP 401` | The API key is wrong or not exported in this shell. |
| `hunter ...: HTTP 429 (quota exhausted?)` | Monthly free lookups are used up. Cached companies still work. |
| Salary looks wrong | If it's marked "(parsed)" it was read from the description and may be a different region's pay band. Check the posting. |
| `salary research ...: HTTP 401` | `ANTHROPIC_API_KEY` is wrong or not exported in this shell. |
| "N roles skipped, max_lookups_per_run reached" | Rerun; finished lookups are cached, so it continues where it stopped. |
| An estimate looks off | Delete its file in `.cache/salary/` and rerun, or check the linked sources in the report. |

## 10. Running the tests

```bash
python3 -m unittest discover -s tests -t .
```
