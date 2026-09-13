#!/usr/bin/env python3
"""
jobwatch.py - watches job boards and GitHub for roles worth applying to.

Three kinds of source:
  1. named job boards  - specific companies you care about
  2. broad job boards  - every crypto company, filtered hard
  3. github search     - hiring posts that only ever exist in a repo

Silent unless something matches. Standard library only.

Usage:
    python3 jobwatch.py              # normal run
    python3 jobwatch.py --dry-run    # print, send nothing
    python3 jobwatch.py --test       # one test message to Discord
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SEEN_PATH = os.path.join(HERE, "seen.json")
TOKEN_PATH = os.path.join(HERE, "token.txt")
LOG_PATH = os.path.join(HERE, "jobwatch.log")

TIMEOUT = 25
UA = "jobwatch/2.0 (personal job alert script)"


# ---------------------------------------------------------------- utilities

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fetch(url, headers=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        log(f"  ! could not reach {url.split('?')[0]} ({e})")
        return None


def load_json_file(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"! could not read {path} ({e})")
        return default


def save_json_file(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_token():
    if not os.path.exists(TOKEN_PATH):
        return None
    try:
        with open(TOKEN_PATH) as f:
            t = f.read().strip()
        return t or None
    except OSError:
        return None


# ---------------------------------------------------------------- matching

def make_matchers(config):
    role = [r.lower() for r in config.get("role_keywords", [])]
    domain = [d.lower() for d in config.get("domain_keywords", [])]
    exclude = [e.lower() for e in config.get("exclude", [])]

    def blocked(text):
        t = text.lower()
        return any(e in t for e in exclude)

    def role_hit(text):
        t = text.lower()
        for r in role:
            if r in t:
                return r
        return None

    def narrow(text):
        """For sources that are already the right companies: role word only."""
        if blocked(text):
            return None
        return role_hit(text)

    def broad(text):
        """For the whole internet: needs a role word AND a crypto word."""
        if blocked(text):
            return None
        r = role_hit(text)
        if not r:
            return None
        t = text.lower()
        for d in domain:
            if d in t:
                return f"{r} + {d}"
        return None

    return narrow, broad


# ---------------------------------------------------------------- named boards

def board_url(kind, token):
    kind = kind.lower()
    return {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
        "lever": f"https://api.lever.co/v0/postings/{token}?mode=json",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{token}",
        "recruitee": f"https://{token}.recruitee.com/api/offers/",
    }.get(kind)


def parse_board(kind, raw):
    kind = kind.lower()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out = []
    if kind == "greenhouse":
        for j in data.get("jobs", []):
            out.append((str(j.get("id")), j.get("title", ""),
                        (j.get("location") or {}).get("name", ""),
                        j.get("absolute_url", "")))
    elif kind == "lever":
        for j in data:
            out.append((str(j.get("id")), j.get("text", ""),
                        (j.get("categories") or {}).get("location", ""),
                        j.get("hostedUrl", "")))
    elif kind == "ashby":
        for j in data.get("jobs", []):
            out.append((str(j.get("id")), j.get("title", ""),
                        j.get("location", ""), j.get("jobUrl", "")))
    elif kind == "recruitee":
        for j in data.get("offers", []):
            out.append((str(j.get("id")), j.get("title", ""),
                        j.get("location", ""), j.get("careers_url", "")))
    return out


def check_named_boards(config, narrow, seen):
    hits = []
    for b in config.get("job_boards", []):
        name, kind, tok = b["name"], b["type"], b["token"]
        url = board_url(kind, tok)
        if not url:
            log(f"  ? unknown board type '{kind}' for {name}")
            continue
        raw = fetch(url)
        if raw is None:
            continue
        posts = parse_board(kind, raw)
        if not posts:
            log(f"  - {name}: no postings parsed")
            continue
        for jid, title, loc, link in posts:
            key = f"board:{name}:{jid}"
            if key in seen:
                continue
            hit = narrow(title)
            if hit:
                hits.append({"source": name, "title": title,
                             "detail": loc, "url": link, "matched": hit})
            seen[key] = int(time.time())
        log(f"  - {name}: {len(posts)} scanned")
    return hits


# ---------------------------------------------------------------- broad boards

class FeedParser(HTMLParser):
    """Handles both RSS <item> and Atom <entry>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.entries = []
        self._cur = None
        self._field = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("item", "entry"):
            self._cur = {"title": "", "link": "", "id": ""}
        elif self._cur is not None:
            if tag == "link" and a.get("href"):
                self._cur["link"] = a["href"]
            elif tag in ("title", "link", "guid", "id"):
                self._field = tag

    def handle_data(self, data):
        if self._cur is None or not self._field:
            return
        d = data.strip()
        if self._field == "title":
            self._cur["title"] += d
        elif self._field == "link" and d:
            self._cur["link"] = d
        elif self._field in ("guid", "id") and d:
            self._cur["id"] = d

    def handle_endtag(self, tag):
        if tag in ("item", "entry") and self._cur is not None:
            if not self._cur["id"]:
                self._cur["id"] = self._cur["link"] or self._cur["title"]
            self.entries.append(self._cur)
            self._cur = None
        elif tag in ("title", "link", "guid", "id"):
            self._field = None


def parse_broad(kind, raw):
    """Return (id, title, company, url) tuples."""
    kind = kind.lower()
    out = []
    if kind == "remoteok":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        for j in data:
            if not isinstance(j, dict) or not j.get("position"):
                continue
            out.append((str(j.get("id")), j.get("position", ""),
                        j.get("company", ""), j.get("url", "")))
    elif kind == "remotive":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        for j in data.get("jobs", []):
            out.append((str(j.get("id")), j.get("title", ""),
                        j.get("company_name", ""), j.get("url", "")))
    elif kind == "rss":
        p = FeedParser()
        p.feed(raw)
        for e in p.entries:
            out.append((e["id"], e["title"], "", e["link"]))
    return out


def check_broad_boards(config, broad, seen):
    hits = []
    for b in config.get("broad_boards", []):
        name, kind, url = b["name"], b["type"], b["url"]
        raw = fetch(url)
        if raw is None:
            continue
        posts = parse_broad(kind, raw)
        if not posts:
            log(f"  - {name}: no postings parsed")
            continue
        for jid, title, company, link in posts:
            key = f"broad:{name}:{jid}"
            if key in seen:
                continue
            # match against title and company together
            hit = broad(f"{title} {company}")
            if hit:
                hits.append({"source": name, "title": title,
                             "detail": company, "url": link, "matched": hit})
            seen[key] = int(time.time())
        log(f"  - {name}: {len(posts)} scanned")
    return hits


# ---------------------------------------------------------------- github

class AtomParser(FeedParser):
    pass



def check_forums(config, seen):
    """Watch DAO governance forums. Discourse exposes /latest.json
    publicly with no key - this is where funding proposals for
    external testing, audits and service providers get born."""
    forums = config.get("forums", [])
    if not forums:
        return []
    kws = [k.lower() for k in config.get("forum_keywords", [])]
    hits = []
    for f in forums:
        name, host = f["name"], f["host"].rstrip("/")
        raw = fetch(f"{host}/latest.json")
        if raw is None:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log(f"  - {name}: not a discourse feed")
            continue
        topics = ((data.get("topic_list") or {}).get("topics") or [])
        if not topics:
            log(f"  - {name}: no topics parsed")
            continue
        kept = 0
        for t in topics:
            tid = t.get("id")
            key = f"forum:{name}:{tid}"
            if key in seen:
                continue
            seen[key] = int(time.time())
            title = t.get("title", "")
            tl = title.lower()
            hit = next((k for k in kws if k in tl), None)
            if not hit:
                continue
            tech = [w.lower() for w in config.get("forum_tech_keywords", [])]
            if tech:
                t2 = next((w for w in tech if w in tl), None)
                if not t2:
                    continue
                hit = f"{hit} + {t2}"
            slug = t.get("slug", "")
            hits.append({"source": name, "title": title, "detail": "",
                         "url": f"{host}/t/{slug}/{tid}",
                         "matched": hit, "label": "JOB"})
            kept += 1
        log(f"  - {name}: {len(topics)} topics, {kept} kept")
    return hits


def check_github_repos(config, narrow, seen):
    hits = []
    for repo in config.get("github_repos", []):
        slug = repo["repo"]
        branch = repo.get("branch", "main")
        for kind, url in (
            ("commits", f"https://github.com/{slug}/commits/{branch}.atom"),
            ("releases", f"https://github.com/{slug}/releases.atom"),
        ):
            raw = fetch(url)
            if raw is None:
                continue
            p = AtomParser()
            p.feed(raw)
            for e in p.entries:
                key = f"gh:{slug}:{e['id']}"
                if key in seen:
                    continue
                hit = narrow(e["title"])
                if hit:
                    hits.append({"source": f"{slug} ({kind})",
                                 "title": e["title"], "detail": "",
                                 "url": e["link"], "matched": hit,
                                 "label": "INFRA"})
                seen[key] = int(time.time())
    return hits



def check_github_discussions(config, narrow, seen, token):
    """Watch repo Discussions - where community job posts actually start.
    Needs a token; Discussions have no RSS feed."""
    repos = [r["repo"] for r in config.get("github_repos", [])
             if r.get("discussions", True)]
    if not repos or not token:
        if repos and not token:
            log("  ! no token - skipping discussions")
        return []
    hits = []
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    for slug in repos:
        try:
            owner, name = slug.split("/", 1)
        except ValueError:
            continue
        q = ('{repository(owner:"%s",name:"%s"){discussions(first:15,'
             'orderBy:{field:UPDATED_AT,direction:DESC}){'
             'nodes{title url body}}}}' % (owner, name))
        body = json.dumps({"query": q}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.github.com/graphql", data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, json.JSONDecodeError) as e:
            log(f"  ! discussions {slug} ({e})")
            continue
        repo = (data.get("data") or {}).get("repository")
        if not repo:
            continue
        nodes = ((repo.get("discussions") or {}).get("nodes") or [])
        for n in nodes:
            url = n.get("url", "")
            key = f"ghd:{url}"
            if key in seen:
                continue
            _t = n.get("title", "")
            _blk = config.get("discussion_block_title", "")
            if _blk and re.search(_blk, _t, re.I):
                seen[key] = int(time.time())
                continue
            hit = narrow(_t)
            if not hit:
                body = (n.get("body") or "")[:4000].lower()
                for w in config.get("body_keywords", []):
                    if w.lower() in body:
                        hit = f"body: {w}"
                        break
            if hit:
                hits.append({"source": f"{slug} (discussion)",
                             "title": n.get("title", ""), "detail": "",
                             "url": url, "matched": hit})
            seen[key] = int(time.time())
    return hits



def check_discussion_search(config, seen, token, narrow=None):
    """Search Discussions across ALL of GitHub - finds projects you
    have never heard of. This is the wide net."""
    queries = config.get("discussion_search", [])
    if not queries:
        return []
    if not token:
        log("  ! no token - skipping global discussion search")
        return []
    hits = []
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    gql = ('{search(query:"%s",type:DISCUSSION,first:15){'
           'nodes{... on Discussion{title url createdAt '
           'repository{nameWithOwner}}}}}')
    for q in queries:
        safe = q.replace('"', '\\"')
        body = json.dumps({"query": gql % safe}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.github.com/graphql", data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, json.JSONDecodeError) as e:
            log(f"  ! discussion search '{q[:28]}' ({e})")
            continue
        nodes = ((data.get("data") or {}).get("search") or {}).get("nodes") or []
        for n in nodes:
            if not n:
                continue
            url = n.get("url", "")
            key = f"gds:{url}"
            if key in seen:
                continue
            repo = (n.get("repository") or {}).get("nameWithOwner", "")
            title = n.get("title", "")
            seen[key] = int(time.time())
            hit = narrow(title) if narrow else q
            if not hit:
                tl = title.lower()
                for w in config.get("body_keywords", []):
                    if w.lower() in tl:
                        hit = w
                        break
            if not hit:
                continue
            hits.append({"source": "discussion search",
                         "title": title, "detail": repo,
                         "url": url, "matched": hit})
        log(f"  - discussion search '{q[:34]}': {len(nodes)}")
    return hits



def check_project_search(config, seen, token):
    """Separate net: early projects worth running a node on,
    not paid roles. Tagged PROJECT in Discord."""
    queries = config.get("project_search", [])
    kws = [k.lower() for k in config.get("project_keywords", [])]
    if not queries or not token:
        return []
    hits = []
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    gql = ('{search(query:"%s",type:DISCUSSION,first:15){'
           'nodes{... on Discussion{title url '
           'repository{nameWithOwner stargazerCount '
           'owner{__typename}}}}}}')
    cap_stars = int(config.get("project_max_stars", 3000))
    orgs_only = bool(config.get("project_orgs_only", True))
    per_repo = {}
    max_per_repo = int(config.get("project_max_per_repo", 1))
    skip_orgs = {o.lower() for o in config.get("project_exclude_orgs", [])}
    for q in queries:
        body = json.dumps({"query": gql % q.replace('"', '')}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.github.com/graphql", data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, json.JSONDecodeError) as e:
            log(f"  ! project search '{q[:26]}' ({e})")
            continue
        nodes = ((data.get("data") or {}).get("search") or {}).get("nodes") or []
        kept = 0
        for n in nodes:
            if not n:
                continue
            url = n.get("url", "")
            key = f"prj:{url}"
            if key in seen:
                continue
            seen[key] = int(time.time())
            repo = n.get("repository") or {}
            stars = repo.get("stargazerCount", 0) or 0
            if stars > cap_stars:
                continue
            if orgs_only and (repo.get("owner") or {}).get(
                    "__typename") != "Organization":
                continue
            title = n.get("title", "")
            tl = title.lower()
            hit = next((k for k in kws if k in tl), None)
            if not hit:
                continue
            slug = repo.get('nameWithOwner', '')
            if slug.split('/')[0].lower() in skip_orgs:
                continue
            if per_repo.get(slug, 0) >= max_per_repo:
                continue
            per_repo[slug] = per_repo.get(slug, 0) + 1
            hits.append({"source": f"{slug} ({stars}*)",
                         "title": title, "detail": "", "url": url,
                         "matched": hit, "label": "PROJECT"})
            kept += 1
        log(f"  - project search '{q[:30]}': {kept} kept")
    return hits


def check_github_search(config, seen, token):
    """Search all of GitHub for hiring posts, not just repos we listed."""
    hits = []
    queries = config.get("github_search", [])
    if not queries:
        return hits
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for q in queries:
        kind = q.get("type", "issues")
        query = q["query"]
        url = (f"https://api.github.com/search/{kind}"
               f"?q={urllib.parse.quote(query)}&sort=updated"
               f"&order=desc&per_page=20")
        raw = fetch(url, headers)
        if raw is None:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if "items" not in data:
            log(f"  ! github search rejected: {data.get('message', 'unknown')}")
            continue
        for it in data.get("items", []):
            ident = str(it.get("id") or it.get("node_id") or it.get("html_url"))
            key = f"ghs:{kind}:{ident}"
            if key in seen:
                continue
            title = it.get("title") or it.get("full_name") or ""
            detail = it.get("repository_url", "").replace(
                "https://api.github.com/repos/", "") or it.get("description", "")
            hits.append({"source": f"github search ({kind})",
                         "title": title, "detail": (detail or "")[:80],
                         "url": it.get("html_url", ""), "matched": query})
            seen[key] = int(time.time())
        log(f"  - github search '{query[:40]}': {len(data.get('items', []))} results")
    return hits


# ---------------------------------------------------------------- output

def check_hackathons(config, seen):
    """Devpost hackathon feed. Public JSON API, no auth."""
    cfg = config.get("hackathons", {})
    if not cfg.get("enabled"):
        return []
    min_prize = cfg.get("min_prize", 5000)
    kw = [k.lower() for k in cfg.get("keywords", [])]
    hits = []
    for term in cfg.get("queries", []):
        url = ("https://devpost.com/api/hackathons?search="
               + urllib.parse.quote(term)
               + "&status[]=open&order_by=prize-amount")
        raw = fetch(url)
        if raw is None:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for h in data.get("hackathons", []):
            hid = h.get("id")
            key = f"devpost:{hid}"
            if key in seen:
                continue
            if h.get("invite_only"):
                continue
            if h.get("open_state") != "open":
                continue

            loc = (h.get("displayed_location") or {}).get("location", "")
            if cfg.get("online_only", True) and "online" not in loc.lower():
                continue

            if (h.get("prizes_counts") or {}).get("cash", 0) < 1:
                continue

            prize_raw = h.get("prize_amount") or ""
            prize_txt = re.sub(r"<[^>]+>", "", prize_raw)
            digits = re.sub(r"[^0-9]", "", prize_txt)
            prize = int(digits) if digits else 0
            if prize < min_prize:
                continue

            title = h.get("title", "")
            themes = " ".join(t.get("name", "") for t in h.get("themes", []))
            blob = f"{title} {themes}".lower()
            matched = next((k for k in kw if k in blob), None)
            if kw and not matched:
                continue

            seen[key] = int(time.time())
            hits.append({
                "source": f"devpost ({h.get('organization_name','')})",
                "title": title,
                "detail": (f"{prize_txt} · {(h.get('prizes_counts') or {}).get('cash',0)} cash prizes"
                           f" · {h.get('registrations_count',0)} registered"
                           f" · {h.get('time_left_to_submission','')}"
                           f" · {h.get('submission_period_dates','')}"),
                "url": h.get("url", ""),
                "matched": matched or term,
                "label": "HACKATHON",
            })
    return hits

def check_hackodds(config, seen):
    """HackOdds aggregator. Covers devpost, dorahacks, devfolio, hacklist,
    hackquest and openhack in one JSON feed."""
    cfg = config.get("hackodds", {})
    if not cfg.get("enabled"):
        return []

    min_prize = cfg.get("min_prize", 3000)
    max_reg = cfg.get("max_registrations", 4000)
    kw = [k.lower() for k in cfg.get("keywords", [])]
    block = re.compile(cfg.get("block", r"$^"), re.I)

    raw = fetch(cfg.get("url", ""))
    if raw is None:
        return []
    try:
        items = json.loads(raw).get("hackathons", [])
    except Exception:
        log("  ! hackodds: bad json")
        return []

    hits, kept = [], 0
    for h in items:
        hid = h.get("id") or h.get("sourceId")
        key = f"hackodds:{hid}"
        if key in seen:
            continue
        seen[key] = int(time.time())

        if h.get("source") in (cfg.get("block_sources") or []):
            continue
        if not h.get("online"):
            continue
        prize = h.get("prizeUsd") or 0
        if prize < min_prize:
            continue
        reg = h.get("registrations")
        if reg is not None and reg > max_reg:
            continue

        title = h.get("title") or ""
        themes = " ".join(h.get("themes") or [])
        blob = f"{title} {themes}".lower()
        if block.search(blob):
            continue
        matched = next((k for k in kw if k in blob), None)
        if kw and not matched:
            continue

        kept += 1
        ratio = f"1 in {int(reg/1):,}" if reg else "no reg count"
        hits.append({
            "source": f"hackodds ({h.get('source')})",
            "title": title[:110],
            "detail": (f"${prize:,} · {reg if reg is not None else '?'} registered"
                       f" · {h.get('organization') or ''}"
                       f" · {themes[:60]}"),
            "url": h.get("url") or "",
            "matched": matched or "online",
            "label": "HACKATHON",
        })
    log(f"  - hackodds: {len(items)} scanned, {kept} kept")
    return hits

def check_aijobs(config, seen):
    """artificialintelligencejobs.co RSS. Filters out roles gated on
    seniority or a degree, keeps the ones actually open to us."""
    cfg = config.get("aijobs", {})
    if not cfg.get("enabled"):
        return []

    block_title = re.compile(cfg.get("block_title", r"$^"), re.I)
    block_loc = re.compile(cfg.get("block_loc", r"$^"), re.I)
    want_title = re.compile(cfg.get("want_title", r".*"), re.I)
    want_loc = re.compile(cfg.get("want_loc", r".*"), re.I)

    hits = []
    for url in cfg.get("feeds", []):
        raw = fetch(url)
        if raw is None:
            continue

        items = re.findall(r"<item>(.*?)</item>", raw, re.S)
        if not items:
            log(f"  - aijobs {url.rsplit('/',1)[-1]}: no items parsed")
            continue

        kept = 0
        for it in items:
            def tag(t):
                m = re.search(rf"<{t}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{t}>", it, re.S)
                return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""

            title = tag("title")
            link = tag("link")
            desc = tag("description")
            if not title or not link:
                continue

            key = f"aijobs:{link}"
            if key in seen:
                continue
            seen[key] = int(time.time())

            blob = f"{title} {desc}"
            if block_title.search(title):
                continue
            if block_loc.search(blob):
                continue
            if not want_loc.search(blob):
                continue
            m = want_title.search(title)
            if not m:
                continue

            kept += 1
            hits.append({
                "source": "aijobs.co",
                "title": title[:120],
                "detail": re.sub(r"<[^>]+>", " ", desc)[:160],
                "url": link,
                "matched": m.group(0),
                "label": "JOB",
            })
        log(f"  - aijobs {url.rsplit('/',1)[-1]}: {len(items)} scanned, {kept} kept")
    return hits

def post_discord(webhook, content):
    payload = json.dumps({"content": content, "flags": 4}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        log(f"! discord post failed ({e})")
        return False


def format_hit(h):
    tag = h.get("label", "JOB")
    bits = [f"[{tag}] **{h['title']}**", h["source"]]
    if h["detail"]:
        bits.append(h["detail"])
    return f"{' — '.join(bits)}\nmatched `{h['matched']}`\n{h['url']}"



def maybe_heartbeat(config, webhook, counts, dry):
    """Post a weekly still-alive note so silence is never ambiguous."""
    days = int(config.get("heartbeat_days", 7))
    if days <= 0 or not webhook:
        return
    path = os.path.join(HERE, "heartbeat.txt")
    now = int(time.time())
    last = 0
    try:
        with open(path) as f:
            last = int(f.read().strip() or 0)
    except (OSError, ValueError):
        last = 0
    if now - last < days * 86400:
        return
    msg = (f"[STATUS] jobwatch alive - {counts} items checked this run, "
           f"nothing new to report. Next status in {days} days.")
    if dry:
        print("\n" + msg)
    elif post_discord(webhook, msg):
        try:
            with open(path, "w") as f:
                f.write(str(now))
        except OSError:
            pass


def rotate_logs(max_bytes=2_000_000):
    """Keep logs from growing forever on a small disk."""
    for name in ("jobwatch.log", "cron.log"):
        p = os.path.join(HERE, name)
        try:
            if os.path.exists(p) and os.path.getsize(p) > max_bytes:
                os.replace(p, p + ".old")
        except OSError:
            pass


def prune_seen(seen, days=180):
    cutoff = int(time.time()) - days * 86400
    return {k: v for k, v in seen.items() if v > cutoff}


def main():
    dry = "--dry-run" in sys.argv
    config = load_json_file(CONFIG_PATH, None)
    if config is None:
        log(f"! no config at {CONFIG_PATH}")
        return 1

    webhook = config.get("discord_webhook", "").strip()

    if "--test" in sys.argv:
        if not webhook:
            log("! no discord_webhook set")
            return 1
        ok = post_discord(webhook, "jobwatch test message - wiring works")
        log("test sent" if ok else "test FAILED")
        return 0 if ok else 1

    token = load_token()
    rotate_logs()
    log("run started" + ("" if token else " (no github token - search limited)"))

    seen = load_json_file(SEEN_PATH, {})
    first_run = len(seen) == 0
    narrow, broad = make_matchers(config)

    hits = []
    log(" named boards")
    hits += check_named_boards(config, narrow, seen)
    log(" broad boards")
    hits += check_broad_boards(config, broad, seen)
    log(" forums")
    hits += check_forums(config, seen)
    log(" github repos")
    hits += check_github_repos(config, narrow, seen)
    log(" github discussions")
    hits += check_github_discussions(config, narrow, seen, token)
    log(" discussion search (all github)")
    hits += check_discussion_search(config, seen, token, narrow)
    log(" project search")
    hits += check_project_search(config, seen, token)
    log(" github search")
    hits += check_github_search(config, seen, token)
    log(" hackathons")
    hits += check_hackathons(config, seen)
    log(" aijobs.co")
    hits += check_aijobs(config, seen)
    log(" hackodds")
    hits += check_hackodds(config, seen)

    seen = prune_seen(seen)
    save_json_file(SEEN_PATH, seen)

    if first_run:
        log(f"first run - {len(seen)} items marked as already seen")
        log(f"({len(hits)} would have matched; sending nothing this time)")
        if dry:
            for h in hits[:10]:
                print("\n" + format_hit(h))
        return 0

    cap = int(config.get("max_alerts_per_run", 8))
    if not hits:
        log("no new matches")
        maybe_heartbeat(config, webhook, len(seen), dry)
        rotate_logs()
        return 0

    total = len(hits)
    overflow = total - cap
    hits = hits[:cap]
    log(f"{total} new match(es), sending {len(hits)}")

    for h in hits:
        msg = format_hit(h)
        if dry or not webhook:
            print("\n" + msg)
        else:
            post_discord(webhook, msg)
            time.sleep(1)

    if overflow > 0:
        note = f"...and {overflow} more matches this run (capped)."
        if dry or not webhook:
            print("\n" + note)
        else:
            post_discord(webhook, note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
