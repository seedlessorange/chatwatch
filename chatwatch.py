#!/usr/bin/env python3
"""chatwatch: what each open Claude Code chat is using of your plan, and what to do about it.

    python3 chatwatch.py            # table of open chats, heaviest first
    python3 chatwatch.py serve      # the page on http://127.0.0.1:7731, plus alerts every 5 min
    python3 chatwatch.py check      # one alert check (add --dry to print without notifying)
    python3 chatwatch.py calibrate  # re-measure how much of the plan a unit of usage is
    python3 chatwatch.py fit        # fit how much each token type moves the meter (--save to use it)
    python3 chatwatch.py json       # the raw numbers

Where the numbers come from:
- Every running Claude Code chat writes ~/.claude/sessions/<pid>.json (session
  id, folder, busy or idle). A chat counts as open while that pid is alive.
- Its transcript, ~/.claude/projects/<folder>/<session>.jsonl, records the
  tokens of every step. Subagent transcripts sit in <session>/subagents/ and
  count toward the chat that started them.
- The plan's meters (the weekly limit and the 5-hour window, the numbers
  /usage shows) come from Anthropic, asked with Claude Code's own login. The
  login is read for that one request, sent only to api.anthropic.com, and
  never written anywhere. It is never refreshed: when it has expired, the last
  reading is used until Claude Code next renews it.

Anthropic does not split the meters per chat. chatwatch takes a meter reading
every 5 minutes and divides how far the meter moved by how much every chat
used over the same time (each token type weighted by how much it moves the
meter, fitted from your own readings with `chatwatch.py fit`). That gives "percent of the limit per unit of usage", which
turns each chat's usage into a share of the limit. It is an estimate.

Close + handoff: a throwaway copy of the chat (--resume --fork-session, no
tools, nothing saved) writes a handoff note on the chat's own model. Only
after the note is saved is the chat's process stopped.
"""

import bisect
import glob
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"
HOME = os.path.expanduser("~")
CLAUDE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
SESSIONS = os.environ.get("CHATWATCH_SESSIONS", os.path.join(CLAUDE, "sessions"))
PROJECTS = os.path.join(CLAUDE, "projects")
HANDOFFS = os.path.join(CLAUDE, "handoffs")
STATE = os.path.join(CLAUDE, "chatwatch-state.json")
CALIB = os.path.join(CLAUDE, "chatwatch-calibration.json")
PLAN_CACHE = os.path.join(CLAUDE, "chatwatch-plan.json")
PLAN_HISTORY = os.path.join(CLAUDE, "chatwatch-plan-history.jsonl")
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("CHATWATCH_PORT", "7731"))

# Alert thresholds. One notification per check, listing only what is new.
CTX_ALERT = 250_000          # context past this: start fresh
FABLE_IDLE_MIN = 30          # a Fable chat idle this long: switch or close
HOUR_ALERT_PCT = 2.0         # one chat using this share of the limit in the last hour
HOUR_ALERT_USD = 10.0        # the same, in API dollars, until the meter is calibrated
IDLE_PILE_HOURS = 3          # chats idle this long...
IDLE_PILE_COUNT = 10         # ...when at least this many are open
REALERT_HOURS = 6            # the same alert for the same chat, at most this often
# Fast-burn alerts, checked every minute
BURN_WINDOW_MIN = 10         # the pace is measured over this many minutes
BURN_PCT_H = {"week": float(os.environ.get("CHATWATCH_BURN_WEEK", 1.0)),     # one chat burning faster
              "five_hour": float(os.environ.get("CHATWATCH_BURN_5H", 15.0))}  # than this, per hour
BURN_USD_H = 25.0            # the same, in API dollars, until the meter is calibrated
CTX_JUMP_MIN = 15            # context growth is measured over this many minutes
CTX_JUMP = 80_000            # ...and alerts past this much growth
CTX_JUMP_FLOOR = 150_000     # ...once the chat holds at least this much
# Waste: usage that better token handling would have saved (see waste_of)
WASTE_WINDOW_MIN = 30        # measured over this many minutes
LEAN_CTX = 80_000            # what a fresh chat with a handoff carries
REBUILD_FLOOR = 30_000       # cache writes past this in one step count as a rebuild
WASTE_PCT_H = {"week": 0.4, "five_hour": 6.0}   # alert when a chat wastes this much per hour
WASTE_USD_H = 10.0           # the same in API dollars, until the meter is calibrated
RUNAWAY_PCT_H = {"week": 3.0, "five_hour": 40.0}  # raw burn that alerts whatever the cause
RUNAWAY_USD_H = 60.0

# Calibration
CALIB_HOURS = {"week": 48, "five_hour": 10}   # how far back to look for readings
CALIB_MIN_POINTS = 3         # the meters move in whole percents; fewer is mostly rounding
PLAN_TTL = 240               # ask Anthropic at most this often (seconds)

# Going cold: a big chat on the 1-hour cache, idle long enough that the cache is about to expire
COLD_CTX = 200_000           # context at least this
COLD_WARN_MIN = 50           # warn once it has been idle this long (the cache lasts 60)

# How much each token type moves the plan meter, per million tokens, per model.
# Fitted against the weekly meter (python3 chatwatch.py fit): on one Max account,
# Oct 25 to Nov 23 2026, cache reads counted about 7x less than API prices say,
# Sonnet 0.6x Opus and Fable 3.15x Opus. Uncached input was too rare to measure and
# is counted like a cache write. Scaled so Opus output is $25 per million, its API
# price, so these "meter dollars" stay close to API dollars for Opus output.
WEIGHTS_FILE = os.path.join(CLAUDE, "chatwatch-weights.json")
DEFAULT_WEIGHTS = {
    "opus":   {"in": 5.37,  "out": 25.00, "cr": 0.069, "cw": 5.37},
    "sonnet": {"in": 3.17,  "out": 14.75, "cr": 0.041, "cw": 3.17},
    "fable":  {"in": 16.92, "out": 78.75, "cr": 0.217, "cw": 16.92},
    "haiku":  {"in": 1.06,  "out": 4.92,  "cr": 0.014, "cw": 1.06},   # not measured: Sonnet / 3
}
# Published API prices ($ per million), kept only to compare fits against.
API_RATES = {
    "haiku":  {"in": 1.00, "out": 5.00,  "cr": 0.10, "cw": 2.00},
    "sonnet": {"in": 3.00, "out": 15.00, "cr": 0.30, "cw": 6.00},
    "opus":   {"in": 5.00, "out": 25.00, "cr": 0.50, "cw": 10.00},
    "fable":  {"in": 10.00, "out": 50.00, "cr": 1.00, "cw": 20.00},
}


def _load_weights():
    try:
        with open(WEIGHTS_FILE, encoding="utf-8") as f:
            w = json.load(f).get("weights") or {}
        if all(set(w.get(m, {})) >= {"in", "out", "cr", "cw"} for m in DEFAULT_WEIGHTS):
            return w
    except Exception:
        pass
    return DEFAULT_WEIGHTS


RATES = _load_weights()
FABLE_X = RATES["fable"]["out"] / RATES["opus"]["out"]   # how much more a Fable token counts than an Opus one
FABLE_SAVING = 1 - 1 / FABLE_X                            # the share of a Fable step Opus would save
# calibration is in units of these weights: a new set of weights starts a new calibration
BASIS = json.dumps(RATES, sort_keys=True)
METERS = {"week": ("seven_day", "w", "wr", "weekly limit"),
          "five_hour": ("five_hour", "h", "hr", "5-hour window")}


def family(model):
    m = (model or "").lower()
    for name in ("haiku", "sonnet", "opus", "fable"):
        if name in m:
            return name
    return "fable" if "mythos" in m else "opus"


def step_cost(model, u, rates=None):
    """One step in meter dollars (see RATES). Both cache lifetimes count as one write price:
    the meter did not tell them apart."""
    r = (rates or RATES)[family(model)]
    return ((u.get("input_tokens", 0) or 0) * r["in"]
            + (u.get("output_tokens", 0) or 0) * r["out"]
            + (u.get("cache_read_input_tokens", 0) or 0) * r["cr"]
            + (u.get("cache_creation_input_tokens", 0) or 0) * r["cw"]) / 1e6


def _ts(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except Exception:
        return 0.0


def _when(ts):
    d = datetime.fromtimestamp(ts)
    return f"{d:%a} {d.hour % 12 or 12}:{d:%M} {'AM' if d.hour < 12 else 'PM'}"


# ---------------------------------------------------------------- operating system
def proc_cmd(pid):
    """The command line (or on Windows the program name) of a live pid, else ''."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    try:
        if WINDOWS:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=5,
                                 creationflags=0x08000000).stdout.strip()
            return out.split('","')[0].strip('"') if out.startswith('"') else ""
        return subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                              capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return ""


def stop_pid(pid):
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=0x08000000)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(50):
        time.sleep(0.1)
        if not proc_cmd(pid):
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def copy_text(text):
    cmds = [["pbcopy"]] if MAC else [["clip"]] if WINDOWS else [["wl-copy"], ["xclip", "-selection", "clipboard"]]
    for c in cmds:
        if shutil.which(c[0]):
            subprocess.run(c, input=text, text=True)
            return True
    return False


def open_path(p):
    if WINDOWS:
        os.startfile(p)                                  # noqa: S606
    else:
        subprocess.run(["open" if MAC else "xdg-open", p])


def notify(title, text, open_url=None):
    url = open_url or f"http://127.0.0.1:{PORT}"
    try:
        if MAC:
            tn = shutil.which("terminal-notifier") or "/opt/homebrew/bin/terminal-notifier"
            if os.path.exists(tn):
                subprocess.run([tn, "-title", title, "-message", text, "-group", "chatwatch",
                                "-open", url], capture_output=True)
            else:
                esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
                subprocess.run(["osascript", "-e",
                                f'display notification "{esc(text)}" with title "{esc(title)}"'],
                               capture_output=True)
        elif WINDOWS:
            esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "''")
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
                "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
                "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
                f"$x.LoadXml('<toast activationType=\"protocol\" launch=\"{esc(url)}\"><visual><binding template=\"ToastGeneric\">"
                f"<text>{esc(title)}</text><text>{esc(text)}</text></binding></visual></toast>');"
                "$t = New-Object Windows.UI.Notifications.ToastNotification $x;"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($t)")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                           creationflags=0x08000000)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, text], capture_output=True)
    except Exception as e:
        print("notification failed:", e, file=sys.stderr)


# ---------------------------------------------------------------- transcripts
class Transcript:
    """Reads a .jsonl incrementally: only the bytes added since last time."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.steps = {}       # message id -> (ts, model, cost, tokens, context, sidechain, output,
        #                                    cache writes, uncached input, cache reads)
        self.title = ""
        self.last_prompt = ""
        self.cache_1h = None  # whether this chat writes the 1-hour or the 5-minute cache
        self.last_text = ""   # the main chat's last reply, for done_state
        self.todos = []       # its last TodoWrite list
        self.tasks = {}       # task id or subject -> status, from TaskCreate and TaskUpdate

    def update(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.offset:               # rewritten: start again
            self.__init__(self.path)
        if size == self.offset:
            return
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return
        self.offset += end + 1
        for line in data[:end].splitlines():
            if b'"usage"' not in line and b'"ai-title"' not in line and b'"last-prompt"' not in line:
                continue
            try:
                m = json.loads(line)
            except Exception:
                continue
            t = m.get("type")
            if t == "ai-title":
                self.title = m.get("aiTitle") or self.title
            elif t == "last-prompt":
                self.last_prompt = m.get("lastPrompt") or self.last_prompt
            elif t == "assistant":
                msg = m.get("message") or {}
                u = msg.get("usage")
                if not u or msg.get("model") == "<synthetic>":
                    continue
                if not m.get("isSidechain"):
                    self._read_content(msg.get("content"))
                cc = u.get("cache_creation") or {}
                if (cc.get("ephemeral_1h_input_tokens") or 0) > 0:
                    self.cache_1h = True
                elif (cc.get("ephemeral_5m_input_tokens") or 0) > 0:
                    self.cache_1h = False
                key = msg.get("id") or m.get("requestId") or m.get("uuid")
                ctx = ((u.get("input_tokens", 0) or 0) + (u.get("cache_read_input_tokens", 0) or 0)
                       + (u.get("cache_creation_input_tokens", 0) or 0))
                # one response is written as several lines; the last one wins
                self.steps[key] = (_ts(m.get("timestamp", "")), msg.get("model", ""),
                                   step_cost(msg.get("model", ""), u),
                                   ctx + (u.get("output_tokens", 0) or 0), ctx,
                                   bool(m.get("isSidechain")),
                                   u.get("output_tokens", 0) or 0,
                                   u.get("cache_creation_input_tokens", 0) or 0,
                                   u.get("input_tokens", 0) or 0,
                                   u.get("cache_read_input_tokens", 0) or 0)

    def _read_content(self, content):
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and (c.get("text") or "").strip():
                self.last_text = c["text"]
            elif c.get("type") == "tool_use":
                i, name = c.get("input") or {}, c.get("name")
                if name == "TodoWrite":
                    self.todos = i.get("todos") or []
                elif name == "TaskCreate":
                    self.tasks[i.get("subject") or c.get("id")] = "pending"
                elif name == "TaskUpdate" and i.get("status"):
                    self.tasks[i.get("taskId") or "?"] = i["status"]


# Is a chat finished? Read from its last reply, no model call. Her chats end each turn with
# "committed and pushed" or what is still pending and why, so the closing lines say it.
DONE_WAITS = re.compile(r"(waits? (for|on) your|until you (answer|reply|say|decide)|your (yes|answer|call|go-ahead)"
                        r"|tell me (which|whether|if|what)|once you|want me to|should I|shall I|do you want"
                        r"|which (one|option) do you|\?\s*$)", re.I)
DONE_LOOSE = re.compile(r"((not|n.t) (yet )?(pushed|merged|committed)|uncommitted|still (pending|to |owed|open)|pending"
                        r"|left to do|not done|blocked|owed)", re.I)
DONE_OK = re.compile(r"(committed and pushed|all done|nothing (is )?(left|pending|to commit)|nothing in the repo changed"
                     r"|that's everything|a fresh chat (would|for)|suggest a fresh chat)", re.I)
DONE_PILLS = {"done": "Done", "waiting": "Waiting on you", "loose": "Loose ends"}


def done_state(t):
    """(state, closing line): done, waiting (on her answer), loose (open to-dos or unpushed work) or unclear."""
    text = t.last_text.strip()
    lines = [l.strip(" -*#`") for l in text.splitlines() if l.strip(" -*#`")]
    line = lines[-1] if lines else ""
    end = text[-400:]
    open_items = ([x for x in t.todos if isinstance(x, dict) and x.get("status") != "completed"]
                  + [k for k, v in t.tasks.items() if v not in ("completed", "deleted")])
    if DONE_WAITS.search(end[-300:]):
        return "waiting", line
    if open_items:
        return "loose", f"{len(open_items)} open to-do{'s' if len(open_items) > 1 else ''}. " + line
    if DONE_LOOSE.search(end[-300:]):
        return "loose", line
    if DONE_OK.search(end):
        return "done", line
    return "unclear", line


_cache = {}


def transcript(path):
    t = _cache.get(path)
    if t is None:
        t = _cache[path] = Transcript(path)
    t.update()
    return t


# ---------------------------------------------------------------- live chats
def live_sessions():
    out = []
    for f in glob.glob(os.path.join(SESSIONS, "*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                s = json.load(fh)
        except Exception:
            continue
        cmd = proc_cmd(s.get("pid"))
        # Windows only gives the program name, and an npm install runs as node.exe
        ok = ("claude" in cmd.lower() or (WINDOWS and cmd.lower() == "node.exe")
              or os.environ.get("CHATWATCH_SESSIONS"))
        if not cmd or not ok:
            continue
        s["_cmd"] = cmd
        out.append(s)
    return out


def chats():
    now = time.time()
    rows = []
    for s in live_sessions():
        sid = s.get("sessionId", "")
        main = glob.glob(os.path.join(PROJECTS, "*", sid + ".jsonl"))
        if not main:
            continue
        mt = transcript(main[0])
        subs = glob.glob(os.path.join(os.path.dirname(main[0]), sid, "subagents", "*.jsonl"))
        ctx, model, last_ts = 0, "", 0.0
        main_steps = sorted(v for v in mt.steps.values() if not v[5])
        if main_steps:
            last_ts, model, _, _, ctx = main_steps[-1][:5]
        hour = day = total = recent = 0.0
        hour_tok = nsteps = subs_active = 0
        for i, t in enumerate([mt] + [transcript(p) for p in subs]):
            active = False
            for ts, _, c, tok, *_ in t.steps.values():
                total += c
                nsteps += 1
                if now - ts < 3600:
                    hour += c
                    hour_tok += tok
                if now - ts < 86400:
                    day += c
                if now - ts < BURN_WINDOW_MIN * 60:
                    recent += c
                    active = True
            if i > 0 and active:
                subs_active += 1
        # how much the context grew over the last CTX_JUMP_MIN minutes
        before = [v[4] for v in main_steps if v[0] <= now - CTX_JUMP_MIN * 60]
        ctx_growth = ctx - before[-1] if before else 0
        waste = waste_of([v for t in [mt] + [transcript(p) for p in subs]
                          for v in t.steps.values() if now - v[0] < WASTE_WINDOW_MIN * 60])
        busy = s.get("status") == "busy"
        idle_min = 0 if busy else max(0, (now - max(last_ts, (s.get("statusUpdatedAt") or 0) / 1000)) / 60)
        ttl_min = 60 if mt.cache_1h else 5
        since_step = (now - last_ts) / 60 if last_ts else 0
        warm = since_step < ttl_min
        r = RATES[family(model)]
        wake = ctx * (r["cr"] if warm else r["cw"]) / 1e6
        cold_wake = ctx * r["cw"] / 1e6           # the next step once the cache has expired
        cwd = s.get("cwd", "")
        state, closing = done_state(mt)
        rows.append({
            "state": state, "closing": closing[:300],
            "pid": s.get("pid"), "sid": sid, "cwd": cwd,
            # VS Code's Claude extension opens (or brings forward) a chat from this link
            "jump": f"vscode://anthropic.claude-code/open?session={sid}" if s.get("entrypoint") == "claude-vscode" else "",
            "folder": os.path.basename(cwd.rstrip("/\\")) or "~",
            "title": mt.title or (mt.last_prompt[:70] if mt.last_prompt else s.get("name") or sid[:8]),
            "model": model, "family": family(model) if model else "",
            "busy": busy, "idle_min": round(idle_min), "ctx": ctx,
            "hour_usd": round(hour, 4), "hour_tok": hour_tok, "day_usd": round(day, 4),
            "total_usd": round(total, 2), "steps": nsteps, "subagents": len(subs),
            "cache_warm": warm, "cache_min": ttl_min, "wake_usd": round(wake, 4),
            "cache_1h": bool(mt.cache_1h), "since_step_min": round(since_step, 1), "last_step": last_ts,
            "cold_wake_usd": round(cold_wake, 4),
            # usage per hour at the pace of the last BURN_WINDOW_MIN minutes
            "rate_usd_h": round(recent * 60 / BURN_WINDOW_MIN, 4),
            "subs_active": subs_active, "ctx_growth": ctx_growth, "waste": waste,
            "waste_usd_h": round(waste["total"] * 60 / WASTE_WINDOW_MIN, 4),
            "started": (s.get("startedAt") or 0) / 1000, "entry": s.get("entrypoint", ""),
            "_cmd": s.get("_cmd", ""),
        })
    hour_all = sum(r["hour_usd"] for r in rows) or 1
    unit, k = share_unit()
    for r in rows:
        r["hour_share"] = round(100 * r["hour_usd"] / hour_all)
        r["unit"] = unit
        r["hour_pct"] = round(r["hour_usd"] * k, 4) if k else None
        r["day_pct"] = round(r["day_usd"] * k, 3) if k else None
        r["wake_pct"] = round(r["wake_usd"] * k, 4) if k else None
        r["cold_wake_pct"] = round(r["cold_wake_usd"] * k, 4) if k else None
        r["rate_pct_h"] = round(r["rate_usd_h"] * k, 3) if k else None
        r["waste_pct_h"] = round(r["waste_usd_h"] * k, 3) if k else None
        r["advice"] = advice(r)
        c = causes(r) if r["waste"]["share"] >= 0.3 and r["waste_usd_h"] >= 2 else []
        r["cause"] = c[0] if c else None
    rows.sort(key=lambda r: (-r["waste_usd_h"], -r["rate_usd_h"], -r["hour_usd"]))
    return rows


def waste_of(steps):
    """What better token handling would have saved, over a set of steps, priced at
    the fitted meter weights (RATES):
    - context: every token re-read beyond LEAN_CTX (what a fresh chat with a
      handoff carries), priced as a cache read;
    - rebuild: cache writes past REBUILD_FLOOR in one step mean the cache was
      rebuilt (idle past its lifetime, or compacted), priced as write minus read;
    - fable: what is left on Fable steps, less what Opus would have counted.
    Returns meter dollars per cause, the total, and the share of all usage."""
    cost = ctx_w = reb_w = fab_w = sub_w = 0.0
    rebuilds = ctx_sum = n = sub_sum = sub_n = 0
    for v in steps:
        ts, model, c, tok, ctx, side, out, writes = (list(v) + [0, 0])[:8]
        r = RATES[family(model)]
        cost += c
        n += 1
        ctx_sum += ctx
        cw = max(0.0, ctx - LEAN_CTX) * r["cr"] / 1e6
        rw = 0.0
        if writes > REBUILD_FLOOR:
            rebuilds += 1
            rw = (writes - REBUILD_FLOOR) * (r["cw"] - r["cr"]) / 1e6
        cw, rw = min(cw, c), min(rw, max(0.0, c - cw))
        fw = (c - cw - rw) * FABLE_SAVING if family(model) == "fable" else 0.0
        ctx_w += cw
        if side:                      # a subagent's own context, which a fresh main chat would not shrink
            sub_w += cw
            sub_sum += ctx
            sub_n += 1
        reb_w += rw
        fab_w += fw
    total = ctx_w + reb_w + fab_w
    return {"context": round(ctx_w, 4), "rebuild": round(reb_w, 4), "fable": round(fab_w, 4),
            "total": round(total, 4), "share": round(total / cost, 3) if cost else 0.0,
            "rebuilds": rebuilds, "avg_ctx": int(ctx_sum / n) if n else 0, "steps": n,
            "sub_context": round(sub_w, 4), "sub_avg_ctx": int(sub_sum / sub_n) if sub_n else 0,
            "main_avg_ctx": int((ctx_sum - sub_sum) / (n - sub_n)) if n > sub_n else 0}


# ---------------------------------------------------------------- the plan's meters
def plan_name():
    """'Pro', 'Max 5x', 'Max 20x'... from Claude Code's login; only the plan fields are read."""
    raw = None
    if MAC:
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                               capture_output=True, text=True, timeout=10)
            raw = r.stdout if r.returncode == 0 else None
        except Exception:
            pass
    if raw is None:
        try:
            with open(os.path.join(CLAUDE, ".credentials.json"), encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            return ""
    try:
        o = (json.loads(raw) or {}).get("claudeAiOauth") or {}
    except ValueError:
        return ""
    sub, tier = (o.get("subscriptionType") or "").lower(), (o.get("rateLimitTier") or "").lower()
    m = re.search(r"(\d+)x", tier)
    if sub == "max":
        # renamed on 30 Oct 2026: Max 20x is Ultra, Max 5x is Max
        return "Ultra" if m and m.group(1) == "20" else "Max"
    return sub.capitalize()


def _token():
    """(token, why-not). Claude Code keeps its login in the macOS Keychain, or
    in ~/.claude/.credentials.json on Windows and Linux."""
    raw = None
    if MAC:
        try:
            r = subprocess.run(["security", "find-generic-password", "-s",
                                "Claude Code-credentials", "-w"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                raw = r.stdout
        except Exception:
            pass
    if raw is None:
        try:
            with open(os.path.join(CLAUDE, ".credentials.json"), encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            return None, "Claude Code is not signed in on this computer"
    try:
        o = (json.loads(raw) or {}).get("claudeAiOauth") or {}
    except ValueError:
        return None, "the Claude Code login could not be read"
    tok, exp = o.get("accessToken"), o.get("expiresAt")
    if not tok:
        return None, "Claude Code is signed in with an API key, which has no plan limits to show"
    try:
        if exp and float(exp) / 1000 < time.time():
            return None, "the login has expired until Claude Code next runs"
    except (TypeError, ValueError):
        pass
    return tok, ""


def _meter(d):
    if not isinstance(d, dict) or d.get("utilization") is None:
        return None
    try:
        return {"pct": round(float(d["utilization"]), 1), "resets": d.get("resets_at") or ""}
    except (TypeError, ValueError):
        return None


def _read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def plan_history():
    rows = []
    try:
        with open(PLAN_HISTORY, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("at"):
                    rows.append(r)
    except OSError:
        pass
    return rows


def _remember(out):
    line = json.dumps({"at": round(out["at"]),
                       "w": (out.get("week") or {}).get("pct"),
                       "wr": (out.get("week") or {}).get("resets") or "",
                       "h": (out.get("five_hour") or {}).get("pct"),
                       "hr": (out.get("five_hour") or {}).get("resets") or ""})
    with open(PLAN_HISTORY, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if os.path.getsize(PLAN_HISTORY) > 400_000:
        keep = [r for r in plan_history() if r["at"] > time.time() - 8 * 86400]
        with open(PLAN_HISTORY + ".tmp", "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in keep))
        os.replace(PLAN_HISTORY + ".tmp", PLAN_HISTORY)


def same_window(a, b):
    """Two reset times name the same window (they wobble by a second)."""
    ta, tb = _ts(a), _ts(b)
    return bool(ta and tb) and abs(ta - tb) < 600


def plan_status(force=False):
    """{"at", "week", "five_hour", "why"}; each meter is {"pct", "resets"} or None."""
    cached = _read_json(PLAN_CACHE)
    if not force and cached and time.time() - float(cached.get("at") or 0) < PLAN_TTL:
        return dict(cached, why="")
    tok, why = _token()
    data = None
    if tok:
        req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": "Bearer " + tok, "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json", "User-Agent": "chatwatch"})
        tok = None
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.load(r)
        except Exception as exc:
            why = "Anthropic did not answer" + (f" ({exc.code})" if hasattr(exc, "code") else "")
    if isinstance(data, dict):
        scoped = []
        for l in data.get("limits") or []:
            if not isinstance(l, dict) or l.get("kind") != "weekly_scoped":
                continue
            sc = (l.get("scope") or {})
            name = ((sc.get("model") or {}).get("display_name") or (sc.get("surface") or {}).get("display_name")
                    if isinstance(sc.get("surface"), dict) else (sc.get("model") or {}).get("display_name"))
            if name and l.get("percent") is not None:
                scoped.append({"label": name, "pct": float(l["percent"]), "resets": _ts(l.get("resets_at")) or None})
        out = {"at": time.time(), "week": _meter(data.get("seven_day")),
               "five_hour": _meter(data.get("five_hour")), "scoped": scoped, "plan": plan_name()}
        try:
            with open(PLAN_CACHE, "w", encoding="utf-8") as f:
                json.dump(out, f)
            _remember(out)
        except OSError:
            pass
        return dict(out, why="")
    if cached:
        return dict(cached, why=why)
    return {"at": 0, "week": None, "five_hour": None, "why": why}


def all_steps(since):
    """(ts, cost) for every step in every transcript touched since `since`,
    open chats or not: a closed chat moved the meter too."""
    out = []
    for pat in (os.path.join(PROJECTS, "*", "*.jsonl"),
                os.path.join(PROJECTS, "*", "*", "subagents", "*.jsonl")):
        for p in glob.glob(pat):
            try:
                if os.path.getmtime(p) < since:
                    continue
            except OSError:
                continue
            for ts, _, c, *_ in transcript(p).steps.values():
                if ts >= since:
                    out.append((ts, c))
    for p in [p for p in _cache if not os.path.exists(p) or os.path.getmtime(p) < since - 86400]:
        _cache.pop(p, None)
    return sorted(out)


def calib_read():
    """The saved calibration, keeping only meters measured with the current weights."""
    cal = _read_json(CALIB) or {}
    return {m: v for m, v in cal.items() if isinstance(v, dict) and v.get("basis") == BASIS}


def calibrate():
    """For each meter: percent of the limit per API-dollar, from how far the
    meter moved against how much every chat used over the same spans."""
    now = time.time()
    hist = plan_history()
    old = calib_read()
    out = {}
    for meter, (_, key, rkey, _) in METERS.items():
        h = [r for r in hist if r.get(key) is not None and r["at"] > now - CALIB_HOURS[meter] * 3600]
        res = None
        if len(h) >= 2:
            steps = all_steps(h[0]["at"])
            times = [t for t, _ in steps]
            cum = [0.0]
            for _, c in steps:
                cum.append(cum[-1] + c)
            used = lambda a, b: cum[bisect.bisect_right(times, b)] - cum[bisect.bisect_right(times, a)]
            dpct = dusd = 0.0
            for a, b in zip(h, h[1:]):
                if same_window(a.get(rkey), b.get(rkey)) and b[key] >= a[key]:
                    dpct += b[key] - a[key]
                    dusd += used(a["at"], b["at"])
            if dpct >= CALIB_MIN_POINTS and dusd > 0:
                res = {"at": now, "pct_per_usd": dpct / dusd, "points": dpct, "usd": round(dusd, 2), "basis": BASIS,
                       "hours": round((h[-1]["at"] - h[0]["at"]) / 3600, 1)}
        # keep the last good figure while there is too little movement for a new one
        out[meter] = res or old.get(meter)
    with open(CALIB, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return out


# ---------------------------------------------------------------- fitting the weights
FIT_BLOCK_H = 3              # meter readings are compared this many hours apart
FIT_FAMS = ("opus", "sonnet", "fable")
FIT_TYPES = ("cr", "cw", "out")


def _solve(G, b):
    """Solve G x = b for a small square system (Gaussian elimination); None if singular."""
    n = len(b)
    M = [list(G[i]) + [b[i]] for i in range(n)]
    for c in range(n):
        p = max(range(c, n), key=lambda i: abs(M[i][c]))
        if abs(M[p][c]) < 1e-12:
            return None
        M[c], M[p] = M[p], M[c]
        for i in range(n):
            if i != c:
                f = M[i][c] / M[c][c]
                M[i] = [a - f * bb for a, bb in zip(M[i], M[c])]
    return [M[i][n] / M[i][i] for i in range(n)]


def _nnls3(G, b, yy):
    """Non-negative least squares on 3 unknowns from the normal equations,
    by trying every set of free unknowns. Returns (x, squared error)."""
    best = ([0.0, 0.0, 0.0], yy)
    for mask in range(1, 8):
        idx = [i for i in range(3) if mask >> i & 1]
        x = _solve([[G[i][j] for j in idx] for i in idx], [b[i] for i in idx])
        if x is None or min(x) < 0:
            continue
        full = [0.0] * 3
        for i, v in zip(idx, x):
            full[i] = v
        sse = yy - 2 * sum(full[i] * b[i] for i in range(3)) + sum(
            full[i] * G[i][j] * full[j] for i in range(3) for j in range(3))
        if sse < best[1]:
            best = (full, sse)
    return best


def _fit_factorised(blocks):
    """Weights w(model, type) = m_model * r_type with m_opus = 1, fitted to the
    meter by least squares, all non-negative. blocks: [(features, moved)], where
    features[f][t] is millions of tokens. Returns ((r_cr, r_cw, r_out), {model: m}, sse)."""
    F = [(f, t) for f in FIT_FAMS for t in FIT_TYPES]
    G9 = [[sum(x[a[0]][a[1]] * x[c[0]][c[1]] for x, _ in blocks) for c in F] for a in F]
    b9 = [sum(x[a[0]][a[1]] * y for x, y in blocks) for a in F]
    yy = sum(y * y for _, y in blocks)

    def at(ms, mf):
        m = {"opus": 1.0, "sonnet": ms, "fable": mf}
        G = [[sum(m[F[i][0]] * m[F[j][0]] * G9[i][j] for i in range(9) if F[i][1] == t
                  for j in range(9) if F[j][1] == s) for s in FIT_TYPES] for t in FIT_TYPES]
        b = [sum(m[F[i][0]] * b9[i] for i in range(9) if F[i][1] == t) for t in FIT_TYPES]
        x, sse = _nnls3(G, b, yy)
        return sse, x, m

    best = min((at(ms / 10, mf / 4) for ms in range(1, 21) for mf in range(2, 41)), key=lambda z: z[0])
    for step in (0.02, 0.005):         # refine around the best grid point
        ms0, mf0 = best[2]["sonnet"], best[2]["fable"]
        best = min((at(max(0.0, ms0 + i * step), max(0.0, mf0 + j * step * 2.5))
                    for i in range(-5, 6) for j in range(-5, 6)), key=lambda z: z[0])
    sse, x, m = best
    return tuple(x), m, sse


def fit(save=False, quiet=False):
    """Fit how much each token type and model moves the weekly meter, from the
    meter readings and every transcript, and print how good and how stable it is."""
    now = time.time()
    s = _read_json(os.path.join(CLAUDE, "settings.json")) or {}
    keep_days = int(s.get("cleanupPeriodDays") or 30)
    # older transcripts have been deleted by Claude Code, so older readings cannot be explained
    since = now - (keep_days - 1) * 86400
    hist = [r for r in plan_history() if r.get("w") is not None and r["at"] > since]
    steps = {}
    for pat in (os.path.join(PROJECTS, "*", "*.jsonl"),
                os.path.join(PROJECTS, "*", "*", "subagents", "*.jsonl")):
        for p in glob.glob(pat):
            try:
                if os.path.getmtime(p) < since:
                    continue
            except OSError:
                continue
            for key, v in transcript(p).steps.items():
                if v[0] >= since:
                    steps[key] = v          # a forked chat repeats its parent's steps: count once
    rows = sorted(steps.values())
    times = [v[0] for v in rows]
    F = [(f, t) for f in FIT_FAMS for t in FIT_TYPES]
    cum = [[0.0] * len(F)]
    for v in rows:
        f = family(v[1])
        scale = 1.0
        if f == "haiku":
            f, scale = "sonnet", 1 / 3
        out_t, writes, inp, cr = v[6], v[7], v[8], v[9]
        x = {"cr": cr, "cw": writes + inp, "out": out_t}
        c = list(cum[-1])
        for i, (ff, t) in enumerate(F):
            if ff == f:
                c[i] += x[t] * scale / 1e6
        cum.append(c)
    api = [0.0]
    for v in rows:                     # the same steps at API prices, to compare against
        u = {"input_tokens": v[8], "output_tokens": v[6], "cache_read_input_tokens": v[9],
             "cache_creation_input_tokens": v[7]}
        api.append(api[-1] + step_cost(v[1], u, API_RATES))

    def used(a, b):
        i, j = bisect.bisect_right(times, a), bisect.bisect_right(times, b)
        return [cj - ci for ci, cj in zip(cum[i], cum[j])], api[j] - api[i]

    windows = {}
    for r in hist:
        windows.setdefault(round(_ts(r["wr"]) / 3600), []).append(r)
    blocks = []                        # (day, features, api dollars, points moved)
    for hs in windows.values():
        hs.sort(key=lambda r: r["at"])
        i = 0
        while i < len(hs) - 1:
            j = i + 1
            while j < len(hs) and hs[j]["at"] - hs[i]["at"] < FIT_BLOCK_H * 3600:
                j += 1
            if j >= len(hs):
                break
            a, b = hs[i], hs[j]
            if b["w"] >= a["w"]:
                vec, usd = used(a["at"], b["at"])
                feats = {f: {t: vec[k] for k, (ff, t) in enumerate(F) if ff == f} for f in FIT_FAMS}
                blocks.append((datetime.fromtimestamp(a["at"]).strftime("%m-%d"), feats, usd, b["w"] - a["w"]))
            i = j
    if len(blocks) < 10:
        print(f"Only {len(blocks)} stretches of {FIT_BLOCK_H} h with meter readings: too few to fit. "
              "Leave the service running for a few days.")
        return None

    def pred(r, m, x):
        return sum(m[f] * r[k] * x[f][t] for f in FIT_FAMS for k, t in enumerate(FIT_TYPES))

    def fit_on(bs):
        r, m, _ = _fit_factorised([(x, y) for _, x, _, y in bs])
        return r, m

    def api_k(bs):
        zz = sum(u * u for _, _, u, _ in bs)
        return sum(u * y for _, _, u, y in bs) / zz if zz else 0.0

    ys = [y for *_, y in blocks]
    mean = sum(ys) / len(ys)
    sst = sum((y - mean) ** 2 for y in ys)
    r, m = fit_on(blocks)
    k = api_k(blocks)
    res = [y - pred(r, m, x) for _, x, _, y in blocks]
    res_api = [y - k * u for _, _, u, y in blocks]
    days = sorted({d for d, *_ in blocks})
    loo, loo_api = [], []              # leave one day out: fit on the others, predict it
    for d in days:
        tr = [b for b in blocks if b[0] != d]
        rr, mm = fit_on(tr)
        kk = api_k(tr)
        for dd, x, u, y in blocks:
            if dd == d:
                loo.append(y - pred(rr, mm, x))
                loo_api.append(y - kk * u)
    import random
    rnd = random.Random(1)
    boot = []                          # resample whole days, to see how much the weights move
    for _ in range(60):
        pick = [rnd.choice(days) for _ in days]
        bs = [b for d in pick for b in blocks if b[0] == d]
        rr, mm = fit_on(bs)
        boot.append((rr[0] / rr[2] if rr[2] else 0, rr[1] / rr[2] if rr[2] else 0, mm["sonnet"], mm["fable"]))

    rms = lambda e: (sum(v * v for v in e) / len(e)) ** 0.5
    pctl = lambda vals, q: sorted(vals)[min(len(vals) - 1, int(q * len(vals)))]
    if not quiet:
        print(f"{len(blocks)} stretches of {FIT_BLOCK_H}+ h over {len(days)} days "
              f"(the last {keep_days - 1}, while transcripts are kept); the meter moved {sum(ys):.0f} points.")
        print(f"{'':32}{'R2':>6} {'error':>6} {'error on held-out days':>24}")
        print(f"{'API prices, one scale':32}{1 - sum(e * e for e in res_api) / sst:6.3f} {rms(res_api):6.2f} {rms(loo_api):24.2f}")
        print(f"{'fitted weights':32}{1 - sum(e * e for e in res) / sst:6.3f} {rms(res):6.2f} {rms(loo):24.2f}")
        print("(error in meter points per stretch; whole-percent rounding alone gives about 0.41)\n")
        print("Per token, with Opus output = 1     fitted   10th-90th pct over days   API prices")
        rows_out = [("cache read", r[0] / r[2], [b[0] for b in boot], "0.02"),
                    ("cache write (and uncached input)", r[1] / r[2], [b[1] for b in boot], "0.25 to 0.4"),
                    ("Sonnet, against Opus", m["sonnet"], [b[2] for b in boot], "0.6"),
                    ("Fable, against Opus", m["fable"], [b[3] for b in boot], "2.0")]
        for name, v, bs, a in rows_out:
            print(f"  {name:34}{v:7.4f}   {pctl(bs, .1):7.4f} .. {pctl(bs, .9):7.4f}     {a}")
    scale = 25.0 / r[2] if r[2] else 0
    weights = {}
    for f, mult in (("opus", 1.0), ("sonnet", m["sonnet"]), ("fable", m["fable"]), ("haiku", m["sonnet"] / 3)):
        cw = round(r[1] * scale * mult, 3)
        weights[f] = {"in": cw, "out": round(25.0 * mult, 3), "cr": round(r[0] * scale * mult, 4), "cw": cw}
    out = {"at": now, "weights": weights, "days": len(days), "points": sum(ys),
           "error": round(rms(res), 3), "error_held_out": round(rms(loo), 3), "error_api": round(rms(loo_api), 3)}
    if save:
        if rms(loo) >= rms(loo_api):
            print("\nNot saved: the fit does no better than API prices on held-out days.")
        else:
            with open(WEIGHTS_FILE, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=1)
            print(f"\nSaved to {WEIGHTS_FILE}. Restart chatwatch to use them; calibration starts again.")
    elif not quiet:
        print("\nRun `python3 chatwatch.py fit --save` to use these weights.")
    return out


def share_unit():
    """Which limit per-chat shares are shown against, and its pct-per-dollar:
    the calibrated meter that is closer to full."""
    cal = calib_read()
    s = _read_json(PLAN_CACHE) or {}
    best = (None, None, -1)
    for meter in ("week", "five_hour"):
        k = (cal.get(meter) or {}).get("pct_per_usd")
        m = s.get(meter) or {}
        pct = m.get("pct")
        if _ts(m.get("resets")) and _ts(m.get("resets")) < time.time():
            pct = 0.0
        if k and pct is not None and pct > best[2]:
            best = (meter, k, pct)
    return best[0], best[1]


def plan_summary(rows, force=False):
    """Each meter, its reset, the last hour's share, and when that pace reaches 100%."""
    s = plan_status(force=force) or {}
    cal = calib_read()
    hour_usd = sum(r["hour_usd"] for r in rows)
    unit, _ = share_unit()
    out = {"why": s.get("why", ""), "read_at": s.get("at"), "unit": unit, "meters": {},
           "plan": s.get("plan", ""), "scoped": s.get("scoped") or [],
           # Pro plans always have a 5-hour window, even before it has started
           "session_expected": s.get("plan", "") == "Pro" or s.get("five_hour") is not None,
           "settings": settings_check()}
    for meter, (_, _, _, label) in METERS.items():
        m = s.get(meter)
        if not m:
            continue
        resets = _ts(m.get("resets"))
        pct = 0.0 if resets and resets < time.time() else m["pct"]
        c = cal.get(meter) or {}
        k = c.get("pct_per_usd")
        e = {"label": label, "pct": pct, "resets": resets or None, "pct_per_usd": k,
             "calib_points": c.get("points"), "calib_hours": c.get("hours"),
             "hour_pct": None, "full_at": None}
        if k:
            e["hour_pct"] = round(hour_usd * k, 2)
            if e["hour_pct"] > 0:
                e["full_at"] = time.time() + (100 - pct) / e["hour_pct"] * 3600
        out["meters"][meter] = e
    return out


# ---------------------------------------------------------------- advice
def settings_check():
    """Which of the usage settings in SETTINGS-ADVICE.md are in place, and the advice to paste."""
    s = _read_json(os.path.join(CLAUDE, "settings.json")) or {}
    env = s.get("env") or {}
    efforts = [(v or {}).get("effortLevel") for k, v in (s.get("modelSettings") or {}).items() if "opus" in k]
    try:
        compact = int(s.get("autoCompactWindow") or 0)
    except (TypeError, ValueError):
        compact = 0
    checks = [
        ("Opus at medium effort", bool(efforts) and all(e in ("low", "medium") for e in efforts)),
        ("Compact at 250K or less", 0 < compact <= 300_000),
        ("Subagents keep a 1-hour cache", s.get("subagentPromptCacheTtl") == "1h"),
        ("Unnamed subagents on Sonnet", str(env.get("CLAUDE_CODE_SUBAGENT_MODEL", "")).startswith(("sonnet", "haiku", "claude-sonnet", "claude-haiku"))),
        ("Shell output capped", bool(env.get("BASH_MAX_OUTPUT_LENGTH"))),
        ("Unused skills listed by name only", bool(s.get("skillOverrides"))),
    ]
    advice = ""
    try:
        with open(os.path.join(HERE, "SETTINGS-ADVICE.md"), encoding="utf-8") as f:
            t = f.read()
        advice = t.split("<!-- advice-start -->")[1].split("<!-- advice-end -->")[0].strip()
    except (OSError, IndexError):
        pass
    return {"checks": [{"label": l, "ok": ok} for l, ok in checks], "advice": advice}


def heavy(r):
    if r.get("hour_pct") is not None:
        return r["hour_pct"] >= HOUR_ALERT_PCT
    return r["hour_usd"] >= HOUR_ALERT_USD


def unit_label(unit):
    return METERS[unit][3] if unit in METERS else "limit"


def advice(r):
    """Plain suggestions, most useful first. Each is (kind, text)."""
    out = []
    idle_h = r["idle_min"] / 60
    if r["family"] == "fable":
        out.append(("model", "On Fable, the heaviest model. Type /model opus in this chat unless it really needs Fable."))
    if r["waste"]["share"] >= 0.3 and r["waste_usd_h"] >= 2:
        reasons = waste_reasons(r)
        if reasons:
            out.append(("fresh", reasons[0][1]))
    if r["busy"] and heavy(r):
        used = (f"{r['hour_pct']:.1f}% of your {unit_label(r['unit'])}" if r["hour_pct"] is not None
                else f"about ${r['hour_usd']:.0f} worth")
        out.append(("heavy", f"Used {used} in the last hour ({r['hour_share']}% of all open chats)."))
    if r["family"] == "opus" and r["ctx"] < 60_000 and r["steps"] > 20 and not out:
        out.append(("model", f"Small, simple work? /model sonnet counts about {round(100 * (1 - RATES['sonnet']['out'] / RATES['opus']['out']))}% less per step."))
    return out


# ---------------------------------------------------------------- terminal table
def _k(n):
    return f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.0f}K"


def _pct(n):
    return "<0.01%" if 0 < n < 0.01 else f"{n:.2f}%"


def print_table(rows):
    plan = plan_summary(rows)
    for m in plan["meters"].values():
        line = f"{m['label'].capitalize()}: {m['pct']:.0f}% used"
        if m["resets"]:
            line += f", resets {_when(m['resets'])}"
        if m["hour_pct"] is not None:
            line += f". Last hour: {m['hour_pct']:.1f}%."
            if m["full_at"] and m["resets"] and m["full_at"] < m["resets"]:
                line += f" At that pace, 100% {_when(m['full_at'])}, before the reset."
        else:
            line += ". Per-chat shares start once this meter has moved a few points."
        print(line)
    if not plan["meters"]:
        print(f"Plan limits unavailable ({plan['why'] or 'no reading yet'}). Showing meter dollars.")
    busy = sum(r["busy"] for r in rows)
    tot = sum(r["hour_usd"] for r in rows)
    print(f"{len(rows)} open chats, {busy} working. Last hour: ${tot:.2f} in meter dollars.\n")
    pct = plan["unit"] is not None
    print(f"{'last hr':>8} {'share':>5} {'context':>7} {'wake':>6} {'model':7} {'state':>9}  chat")
    for r in rows:
        state = "working" if r["busy"] else (f"idle {r['idle_min']}m" if r["idle_min"] < 120 else f"idle {r['idle_min']//60}h")
        hr = _pct(r["hour_pct"]) if pct else f"${r['hour_usd']:.2f}"
        wk = _pct(r["wake_pct"]) if pct else f"${r['wake_usd']:.2f}"
        print(f"{hr:>8} {str(r['hour_share'])+'%':>5} {_k(r['ctx']):>7} "
              f"{wk:>6} {r['family'][:7]:7} {state:>9}  {r['title'][:48]} ({r['folder']})")
        for _, text in r["advice"][:2]:
            print(f"{'':>48}-> {text}")
    if pct:
        print(f"\nlast hr and wake are shares of your {unit_label(plan['unit'])}; share is of all open chats.")


# ---------------------------------------------------------------- close + handoff
HANDOFF_PROMPT = (
    "This chat is being closed to save usage. Write a handoff note so a fresh chat can "
    "pick up the work with no other context. Use these headings: Goal; Done (commits, "
    "files, with paths); In progress (exactly where it stopped); Next steps; Open questions; "
    "Traps and gotchas learned. Plain markdown, under 350 words. "
    "Do not use any tools. Reply with the note only.")

jobs = {}   # pid -> {"state", "msg", "path"}


def _binary(cmd):
    """The same Claude Code that runs the chat when we can see it, else the one on PATH."""
    if not WINDOWS and cmd:
        first = cmd.split(" --")[0].strip()
        if os.path.exists(first) and os.path.basename(first) == "claude":
            return first
    return shutil.which("claude")


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:50] or "chat"


def write_handoff(row, timeout=600):
    exe = _binary(row["_cmd"])
    if not exe:
        raise RuntimeError("the claude command was not found on PATH (install Claude Code's CLI)")
    # subscription only: never let an API key in the environment pay for this
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE",
                        "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")}
    cmd = [exe, "-p", "--resume", row["sid"], "--fork-session", "--no-session-persistence",
           "--tools", "", "--output-format", "json"]
    if row["model"]:
        cmd += ["--model", row["model"]]
    if WINDOWS and exe.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c"] + cmd
    # the prompt goes on stdin, so no shell ever parses it
    p = subprocess.run(cmd, cwd=row["cwd"] or HOME, env=env, input=HANDOFF_PROMPT,
                       capture_output=True, text=True, timeout=timeout,
                       creationflags=0x08000000 if WINDOWS else 0)
    text = ""
    try:
        res = json.loads(p.stdout)
        text = res.get("result", "") if not res.get("is_error") else ""
    except Exception:
        pass
    if not text.strip():
        raise RuntimeError((p.stderr or p.stdout or "no output")[-400:])
    os.makedirs(HANDOFFS, exist_ok=True)
    path = os.path.join(HANDOFFS, f"{datetime.now():%Y-%m-%d-%H%M}-{_slug(row['title'])}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Handoff: {row['title']}\n\n"
                f"- Folder: {row['cwd']}\n- Closed: {datetime.now():%Y-%m-%d %H:%M}\n"
                f"- Old chat: {row['sid']} ({row['model']}, {row['ctx']//1000}K context)\n"
                f"- Reopen the old chat if needed: `claude --resume {row['sid']}` in that folder\n\n"
                f"{text.strip()}\n")
    return path


def jump_to(sid):
    """Bring a VS Code chat's tab to the front. The link goes to whichever VS Code window was
    last in front, and a window that doesn't hold the chat opens an empty new one instead, so
    first bring forward the window for the chat's folder (each window's chats share its folder)."""
    row = next((r for r in chats() if r["sid"] == sid and r.get("jump")), None)
    if not row:
        return False
    if MAC and os.path.isdir(row["cwd"]):
        subprocess.run(["open", "-a", "Visual Studio Code", row["cwd"]], timeout=10)
        time.sleep(1.5)
    open_path(row["jump"])
    return True


def close_chat(pid, handoff=True):
    row = next((r for r in chats() if r["pid"] == pid), None)
    if not row:
        jobs[pid] = {"state": "error", "msg": "That chat is no longer open."}
        return
    try:
        path = ""
        if handoff:
            jobs[pid] = {"state": "running", "msg": "Writing the handoff note..."}
            path = write_handoff(row)
        # the session file must still name this pid, or the pid was reused
        s = _read_json(os.path.join(SESSIONS, f"{pid}.json")) or {}
        if s.get("sessionId") != row["sid"]:
            raise RuntimeError("that process no longer belongs to this chat; not stopping it")
        stop_pid(pid)
        copied = path and copy_text(f"Continue the work from the handoff note at {path}. "
                                    "Read it first, then check git status before changing anything.")
        msg = "Closed."
        if path:
            msg += (" Handoff saved; the line to start a new chat is on your clipboard."
                    if copied else f" Handoff saved to {path}.")
        jobs[pid] = {"state": "done", "path": path, "msg": msg}
        notify("Chat closed", row["title"][:60])
    except Exception as e:
        jobs[pid] = {"state": "error", "msg": f"Not closed: {e}"}


# ---------------------------------------------------------------- alerts
NOTIFY_KINDS = ("waste", "runaway", "cold")       # the menu bar app adds action buttons
APP_SEEN = [0.0]                            # last time the menu bar app asked for data


def app_active():
    return time.time() - APP_SEEN[0] < 90


def waste_reasons(r):
    """[(cause, sentence)], biggest cause first, each with its share of the chat's usage."""
    w = r["waste"]
    cost = w["total"] / w["share"] if w["share"] else 0
    pc = lambda x: f"{round(100 * x / cost)}%" if cost else "some"
    out = []
    main_ctx = w["context"] - w["sub_context"]
    if main_ctx > 0:
        out.append((main_ctx, "context",
                    f"Re-reads {w['main_avg_ctx'] // 1000}K of context on every step. "
                    f"A fresh chat with a handoff would save about {pc(main_ctx)} of its usage."))
    if w["sub_context"] > 0:
        out.append((w["sub_context"], "subagents",
                    f"Its subagents re-read {w['sub_avg_ctx'] // 1000}K each per step: "
                    f"{pc(w['sub_context'])} of its usage. Narrower subagent tasks, or fewer files per subagent, would cut it."))
    if w["rebuild"] > 0:
        times = "once" if w["rebuilds"] == 1 else f"{w['rebuilds']} times"
        out.append((w["rebuild"], "rebuild",
                    f"Rebuilt its whole cache {times} in {WASTE_WINDOW_MIN} min (after sitting idle, or compacting): "
                    f"{pc(w['rebuild'])} of its usage."))
    if w["fable"] > 0:
        out.append((w["fable"], "fable", f"On Fable, which counts {FABLE_X:.1f}x Opus per token: Opus would save {pc(w['fable'])} of its usage."))
    out.sort(key=lambda x: -x[0])
    # a cause under a tenth of the chat's usage is not worth reading
    reasons = [(c, t) for amt, c, t in out if not cost or amt / cost >= 0.10]
    if r["ctx_growth"] >= CTX_JUMP and r["ctx"] >= CTX_JUMP_FLOOR:
        reasons.insert(0, ("jump", f"Context grew {r['ctx_growth'] // 1000}K in {CTX_JUMP_MIN} min: "
                                   "large tool outputs or file reads are filling it."))
    return reasons


HANDOFF_HEADINGS = ("Goal; Done (commits, files, with paths); In progress (exactly where it stopped); "
                    "Next steps; Open questions; Traps and gotchas learned")
# What to paste into the chat for each cause: a short message the chat acts on itself
FIXES = {
    "context": ("/compact Keep the goal, the decisions made, the files changed and exactly where the "
                "work stopped. Drop old tool output and file contents."),
    "subagents": ("Your subagents each re-read a lot of context. From now on, give every subagent a narrow "
                  "brief: one question, the exact files or folders to look in, and an answer under 200 words. "
                  "Have them read files with offset and limit, not whole, and run one focused subagent rather "
                  "than several that overlap."),
    "jump": ("Your context is filling fast with large tool output. From here on, read files with offset and "
             "limit around the part you need, cap shell output with head or tail, and use grep -l or grep -c "
             "before printing whole matches."),
    "fable": "/model opus",
    "rebuild": ("Write a handoff note for a fresh chat now, under these headings: " + HANDOFF_HEADINGS +
                ". Save it in ~/.claude/handoffs/ and give me the path. After a break I will start a new chat "
                "from it instead of resuming this one, since resuming rebuilds your whole cache."),
    "cold": ("Write a handoff note for a fresh chat now, before your cache expires, under these headings: " +
             HANDOFF_HEADINGS + ". Save it in ~/.claude/handoffs/ and give me the path, then stop. "
             "I will continue in a new chat instead of resuming this one."),
}


def causes(r):
    """Short pills for a chat's waste, with the full sentence for the details view and
    the message to paste into the chat: [{kind, pill, text, fix}], biggest first."""
    w = r["waste"]
    pills = {"context": f"Context {w['main_avg_ctx'] // 1000}K",
             "subagents": f"Subagents {w['sub_avg_ctx'] // 1000}K each",
             "rebuild": f"Cache rebuilt x{w['rebuilds']}",
             "fable": "Fable",
             "jump": f"+{r['ctx_growth'] // 1000}K in {CTX_JUMP_MIN} min"}
    return [{"kind": k, "pill": pills.get(k, k), "text": t, "fix": FIXES.get(k, "")} for k, t in waste_reasons(r)]


def going_cold(r):
    """A big chat on the 1-hour cache whose cache is about to expire: the alert, or None."""
    if r["busy"] or not r.get("cache_1h") or r["ctx"] < COLD_CTX:
        return None
    since = r.get("since_step_min") or 0
    if not COLD_WARN_MIN <= since < r["cache_min"]:
        return None
    left = max(1, round(r["cache_min"] - since))
    if r.get("cold_wake_pct") is not None:
        cold, warm = f"{r['cold_wake_pct']:.2f}% of your {unit_label(r['unit'])}", f"{r['wake_pct']:.3f}%"
    else:
        cold, warm = f"${r['cold_wake_usd']:.2f}", f"${r['wake_usd']:.3f}"
    done = r.get("state") == "done"
    text = (f"Idle {int(since)} min, holding {r['ctx'] // 1000}K. Its cache expires in {left} min, and after "
            f"that the next message re-pays the whole context: about {cold}, against {warm} while it is warm. "
            + ("It looks finished, so letting it go cold costs nothing." if done else "A handoff now is still cheap."))
    pills = [{"kind": "cold", "pill": f"Cold in {left} min", "text": text, "fix": "" if done else FIXES["cold"]}]
    if r.get("state") in DONE_PILLS:
        pills.append({"kind": "state-" + r["state"], "pill": DONE_PILLS[r["state"]],
                      "text": "Its last reply ends: " + (r.get("closing") or "(no text)"), "fix": ""})
    return {"id": f"{r['sid']}:cold:{int(r.get('last_step') or 0)}", "sid": r["sid"], "pid": r["pid"],
            "kind": "cold", "title": r["title"], "text": text, "reasons": [text],
            "causes": pills, "state": r.get("state"), "closing": r.get("closing", ""),
            "fix": "handoff", "cold_in_min": left, "cold_cost_pct": r.get("cold_wake_pct"),
            "cold_cost_usd": r["cold_wake_usd"], "waste_pct_h": None, "waste_usd_h": 0.0, "share": 0.0,
            "family": r["family"], "busy": False, "subs_active": r["subs_active"], "notify": not done,
            "jump": r.get("jump", "")}


def live_alerts(rows):
    """Chats burning a lot for bad reasons, snoozed ones left out, worst first:
    [{id, sid, pid, kind, title, text, reasons, fix, family, busy, notify}].
    "waste": what better token handling would save is past WASTE_PCT_H.
    "runaway": raw burn past RUNAWAY_PCT_H, whatever the cause.
    "cold": a big idle chat whose 1-hour cache expires within minutes (see going_cold)."""
    st = _read_json(STATE) or {}
    now = time.time()
    out = []
    for r in rows:
        cold = going_cold(r)
        if cold and st.get("snooze:" + cold["id"], 0) <= now:
            out.append(cold)
        unit = r["unit"]
        if r.get("waste_pct_h") is not None:
            wasteful = r["waste_pct_h"] >= WASTE_PCT_H.get(unit, 0.4)
            runaway = r["rate_pct_h"] >= RUNAWAY_PCT_H.get(unit, 3.0)
            rate = f"{r['waste_pct_h']:.1f}% of your {unit_label(unit)} per hour"
            burn = f"{r['rate_pct_h']:.1f}% of your {unit_label(unit)} per hour"
        else:
            wasteful = r["waste_usd_h"] >= WASTE_USD_H
            runaway = r["rate_usd_h"] >= RUNAWAY_USD_H
            rate = f"${r['waste_usd_h']:.0f} an hour in meter dollars"
            burn = f"${r['rate_usd_h']:.0f} an hour in meter dollars"
        if not (wasteful or runaway):
            continue
        kind = "waste" if wasteful else "runaway"
        aid = f"{r['sid']}:{kind}"
        if st.get("snooze:" + aid, 0) > now:
            continue
        reasons = waste_reasons(r)
        subs = f" with {r['subs_active']} subagents" if r["subs_active"] else ""
        text = (f"Wasting {rate} ({round(100 * r['waste']['share'])}% of what it uses)." if wasteful
                else f"Burning {burn}{subs}, mostly on real work.")
        top = reasons[0][0] if reasons else ""
        fix = ("model" if top == "fable" else "close" if top == "rebuild" and not r["busy"]
               else "wait" if top == "subagents" else "handoff")
        out.append({"id": aid, "sid": r["sid"], "pid": r["pid"], "kind": kind, "title": r["title"],
                    "text": text, "reasons": [t for _, t in reasons[:3]], "causes": causes(r)[:3], "fix": fix,
                    "waste_pct_h": r.get("waste_pct_h"), "waste_usd_h": r["waste_usd_h"],
                    "share": r["waste"]["share"],
                    "family": r["family"], "busy": r["busy"], "subs_active": r["subs_active"],
                    "notify": kind in NOTIFY_KINDS})
    # going cold first: it only helps in the next few minutes
    out.sort(key=lambda a: (a["kind"] != "cold", -next(r["waste_usd_h"] for r in rows if r["sid"] == a["sid"])))
    return out


def snooze(aid, hours):
    st = _read_json(STATE) or {}
    st["snooze:" + aid] = time.time() + hours * 3600
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f)


def check(rows=None, send=True):
    rows = chats() if rows is None else rows
    st = _read_json(STATE) or {}
    now = time.time()
    fresh = []

    def due(key):
        return now - st.get(key, 0) > REALERT_HOURS * 3600

    # the fast alerts; the menu bar app delivers these itself, with buttons, while it runs
    if not app_active():
        for a in live_alerts(rows):
            if a["notify"] and due(a["id"]):
                fresh.append((a["id"], f"{a['title'][:40]}: {a['text']}"))
    plan = plan_summary(rows)
    for meter, m in plan["meters"].items():
        if m["full_at"] and m["resets"] and m["full_at"] < m["resets"] and due("pace:" + meter):
            fresh.append(("pace:" + meter, f"{m['label'].capitalize()} at {m['pct']:.0f}%: at this hour's pace "
                                           f"you reach it {_when(m['full_at'])}, before it resets {_when(m['resets'])}"))
    idle = [r for r in rows if not r["busy"] and r["idle_min"] >= IDLE_PILE_HOURS * 60]
    if len(idle) >= IDLE_PILE_COUNT and due("pile"):
        fresh.append(("pile", f"{len(idle)} chats idle {IDLE_PILE_HOURS} h or more"))
    if fresh and send:
        head = f"{len(fresh)} thing{'s' if len(fresh) > 1 else ''} worth a look"
        notify(head, "; ".join(t for _, t in fresh[:3]) + (" and more" if len(fresh) > 3 else ""))
        for key, _ in fresh:
            st[key] = now
        live = {r["sid"] for r in rows}
        # a snooze pressed while this check ran was written after `st` was read: keep it
        for key, v in (_read_json(STATE) or {}).items():
            if key.startswith("snooze:"):
                st[key] = max(v, st.get(key, 0))
        st = {key: v for key, v in st.items()
              if key == "pile" or key.startswith(("pace:", "snooze:")) or key.split(":")[0] in live}
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    return fresh


# ---------------------------------------------------------------- server
TOKEN = secrets.token_urlsafe(24)
_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _local(self):
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._local():
            return self._send(403, "{}")
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "page.html"), encoding="utf-8") as f:
                return self._send(200, f.read().replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
        if self.path == "/api/chats":
            # the app delivers the fast alerts only while macOS lets it notify;
            # otherwise the server keeps sending them itself
            if self.headers.get("X-Chatwatch-App") and self.headers.get("X-Chatwatch-Notify") != "0":
                APP_SEEN[0] = time.time()
            with _lock:
                rows = chats()
                plan = plan_summary(rows)
                alerts = live_alerts(rows)
            for r in rows:
                r.pop("_cmd", None)
                r["job"] = jobs.get(r["pid"])
            done = {pid: j for pid, j in jobs.items() if not any(r["pid"] == pid for r in rows)}
            return self._send(200, json.dumps({
                "chats": rows, "closed": done, "now": time.time(), "plan": plan, "alerts": alerts,
                "thresholds": {"ctx": CTX_ALERT, "hour": HOUR_ALERT_USD, "hour_pct": HOUR_ALERT_PCT}}))
        self._send(404, "{}")

    def do_POST(self):
        origin = self.headers.get("Origin") or ""
        if (not self._local() or self.headers.get("X-Token") != TOKEN
                or (origin and not re.match(rf"^http://(127\.0\.0\.1|localhost):{PORT}$", origin))):
            return self._send(403, '{"error":"forbidden"}')
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, "{}")
        if self.path == "/api/close":
            pid = int(body.get("pid", 0))
            if jobs.get(pid, {}).get("state") == "running":
                return self._send(409, '{"error":"already closing"}')
            jobs[pid] = {"state": "running", "msg": "Starting..."}
            threading.Thread(target=close_chat, args=(pid, bool(body.get("handoff", True))),
                             daemon=True).start()
            return self._send(200, '{"ok":true}')
        if self.path == "/api/jump":
            sid = str(body.get("sid", ""))
            threading.Thread(target=jump_to, args=(sid,), daemon=True).start()
            return self._send(200, '{"ok":true}')
        if self.path == "/api/snooze":
            snooze(str(body.get("id", "")), float(body.get("hours", 1)))
            return self._send(200, '{"ok":true}')
        if self.path == "/api/open":
            p = os.path.abspath(body.get("path", ""))
            if p.startswith(os.path.abspath(HANDOFFS) + os.sep) and os.path.exists(p):
                open_path(p)
                return self._send(200, '{"ok":true}')
            return self._send(400, "{}")
        self._send(404, "{}")


def alert_loop():
    n = 0
    while True:
        try:
            if n % 5 == 0:                # every 5 minutes: a fresh meter reading and calibration
                plan_status(force=True)
                with _lock:
                    calibrate()
            with _lock:                   # every minute: the fast alerts
                rows = chats()
            check(rows)
        except Exception as e:
            print("alert check failed:", e, file=sys.stderr, flush=True)
        n += 1
        time.sleep(60)


def serve():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(f"Port {PORT} is taken. Is chatwatch already running? "
                 f"Open http://127.0.0.1:{PORT}, or set CHATWATCH_PORT to another number.")
    # the menu bar app reads the key from here (readable by this user only)
    tok = os.path.join(CLAUDE, "chatwatch-token")
    with open(tok, "w", encoding="utf-8") as f:
        f.write(TOKEN)
    if not WINDOWS:
        os.chmod(tok, 0o600)
    threading.Thread(target=alert_loop, daemon=True).start()
    print(f"chatwatch on http://127.0.0.1:{PORT}  (pid {os.getpid()})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    if sys.version_info < (3, 8):
        sys.exit("chatwatch needs Python 3.8 or newer.")
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "serve":
        serve()
    elif arg == "calibrate":
        plan_status(force=True)
        print(json.dumps(calibrate(), indent=1))
    elif arg == "fit":
        fit(save="--save" in sys.argv)
    elif arg == "check":
        found = check(send="--dry" not in sys.argv)
        print("\n".join(t for _, t in found) or "nothing new to alert")
    elif arg == "json":
        rows = chats()
        for r in rows:
            r.pop("_cmd", None)
        print(json.dumps({"chats": rows, "plan": plan_summary(rows)}, indent=1))
    else:
        print_table(chats())
