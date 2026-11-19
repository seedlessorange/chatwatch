#!/usr/bin/env python3
"""chatwatch: what each open Claude Code chat is using of your plan, and what to do about it.

    python3 chatwatch.py            # table of open chats, heaviest first
    python3 chatwatch.py serve      # the page on http://127.0.0.1:7731, plus alerts every 5 min
    python3 chatwatch.py check      # one alert check (add --dry to print without notifying)
    python3 chatwatch.py calibrate  # re-measure how much of the plan a unit of usage is
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
used over the same time (weighted at API prices, so a step on a bigger model
counts for more). That gives "percent of the limit per unit of usage", which
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

# Calibration
CALIB_HOURS = {"week": 48, "five_hour": 10}   # how far back to look for readings
CALIB_MIN_POINTS = 3         # the meters move in whole percents; fewer is mostly rounding
PLAN_TTL = 240               # ask Anthropic at most this often (seconds)

# $ per million tokens at published API prices: a size signal, nothing is billed.
RATES = {
    "haiku":  {"in": 1.00, "out": 5.00,  "cr": 0.10, "cw5": 1.25,  "cw1": 2.00},
    "sonnet": {"in": 3.00, "out": 15.00, "cr": 0.30, "cw5": 3.75,  "cw1": 6.00},
    "opus":   {"in": 5.00, "out": 25.00, "cr": 0.50, "cw5": 6.25,  "cw1": 10.00},
    "fable":  {"in": 10.00, "out": 50.00, "cr": 1.00, "cw5": 12.50, "cw1": 20.00},
}
METERS = {"week": ("seven_day", "w", "wr", "weekly limit"),
          "five_hour": ("five_hour", "h", "hr", "5-hour window")}


def family(model):
    m = (model or "").lower()
    for name in ("haiku", "sonnet", "opus", "fable"):
        if name in m:
            return name
    return "fable" if "mythos" in m else "opus"


def step_cost(model, u):
    r = RATES[family(model)]
    cc = u.get("cache_creation") or {}
    w1 = cc.get("ephemeral_1h_input_tokens", 0) or 0
    w5 = cc.get("ephemeral_5m_input_tokens")
    if w5 is None:
        w5 = max(0, (u.get("cache_creation_input_tokens", 0) or 0) - w1)
    return ((u.get("input_tokens", 0) or 0) * r["in"]
            + (u.get("output_tokens", 0) or 0) * r["out"]
            + (u.get("cache_read_input_tokens", 0) or 0) * r["cr"]
            + w5 * r["cw5"] + w1 * r["cw1"]) / 1e6


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
        self.steps = {}       # message id -> (ts, model, cost, tokens, context, sidechain)
        self.title = ""
        self.last_prompt = ""
        self.cache_1h = None  # whether this chat writes the 1-hour or the 5-minute cache

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
                                   bool(m.get("isSidechain")))


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
            last_ts, model, _, _, ctx, _ = main_steps[-1]
        hour = day = total = recent = 0.0
        hour_tok = nsteps = subs_active = 0
        for i, t in enumerate([mt] + [transcript(p) for p in subs]):
            active = False
            for ts, _, c, tok, _, _ in t.steps.values():
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
        busy = s.get("status") == "busy"
        idle_min = 0 if busy else max(0, (now - max(last_ts, (s.get("statusUpdatedAt") or 0) / 1000)) / 60)
        ttl_min = 60 if mt.cache_1h else 5
        warm = (now - last_ts) / 60 < ttl_min
        r = RATES[family(model)]
        wake = ctx * (r["cr"] if warm else (r["cw1"] if mt.cache_1h else r["cw5"])) / 1e6
        cwd = s.get("cwd", "")
        rows.append({
            "pid": s.get("pid"), "sid": sid, "cwd": cwd,
            "folder": os.path.basename(cwd.rstrip("/\\")) or "~",
            "title": mt.title or (mt.last_prompt[:70] if mt.last_prompt else s.get("name") or sid[:8]),
            "model": model, "family": family(model) if model else "",
            "busy": busy, "idle_min": round(idle_min), "ctx": ctx,
            "hour_usd": round(hour, 4), "hour_tok": hour_tok, "day_usd": round(day, 4),
            "total_usd": round(total, 2), "steps": nsteps, "subagents": len(subs),
            "cache_warm": warm, "cache_min": ttl_min, "wake_usd": round(wake, 4),
            # usage per hour at the pace of the last BURN_WINDOW_MIN minutes
            "rate_usd_h": round(recent * 60 / BURN_WINDOW_MIN, 4),
            "subs_active": subs_active, "ctx_growth": ctx_growth,
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
        r["rate_pct_h"] = round(r["rate_usd_h"] * k, 3) if k else None
        r["advice"] = advice(r)
    rows.sort(key=lambda r: (-r["hour_usd"], -r["ctx"]))
    return rows


# ---------------------------------------------------------------- the plan's meters
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
        out = {"at": time.time(), "week": _meter(data.get("seven_day")),
               "five_hour": _meter(data.get("five_hour"))}
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
            for ts, _, c, _, _, _ in transcript(p).steps.values():
                if ts >= since:
                    out.append((ts, c))
    for p in [p for p in _cache if not os.path.exists(p) or os.path.getmtime(p) < since - 86400]:
        _cache.pop(p, None)
    return sorted(out)


def calibrate():
    """For each meter: percent of the limit per API-dollar, from how far the
    meter moved against how much every chat used over the same spans."""
    now = time.time()
    hist = plan_history()
    old = _read_json(CALIB) or {}
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
                res = {"at": now, "pct_per_usd": dpct / dusd, "points": dpct, "usd": round(dusd, 2),
                       "hours": round((h[-1]["at"] - h[0]["at"]) / 3600, 1)}
        # keep the last good figure while there is too little movement for a new one
        out[meter] = res or old.get(meter)
    with open(CALIB, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return out


def share_unit():
    """Which limit per-chat shares are shown against, and its pct-per-dollar:
    the calibrated meter that is closer to full."""
    cal = _read_json(CALIB) or {}
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
    cal = _read_json(CALIB) or {}
    hour_usd = sum(r["hour_usd"] for r in rows)
    unit, _ = share_unit()
    out = {"why": s.get("why", ""), "read_at": s.get("at"), "unit": unit, "meters": {}}
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
    if r["ctx"] > CTX_ALERT:
        out.append(("fresh", f"Holding {r['ctx']//1000}K of context: every step re-reads all of it. Close + handoff and start fresh."))
    if not r["busy"] and idle_h >= IDLE_PILE_HOURS:
        why = ("its cache has expired, so waking it rebuilds the whole context"
               if not r["cache_warm"] else "nothing is running")
        out.append(("close", f"Idle {idle_h:.0f} h ({why}). Close it if the task is done."))
    if r["busy"] and heavy(r):
        used = (f"{r['hour_pct']:.1f}% of your {unit_label(r['unit'])}" if r["hour_pct"] is not None
                else f"about ${r['hour_usd']:.0f} worth")
        out.append(("heavy", f"Used {used} in the last hour ({r['hour_share']}% of all open chats)."))
    if r["family"] == "opus" and r["ctx"] < 60_000 and r["steps"] > 20 and not out:
        out.append(("model", "Small, simple work? /model sonnet costs about 40% less per step."))
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
        print(f"Plan limits unavailable ({plan['why'] or 'no reading yet'}). Showing API-rate dollars.")
    busy = sum(r["busy"] for r in rows)
    tot = sum(r["hour_usd"] for r in rows)
    print(f"{len(rows)} open chats, {busy} working. Last hour: ${tot:.2f} at API rates.\n")
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
NOTIFY_KINDS = ("burn", "jump", "fable")   # the fast ones; the menu bar app adds action buttons
APP_SEEN = [0.0]                            # last time the menu bar app asked for data


def app_active():
    return time.time() - APP_SEEN[0] < 90


def live_alerts(rows):
    """Chats that need a look right now, snoozed ones left out:
    [{id, sid, pid, kind, title, text, family, busy, notify}], most urgent first."""
    st = _read_json(STATE) or {}
    now = time.time()
    out = []

    def add(r, kind, text):
        aid = f"{r['sid']}:{kind}"
        if st.get("snooze:" + aid, 0) > now:
            return
        out.append({"id": aid, "sid": r["sid"], "pid": r["pid"], "kind": kind, "title": r["title"],
                     "text": text, "family": r["family"], "busy": r["busy"], "notify": kind in NOTIFY_KINDS})

    for r in rows:
        subs = f", {r['subs_active']} subagents running" if r["subs_active"] else ""
        if r.get("rate_pct_h") is not None:
            fast = r["rate_pct_h"] >= BURN_PCT_H.get(r["unit"], 1.0)
            pace = f"{r['rate_pct_h']:.1f}% of your {unit_label(r['unit'])} per hour"
        else:
            fast = r["rate_usd_h"] >= BURN_USD_H
            pace = f"${r['rate_usd_h']:.0f} an hour at API rates"
        if fast:
            add(r, "burn", f"Burning {pace} (last {BURN_WINDOW_MIN} min){subs}.")
        if r["ctx"] >= CTX_JUMP_FLOOR and r["ctx_growth"] >= CTX_JUMP:
            add(r, "jump", f"Context grew {r['ctx_growth']//1000}K in {CTX_JUMP_MIN} min, now {r['ctx']//1000}K{subs}.")
        if r["family"] == "fable" and r["busy"]:
            add(r, "fable", "Working on Fable, the heaviest model. Switch with /model opus unless it needs Fable.")
        elif r["family"] == "fable" and r["idle_min"] >= FABLE_IDLE_MIN:
            add(r, "fable_idle", "Idle on Fable. Its next step is priced as Fable.")
    order = {"burn": 0, "jump": 1, "fable": 2, "fable_idle": 3}
    out.sort(key=lambda a: order.get(a["kind"], 9))
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
    for r in rows:
        name = r["title"][:40]
        if r["ctx"] > CTX_ALERT and due(r["sid"] + ":ctx"):
            fresh.append((r["sid"] + ":ctx", f"{name}: {r['ctx']//1000}K context"))
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
            if self.headers.get("X-Chatwatch-App"):
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
