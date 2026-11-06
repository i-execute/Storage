#!/usr/bin/env python3
"""Antigravity (Google AI Pro) quota viewer via daily-cloudcode-pa internal API."""

import json
import os
import sys
import time
from datetime import datetime, timezone

TOKEN_FILE = os.environ.get("AGY_TOKEN_FILE", os.path.expanduser("~/.gemini/antigravity-cli/antigravity-oauth-token"))
CLIENT_ID = os.environ.get("AGY_CLIENT_ID")
CLIENT_SECRET = os.environ.get("AGY_CLIENT_SECRET")
API = "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
MODELS_API = "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
PROXY = os.environ.get("AGY_PROXY")  # e.g. socks5://host:port, empty = direct

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"


def use_color():
    if "--no-color" in sys.argv or os.environ.get("NO_COLOR"):
        return False
    if "--color" in sys.argv:
        return True
    return sys.stdout.isatty()


COLOR = use_color()


def paint(code, text):
    return f"{code}{text}{RESET}" if COLOR else str(text)


def fetch_token():
    with open(TOKEN_FILE) as f:
        rt = json.load(f)["token"]["refresh_token"]
    import urllib.parse
    import urllib.request
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": rt,
    }).encode()
    opener = build_opener()
    with opener.open("https://oauth2.googleapis.com/token", data=data, timeout=30) as r:
        t = json.loads(r.read().decode())
    if "access_token" not in t:
        raise RuntimeError(f"token refresh failed: {str(t)[:200]}")
    return t["access_token"]


def build_opener():
    import urllib.request
    if not PROXY:
        return urllib.request.build_opener()
    try:
        from pysocks import sockshandler  # noqa
        import socks, socket
    except ImportError:
        try:
            import socks, socket  # PySocks
        except ImportError:
            return urllib.request.build_opener()
    socks.set_default_proxy(*parse_proxy(PROXY))
    socket.socket = socks.socksocket
    return urllib.request.build_opener()


def parse_proxy(url):
    # socks5://host:port -> (socks.SOCKS5, host, port)
    scheme, _, rest = url.partition("://")
    host, _, port = rest.rpartition(":")
    import socks
    proto = {"socks5": socks.SOCKS5, "socks4": socks.SOCKS4, "socks5h": socks.SOCKS5}.get(scheme, socks.SOCKS5)
    return (proto, host, int(port or 1080))


def fetch_quota():
    import urllib.request
    at = fetch_token()
    body = b"{}"
    headers = {
        "Authorization": f"Bearer {at}",
        "Content-Type": "application/json",
        "User-Agent": "antigravity-cli/1.4.12",
        "x-goog-api-client": "antigravity-cli/1.4.12",
    }
    req = urllib.request.Request(API, data=body, headers=headers)
    opener = build_opener()
    try:
        with opener.open(req, timeout=40) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}")


def fmt_frac(fr):
    if fr is None:
        return "n/a"
    rem = int(round(fr * 100))
    return paint(color_for(100 - rem), f"{rem}%")


def color_for(pct):
    if pct <= 10:
        return RED
    if pct <= 30:
        return YELLOW
    return GREEN


def fmt_date(iso):
    if not iso:
        return "n/a"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d.%m.%Y %H:%M UTC")
    except ValueError:
        return iso


def fmt_reset(iso):
    if not iso:
        return "n/a"
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return iso
    left = int(ts - time.time())
    if left <= 0:
        return paint(GREEN, "now")
    d, rem = divmod(left, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def line(label, value):
    return f"{label:>12} {value}"


def render(q):
    groups = q.get("groups") or []
    worst = min((b.get("remainingFraction", 1.0) for g in groups for b in (g.get("buckets") or [])), default=1.0)
    worst_pct = int(round((1 - worst) * 100))
    if worst_pct >= 100:
        color, state = RED, "failed (quota-exhausted)"
    elif worst_pct >= 90:
        color, state = RED, "degraded (quota-critical)"
    elif worst_pct >= 70:
        color, state = YELLOW, "degraded (quota-low)"
    else:
        color, state = GREEN, "active (running)"

    out = [
        f"{paint(color, '●')} {paint(BOLD, 'antigravity.service')} - Google AI Pro usage",
        line("Loaded:", "loaded (plan: Google AI Pro)"),
        line("Active:", paint(color, state)),
    ]
    total = sum(len(g.get("buckets") or []) for g in groups)
    out.append(line("Tasks:", f"{total} (limit: {total})"))

    indent = " " * 13
    for g in groups:
        buckets = g.get("buckets") or []
        for idx, b in enumerate(buckets):
            last = idx == len(buckets) - 1
            branch = "└─" if last else "├─"
            cont = "   " if last else "│  "
            fr = b.get("remainingFraction")
            pct = int(round((fr or 0) * 100))
            name = (b.get("displayName") or b.get("bucketId") or "?").replace(" Limit Remaining", " Limit")
            ts = b.get("resetTime")
            out.append(f"{indent}{branch} {name} {fmt_frac(fr)}")
            out.append(f"{indent}{cont}{fmt_date(ts)}")
            out.append(f"{indent}{cont}resets in {fmt_reset(ts)}")
    return "\n".join(out)


def fetch_models():
    import urllib.request
    at = fetch_token()
    headers = {
        "Authorization": f"Bearer {at}",
        "Content-Type": "application/json",
        "User-Agent": "antigravity-cli/1.4.12",
        "x-goog-api-client": "antigravity-cli/1.4.12",
    }
    req = urllib.request.Request(MODELS_API, data=b"{}", headers=headers)
    opener = build_opener()
    try:
        with opener.open(req, timeout=40) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}")


def render_models(m):
    out = []
    indent = " " * 13
    lines = []
    for name, mm in sorted((m.get("models") or {}).items()):
        q = mm.get("quotaInfo") or {}
        fr = q.get("remainingFraction")
        disp = mm.get("displayName") or name
        lines.append((disp, fr, q.get("resetTime"), bool(mm.get("supportsImages"))))
    out.append(f"{paint(BOLD, 'Models:')} {len(lines)} available")
    for idx, (disp, fr, reset, img) in enumerate(lines):
        last = idx == len(lines) - 1
        branch = "└─" if last else "├─"
        cont = "   " if last else "│  "
        qtxt = "n/a" if fr is None else paint(color_for(100 - int(round(fr * 100))), f"{int(round(fr * 100))}%")
        out.append(f"{indent}{branch} {name}  {qtxt}")
        if reset:
            out.append(f"{indent}{cont}{fmt_date(reset)} (quota reset)")
    return "\n".join(out)


def main():
    print(render(fetch_quota()))
    if "--models" in sys.argv:
        print()
        print(render_models(fetch_models()))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as e:
        print(f"{paint(RED, 'agy-limits: ERROR:')} {e}")
        raise SystemExit(1)
