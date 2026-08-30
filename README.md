# omniscrape

A Claude Code skill that scrapes a page and **escalates on its own** until the data
comes back clean. You say "get me the headlines off this page"; it starts with a plain
HTTP fetch, and if the page fights back it climbs to a JS-rendering browser, then to a
stealth browser, and tells you which tier won.

Free. Runs on your machine. No API key, no account, no card.

## What it actually does

| Tier | How it fetches | Beats |
|---|---|---|
| 0 `static` | plain HTTP | static HTML, JSON APIs, sitemaps |
| 1 `dynamic` | headless browser | SPAs, JS-rendered content, lazy loading |
| 2 `stealth` | stealth browser | Cloudflare Turnstile, PerimeterX, fingerprinting |

By default it starts at Tier 0, because most pages are a Tier 0 page and burning a
browser on them is slow and rude. It only climbs when it has to.

Verified on 2026-08-29, no proxy:

- Hacker News front page → 30 headlines, **1.6s**, Tier 0
- `quotes.toscrape.com/js/` → Tier 0 returns **0 rows**, escalates itself, gets all 10
- `scrapingcourse.com/cloudflare-challenge` → **403, 403, then 200** in 19.7s at Tier 2.
  The page it pulls back reads *"You bypassed the Cloudflare challenge! :D"*

## Install

```bash
git clone https://github.com/Tom2Turnt/omniscrape ~/.claude/skills/omniscrape
cd ~/.claude/skills/omniscrape
./install.sh
```

**macOS and Linux. Windows via WSL** — the scripts assume `.venv/bin`.

`install.sh` builds an isolated `.venv` next to the skill and downloads the stealth
browser binaries. It does not touch your system Python. Needs Python 3.10+; uses `uv`
if you have it, falls back to `venv` + `pip` if you don't.

The browser download is a few hundred MB and only happens once. If it fails, Tier 0 (plain HTTP)
still works; both browser tiers need that binary.

Smoke test:

```bash
./scripts/omniscrape https://example.com --extract h1
```

Then just ask Claude to scrape something. The skill triggers on "scrape", "crawl",
"pull the data", "this site keeps blocking me", and similar.

## Direct use, without Claude

```bash
# one page, one selector
./scripts/omniscrape https://news.ycombinator.com --extract ".titleline > a"

# force a tier instead of letting it climb
./scripts/omniscrape https://example.com --tier stealth

# save a bundle: page.html + summary.md, plus data.csv because --extract is used
./scripts/omniscrape https://news.ycombinator.com --extract '.titleline > a' --out ./bundle
```

Always call `scripts/omniscrape`, not `omniscrape.py` directly — the wrapper is what
points Python at the venv that has the dependencies.

## What it will not do

- **It does not defeat authentication.** Login-walled and personal-profile data is out
  of scope by design. That's the line between aggressive scraping and account bans.
- Tiers 3 and 4 in `SKILL.md` (real logged-in Chrome over CDP, licensed Apify actors)
  are **handoff paths, not bundled code**. If you don't have those, the skill stops at
  Tier 2 and says so.
- `/button-click` and `/infinite-scrolling` style pages report success with partial
  data — the engine has no click or scroll action, so it only sees the first batch the
  server already sent. Those are false wins; don't trust them.

## Be decent about it

Default delay is 1.0s with jittered user-agent rotation, on purpose. It makes one
request at a time; if you loop it over many urls, don't crank the pace on a single host — that's how a scrape becomes an attack and how IP ranges
get burned. Cache with `--out` so re-extraction doesn't re-hit the site. Respect the
terms of the sites you point it at; that part is on you.

## Files

| File | What it is |
|---|---|
| `SKILL.md` | What Claude reads. The escalation ladder and routing logic. |
| `reference.md` | Tier 3–4 handoff notes and the Apify actor map. |
| `scripts/omniscrape` | Wrapper. Points at the venv. **Call this one.** |
| `scripts/omniscrape.py` | The engine. |
| `install.sh` | Builds the venv, fetches browsers. |

## License

MIT — see [LICENSE](LICENSE). Use it, change it, ship it.
