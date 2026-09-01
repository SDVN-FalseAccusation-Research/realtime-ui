#!/usr/bin/env python3
"""Headless browser check: screenshot a page and report its console errors.

    ../.venv/bin/python tools/shot.py <url> <out.png> [--wait 12] [--width 1600] [--height 1000]

Why not `google-chrome --screenshot`? Because `--virtual-time-budget` races the WebSocket
handshake — the page renders before the event stream has connected, so you photograph an
empty map and conclude the UI is broken. Driving Chrome over the DevTools protocol lets us
wait for real time to pass, and — more importantly — actually capture console errors and
uncaught exceptions, which the CLI flags silently drop.

Exit code is non-zero if the page logged an error, so this is usable as a check.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                ".venv", "lib", "python3.12", "site-packages"))
import websockets  # noqa: E402

CHROME = "/usr/bin/google-chrome"
PORT = 9222


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


async def run(url, out, wait_s, width, height):
    profile = tempfile.mkdtemp(prefix="shot-")
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--mute-audio",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
         f"--window-size={width},{height}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        target = None
        for _ in range(80):                       # wait for the debug endpoint
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json") as r:
                    tabs = json.load(r)
                pages = [t for t in tabs if t.get("type") == "page"]
                if pages:
                    target = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.25)
        if not target:
            print("could not reach the DevTools endpoint", file=sys.stderr)
            return 2

        logs, errors = [], []
        async with websockets.connect(target, max_size=None, ping_interval=None) as ws:
            n = 0

            async def send(method, params=None):
                nonlocal n
                n += 1
                await ws.send(json.dumps({"id": n, "method": method,
                                          "params": params or {}}))
                return n

            async def drain(until, collect=True):
                """Pump events until `until` seconds have passed, recording console output."""
                deadline = time.time() + until
                results = {}
                while time.time() < deadline:
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.4))
                    except asyncio.TimeoutError:
                        continue
                    if "id" in msg:
                        results[msg["id"]] = msg
                        continue
                    if not collect:
                        continue
                    m = msg.get("method")
                    if m == "Runtime.consoleAPICalled":
                        lvl = msg["params"]["type"]
                        txt = " ".join(str(a.get("value", a.get("description", "")))
                                       for a in msg["params"]["args"])
                        logs.append((lvl, txt))
                        if lvl in ("error", "assert"):
                            errors.append(txt)
                    elif m == "Runtime.exceptionThrown":
                        d = msg["params"]["exceptionDetails"]
                        txt = d.get("exception", {}).get("description") or d.get("text")
                        errors.append(txt)
                return results

            await send("Runtime.enable")
            await send("Page.enable")
            await send("Log.enable")
            await send("Page.navigate", {"url": url})
            await drain(wait_s)

            shot_id = await send("Page.captureScreenshot", {"format": "png"})
            res = await drain(3.0)
            data = res.get(shot_id, {}).get("result", {}).get("data")
            if data:
                import base64
                with open(out, "wb") as fh:
                    fh.write(base64.b64decode(data))

        print(f"screenshot: {out}")
        if logs:
            print(f"console ({len(logs)} messages):")
            for lvl, txt in logs[:25]:
                print(f"  [{lvl}] {txt[:200]}")
        else:
            print("console: (silent)")
        if errors:
            print(f"\nERRORS ({len(errors)}):", file=sys.stderr)
            for e in errors[:10]:
                print("  " + str(e)[:400], file=sys.stderr)
            return 1
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    sys.exit(asyncio.run(run(sys.argv[1], sys.argv[2],
                             float(arg("--wait", 12)),
                             int(arg("--width", 1600)),
                             int(arg("--height", 1000)))))
