---
name: omniscrape
description: >-
  The all-in-one, auto-escalating web scraper. Use whenever the user wants to scrape,
  extract, pull, harvest, or "get the data from" any page or API —
  e.g. "scrape acme.com", "get all products off this listing", "pull the prices",
  "what's on this page", "this site keeps blocking me". Scrapes ONE url per call;
  there is no crawling, link-following or pagination — hand it urls in a loop.
  Starts with the cheapest fetch and automatically climbs a ladder (static HTTP →
  JS-rendered → stealth/anti-bot → real Chrome → licensed actors) until it gets
  clean, un-blocked data. Reports which tier won and returns a clean bundle
  (raw html + summary.md, plus data.csv when --extract is used).
version: 1.0.0
metadata:
  type: router-skill
  surface: claude-code
  # Tiers 3-4 hand off to separate skills that are NOT bundled here. If you don't
  # have them, Tiers 0-2 still work standalone and solve the large majority of pages.
  # Tier 3 wants any CDP harness that can drive a signed-in Chrome.
  # Tier 4 wants an Apify account. Neither is bundled.
prerequisites:
  commands: [python3]
  # Tiers 0-2 need parsel+lxml. A uv venv at the skill root supplies them;
  # always invoke via scripts/omniscrape (the wrapper), not omniscrape.py directly.
---

# omniscrape — one scraper to rule them all

You are the dispatcher for a self-escalating scraper. The user says "get me X from Y";
you get it, climbing tiers automatically until the data comes back clean. **Default to
ACTING, not asking.** Only ask a question if the *target* or *what to extract* is truly
ambiguous — never ask about method; you decide the method.

The whole idea: **start cheap, escalate on block, report the tier that won.** Most pages
are solved at Tier 0-2 by the bundled engine. Tiers 3-5 are escalation paths you invoke
by hand when the engine says it's blocked.

## The escalation ladder

| Tier | Name | Tool | Beats |
|---|---|---|---|
| 0 | static | `omniscrape.py` (curl/urllib) | static HTML, JSON APIs, sitemaps |
| 1 | dynamic | `omniscrape.py` (Scrapling Dynamic) | SPAs, JS-rendered content, lazy loads |
| 2 | stealth | `omniscrape.py` (Scrapling Stealthy) | Cloudflare Turnstile, PerimeterX, fingerprinting |
| 3 | real-chrome | `browser-harness` (CDP) | sites that only trust a real logged-in profile |
| 4 | licensed | Apify Store actors | login-walled social & personal data (compliant path) |

### Tiers 0–2 — the bundled engine (your default first move)

Run the engine. It does Tiers 0→2 in one shot, escalating internally:

```bash
~/.claude/skills/omniscrape/scripts/omniscrape <url> \
  --extract "<css selector>" --out /tmp/scrape --json
```

Key flags (full list: `scripts/omniscrape -h`):
- `--extract "CSS"` — pull matching text as rows (`--attr href` for an attribute).
- `--max-tier stealth|dynamic|static` — cap how far it climbs (default: stealth).
- `--tier stealth` — force a specific tier (skip the cheap ones for a known-hard site).
- `--wait-selector "CSS"` — wait for content to appear (dynamic/stealth tiers).
- `--out DIR` — write `page.html` and `summary.md`, plus `data.csv` when `--extract` is used.
- `--headful` — watch the browser (debugging a stubborn page).
- `--json` — machine-readable result; read `tier_used`, `ok`, `attempts`, `extracted`.

**Complete-stealth flags** (Tier 2) — see `reference.md` for the full playbook incl. the
recommended provider (**Decodo** is the 2026 top pick; **NetNut is seized — never use it**):
- `--proxy URL` / `OMNISCRAPE_PROXIES` env — route through residential/mobile proxies. This is
  the #1 lever; a datacenter IP is the top detection signal and no fingerprint trick beats it.
  One identity is chosen per run (rotation happens across runs) so geo/locale stays consistent.
- `--locale en-US` / `--timezone America/New_York` — match your proxy's country (mismatches leak).
- `--real-chrome` — drive the actually-installed Chrome (real TLS/JA3 fingerprint).
- `--session-dir DIR` — persist + warm cookies/localStorage across runs (locked; one run at a time).
- Always on at the stealth tier: WebRTC-leak block, canvas-noise, DNS-over-HTTPS, and a
  patched-Firefox engine that spoofs `navigator.webdriver` and hardware fingerprints.
- `--lite` — **ON by default** (cost saver). On the JS-render (dynamic) tier it skips downloading
  images/media/fonts (~60–85% of page bytes) while keeping CSS/JS/XHR/websockets, so data still
  renders — **~3–5× cheaper** on that tier. It is never applied on the stealth tier (resource-blocking
  breaks the Cloudflare Turnstile solve). Disable per-scrape with `--no-lite`.

## Deciding --lite (do this before every scrape)

Lite stays ON unless the target/task needs the actual image/media/font *bytes*. **Look at what the
task is asking for and choose:**

- **KEEP lite on (default) — the common case:** extracting text, prices, titles, links, tables,
  structured data, JSON, or **image URLs** (an `<img src>`/`data-src` stays in the DOM even when the
  image bytes aren't downloaded — JS still runs and populates it). This is the vast majority of scrapes.
- **Add `--no-lite` — only when the deliverable IS the media or the render:**
  - downloading the actual images / videos / audio / files (galleries, product photos, media archives),
  - screenshots or any "show me / render the page visually" task,
  - OCR or reading text baked into images / drawn to `<canvas>`,
  - a page whose *text content* genuinely only appears after images/fonts load (rare — icon-font UIs,
    image-triggered lazy loads),
  - debugging why a page looks wrong (you want to see it as a human does).

The choice only affects **dynamic-tier** (JS-rendered) pages — it's a no-op on static (no browser)
and stealth (always loads everything). When unsure, keep lite on; if a scrape comes back missing
data you expected, retry that one with `--no-lite`.

**Tier control:** `--tier` sets the *starting* tier and it escalates up to `--max-tier`. For a
single fixed tier, set them equal (e.g. `--tier stealth --max-tier stealth`). Default `--max-tier`
is `stealth`, so `--tier stealth` alone already means "stealth only".

**Safety (on by default):** the engine refuses non-http(s) URLs and any host resolving to a
private/loopback/link-local address or the cloud-metadata IP (SSRF/local-file guard). Pass
`--allow-private` only when you deliberately need to scrape an internal host you own.

If the result is `ok: true`, you're done — hand back what it extracted. If `ok: false`,
the `attempts` array tells you *why* each tier failed; escalate to Tier 3+.

**First run:** the stealth/dynamic tiers need Scrapling. If the engine returns
`error: scrapling-missing`, the skill was never installed — run the installer once from the
skill root:
```bash
./install.sh
```
That builds an isolated `.venv` beside the skill and fetches the stealth browser binaries.
The wrapper at `scripts/omniscrape` finds that venv automatically, so nothing is installed
into your system Python. (`scrapling install` is a CLI entry point; `python3 -m scrapling`
does NOT work.)

### Tier 3 — real Chrome (the engine got walled)

When Tiers 0–2 are all blocked and the site needs a *trusted, logged-in* browser, hand off
to a real browser over the Chrome DevTools Protocol, driving a Chrome that is already
signed in. **No Tier 3 tool ships with this skill** — supply your own CDP harness, or stop
at Tier 2 and tell the user the site needs a trusted logged-in session.

### Tier 4 — licensed actors (walled / social / personal data)

Login-walled social platforms and personal-profile data must go through a **licensed Apify
Store actor** (paid, separate account), not a stealth bypass — that's the line between
aggressive scraping and account bans / unauthorized access. **Not bundled.** See
`reference.md` for the actor map (Google Maps, TikTok, Instagram, LinkedIn, etc.).

## Routing logic (how you pick the entry tier)

1. **Plain page / API / "what's on this page"** → run the engine at `--tier auto`. Done in one call.
2. **Known-hard site** (Cloudflare, "keeps blocking me") → run the engine at `--tier stealth`.
3. **Social profile / login-walled / personal data** → Tier 4 licensed actor. Say so plainly.
4. **Engine returns `ok:false`** → read `attempts`, escalate one tier up (2 → 3 → 4).

## Output contract

Always return: **what you got** (the rows / the answer), **which tier won**
(`tier_used`), and **where the bundle is** (`out_dir`) if `--out` was used. If everything was
blocked, say which tiers you tried and what the next escalation is — never fail silently.

## Operating notes (keeps your IPs alive)

- Default `--delay 1.0` + jittered UA rotation is polite on purpose. Don't crank concurrency
  into DoS territory on a single host — that's how IP ranges get burned and how a scrape
  becomes an attack. The engine is one request at a time by design; if you loop it over
  many urls, keep `--delay` humane and randomize order.
- The engine rotates user-agents on the static tier; the browser tiers generate their own — that's the
  "maximum bypass" posture. It does **not** defeat authentication; walled data → Tier 4.
