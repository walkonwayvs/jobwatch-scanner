# jobwatch-scanner

A small cron script that watches job boards and GitHub for two things:
paid work in crypto infrastructure, and early-stage networks worth running
a node on. It posts to Discord when it finds something and stays silent
otherwise.

Python standard library only. No dependencies, no framework, no database.

## Why it exists

I found a QA contract role for a decentralised inference network by
scrolling past it on Twitter. That posting had existed for three weeks
before I saw it — first as a governance discussion on GitHub, then as a
markdown file in a repo, then finally as a tweet. I only caught it because
I happened to pause on the right post.

This script is the answer to "what if I hadn't paused."

It checks the places these postings actually originate, rather than the
place they eventually end up.

## What it watches

**Named job boards.** Greenhouse, Lever, Ashby and Recruitee all publish
public JSON for a company's open roles. No key required.

**Broad job boards.** RemoteOK and We Work Remotely, filtered hard (see
below).

**GitHub repo activity.** Commit and release feeds for repos you care
about, via each repo's Atom feed. This is how the original posting first
appeared — as a commit adding `jobs/qa-testing-engineer.md`.

**GitHub Discussions, on repos you list.** Via GraphQL. Discussions have
no RSS feed, so this needs a token.

**GitHub Discussions, across all of GitHub.** The wide net. This is the
part that finds projects you have never heard of, and it is the single
most useful source in the script.

## Two nets, one channel

Alerts are tagged so you can tell them apart at a glance:

- `[JOB]` — a paid role or contract
- `[PROJECT]` — an early network worth investigating
- `[STATUS]` — weekly "still alive" note, so silence is never ambiguous

The two nets use separate keyword lists and separate filters. A job alert
and a project alert are looking for completely different things.

## Filtering

The hard problem is not finding matches. It is not drowning in them.

**Broad boards need two conditions.** A posting must contain a role word
*and* a domain word. Searching for "QA" alone returns every insurance
company on earth. Requiring "QA" *and* "blockchain" cuts that by
roughly 99%.

**Project hits must be organisation-owned.** Not personal accounts. This
was chosen over a star-count floor after testing — see below.

**One hit per repo per run.** A documentation repo will otherwise fire
three near-identical alerts from three near-identical pages.

**An explicit denylist of established orgs.** Star count does not
identify big projects reliably, because large chains post governance
discussions in side repos with low star counts. There is no clean
programmatic signal for "already too well known." A hand-maintained list
is blunt, and it is what works.

**A cap on alerts per run**, so a badly chosen keyword annoys you eight
times rather than eighty.

## What did not work

Documented because the dead ends took longer than the working code, and
because the reasons are not obvious from the outside.

**GitHub code search cannot find new repos.** Searching file contents
seemed like the obvious way to find job postings committed as markdown.
It is not: brand-new repositories with no stars are never added to the
code search index. Querying `repo:<the exact repo> QA` returned zero
results for a repo containing the word "QA" in six files. Community job
postings live almost exclusively in exactly this kind of repo, so this
approach cannot work in principle, not just in practice.

**Remotive's API silently ignores its search parameter.** Every query
returned exactly 35 results. Searching `qa`, `blockchain` and
`nonsensewordxyz` returned the same 35 records. The response body carries
a deprecation notice. Five configured searches were, in effect, one
search repeated five times.

**CryptoJobsList is behind Cloudflare.** The documented RSS URL is
correct, but returns a bot-challenge page rather than a feed.

**Organisation-level Atom feeds do not exist.** `github.com/<name>.atom`
returns entries for user accounts and nothing for organisations, which is
not stated anywhere obvious. Feeds must be configured per repository.

**GraphQL `discussions(last: N)` is not "the newest N."** An explicit
`orderBy: {field: UPDATED_AT, direction: DESC}` is required. Without it,
the discussion this whole project was built around did not appear in the
results.

**Title matching alone is insufficient.** The discussion that contained
the job was titled "External Test Lab & Community DevNet" — no "QA", no
"hiring", no "vacancy". The role was described in the body. Body text has
to be searched, with a stricter keyword list to control noise.

**A star-count floor was the wrong filter.** Filtering out repos with
fewer than three stars removed the single most promising lead found in
testing: a post-quantum L1 with ten public repositories, a grants
programme, a live testnet and a wallet-integration pull request open
against a major hardware wallet — whose proposals repo had zero stars.
Star count punishes exactly the thing being searched for. Account type
turned out to be the better signal.

## Setup

```bash
git clone <this repo> && cd jobwatch-scanner
cp config.example.json config.json
```

Edit `config.json`: add a Discord webhook URL, then the companies, repos
and keywords you care about.

A GitHub token is optional but recommended — without one, both
Discussions sources are skipped. A fine-grained token with public
repository read-only access is sufficient:

```bash
echo "ghp_your_token_here" > token.txt
chmod 600 token.txt
```

Check the wiring, then do a first run:

```bash
python3 jobwatch.py --test      # sends one message to Discord
python3 jobwatch.py             # first run: silent by design
```

The first run records everything currently posted as already seen, so you
are not flooded with several hundred existing listings. From the second
run onward you only hear about new things.

Schedule it:

```
0 */4 * * * cd /path/to/jobwatch && /usr/bin/python3 jobwatch.py >> cron.log 2>&1
```

## Adding a company

Find the company's careers page and check where it redirects:

| Board      | URL looks like                     | `token` value       |
|------------|------------------------------------|---------------------|
| greenhouse | `boards.greenhouse.io/acme`        | `acme`              |
| lever      | `jobs.lever.co/acme`               | `acme`              |
| ashby      | `jobs.ashbyhq.com/acme-inc`        | `acme-inc`          |
| recruitee  | `acme.recruitee.com`               | `acme`              |

If a company does not use one of these, watch their GitHub repo instead.
For the kind of work this script is aimed at, the repo is usually the
better source anyway.

## Files

| File                  | Purpose                              |
|-----------------------|--------------------------------------|
| `jobwatch.py`         | The script                           |
| `config.example.json` | Template — copy to `config.json`     |
| `seen.json`           | What you have already been told      |
| `token.txt`           | GitHub token (gitignored)            |
| `jobwatch.log`        | Run history, rotates at 2 MB         |

`config.json`, `token.txt` and all state files are gitignored. Do not
commit them: `config.json` holds a Discord webhook and `token.txt` holds
a GitHub credential.

## Notes

Failures are non-fatal by design. An unreachable source logs a line and
the run continues. The script only reads from the internet and writes to
its own directory.

Tuning is meant to be done against real output rather than in advance.
Every filter in this repository exists because something specific and
unwanted showed up in a Discord channel first.

## Licence

MIT
