#!/usr/bin/env python
"""Local control panel — one page to SEE and CONTROL the bot.

Serves http://127.0.0.1:8787 with:
  - live status: bot pid/alive, kill-switch state, log freshness, market phase
  - buttons: KILL SWITCH on/off (touch/remove state/KILL — the bot's own halt
    path: blocks new buys, exits keep working), RESTART BOT (SIGTERM the lock
    holder, wait for the flock to clear, relaunch detached), RUN PREFLIGHT
  - live log tail (bot.log) and the trading dashboard (dashboard.html) inline

SECURITY: binds to 127.0.0.1 ONLY and there is no auth — anyone with local
access to this machine already has the .env keys. Do NOT bind 0.0.0.0 and do
NOT port-forward this; for remote eyes use the read-only dashboard artifact
instead.

Run:      nohup .venv/bin/python ops/control_panel.py >> logs/panel.log 2>&1 &
Install:  cp ops/launchd/com.investment-strategy.panel.plist ~/Library/LaunchAgents/
          launchctl load ~/Library/LaunchAgents/com.investment-strategy.panel.plist
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ops.deadman import bot_alive, bot_pid, log_age_minutes, market_hours  # noqa: E402

ET = ZoneInfo("America/New_York")
HOST, PORT = "127.0.0.1", 8787
KILL_FILE = ROOT / "state" / "KILL"
PYTHON = ROOT / ".venv" / "bin" / "python"


# --------------------------------------------------------------------------- #
# Actions (also unit-tested directly)
# --------------------------------------------------------------------------- #
def kill_switch_on(reason: str = "control panel") -> str:
    KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
    KILL_FILE.write_text(
        f"engaged via control panel {dt.datetime.now(ET):%Y-%m-%d %H:%M:%S ET}"
        f" — {reason}\n", encoding="utf-8",
    )
    return "Kill switch ENGAGED — the bot will refuse to open anything new."


def kill_switch_off() -> str:
    try:
        KILL_FILE.unlink()
        return "Kill switch cleared — new entries allowed again."
    except FileNotFoundError:
        return "Kill switch was not engaged."


def restart_bot(timeout_s: float = 30.0) -> str:
    """SIGTERM the running bot (SIGINT is ignored by nohup'd children of
    non-interactive shells), wait for it to exit, relaunch detached. Refuses
    to start a second instance if the old one won't die — the bot's own flock
    guard would refuse anyway, but failing here gives a clear message."""
    pid = bot_pid()
    if pid and bot_alive(pid):
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if not bot_alive(pid):
                break
            time.sleep(0.5)
        else:
            return f"Old bot (pid {pid}) did not exit within {timeout_s:.0f}s — NOT restarting. Investigate manually."
    out = open(ROOT / "logs" / "stdout.log", "ab")
    proc = subprocess.Popen(
        [str(PYTHON), "-m", "investment_strategy"],
        cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
    )
    time.sleep(4)
    if not bot_alive(proc.pid):
        return "Bot relaunched but died within 4s — check the log tail below."
    return f"Bot restarted (pid {proc.pid})."


def run_preflight() -> str:
    try:
        r = subprocess.run(
            [str(PYTHON), "-m", "investment_strategy.preflight", "--no-alert-test"],
            cwd=ROOT, capture_output=True, text=True, timeout=180,
        )
        return r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return "preflight timed out after 180s"


def tail_log(n: int = 120, log_file: Path | None = None) -> str:
    log_file = log_file or ROOT / "logs" / "bot.log"
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max(1, min(n, 2000)):])
    except OSError as e:
        return f"(no log: {e})"


def status() -> dict:
    pid = bot_pid()
    age = log_age_minutes()
    now = dt.datetime.now(ET)
    return {
        "time_et": f"{now:%Y-%m-%d %H:%M:%S ET}",
        "bot_pid": pid,
        "bot_alive": bot_alive(pid),
        "kill_switch": KILL_FILE.exists(),
        "log_age_min": None if age is None else round(age, 1),
        "market_hours": market_hours(now),
    }


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Bot Control</title>
<style>
 body{font-family:-apple-system,Helvetica,sans-serif;background:#111;color:#eee;
      max-width:960px;margin:24px auto;padding:0 16px}
 h1{font-size:20px} .cards{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}
 .card{background:#1c1c1e;border-radius:10px;padding:12px 16px;min-width:130px}
 .card b{display:block;font-size:12px;color:#999;font-weight:600}
 .card span{font-size:17px} .ok{color:#30d158}.bad{color:#ff453a}.warn{color:#ffd60a}
 button{background:#2c2c2e;color:#eee;border:1px solid #444;border-radius:8px;
        padding:10px 14px;margin:4px 6px 4px 0;font-size:14px;cursor:pointer}
 button:hover{background:#3a3a3c} button.danger{border-color:#ff453a;color:#ff453a}
 button.go{border-color:#30d158;color:#30d158}
 pre{background:#000;border-radius:10px;padding:12px;overflow-x:auto;font-size:11px;
     max-height:420px;overflow-y:auto;white-space:pre-wrap}
 #msg{margin:8px 0;color:#ffd60a} a{color:#64d2ff}
 iframe{width:100%;height:900px;border:1px solid #333;border-radius:10px;background:#fff}
</style></head><body>
<h1>Trading bot — control panel <small style="color:#666">(localhost only)</small></h1>
<div class="cards" id="cards">loading…</div>
<div id="msg"></div>
<div>
 <button class="go" onclick="act('restart', 'Restart the bot?')">&#8635; Restart bot</button>
 <button class="danger" onclick="act('kill', 'ENGAGE the kill switch? New entries stop; exits keep working.')">&#9632; Kill switch ON</button>
 <button onclick="act('unkill', 'Clear the kill switch?')">&#9654; Kill switch OFF</button>
 <button onclick="preflight()">&#10003; Run preflight</button>
 <button onclick="logs()">&#8801; Refresh logs</button>
 <button onclick="dash()">&#128200; Toggle dashboard</button>
</div>
<pre id="out">(logs will appear here)</pre>
<div id="dashwrap" style="display:none"><iframe src="/dashboard"></iframe></div>
<script>
async function refresh(){
  const s = await (await fetch('/api/status')).json();
  const c = (l,v,cls)=>`<div class="card"><b>${l}</b><span class="${cls||''}">${v}</span></div>`;
  document.getElementById('cards').innerHTML =
    c('BOT', s.bot_alive?('RUNNING pid '+s.bot_pid):'DOWN', s.bot_alive?'ok':'bad')
   +c('KILL SWITCH', s.kill_switch?'ENGAGED':'off', s.kill_switch?'bad':'ok')
   +c('LOG AGE', s.log_age_min===null?'—':s.log_age_min+' min',
      s.log_age_min!==null&&s.log_age_min<40?'ok':'warn')
   +c('MARKET HOURS', s.market_hours?'OPEN window':'closed', s.market_hours?'ok':'')
   +c('TIME', s.time_et,'');
}
async function act(a, confirmMsg){
  if(confirmMsg && !confirm(confirmMsg)) return;
  document.getElementById('msg').textContent = '…working';
  const r = await fetch('/api/'+a, {method:'POST'});
  document.getElementById('msg').textContent = await r.text();
  refresh();
}
async function logs(){
  document.getElementById('out').textContent = await (await fetch('/api/logs?n=200')).text();
}
async function preflight(){
  document.getElementById('msg').textContent = '…running preflight (can take ~30s)';
  document.getElementById('out').textContent = await (await fetch('/api/preflight', {method:'POST'})).text();
  document.getElementById('msg').textContent = 'preflight done';
}
function dash(){
  const d = document.getElementById('dashwrap');
  d.style.display = d.style.display==='none'?'block':'none';
}
refresh(); logs(); setInterval(refresh, 15000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, ctype: str = "text/plain; charset=utf-8",
              code: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/" or self.path.startswith("/index"):
            self._send(_PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._send(json.dumps(status()), "application/json")
        elif self.path.startswith("/api/logs"):
            n = 200
            if "n=" in self.path:
                try:
                    n = int(self.path.split("n=")[1].split("&")[0])
                except ValueError:
                    pass
            self._send(tail_log(n))
        elif self.path == "/dashboard":
            try:
                self._send((ROOT / "dashboard.html").read_text(encoding="utf-8"),
                           "text/html; charset=utf-8")
            except OSError:
                self._send("dashboard.html not generated yet", code=404)
        else:
            self._send("not found", code=404)

    def do_POST(self):  # noqa: N802
        if self.path == "/api/kill":
            self._send(kill_switch_on())
        elif self.path == "/api/unkill":
            self._send(kill_switch_off())
        elif self.path == "/api/restart":
            self._send(restart_bot())
        elif self.path == "/api/preflight":
            self._send(html.escape(run_preflight()))
        else:
            self._send("not found", code=404)

    def log_message(self, fmt, *args):  # quiet: one line per request is noise
        pass


def main() -> int:
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"control panel on http://{HOST}:{PORT}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
