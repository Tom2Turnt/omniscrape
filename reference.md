# omniscrape — escalation reference

Deep detail for the tiers the main `SKILL.md` only summarizes. Read this when a job
falls off the bottom of the bundled engine (Tiers 0–2) and needs Tier 3 or 4.

## Block signatures the engine watches for

The engine flags a fetch as "blocked" (and escalates) on any of:

- HTTP `401 / 403 / 407 / 429 / 503`
- Body markers: `just a moment`, `cf-chl`, `challenge-platform`, `attention required`,
  `verify you are human`, `perimeterx`, `distil`, `incapsula`, `imperva`, `px-captcha`,
  `unusual traffic`, `are you a robot`, `captcha-delivery`
- A `200` with a body under ~200 bytes (silent block / empty shell)

If you see a *new* wall pattern in the wild, add its marker to `BLOCK_MARKERS` in
`scripts/omniscrape.py` so the engine learns it.

## Tier 3 — real Chrome via CDP

When even the stealth tier is walled, the site is likely gating on a *trusted, warmed,
logged-in* browser profile. Two paths:

- **A CDP harness** (not bundled) — attaches over the Chrome DevTools Protocol to a Chrome the
  user is already running. It IS their real profile: real cookies, real history, real TLS
  fingerprint. Nothing looks automated because almost nothing is.
- **A visible stealth browser** (not bundled) — launches a visible stealth Chromium you can watch act in
  real time; anti-bot stealth is baked in. Good when you also need to log in by hand first.

Use Tier 3 for: heavily-fingerprinted SaaS dashboards, sites that A/B challenge every
datacenter IP, or anything behind a login the user legitimately holds.

## Tier 4 — licensed Apify actors (walled / social / personal)

Do **not** try to stealth-bypass login walls on social platforms or scrape personal
profiles with the raw engine — that path leads to account bans, IP blocks, and legal/ToS
exposure, and the data is usually behind auth the engine can't (and shouldn't) forge. Route
these to a licensed Apify Store actor instead:

| Target | Actor |
|---|---|
| Google Maps / local places | `compass/crawler-google-places` |
| Google Search results | `apify/google-search-scraper` |
| TikTok | `clockworks/tiktok-scraper` |
| Instagram | `apify/instagram-scraper` |
| Facebook pages/posts | `apify/facebook-pages-scraper` |
| LinkedIn (company/public) | licensed LinkedIn actor (respect their ToS + rate caps) |
| X / Twitter | `apidojo/tweet-scraper` |
| YouTube | `streamers/youtube-scraper` |
| Amazon products | `junglee/amazon-crawler` |

These are metered but compliant, warm-IP, and won't get *your* infrastructure blocklisted.

## Complete-stealth mode — making Tier 2 undetectable

"Undetectable" is a spectrum, not a switch — detection is an arms race. But the stealth tier
is configured for the strongest *public-data* posture Scrapling supports, and here's the
order the levers actually matter in:

### 1. IP reputation — THE dominant signal (fix this first)
No fingerprint trick beats a flagged **datacenter IP**. This box's IP is a datacenter IP;
Cloudflare Enterprise / DataDome / Kasada flag those on sight regardless of how perfect the
browser looks. The single highest-impact change is routing through **residential or mobile
proxies**:

```bash
# one proxy
scripts/omniscrape <url> --tier stealth --proxy "http://user:pass@gate.provider.com:7777"
# a rotating pool (one identity is chosen per run; rotation happens across runs)
export OMNISCRAPE_PROXIES="http://u:p@a:7777, http://u:p@b:7777, socks5://u:p@c:1080"
scripts/omniscrape <url> --tier stealth
```
**Without a proxy the engine prints `proxy: NONE` and warns you** — that's the honest state,
not a silent success.

#### Recommended provider (2026 research — privacy + performance balanced)
A multi-agent research pass (privacy claims adversarially fact-checked) ranked the field for
*this* project — a privacy-conscious solo operator scraping public data:

| Rank | Provider | Verdict |
|---|---|---|
| **1 — top pick** | **Decodo** (formerly Smartproxy) | Best balance. Fastest residential in Proxyway's 2025 test (~99.86% success), **EU/Lithuania** (non-Five-Eyes, GDPR), **no upfront KYC**, **crypto via CoinGate**, clean IP sourcing. Plugs into omniscrape as `http://user-<USER>-country-us:<PASS>@gate.decodo.com:7000` (HTTP + SOCKS5, same host). Use the plain rotating gateway in `OMNISCRAPE_PROXIES`; add `-session-<id>` to the username only for jobs that need a sticky IP. |
| 2 | Bright Data | Performance king for the hardest anti-bot, but privacy-hostile: heavy business KYC (rejects Gmail signups), explicit law-enforcement cooperation, a license clause reusing your scraped data, Hola/Luminati sourcing legacy. Use only if a target defeats Decodo. |
| 3 | Oxylabs / Webshare | Elite/mid performance but mandatory KYC (Oxylabs), no crypto, long retention; US/Tesonet overhang. |
| ⛔ AVOID | **NetNut** | Serious unresolved questions have been raised about the sourcing of its residential pool. Not recommended; check current reporting yourself before using it or any white-label reseller. |

Watch-outs on the top pick: Decodo is **not no-logs** (it logs auth IP, timestamps, bandwidth
and the target *domain*, tied to your account, ~billing period + 90 days) — favorable
jurisdiction is not a shield, so pay with crypto and avoid its screened sensitive categories
(streaming/email/banking) to stay under its conditional biometric KYC. Its hardest-target
"Site Unblocker" add-on is a separate API, **not** a plain `user:pass` proxy, so it does not
drop into omniscrape the same way. Decodo/Oxylabs/Webshare all trace to the same Lithuanian
(Tesonet) ownership, so spreading across them is less diversification than it looks. Don't
confuse Decodo/Smartproxy(.com) with the unrelated `smartproxy.ORG`, which is a different
operator entirely — check the domain before you buy.

### 2. Geo/locale consistency — don't contradict your own IP
A US residential IP with a Berlin timezone and `de-DE` locale screams "bot." Match them to
the proxy's country:
```bash
scripts/omniscrape <url> --tier stealth --proxy <us-proxy> --locale en-US --timezone America/New_York
```

### 3. Browser fingerprint — on by default at Tier 2
Always-on in the stealth tier: `block_webrtc` (stops WebRTC leaking your real IP behind the
proxy — critical), `hide_canvas` (canvas-noise), `allow_webgl` (kept ON; WAFs flag its
absence), `dns_over_https` (no DNS leak), `google_search` referer (arrive like a human from
search). The underlying engine is a patched Firefox (Camoufox) that already spoofs
`navigator.webdriver`, plugins, and screen/hardware properties.

### 4. Real installed Chrome — the strongest fingerprint
`--real-chrome` launches your actual Chrome instead of the bundled browser — real TLS/JA3,
real build. Combine with `--session-dir ./sess` to persist and *warm* a session (cookies +
localStorage carry over, so a deep page isn't hit cold):
```bash
scripts/omniscrape <url> --tier stealth --real-chrome --session-dir ~/.omniscrape/sess1
```

### 5. Behavioral pacing — don't look robotic
The engine already jitters delays and rotates user-agents. For a crawl, keep `--delay` humane
(1–4s), randomize order, and don't hit hundreds of pages/min from one identity — that pattern
is detectable no matter how clean each request is, and it's what burns proxy IPs. For big
jobs, keep `--delay` humane and cap concurrency so you don't burn identities.

### What still gives you away (be realistic)
- **Any login.** A warmed real-Chrome session helps, but scraping *authenticated* content is a
  ban/legal issue, not a stealth problem → Tier 4 licensed actor.
- **CAPTCHA walls per request** (hCaptcha/reCAPTCHA v2 image grids) need a solver service; the
  stealth tier avoids *triggering* them but doesn't *solve* them.
- **Kasada / DataDome at enterprise tier** can still win against a lone residential IP — rotate
  a real pool and slow down, or fall back to Tier 4.

## "Maximum bypass" posture — what it does and doesn't do

Per the build decision, the engine runs the most aggressive *public-data* posture the
mainstream tools support:

- Rotating desktop user-agents + jittered timing (Tier 0)
- Full headless browser render for JS/SPA content (Tier 1)
- Scrapling `StealthyFetcher` with `solve_cloudflare=True`, fingerprint/canvas spoofing,
  and human-like interaction to defeat Cloudflare Turnstile, PerimeterX, and similar (Tier 2)

It deliberately does **not**: forge or steal authentication, stuff credentials, or hammer a
single host at DoS concurrency. Those aren't "more scraping" — they're a different (and
harmful) activity, and they get your IP ranges and accounts torched. Walled data → Tier 4.
