#!/usr/bin/env python3

import base64
import json
import os
import socket
import struct
import sys
import time
from datetime import datetime, timezone

SOCK = os.path.expanduser("~/.codex/app-server-control/app-server-control.sock")

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


def ws_frame(data, opcode=0x1):
    data = data.encode() if isinstance(data, str) else data
    mask = os.urandom(4)
    n = len(data)

    if n < 126:
        head = bytes([0x80 | opcode, 0x80 | n])
    elif n < 65536:
        head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", n)
    else:
        head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", n)

    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise RuntimeError("socket closed")
        data += chunk
    return data


def recv_frame(sock):
    b1, b2 = recv_exact(sock, 2)
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    length = b2 & 0x7F

    if length == 126:
        length = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(sock, 8))[0]

    mask = recv_exact(sock, 4) if masked else None
    data = recv_exact(sock, length)

    if mask:
        data = bytes(x ^ mask[i % 4] for i, x in enumerate(data))

    return opcode, data


def send_json(sock, obj):
    sock.sendall(ws_frame(json.dumps(obj, separators=(",", ":"))))


def recv_json(sock):
    while True:
        opcode, data = recv_frame(sock)

        if opcode == 0x1:
            return json.loads(data.decode())
        if opcode == 0x9:
            sock.sendall(ws_frame(data, 0xA))
        elif opcode == 0x8:
            raise RuntimeError("server closed websocket")


def rpc_wait(sock, req_id):
    while True:
        response = recv_json(sock)
        if response.get("id") == req_id:
            if "error" in response:
                raise RuntimeError(json.dumps(response["error"], ensure_ascii=False))
            return response.get("result", {})


def fetch_limits():
    if not os.path.exists(SOCK):
        raise RuntimeError(f"Codex control socket not found: {SOCK}")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect(SOCK)

    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode()
    )

    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("WebSocket handshake failed")
        response += chunk

    status = response.split(b"\r\n", 1)[0]
    if b" 101" not in status:
        raise RuntimeError("WebSocket upgrade failed: " + status.decode(errors="replace"))

    send_json(sock, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"clientInfo": {"name": "codex-limits", "version": "2.0"}},
    })
    rpc_wait(sock, 1)

    send_json(sock, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
    send_json(sock, {
        "jsonrpc": "2.0", "id": 2,
        "method": "account/rateLimits/read", "params": None,
    })
    result = rpc_wait(sock, 2)

    sock.close()
    return result


def used_pct(window):
    return int(round(window.get("usedPercent") or 0))


def format_window(minutes):
    if not minutes:
        return "unknown"
    for size, name in ((43200, "month"), (10080, "week"), (1440, "day"), (60, "hour")):
        if minutes % size == 0:
            n = minutes // size
            return f"{n} {name}" if n == 1 else f"{n} {name}s"
    return f"{minutes} min"


def format_date(ts):
    if not ts:
        return "n/a"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def format_delta(ts):
    if not ts:
        return "n/a"
    left = int(ts - time.time())
    if left <= 0:
        return "now"
    d, rem = divmod(left, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def level(pct):
    if pct >= 100:
        return RED, "failed (quota-exhausted)"
    if pct >= 90:
        return RED, "degraded (quota-critical)"
    if pct >= 70:
        return YELLOW, "degraded (quota-low)"
    return GREEN, "active (running)"


def pct_color(pct):
    return level(pct)[0]


def line(label, value):
    return f"{label:>12} {value}"


def render(result):
    limits = result.get("rateLimits") or {}
    plan = limits.get("planType", "unknown")
    primary = limits.get("primary")
    secondary = limits.get("secondary")

    items = [("primary", primary), ("secondary", secondary)]
    wins = [w for w in (primary, secondary) if w]

    for limit_id, bucket in (result.get("rateLimitsByLimitId") or {}).items():
        if limit_id == limits.get("limitId"):
            continue
        for kind in ("primary", "secondary"):
            if bucket.get(kind):
                items.append((f"{limit_id}:{kind}", bucket[kind]))
                wins.append(bucket[kind])

    worst = max((used_pct(w) for w in wins), default=0)
    color, state = level(worst)

    out = [
        f"{paint(color, '●')} {paint(BOLD, 'codex-limits.service')} - Codex usage",
        line("Loaded:", f"loaded (plan: {plan})"),
        line("Active:", paint(color, state)),
    ]

    credits = limits.get("credits")
    if credits:
        if credits.get("unlimited"):
            out.append(line("Credits:", paint(GREEN, "unlimited")))
        elif credits.get("balance") is not None:
            out.append(line("Credits:", credits["balance"]))

    out.append(line("Tasks:", f"{len(wins)} (limit: {len(wins)})"))

    if primary:
        used = used_pct(primary)
        left = max(0, 100 - used)
        out.append(line("Memory:", f"{paint(pct_color(used), f'{used}%')} of "
                                    f"{format_window(primary.get('windowDurationMins'))} window"))
        out.append(line("CPU:", f"{left}% left"))
    else:
        out.append(line("Memory:", "n/a"))
        out.append(line("CPU:", "n/a"))

    out.append(line("CGroup:", "/codex.slice"))

    indent = " " * 13

    def short(name):
        return name if len(name) <= 18 else name[:17] + "…"

    for idx, (name, win) in enumerate(items):
        last = idx == len(items) - 1
        branch = "└─" if last else "├─"
        cont = "   " if last else "│  "

        if not win:
            out.append(f"{indent}{branch} {short(name)} n/a")
            continue

        used = used_pct(win)
        ts = win.get("resetsAt")
        out.append(f"{indent}{branch} {short(name)} {paint(pct_color(used), f'{used}%')}")
        out.append(f"{indent}{cont}{format_date(ts)}")
        out.append(f"{indent}{cont}resets in {format_delta(ts)}")

    return "\n".join(out)


def main():
    print(render(fetch_limits()))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as e:
        print(f"{paint(RED, 'codex-limits: ERROR:')} {e}")
        raise SystemExit(1)
