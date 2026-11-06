"""
drop_alarm_web.py -- the drop alarm, started from the phone (5 Nov 2026).

Jason: "I would like it to be able to be run from my cell phone". A small
web page served by the PC, reached over Tailscale like Scanner Studio:

  Start   watches the symbol from its live price at that moment, or from
          "Your price" if typed and higher (6 Nov: a buy the PC never saw);
          the alarm line trails `trail` dollars under the highest price
          since then (drop_alert.py's math). At the line: ONE high-priority
          Pushover alarm -- and it keeps watching (6 Nov: "it should keep
          going"). The high at the alarm is the marker ("zero"); no further
          alarm however far it falls, until a new high re-arms it.
  Stop    ends the watch (you sold first).
  Status  price, high, line and the distance to it, every 2 seconds.

Private twice over: Windows Firewall lets only Tailscale devices reach the
port (see install_drop_alarm_web.ps1), and every request must carry the
access code from .env (DROP_ALARM_TOKEN, made on the first run). The link
with the code is printed by  python drop_alarm_web.py --link  .

Prices only. It reads no account and places no order.

    python drop_alarm_web.py            # serve (what the logon task runs)
    python drop_alarm_web.py --link     # print the phone link
    python drop_alarm_web.py --self-test
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import drop_alert
from feed_check import ET, load_env

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
LOG_PATH = os.path.join(HERE, "drop_alarm_web.log")
PORT = int(os.environ.get("DROP_ALARM_PORT", "8790"))
DEFAULT_SYMBOL, DEFAULT_TRAIL, EVERY = "SPCX", 0.35, 2.0
TAILSCALE = r"C:\Program Files\Tailscale\tailscale.exe"


def log(line: str) -> None:
    stamped = f"{datetime.now(ET):%Y-%m-%d %H:%M:%S}  {line}"
    print(stamped, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(stamped + "\n")
    except OSError:
        pass


def access_code(env_path: str = ENV_PATH) -> str:
    """DROP_ALARM_TOKEN from .env, made and saved there on the first run."""
    load_env()
    code = os.getenv("DROP_ALARM_TOKEN", "").strip()
    if code:
        return code
    code = secrets.token_urlsafe(18)
    with open(env_path, "a", encoding="utf-8") as handle:
        handle.write(f"\nDROP_ALARM_TOKEN={code}\n")
    os.environ["DROP_ALARM_TOKEN"] = code
    return code


class Watch:
    """One watch at a time. step() is one read; run() loops it on a thread."""

    def __init__(self, read=drop_alert.latest_price, send: Optional[Callable] = None,
                 clock=None, wait=None):
        self.read, self.send = read, send or self._pushover
        self.clock = clock or (lambda: datetime.now(ET))
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.custom_wait = wait            # tests pass their own; else Stop interrupts the pause
        self.wait = wait or self.stop_event.wait
        self.thread: Optional[threading.Thread] = None
        self.state = {"running": False, "symbol": None, "trail": None, "start": None,
                      "high": None, "line": None, "price": None, "updated": None,
                      "alarm": None, "error": None, "started": None,
                      "your_price": None, "armed": False, "alarms": 0}

    @staticmethod
    def _pushover(symbol: str, text: str) -> Optional[str]:
        from spcx_alert import PRIORITY_SUMMARY, send_pushover   # high priority: loud, once
        return send_pushover(text, title=f"{symbol} drop alarm", priority=PRIORITY_SUMMARY)

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def start(self, symbol: str, trail: float, your_price: Optional[float] = None) -> dict:
        """Read the live price now and begin trailing from it, or from
        `your_price` when that is higher."""
        self.stop()
        symbol = symbol.strip().upper()
        if not symbol.replace(".", "").isalnum() or not 0.01 <= trail <= 50:
            raise ValueError("a symbol like SPCX and a trail between $0.01 and $50")
        if your_price is not None and not 0.01 <= your_price <= 100000:
            raise ValueError("your price must be a price, like 138.50")
        now = self.clock()
        price = self.read(symbol, now)          # raises if the price cannot be read
        high = max(price, your_price or 0)
        with self.lock:
            self.state.update(running=True, symbol=symbol, trail=trail, start=your_price or price,
                              high=high, line=round(high - trail, 2), price=price,
                              updated=f"{now:%H:%M:%S}", alarm=None, error=None,
                              started=f"{now:%H:%M:%S}", your_price=your_price, armed=True, alarms=0)
        log(f"start {symbol} at ${price:,.2f}"
            + (f", your price ${your_price:,.2f}" if your_price else "") + f", trail ${trail:.2f}")
        self.stop_event = threading.Event()
        self.wait = self.custom_wait or self.stop_event.wait
        return self.snapshot()

    def step(self) -> bool:
        """One read. Returns False once the watch has ended."""
        s = self.snapshot()
        if not s["running"]:
            return False
        now = self.clock()
        try:
            price = self.read(s["symbol"], now)
        except Exception as exc:  # noqa: BLE001 -- one bad read is not the end
            with self.lock:
                self.state["error"] = f"{now:%H:%M:%S} could not read the price ({type(exc).__name__})"
            return True
        armed = s["armed"]
        if price > s["high"]:                   # a new high: the line follows, and re-arms
            high, armed = price, True
        else:
            high = s["high"]                    # the marker ("zero") holds
        line = round(high - s["trail"], 2)
        with self.lock:
            self.state.update(price=price, high=high, line=line, armed=armed,
                              updated=f"{now:%H:%M:%S}", error=None)
        if not armed or price > line:
            return True
        text = drop_alert.message(s["symbol"], price, s["start"], high, 0, "high", line, s["trail"])
        error = self.send(s["symbol"], text)
        if error:
            with self.lock:
                self.state["error"] = f"alarm not sent yet ({error}); retrying"
            log(f"NOT SENT ({error}): {text}")
            return True
        with self.lock:                         # keep watching; quiet until a new high
            self.state.update(armed=False, alarms=s["alarms"] + 1, alarm=f"{now:%H:%M:%S}  {text}")
        log(f"ALARM SENT: {text}")
        return True

    def run(self, every: float = EVERY) -> None:
        while self.step():
            if self.wait(every):            # Stop pressed
                break

    def start_thread(self, symbol: str, trail: float, your_price: Optional[float] = None,
                     every: float = EVERY) -> dict:
        snap = self.start(symbol, trail, your_price)
        self.thread = threading.Thread(target=self.run, args=(every,), daemon=True)
        self.thread.start()
        return snap

    def stop(self) -> dict:
        self.stop_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)
        with self.lock:
            if self.state["running"]:
                log(f"stop {self.state['symbol']}")
            self.state["running"] = False
        return self.snapshot()


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes"><title>Drop Alarm</title>
<style>
:root{--bg:#0b0f17;--card:#141b27;--ink:#e8edf5;--muted:#8b97aa;--up:#2bb673;--down:#e5534b;--accent:#4c8dff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px -apple-system,system-ui,sans-serif}
main{max-width:480px;margin:0 auto;padding:16px}h1{font-size:22px;margin:4px 0 12px}
.card{background:var(--card);border-radius:14px;padding:16px;margin-bottom:12px}
label{display:block;color:var(--muted);font-size:13px;margin:8px 0 4px}
input{width:100%;font-size:20px;padding:10px;border-radius:10px;border:1px solid #2a3446;background:#0f1520;color:var(--ink)}
button{width:100%;font-size:20px;font-weight:600;padding:14px;border:0;border-radius:12px;margin-top:12px;color:#fff}
#go{background:var(--up)}#halt{background:var(--down)}
.row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #222c3b}
.row b{font-variant-numeric:tabular-nums}.big{font-size:30px}
#msg{color:var(--muted);font-size:14px;min-height:20px;margin-top:8px}.alarm{color:var(--down);font-weight:600}
</style></head><body><main>
<h1>Drop Alarm</h1>
<div class="card">
<label for="sym">Symbol</label><input id="sym" value="SPCX" autocapitalize="characters">
<label for="trail">Trail ($ under the highest price)</label><input id="trail" value="0.35" inputmode="decimal">
<label for="mine">Your price (optional: start from it if higher than the live price)</label><input id="mine" placeholder="e.g. 138.50" inputmode="decimal">
<button id="go">Start</button><button id="halt">Stop</button>
<div id="msg"></div></div>
<div class="card" id="status">
<div class="row"><span>Status</span><b id="s_state">-</b></div>
<div class="row"><span>Price</span><b id="s_price" class="big">-</b></div>
<div class="row"><span>Highest since start</span><b id="s_high">-</b></div>
<div class="row"><span>Alarm line</span><b id="s_line">-</b></div>
<div class="row"><span>Room before the alarm</span><b id="s_room">-</b></div>
<div class="row"><span>vs the high ("zero")</span><b id="s_vshigh">-</b></div>
<div class="row"><span>vs your price</span><b id="s_vsmine">-</b></div>
<div class="row"><span>Alarms sent</span><b id="s_alarms">-</b></div>
<div class="row"><span>Started at</span><b id="s_start">-</b></div>
<div class="row"><span>Updated</span><b id="s_upd">-</b></div>
</div>
<p style="color:var(--muted);font-size:12px">Prices only. This reads no account and places no order.</p>
</main><script>
const t=new URLSearchParams(location.search).get("t")||"";
const $=id=>document.getElementById(id);
const usd=v=>v==null?"-":"$"+Number(v).toFixed(2);
async function call(path,body){const r=await fetch(path+"?t="+encodeURIComponent(t),
 body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{});
 const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j}
const sgn=v=>v==null?"-":(v>=0?"+":"-")+"$"+Math.abs(v).toFixed(2);
function show(s){const quiet=s.running&&!s.armed;
 $("s_state").textContent=!s.running?"Not watching":(quiet?"Alarm sent - still watching (rings again after a new high)":
  "Watching "+s.symbol+", trail "+usd(s.trail));$("s_state").className=quiet?"alarm":"";
 $("s_price").textContent=usd(s.price);$("s_high").textContent=usd(s.high);$("s_line").textContent=usd(s.line);
 $("s_room").textContent=s.price!=null&&s.line!=null&&s.armed?usd(s.price-s.line):(quiet?"waiting for a new high":"-");
 $("s_vshigh").textContent=s.price!=null&&s.high!=null?sgn(s.price-s.high):"-";
 $("s_vsmine").textContent=s.your_price!=null&&s.price!=null?sgn(s.price-s.your_price):"-";
 $("s_alarms").textContent=s.running||s.alarms?String(s.alarms):"-";
 $("s_start").textContent=s.start!=null?usd(s.start)+" at "+s.started:"-";$("s_upd").textContent=s.updated||"-";
 $("msg").textContent=s.error||(s.alarm?"Last alarm: "+s.alarm:"");$("msg").className=s.alarm&&!s.error?"alarm":""}
async function poll(){try{show(await call("/api/state"))}catch(e){$("msg").textContent="Cannot reach the PC: "+e.message}}
$("go").onclick=async()=>{$("msg").textContent="Starting...";const mine=$("mine").value.trim();
 try{show(await call("/api/start",{symbol:$("sym").value,trail:parseFloat($("trail").value),
 price:mine?parseFloat(mine):null}))}catch(e){$("msg").textContent="Not started: "+e.message}};
$("halt").onclick=async()=>{try{show(await call("/api/stop",{}))}catch(e){$("msg").textContent=e.message}};
poll();setInterval(poll,2000);
</script></body></html>"""


def make_handler(watch: Watch, code: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):   # quiet: the log file records what matters
            pass

        def _reply(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._reply(status, json.dumps(data).encode(), "application/json")

        def _allowed(self) -> bool:
            given = (parse_qs(urlparse(self.path).query).get("t") or [""])[0]
            return hmac.compare_digest(given.encode(), code.encode())

        def do_GET(self):
            path = urlparse(self.path).path
            if not self._allowed():
                if path == "/":
                    return self._reply(403, b"<p style='font:18px sans-serif'>This link needs its access "
                                            b"code. On the PC run: python drop_alarm_web.py --link</p>",
                                       "text/html; charset=utf-8")
                return self._json(403, {"error": "access code missing or wrong"})
            if path == "/":
                return self._reply(200, PAGE.encode(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(200, watch.snapshot())
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._allowed():
                return self._json(403, {"error": "access code missing or wrong"})
            path = urlparse(self.path).path
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 4096)
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad request"})
            if path == "/api/start":
                try:
                    trail = float(body.get("trail", DEFAULT_TRAIL))
                    mine = body.get("price")
                    mine = float(mine) if mine not in (None, "") else None
                    return self._json(200, watch.start_thread(str(body.get("symbol") or DEFAULT_SYMBOL),
                                                              trail, mine))
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                except Exception as exc:  # noqa: BLE001 -- the price could not be read
                    return self._json(502, {"error": f"could not read the price ({type(exc).__name__})"})
            if path == "/api/stop":
                return self._json(200, watch.stop())
            return self._json(404, {"error": "not found"})
    return Handler


def tailscale_ip() -> Optional[str]:
    try:
        out = subprocess.run([TAILSCALE, "ip", "-4"], capture_output=True, text=True, timeout=10).stdout
        return out.split()[0] if out.split() else None
    except (OSError, subprocess.SubprocessError):
        return None


def phone_link(code: str) -> str:
    return f"http://{tailscale_ip() or '<the PC Tailscale address>'}:{PORT}/?t={code}"


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    import tempfile
    import urllib.error
    import urllib.request

    failures = []

    def ok(cond, msg):
        if not cond:
            failures.append(msg)

    global LOG_PATH
    saved_log, LOG_PATH = LOG_PATH, os.devnull
    try:
        prices, sent = [], []
        fixed = lambda: datetime(2026, 11, 5, 10, 0, tzinfo=ET)   # noqa: E731

        def read(symbol, now):
            return prices.pop(0)

        w = Watch(read=read, send=lambda sym, text: sent.append(text), clock=fixed)
        prices[:] = [138.20]
        s = w.start("spcx", 0.35)
        ok(s["running"] and s["symbol"] == "SPCX" and s["start"] == 138.20 and s["line"] == 137.85,
           f"starts from the live price, 35 cents under it: {s}")
        prices[:] = [138.60, 139.00, 138.70, 138.66, 138.65]
        steps = [w.step() for _ in range(5)]
        s = w.snapshot()
        ok(steps == [True] * 5 and len(sent) == 1
           and "DOWN $0.35: now $138.65" in sent[0] and "the high $139.00" in sent[0]
           and s["running"] and not s["armed"] and s["alarms"] == 1 and s["alarm"],
           f"35 cents off the $139 high rings once and keeps watching: {steps} {sent} {s}")
        # Jason's walk: further down, a bounce under the marker, then a new high and a new drop.
        prices[:] = [138.20, 138.90, 139.00]
        [w.step() for _ in range(3)]
        s = w.snapshot()
        ok(len(sent) == 1 and s["high"] == 139.00 and not s["armed"],
           f"no more alarms below the marker, nor at it: {sent} {s}")
        prices[:] = [139.40, 139.06, 139.05, 138.00]
        [w.step() for _ in range(4)]
        s = w.snapshot()
        ok(len(sent) == 2 and "now $139.05" in sent[1] and "the high $139.40" in sent[1]
           and s["alarms"] == 2 and s["running"], f"a new high re-arms; the next 35 cents rings once: {sent} {s}")
        ok(w.stop()["running"] is False and w.step() is False, "Stop ends it")

        prices[:] = [138.20]
        s = Watch(read=read, send=lambda *a: None, clock=fixed).start("SPCX", 0.35, 138.50)
        ok(s["start"] == 138.50 and s["high"] == 138.50 and s["line"] == 138.15 and s["your_price"] == 138.50,
           f"your price above the live price sets the line at once: {s}")
        prices[:] = [139.00]
        s = Watch(read=read, send=lambda *a: None, clock=fixed).start("SPCX", 0.35, 138.50)
        ok(s["high"] == 139.00 and s["line"] == 138.65, f"the live price above yours: trail from it: {s}")
        try:
            Watch(read=read, clock=fixed).start("SPCX", 0.35, -5.0)
            failures.append("a negative price must be refused")
        except ValueError:
            pass

        failed = {"n": 1}

        def flaky(sym, text):
            if failed["n"]:
                failed["n"] -= 1
                return "down"
            sent.append(text)
            return None
        w2 = Watch(read=read, send=flaky, clock=fixed)
        prices[:] = [100.0]
        w2.start("SPCX", 0.35)
        prices[:] = [99.60, 99.60]
        ok(w2.step() is True and "retrying" in (w2.snapshot()["error"] or "")
           and w2.step() is True and w2.snapshot()["alarms"] == 1, "a failed send is retried")

        def broken(symbol, now):
            raise OSError("no net")
        w3 = Watch(read=broken, send=lambda *a: None, clock=fixed)
        try:
            w3.start("SPCX", 0.35)
            failures.append("a start that cannot read the price must say so")
        except OSError:
            pass
        for bad in (("SP CX", 0.35), ("SPCX", 0.0), ("SPCX", 99.0)):
            try:
                Watch(read=read, clock=fixed).start(*bad)
                failures.append(f"rejected input {bad}")
            except ValueError:
                pass

        # The web side, on a real local port, with a fake price that falls.
        tape = iter([138.20] + [138.50] * 3 + [138.10] * 50)
        web = Watch(read=lambda sym, now: next(tape), send=lambda sym, text: sent.append(text) or None,
                    clock=fixed)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(web, "secret-code"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def get(path):
            try:
                with urllib.request.urlopen(base + path, timeout=5) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()

        def post(path, data):
            req = urllib.request.Request(base + path, data=json.dumps(data).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode())

        ok(get("/")[0] == 403 and get("/?t=wrong")[0] == 403 and get("/api/state")[0] == 403,
           "no page or data without the access code")
        ok(post("/api/start?t=wrong", {"symbol": "SPCX", "trail": 0.35})[0] == 403, "no start without it")
        code_, body = get("/?t=secret-code")
        ok(code_ == 200 and "Drop Alarm" in body and "places no order" in body, "the page, with the code")
        st, s = post("/api/start?t=secret-code", {"symbol": "SPCX", "trail": 0.35})
        ok(st == 200 and s["running"] and s["start"] == 138.20, f"started from the phone: {st} {s}")
        st, s = post("/api/start?t=secret-code", {"symbol": "SPCX", "trail": -1})
        ok(st == 400, f"a bad trail is refused: {st}")
        st, s = post("/api/start?t=secret-code", {"symbol": "SPCX", "trail": 0.35, "price": "abc"})
        ok(st == 400, f"a bad price is refused: {st}")
        web.custom_wait = lambda seconds: web.stop_event.wait(0.01)
        st, s = post("/api/start?t=secret-code", {"symbol": "SPCX", "trail": 0.35})
        for _ in range(300):
            s = json.loads(get("/api/state?t=secret-code")[1])
            if s["alarms"]:
                break
            time.sleep(0.01)
        ok(s["running"] and s["alarms"] == 1 and "now $138.10" in s["alarm"] and "the high $138.50" in s["alarm"],
           f"the alarm reaches the page, still watching: {s}")
        time.sleep(0.1)
        ok(json.loads(get("/api/state?t=secret-code")[1])["alarms"] == 1, "no repeat while below the marker")
        st, s = post("/api/stop?t=secret-code", {})
        ok(st == 200 and not s["running"], "stop answers")
        server.shutdown()

        with tempfile.TemporaryDirectory() as folder:
            env = os.path.join(folder, ".env")
            saved = os.environ.pop("DROP_ALARM_TOKEN", None)
            try:
                c1 = access_code(env)
                c2 = access_code(env)
                ok(len(c1) >= 20 and c1 == c2 and f"DROP_ALARM_TOKEN={c1}" in open(env).read(),
                   "an access code is made once and kept in .env")
            finally:
                os.environ.pop("DROP_ALARM_TOKEN", None)
                if saved is not None:
                    os.environ["DROP_ALARM_TOKEN"] = saved

        source = open(__file__, encoding="utf-8").read()
        for forbidden in ("TradingClient", "submit_order", "place_order"):
            ok(forbidden not in source.replace(f'"{forbidden}"', ""), f"no {forbidden} here")
    finally:
        LOG_PATH = saved_log

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("drop_alarm_web self-test passed")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if "--self-test" in sys.argv:
        return self_test()
    code = access_code()
    if "--link" in sys.argv:
        print(phone_link(code))
        return 0
    watch = Watch()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(watch, code))
    log(f"drop alarm page on port {PORT} (Tailscale devices only); "
        f"get the phone link with: python drop_alarm_web.py --link")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        watch.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
