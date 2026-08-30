#!/usr/bin/env python3
"""
omniscrape — auto-escalating web scraper engine.

Climbs a ladder of fetch strategies until it gets clean, un-blocked data:

  Tier 0  static   curl/urllib          fast static HTML & JSON APIs
  Tier 1  dynamic  Scrapling Dynamic    JS-rendered SPAs (real headless browser)
  Tier 2  stealth  Scrapling Stealthy   Cloudflare / anti-bot, fingerprint spoof

Tiers 3-4 (real Chrome via CDP, licensed Apify actors) live outside
this script — see the skill's SKILL.md / reference.md. This engine covers the
local, no-account tiers and is the workhorse of the omniscrape skill.

Usage:
  omniscrape.py <url> [options]

Options:
  --tier auto|static|dynamic|stealth   start tier, then escalate up to --max-tier
                                         (default: auto = start at static)
  --max-tier static|dynamic|stealth    highest tier to escalate to (default: stealth).
                                         For a SINGLE tier, set --tier and --max-tier equal.
  --extract CSS                        CSS selector; prints matching text/attrs as rows
  --attr NAME                          with --extract, pull this attribute instead of text
  --out DIR                            save html/data/summary into DIR
  --wait-selector CSS                  (dynamic/stealth) wait for this selector before capture
  --timeout SECONDS                    per-tier network timeout (default: 30)
  --delay SECONDS                      polite delay before the request (default: 1.0)
  --retries N                          retries per tier on transient failure (default: 2)
  --headful                            (dynamic/stealth) show the browser window
  --proxy URL                          proxy (http|https|socks5://user:pass@host:port)
  --locale / --timezone                match your proxy's country/geo
  --session-dir DIR                    persist+warm cookies across runs (one run at a time)
  --real-chrome                        drive the actually-installed Chrome
  --allow-private                      permit private/loopback/metadata hosts (OFF by default)
  --json                               emit a machine-readable JSON result to stdout
  --quiet                              suppress the human progress log on stderr

Exit code 0 = got un-blocked content; 2 = bad request/blocked URL; 3 = all tiers blocked.
"""
import argparse
import ipaddress
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from urllib.parse import urlparse, urljoin

try:
    import fcntl  # POSIX-only; used to lock --session-dir against concurrent runs
except Exception:
    fcntl = None

TIERS = ["static", "dynamic", "stealth"]

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# Three marker classes, by how safe each string is to match:
# SPECIFIC — distinctive vendor tokens that essentially never occur in prose, so they
#   are scanned across the head of ANY body (real Cloudflare challenge pages run
#   5-15KB of inline challenge JS, past an "interstitial-sized" cutoff). These are
#   deliberately not plain English words (e.g. "distilnetworks", not "distil", which
#   is a substring of distillery/distillation).
# TITLE — human-readable challenge phrases that DO appear in ordinary prose ("just a
#   moment", "attention required"); trusted only inside the page's <title>, where a
#   challenge page always puts them but an article almost never does.
# GENERIC — weak phrases ("access denied", "are you a robot") trusted only on small,
#   interstitial-sized bodies.
SPECIFIC_MARKERS = [
    "cf-chl", "cf_chl", "challenge-platform",
    "enable javascript and cookies to continue",
    "px-captcha", "perimeterx", "distilnetworks", "incapsula", "imperva",
    "captcha-delivery",
]
TITLE_MARKERS = [
    "just a moment",              # Cloudflare interstitial <title>
    "attention required",         # Cloudflare 1020
    "verifying you are human",
]
GENERIC_MARKERS = [
    "verify you are human",
    "access denied", "request unsuccessful",
    "unusual traffic", "are you a robot",
]
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_HEAD_SCAN_LIMIT = 20000   # specific markers: scan the first 20KB of any body
_MARKER_SCAN_LIMIT = 4000  # generic markers: only interstitial-sized bodies

BLOCKED_HOSTNAMES = {"metadata.google.internal", "metadata", "metadata.goog"}


def log(msg, quiet=False):
    if not quiet:
        print(msg, file=sys.stderr, flush=True)


# NOTE: intentionally NOT a subclass of ValueError — otherwise the `except ValueError`
# guards below (which catch "this string isn't an IP") would swallow a real block.
class BlockedURLError(Exception):
    pass


def _parse_ip(s):
    """Return an ip_address or None if the string isn't a literal IP."""
    try:
        return ipaddress.ip_address(s)
    except ValueError:
        return None


def validate_url(url, allow_private=False):
    """Reject non-http(s) schemes and private/loopback/link-local/metadata hosts.

    Prevents SSRF and local-file/ftp reads (file://, ftp://, gopher://, and the
    cloud metadata endpoint 169.254.169.254). Returns the parsed host, or raises
    BlockedURLError. Checks every address the host resolves to AT VALIDATION TIME
    (IPv4 + IPv6), plus browser-tier navigations are re-validated via the page.route
    guard.

    Residual limitation — DNS rebinding: validation and the actual connection each
    resolve DNS independently, so a short-TTL attacker domain that returns a public
    IP here and a private one at connect time is not caught (pinning the resolved IP
    through the connection would break TLS SNI and is out of scope for this engine).
    Literal internal IPs, internal hostnames with stable DNS, and redirect-to-internal
    ARE blocked. For hostile targets, run behind an egress firewall that blocks RFC-1918
    and 169.254.0.0/16.
    """
    p = urlparse(url)
    if p.scheme.lower() not in ("http", "https"):
        raise BlockedURLError(f"scheme '{p.scheme}' not allowed (only http/https)")
    host = p.hostname
    if not host:
        raise BlockedURLError("no host in URL")
    if allow_private:
        return host
    # Strip a trailing FQDN dot so "metadata.google.internal." can't dodge the name
    # set (DNS treats "host" and "host." as the same name).
    if host.rstrip(".").lower() in BLOCKED_HOSTNAMES:
        raise BlockedURLError(f"blocked metadata host: {host}")
    # Accessing p.port parses the port and raises ValueError on a bad one (e.g. :99999);
    # convert that to a clean refusal so run() returns exit-2 instead of a traceback.
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        raise BlockedURLError("invalid port in URL")
    literal = _parse_ip(host)
    if literal is not None:
        _reject_if_internal(literal, host)   # raises BlockedURLError (not ValueError)
        return host
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedURLError(f"cannot resolve host {host}: {e}")
    for info in infos:
        parsed = _parse_ip(info[4][0])
        if parsed is not None:
            _reject_if_internal(parsed, host)  # raises BlockedURLError, propagates
    return host


def _reject_if_internal(ip, host):
    # Normalise IPv4-mapped IPv6 (::ffff:169.254.169.254) to its v4 form.
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        raise BlockedURLError(f"host {host} resolves to non-public address {ip}")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target so a 3xx can't bounce us to file:// or
    an internal IP (SSRF via redirect)."""
    def __init__(self, allow_private):
        self._allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            validate_url(newurl, self._allow_private)
        except BlockedURLError as e:
            raise urllib.error.HTTPError(newurl, code, f"blocked redirect: {e}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ---------------------------------------------------------------- block detection
def looks_blocked(status, html):
    """Heuristic: did we get a wall / failure instead of the real page?"""
    if status == 0:
        return "fetch-failed"
    # A 3xx only reaches here from the no-redirect curl fallback (the urllib path
    # follows redirects to a final 2xx). An unfollowed redirect isn't content → escalate.
    if 300 <= status < 400:
        return f"redirect-{status}"
    # Auth/rate-limit walls and ALL server/gateway errors (500-599) mean no content —
    # a 502/504 error page must not be handed back as a clean success.
    if status in (401, 403, 407, 408, 429) or 500 <= status < 600:
        return f"http {status}"
    body = (html or "")
    low = body.lower()
    head = low[:_HEAD_SCAN_LIMIT]
    for m in SPECIFIC_MARKERS:
        if m in head:
            return f"marker:{m}"
    # Human-readable challenge phrases: trust them only inside the <title>, where a
    # challenge page always puts them but ordinary prose never does.
    tm = _TITLE_RE.search(head)
    title = (tm.group(1).lower() if tm else "")
    for m in TITLE_MARKERS:
        if m in title:
            return f"marker:title:{m}"
    if len(body) < _MARKER_SCAN_LIMIT:
        for m in GENERIC_MARKERS:
            if m in low:
                return f"marker:{m}"
    # Tiny 200 body → likely a silent block ONLY if it's empty or an HTML/JS shell.
    # A short plain-text or JSON API payload (an IP echo, "OK", {"x":1}) is real content.
    stripped = body.strip()
    if status == 200 and len(stripped) < 200:
        if stripped == "" or "<html" in low or "<script" in low or "<body" in low:
            return "empty-body"
    return None


# ---------------------------------------------------------------- Tier 0: static
def fetch_static(url, timeout, ua, proxy=None, allow_private=False):
    handlers = [_SafeRedirectHandler(allow_private)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))  # ignore env proxies unless asked
    # Explicit http/https handlers only — no FileHandler/FTPHandler in this opener.
    # HTTPErrorProcessor drives the redirect/error chain (so 3xx are followed via
    # _SafeRedirectHandler); HTTPDefaultErrorHandler surfaces 4xx/5xx as HTTPError.
    handlers += [urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(),
                 urllib.request.HTTPErrorProcessor(), urllib.request.HTTPDefaultErrorHandler()]
    opener = urllib.request.OpenerDirector()
    for h in handlers:
        opener.add_handler(h)
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
        "Referer": origin,
        "Upgrade-Insecure-Requests": "1",
    })
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read()
            enc = r.headers.get_content_charset() or "utf-8"
            return getattr(r, "status", 200) or 200, raw.decode(enc, errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        # Fall back to curl for TLS quirks. Constrain protocols so file://ftp:// and
        # protocol-downgrade redirects are impossible; pass proxy creds via env, not argv.
        try:
            # No -L / no redirect following in the fallback: --proto-redir only
            # constrains the scheme, not the destination IP, so an origin-controlled
            # 3xx could bounce curl to an internal/metadata address. The urllib path
            # owns redirects (each hop re-validated); a 3xx here escalates instead.
            # curl treats --max-time 0 as UNLIMITED, so a sub-second --timeout must not
            # round down to 0; floor it at 1s (the outer subprocess timeout still caps it).
            cmd = ["curl", "-sS", "--max-time", str(max(int(timeout), 1)), "-A", ua,
                   "-e", "-", "--proto", "=http,https", "--max-redirs", "0",
                   "-w", "\n__HTTP_STATUS__%{http_code}", "--", url]
            env = dict(os.environ)
            if proxy:
                env["ALL_PROXY"] = proxy
                env["HTTPS_PROXY"] = proxy
                env["HTTP_PROXY"] = proxy
            out = subprocess.run(cmd, capture_output=True, timeout=timeout + 5, env=env)
            text = out.stdout.decode("utf-8", errors="replace")
            body, _, code = text.rpartition("__HTTP_STATUS__")
            return (int(code) if code.strip().isdigit() else 0), body
        except Exception as e2:
            return 0, f"<!-- static fetch failed: {e} / {e2} -->"


# ------------------------------------------- Tiers 1/2: Scrapling (headless / stealth)
def _scrapling_available():
    try:
        import scrapling  # noqa: F401
        return True
    except Exception:
        return False


_HEAVY_RESOURCES = {"image", "media", "font"}


def _make_page_setup(lite, allow_private):
    """Build a Playwright page.route handler for the browser tiers.

    Always enforces the SSRF guard on *document/iframe navigations* — the initial
    validate_url() only covers the first URL, but the browser then follows 3xx
    redirects, <meta refresh>, and JS location changes with no re-check. Without
    this, a target could 302 the navigation to http://169.254.169.254/ or an
    internal host and the browser would fetch it. We re-validate each top-level
    navigation and abort internal ones.

    Only document/iframe requests are validated (a handful per page) so the
    synchronous DNS lookup in validate_url doesn't stall the hundreds of subresource
    requests. When `lite` is set, also abort heavy resources (images/media/fonts).
    Returns None if neither guard nor lite is needed (no interception at all)."""
    def _page_setup(page):
        def _route(route):
            try:
                req = route.request
                if req.resource_type in ("document", "iframe"):
                    try:
                        validate_url(req.url, allow_private)
                    except BlockedURLError:
                        route.abort()
                        return
                if lite and req.resource_type in _HEAVY_RESOURCES:
                    route.abort()
                    return
                route.continue_()
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass
        try:
            page.route("**/*", _route)
        except Exception:
            pass  # if routing isn't available, fall back to fetching everything
    return _page_setup


def fetch_scrapling(url, mode, timeout, wait_selector, headful, stealth_cfg=None):
    """mode: 'dynamic' (JS render) or 'stealth' (anti-bot bypass).

    stealth_cfg (dict): proxy, locale, timezone, session_dir, real_chrome.
    Returns (status, html, err) — err is (code, hint) or None.

    Wall-clock is bounded by Scrapling's own `timeout` (passed below). For a hard
    external ceiling on a pathological hang, wrap the CLI in `timeout <n> omniscrape.py …`
    — we deliberately do NOT use SIGALRM here, as raising into the browser's async
    internals corrupts the connection rather than returning cleanly.
    """
    cfg = stealth_cfg or {}
    if not _scrapling_available():
        return None, None, ("scrapling-missing",
                            'python3 -m pip install --break-system-packages "scrapling[fetchers]" && scrapling install')

    # Guard against a shared session dir corrupting a concurrent Chrome profile.
    lock_fh = None
    session_dir = cfg.get("session_dir")
    if session_dir and fcntl is not None:
        try:
            os.makedirs(session_dir, exist_ok=True)
            lock_fh = open(os.path.join(session_dir, ".omniscrape.lock"), "w")
        except OSError as e:
            # A create/permission failure here is NOT a lock contention — report it as such.
            if lock_fh:
                lock_fh.close()
            return None, None, ("session-dir-error", f"cannot use --session-dir {session_dir}: {e}")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_fh.close()
            return None, None, ("session-locked",
                                f"--session-dir {session_dir} is in use by another run")

    try:
        if mode == "stealth":
            from scrapling.fetchers import StealthyFetcher
            kw = dict(
                headless=not headful, network_idle=True, timeout=timeout * 1000,
                solve_cloudflare=True, block_webrtc=True, hide_canvas=True,
                allow_webgl=True, dns_over_https=True, google_search=True,
                disable_resources=False,
            )
            for k, v in (("proxy", cfg.get("proxy")), ("locale", cfg.get("locale")),
                         ("timezone_id", cfg.get("timezone")),
                         ("user_data_dir", cfg.get("session_dir")),
                         ("wait_selector", wait_selector)):
                if v:
                    kw[k] = v
            if cfg.get("real_chrome"):
                kw["real_chrome"] = True
            # NOTE: --lite (resource-blocking) is deliberately NOT applied on the stealth
            # tier — empirically it disrupts Cloudflare's Turnstile solve. But we still
            # install the SSRF navigation guard (lite=False): it only aborts internal-host
            # document navigations, blocks no resources, so the bypass is unaffected.
            kw["page_setup"] = _make_page_setup(lite=False, allow_private=cfg.get("allow_private"))
            page = StealthyFetcher.fetch(url, **kw)
        else:
            from scrapling.fetchers import DynamicFetcher
            kw = dict(headless=not headful, network_idle=True, timeout=timeout * 1000,
                      google_search=True)
            for k, v in (("proxy", cfg.get("proxy")), ("locale", cfg.get("locale")),
                         ("wait_selector", wait_selector)):
                if v:
                    kw[k] = v
            kw["page_setup"] = _make_page_setup(lite=cfg.get("lite"),
                                                allow_private=cfg.get("allow_private"))
            page = DynamicFetcher.fetch(url, **kw)
        status = getattr(page, "status", 200) or 200
        html = getattr(page, "html_content", None) or str(page)
        return status, html, None
    except Exception as e:
        return None, None, ("scrapling-error", str(e))
    finally:
        if lock_fh:
            lock_fh.close()


# ---------------------------------------------------------------- extraction
def _selector(html):
    from parsel import Selector  # raises ImportError if parsel is absent
    return Selector(text=html or "")


def extract(sel, selector, attr, quiet=False):
    """Extract rows from an already-built parsel Selector. Returns a list, or None
    if no --extract selector was given. Never fabricates data rows."""
    if not selector:
        return None
    try:
        if attr:
            return [x for x in sel.css(f"{selector}::attr({attr})").getall() if x.strip()]
        return [t.strip() for t in sel.css(f"{selector} *::text, {selector}::text").getall() if t.strip()]
    except Exception as e:
        log(f"  ! extract error for '{selector}': {e}", quiet)
        return []


def collect_links(sel, base):
    try:
        hrefs = sel.css("a::attr(href)").getall()
        seen, out = set(), []
        for h in hrefs:
            u = urljoin(base, h.strip())
            if u.startswith("http") and u not in seen:
                seen.add(u)
                out.append(u)
        return out
    except Exception:
        return []


def _mask_proxy(proxy):
    try:
        p = urlparse(proxy)
        return f"{p.hostname}:{p.port}" if p.hostname else "set"
    except Exception:
        return "set"


def run(args):
    quiet = args.quiet
    # Validate the target ONCE up front — blocks SSRF/file:// before any fetch.
    try:
        validate_url(args.url, args.allow_private)
    except BlockedURLError as e:
        msg = {"url": args.url, "ok": False, "error": f"blocked-url: {e}"}
        print(json.dumps(msg, indent=2) if args.json else f"REFUSED: {e}",
              file=sys.stdout if args.json else sys.stderr)
        return 2

    # Tier range: start at --tier (or static for auto), escalate up to --max-tier.
    start_i = 0 if args.tier == "auto" else TIERS.index(args.tier)
    max_i = max(TIERS.index(args.max_tier), start_i)
    order = TIERS[start_i:max_i + 1]

    # Proxy chosen ONCE per run so locale/timezone/session stay consistent for this
    # identity. Rotation happens across separate invocations, not mid-scrape.
    proxies = []
    if args.proxy:
        proxies = [args.proxy]
    elif os.environ.get("OMNISCRAPE_PROXIES"):
        raw = os.environ["OMNISCRAPE_PROXIES"].replace("\n", ",")
        proxies = [p.strip() for p in raw.split(",") if p.strip()]
    proxy = random.choice(proxies) if proxies else None

    stealth_cfg = {
        "proxy": proxy, "locale": args.locale, "timezone": args.timezone,
        "session_dir": args.session_dir, "real_chrome": args.real_chrome,
        "lite": args.lite, "allow_private": args.allow_private,
    }
    if proxy:
        log(f"proxy: {_mask_proxy(proxy)} ({len(proxies)} in pool)", quiet)
    else:
        log("proxy: NONE — using this host's IP (datacenter IP = top detection risk)", quiet)
    if args.lite:
        log("lite: ON (default) — image/media/font blocking on the dynamic tier; --no-lite to disable", quiet)
    else:
        log("lite: OFF — loading all resources (images/media/fonts) on browser tiers", quiet)

    attempts = []
    result = None      # {tier, status, html, rows}
    best = None        # highest tier that loaded but gave 0 rows, for honest fallback
    for ti, tier in enumerate(order):
        can_escalate = ti < len(order) - 1
        blocked_reason = None
        for attempt in range(1, args.retries + 2):
            if args.delay:
                time.sleep(args.delay + random.random() * 0.4)
            ua = random.choice(UAS)
            log(f"→ tier={tier} attempt={attempt} ua=…{ua[-14:]}", quiet)
            if tier == "static":
                status, html = fetch_static(args.url, args.timeout, ua, proxy, args.allow_private)
                err = None
            else:
                status, html, err = fetch_scrapling(
                    args.url, tier, args.timeout, args.wait_selector, args.headful, stealth_cfg)
            if err:
                attempts.append({"tier": tier, "attempt": attempt, "error": err[0], "hint": err[1]})
                log(f"  ✗ {err[0]} — {err[1]}", quiet)
                blocked_reason = err[0]
                break  # tool missing/broken/timed-out → escalate, don't retry
            blocked_reason = looks_blocked(status, html)
            # Parse once; reuse the Selector for the empty-extract check, rows, and links.
            # Also try extraction on an 'empty-body' page: if the requested data is
            # actually present, real rows are ground truth and override the size heuristic.
            sel = None
            rows_here = None
            if args.extract and blocked_reason in (None, "empty-body"):
                try:
                    sel = _selector(html)
                    rows_here = len(extract(sel, args.extract, args.attr, quiet) or [])
                except ImportError:
                    rows_here = None  # parsel missing — can't judge emptiness, don't escalate on it
            if blocked_reason == "empty-body" and rows_here:
                blocked_reason = None
            empty_extract = (rows_here == 0 and can_escalate and not blocked_reason)
            attempts.append({"tier": tier, "attempt": attempt, "status": status,
                             "bytes": len(html or ""), "blocked": blocked_reason,
                             **({"rows": rows_here} if rows_here is not None else {})})
            if not blocked_reason and not empty_extract:
                log(f"  ✓ clean — status={status} bytes={len(html)}"
                    + (f" rows={rows_here}" if rows_here is not None else ""), quiet)
                result = {"tier": tier, "status": status, "html": html, "sel": sel}
                break
            if empty_extract:
                log(f"  ~ loaded but 0 rows for '{args.extract}' → escalating", quiet)
                best = {"tier": tier, "status": status, "html": html, "sel": sel}  # keep highest
                break
            log(f"  ✗ blocked ({blocked_reason}) status={status}", quiet)
            if str(blocked_reason).startswith("http 4"):  # hard client error → escalate
                break
        if result:
            break
        log(f"tier={tier} exhausted → escalating", quiet)

    if result is None and best is not None:
        result = best
        log("no tier produced rows; returning cleanest load (0 rows)", quiet)

    out = {"url": args.url, "ok": result is not None, "lite": args.lite, "attempts": attempts}
    if result:
        html = result["html"]
        out.update({"tier_used": result["tier"], "status": result["status"], "bytes": len(html)})
        sel = result.get("sel")
        if sel is None:
            try:
                sel = _selector(html)
            except ImportError:
                sel = None
        if sel is not None:
            rows = extract(sel, args.extract, args.attr, quiet)
            if rows is not None:
                out["extracted"] = rows
            out["links"] = collect_links(sel, args.url)[:200]
        else:
            out["links"] = []
            if args.extract:
                out["extract_error"] = "parsel not installed: python3 -m pip install --break-system-packages parsel"
        if args.out:
            _write_bundle(args.out, args.url, html, out, result)
            out["out_dir"] = os.path.abspath(args.out)
    else:
        out["next_step"] = ("All local tiers blocked/failed. Escalate to Tier 3 (real Chrome "
                            "via browser-harness) or Tier 4 (licensed Apify actor). "
                            "See the skill's reference.md.")

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        if result:
            print(f"OK  tier={out['tier_used']}  status={out['status']}  "
                  f"bytes={out['bytes']}  links={len(out.get('links', []))}"
                  + (f"  rows={len(out['extracted'])}" if 'extracted' in out else ""))
            if 'out_dir' in out:
                print(f"saved → {out['out_dir']}")
            for r in out.get('extracted', [])[:40]:
                print("  •", r)
        else:
            print("BLOCKED on every allowed tier.")
            print(out["next_step"])
    return 0 if result else 3


def _csv_safe(v):
    """Neutralize spreadsheet formula injection (=, +, -, @, tab, CR triggers)."""
    v = "" if v is None else str(v)
    if v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        v = "'" + v
    return '"' + v.replace('"', '""') + '"'


def _write_bundle(out_dir, url, html, out, result):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "page.html"), "w", encoding="utf-8") as f:
        f.write(html)
    rows = out.get("extracted")
    if rows is not None:
        with open(os.path.join(out_dir, "data.csv"), "w", encoding="utf-8") as f:
            f.write("value\n" + "\n".join(_csv_safe(r) for r in rows))
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write(f"# omniscrape: {url}\n\n"
                f"- tier used: **{result['tier']}**\n- status: {result['status']}\n"
                f"- bytes: {len(html)}\n- rows: {len(rows) if rows else 0}\n"
                f"- links: {len(out.get('links', []))}\n")


def main():
    p = argparse.ArgumentParser(add_help=True, description="Auto-escalating web scraper.")
    p.add_argument("url")
    p.add_argument("--tier", default="auto", choices=["auto"] + TIERS)
    p.add_argument("--max-tier", default="stealth", choices=TIERS)
    p.add_argument("--extract", default=None)
    p.add_argument("--attr", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--wait-selector", default=None)
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--retries", type=int, default=2,
                   help="retries per tier on transient failure (negative values clamp to 0)")
    p.add_argument("--headful", action="store_true")
    p.add_argument("--proxy", default=None,
                   help="proxy URL (http|https|socks5://user:pass@host:port). "
                        "Overrides OMNISCRAPE_PROXIES env pool.")
    p.add_argument("--locale", default=None, help="e.g. en-US — match your proxy's country")
    p.add_argument("--timezone", default=None, help="e.g. America/New_York — match your proxy's geo")
    p.add_argument("--session-dir", default=None,
                   help="persist cookies/localStorage here (one run at a time — locked)")
    p.add_argument("--real-chrome", action="store_true",
                   help="drive your actually-installed Chrome (strongest fingerprint)")
    p.add_argument("--lite", action=argparse.BooleanOptionalAction, default=True,
                   help="(default ON) on the JS-render (dynamic) tier, skip images/media/fonts "
                        "to cut proxy bandwidth ~60-85%% (keeps CSS/JS/websockets; data still "
                        "renders). Pass --no-lite when you actually need the image/media bytes "
                        "(downloading images/video/files, screenshots, OCR/canvas-rendered text). "
                        "Never applied on the stealth tier regardless.")
    p.add_argument("--allow-private", action="store_true",
                   help="permit private/loopback/metadata hosts (SSRF guard OFF)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()
    args.retries = max(args.retries, 0)  # a negative retry count would skip the tier entirely
    sys.exit(run(args))


if __name__ == "__main__":
    main()
