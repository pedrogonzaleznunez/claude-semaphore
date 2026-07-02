#!/usr/bin/env python3
"""
Claude Semaphore 🚦 — menu bar status indicator for Claude Code (macOS).

Reads the state that Claude Code's hooks write to ~/.claude-semaphore/state
and shows it in the menu bar:

  ✅  Claude finished  -> Claude is done (Stop hook)
  ⌛️/⏳ Claude working  -> Claude is thinking / running tools (the hourglass flips)
  ‼️  Claude needs you -> Claude is waiting for your confirmation (pending PreToolUse)

The "working" animation only swaps the emoji (⌛️ <-> ⏳): since both emojis are
the same width, the text next to it does not shift. If Claude gets cancelled
(Escape) no hook fires, so a stale "working" state auto-resets to "finished".

Everything visible is driven by ~/.claude-semaphore/config.json (see
config.example.json). macOS-only for now.
"""
import fcntl
import json
import os
import select
import subprocess
import sys
import time

import rumps

try:
    import AppKit  # ships with pyobjc (a rumps dependency)
except Exception:
    AppKit = None

try:
    # Bridge a kqueue fd into the main run loop, so state changes are delivered by
    # event (no polling) and handled on the main thread (safe for UI updates).
    from Foundation import (CFFileDescriptorCreate,
                            CFFileDescriptorCreateRunLoopSource,
                            CFFileDescriptorEnableCallBacks, CFRunLoopAddSource,
                            CFRunLoopGetMain, kCFRunLoopDefaultMode)
except Exception:
    CFFileDescriptorCreate = None  # fall back to a poll timer if unavailable

APP_ID = "com.claude-semaphore.agent"
APP_DIR = os.path.expanduser("~/.claude-semaphore")
STATE_FILE = os.path.join(APP_DIR, "state")           # legacy single-state file (fallback)
SESSIONS_DIR = os.path.join(APP_DIR, "sessions")      # one <session_id>.json per Claude session
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
NOTIF_FILE = os.path.join(APP_DIR, "notif_enabled")   # menu toggle: notifications
SOUND_FILE = os.path.join(APP_DIR, "sound_enabled")   # menu toggle: sounds
SOUNDS_FILE = os.path.join(APP_DIR, "sounds.json")    # menu: per-state sound overrides
LOCK_FILE = "/tmp/claude-semaphore.lock"

# Nice, short names for the per-state sound submenus (falls back to the state token).
SOUND_MENU_NAMES = {"DONE": "Finished", "WORKING": "Working", "WAITING": "Needs you"}

# The state whose emoji animates, and the one that gets debounced (permission wait).
ANIM_STATE = "WORKING"
DEBOUNCE_STATE = "WAITING"

# Aggregating many sessions into one icon: the loudest state wins. If any session
# needs you it's red; else if any is working it's amber; else green.
STATE_PRIORITY = {"DONE": 0, "WORKING": 1, "WAITING": 2}

# TERM_PROGRAM -> AppleScript app name, used only when a bundle id isn't available.
TERM_APP_NAMES = {
    "Apple_Terminal": "Terminal",
    "iTerm.app": "iTerm",
    "vscode": "Visual Studio Code",
    "WezTerm": "WezTerm",
    "Hyper": "Hyper",
    "Tabby": "Tabby",
}

# kqueue file-watching constants (no polling).
O_EVTONLY = 0x8000          # macOS: open a fd only to receive kqueue events
KQ_READ_CB = 1              # kCFFileDescriptorReadCallBack
_DIR_FFLAGS = (select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME)

DEFAULTS = {
    "states": {
        "DONE":    {"icon": "✅", "label": "Claudia finished",  "sound": "Glass.aiff",  "notify": True},
        "WORKING": {"icon": "⌛️", "label": "Claudia working",   "sound": None,          "notify": False},
        "WAITING": {"icon": "‼️", "label": "Claudia needs you", "sound": "Sosumi.aiff", "notify": True},
    },
    "cooking_frames": ["⌛️", "⏳"],
    "red_debounce_seconds": 0.8,
    "anim_seconds": 0.6,
    "stale_seconds": 60,
    "done_ttl_seconds": 20,   # how long a finished session lingers in the menu before it's dropped
    "sweep_seconds": 4,       # janitor cadence while any session is active (expires stale ones)
    "show_elapsed": False,
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy of defaults
    try:
        with open(CONFIG_FILE) as f:
            user = json.load(f)
    except FileNotFoundError:
        return cfg
    except Exception as exc:
        print(f"config.json inválido, uso defaults: {exc}")
        return cfg
    for key, val in user.items():
        if key == "states" and isinstance(val, dict):
            for sk, sv in val.items():
                cfg["states"].setdefault(sk, {}).update(sv)
        elif not key.startswith("_"):
            cfg[key] = val
    return cfg


def resolve_sound(name):
    """A bare name -> a macOS system sound; a path with '/' is used as-is."""
    if not name:
        return None
    if "/" in name:
        return os.path.expanduser(name)
    return f"/System/Library/Sounds/{name}"


def ensure_single_instance():
    """Prevents two icons: if a semaphore is already running, exit."""
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Claude Semaphore is already running.")
        sys.exit(0)
    return lock  # keep open for the whole process lifetime


def hide_from_dock():
    """Accessory mode: menu-bar only, no Dock icon."""
    if AppKit is not None:
        try:
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(1)
        except Exception:
            pass


def read_state():
    try:
        with open(STATE_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return "DONE"


def _read_flag(path, default=True):
    try:
        with open(path) as f:
            return f.read().strip() != "off"
    except FileNotFoundError:
        return default


def _write_flag(path, on):
    with open(path, "w") as f:
        f.write("on" if on else "off")


def load_sound_overrides():
    """Per-state sound choices picked from the menu: {state: "Glass.aiff" | None}."""
    try:
        with open(SOUNDS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_sound_overrides(overrides):
    try:
        with open(SOUNDS_FILE, "w") as f:
            json.dump(overrides, f, indent=2)
    except Exception:
        pass


def list_system_sounds():
    """Bare names (no .aiff) of the macOS system sounds, for the menu."""
    try:
        return sorted(n[:-5] for n in os.listdir("/System/Library/Sounds")
                      if n.endswith(".aiff"))
    except OSError:
        return []


def _sound_name(value):
    """Config value ('Glass.aiff' | 'Glass' | a path | None) -> bare menu name or None."""
    if not value:
        return None
    base = os.path.basename(value)
    return base[:-5] if base.endswith(".aiff") else base


def _fmt_elapsed(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def _osa_escape(s):
    """Escape a string for embedding inside an AppleScript double-quoted literal."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


# Focus the exact Terminal.app tab / iTerm2 session that owns a given tty, then
# bring its app forward. Other terminals can't be driven per-tab reliably, so we
# just activate the app by its bundle id (captured when the session started).
_APPLE_TERMINAL_BY_TTY = '''
tell application "Terminal"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      try
        if tty of t is "{tty}" then
          set selected of t to true
          set frontmost of w to true
        end if
      end try
    end repeat
  end repeat
end tell'''

_ITERM_BY_TTY = '''
tell application "iTerm"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        try
          if tty of s is "{tty}" then
            select s
          end if
        end try
      end repeat
    end repeat
  end repeat
end tell'''


def build_focus_script(rec):
    """AppleScript to focus the terminal a session lives in (best effort)."""
    bundle = rec.get("bundle_id") or ""
    term = rec.get("term_program") or ""
    tty = rec.get("tty") or ""
    if tty and (bundle == "com.apple.Terminal" or term == "Apple_Terminal"):
        return _APPLE_TERMINAL_BY_TTY.format(tty=_osa_escape(tty))
    if tty and (bundle == "com.googlecode.iterm2" or term == "iTerm.app"):
        return _ITERM_BY_TTY.format(tty=_osa_escape(tty))
    if bundle:
        return f'tell application id "{_osa_escape(bundle)}" to activate'
    name = TERM_APP_NAMES.get(term)
    if name:
        return f'tell application "{_osa_escape(name)}" to activate'
    return ""


class Semaphore(rumps.App):
    def __init__(self, cfg):
        super().__init__("🚦", quit_button=rumps.MenuItem("Quit Claude Semaphore"))
        self.cfg = cfg
        self.states = cfg["states"]
        self.frames = cfg.get("cooking_frames") or ["⌛️", "⏳"]

        self.notif_on = _read_flag(NOTIF_FILE)
        self.sound_on = _read_flag(SOUND_FILE)

        # Per-state sound choices from the menu override whatever config.json set.
        self._sound_overrides = load_sound_overrides()
        for st, val in self._sound_overrides.items():
            if st in self.states:
                self.states[st]["sound"] = val

        # Header: a one-line summary; click it to jump to whatever needs attention.
        self.info = rumps.MenuItem("Starting…", callback=self._focus_top)
        # Section header for the session rows (no callback -> renders grayed).
        self.hdr_sessions = rumps.MenuItem("Sessions")
        self._sessions_anchor = "Sessions"   # stable key we insert session rows after
        self.item_notif = rumps.MenuItem("🔔  Notifications", callback=self.toggle_notif)
        self.item_notif.state = 1 if self.notif_on else 0
        self.item_sound = rumps.MenuItem("🔊  Sounds", callback=self.toggle_sound)
        self.item_sound.state = 1 if self.sound_on else 0
        self.item_reset = rumps.MenuItem("♻︎  Restart", callback=self.restart)
        self.menu = [self.info, None,
                     self.hdr_sessions,          # session rows get inserted right after this
                     None,
                     self.item_notif, self.item_sound, self._build_sound_menu(),
                     None, self.item_reset]

        self.current = None        # last shown aggregate state token
        self.red_since = None      # when DEBOUNCE_STATE started (for debounce)
        self.frame = 0             # hourglass frame
        self.work_since = 0.0      # when WORKING started (for the elapsed timer)
        self._sessions = {}        # sid -> record, refreshed on every recompute
        self._since = {}           # sid -> (state, entered_ts): how long it's been in this state
        self._work_took = {}       # sid -> seconds spent WORKING before it last finished
        self._menu_sig = None      # signature of the last-rendered session list (skip no-op rebuilds)
        self._row_keys = []        # menu keys of the currently-inserted session rows

        # Timers are event-driven: the animation timer runs *only* while WORKING;
        # the debounce timer is a one-shot armed when WAITING appears; the sweep
        # timer runs only while sessions are active and expires stale ones (the
        # anti-stuck check, e.g. a run cancelled with Escape). None poll idly.
        self._t_anim = rumps.Timer(self.animate, cfg["anim_seconds"])
        self._anim_running = False
        self._t_debounce = rumps.Timer(self._debounce_fire, cfg["red_debounce_seconds"])
        self._t_sweep = rumps.Timer(self._sweep, cfg.get("sweep_seconds", 4))
        self._sweep_running = False

        os.makedirs(SESSIONS_DIR, exist_ok=True)

        # Show the initial aggregate state (silently, no sound/notification on launch).
        self._recompute(initial=True)

        # Watch the sessions dir by event (kqueue on the main run loop); fall back
        # to a light poll only if the CoreFoundation bridge isn't available.
        self._dir_fds = []
        if CFFileDescriptorCreate is not None:
            self._setup_watch()
        else:
            self._t_poll = rumps.Timer(lambda _: self._recompute(), 0.25)
            self._t_poll.start()

        # Re-assert menu-bar-only mode shortly after launch (run() can reset it).
        self._t_dock = rumps.Timer(self._hide_dock_once, 0.5)
        self._t_dock.start()

    # ---- helpers ----
    def _live(self):
        """Real sessions (drops the legacy single-file placeholder)."""
        return [s for s in self._sessions.values() if s.get("_sid") != "_legacy"]

    def _make_title(self, state, frame_icon=None, elapsed=""):
        st = self.states[state]
        icon = frame_icon if frame_icon else st["icon"]
        suffix = f" {elapsed}" if elapsed else ""
        return f'{icon} {st["label"]}{suffix}'

    def _header_text(self):
        """The top summary line, e.g. '🔴 needs you · 2 working' or '🟢 idle'."""
        live = self._live()
        if not live:
            return f'{self.states["DONE"]["icon"]}  idle'
        agg = self._aggregate_state(self._sessions)
        return f'{self.states[agg]["icon"]}  {self._summary(self._sessions)}'

    # ---- per-state sound picker ----
    def _build_sound_menu(self):
        """A "Sound per state" submenu: one submenu per state listing every
        macOS system sound (plus None), with the current choice checked."""
        self.sound_items = {}  # (state, name|None) -> MenuItem, for updating checkmarks
        root = rumps.MenuItem("Sound per state")
        available = list_system_sounds()
        for state in ("DONE", "WORKING", "WAITING"):
            if state not in self.states:
                continue
            sub = rumps.MenuItem(SOUND_MENU_NAMES.get(state, state))
            current = _sound_name(self.states[state].get("sound"))
            for name in [None] + available:
                item = rumps.MenuItem(name or "None", callback=self._make_sound_cb(state, name))
                item.state = 1 if current == name else 0
                sub.add(item)
                self.sound_items[(state, name)] = item
            root.add(sub)
        return root

    def _make_sound_cb(self, state, name):
        def _cb(_sender):
            self.choose_sound(state, name)
        return _cb

    def choose_sound(self, state, name):
        """Set (and persist) the sound for one state, then preview it."""
        value = f"{name}.aiff" if name else None
        self.states[state]["sound"] = value
        self._sound_overrides[state] = value
        save_sound_overrides(self._sound_overrides)
        for (st, nm), item in self.sound_items.items():
            if st == state:
                item.state = 1 if nm == name else 0
        snd = resolve_sound(value)  # preview the pick regardless of the master toggle
        if snd:
            subprocess.Popen(["afplay", snd],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ---- menu toggles ----
    def toggle_notif(self, sender):
        self.notif_on = not self.notif_on
        sender.state = 1 if self.notif_on else 0
        _write_flag(NOTIF_FILE, self.notif_on)

    def toggle_sound(self, sender):
        self.sound_on = not self.sound_on
        sender.state = 1 if self.sound_on else 0
        _write_flag(SOUND_FILE, self.sound_on)

    def restart(self, _):
        """Hard reset: clear state and relaunch the process via launchd."""
        try:
            with open(STATE_FILE, "w") as f:
                f.write("DONE")
        except Exception:
            pass
        try:
            subprocess.Popen(
                ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{APP_ID}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            pass
        self.current = None
        self.red_since = None
        self.frame = 0
        self.apply("DONE", alert=False)

    def _hide_dock_once(self, sender):
        hide_from_dock()
        sender.stop()

    # ---- sessions-dir watcher (kqueue on the main run loop; no polling) ----
    def _setup_watch(self):
        self._kq = select.kqueue()
        # The hooks write each session atomically (write tmp + rename), which bumps
        # the directory vnode — so watching the dirs delivers every update by event.
        # We watch both the sessions dir and the app dir (the latter also catches
        # the legacy single-state file being (re)created during an upgrade).
        for d in (SESSIONS_DIR, APP_DIR):
            try:
                fd = os.open(d, O_EVTONLY)
                self._kq_register(fd, _DIR_FFLAGS)
                self._dir_fds.append(fd)
            except OSError:
                pass
        self._cffd = CFFileDescriptorCreate(None, self._kq.fileno(), False, self._on_kq, None)
        src = CFFileDescriptorCreateRunLoopSource(None, self._cffd, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), src, kCFRunLoopDefaultMode)
        CFFileDescriptorEnableCallBacks(self._cffd, KQ_READ_CB)
        self._cffd_src = src  # keep a ref so it isn't collected

    def _kq_register(self, fd, fflags):
        ev = select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                           flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR, fflags=fflags)
        self._kq.control([ev], 0, 0)

    def _on_kq(self, cffd, callback_types, info):
        try:
            self._kq.control(None, 16, 0)  # drain, non-blocking
        except OSError:
            pass
        self._recompute()
        CFFileDescriptorEnableCallBacks(cffd, KQ_READ_CB)  # callbacks are one-shot; re-arm

    # ---- sessions (multi-project) ----
    def _scan_sessions(self):
        """Read every live session, expiring stale/old files on the way.

        Returns {sid: record}. A finished (DONE) session lingers for
        done_ttl_seconds so you can see it just finished; a WORKING/WAITING
        session that stopped updating (terminal closed, or a run cancelled with
        Escape so no hook fired) is dropped after stale_seconds (anti-stuck).
        """
        now = time.time()
        stale = self.cfg.get("stale_seconds", 60)
        done_ttl = self.cfg.get("done_ttl_seconds", 20)
        sessions = {}
        try:
            names = os.listdir(SESSIONS_DIR)
        except OSError:
            names = []
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(SESSIONS_DIR, name)
            try:
                with open(path) as f:
                    rec = json.load(f)
            except Exception:
                continue
            state = rec.get("state")
            if state not in self.states:
                continue
            age = now - rec.get("updated", 0)
            if (state == "DONE" and age > done_ttl) or (state != "DONE" and age > stale):
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            rec["_sid"] = name[:-5]
            sessions[name[:-5]] = rec
        # Upgrade fallback: no per-session files yet, but a legacy state file exists.
        if not sessions:
            legacy = read_state()
            if legacy in self.states and legacy != "DONE":
                sessions["_legacy"] = {"state": legacy, "project": "", "_sid": "_legacy"}
        return sessions

    def _aggregate_state(self, sessions):
        """The loudest live state wins (WAITING > WORKING > DONE)."""
        if not sessions:
            return "DONE"
        return max((s["state"] for s in sessions.values()),
                   key=lambda st: STATE_PRIORITY.get(st, 0))

    def _summary(self, sessions):
        counts = {"WAITING": 0, "WORKING": 0, "DONE": 0}
        for s in sessions.values():
            counts[s["state"]] = counts.get(s["state"], 0) + 1
        parts = []
        if counts["WAITING"]:
            parts.append(f'{counts["WAITING"]} needs you')
        if counts["WORKING"]:
            parts.append(f'{counts["WORKING"]} working')
        if counts["DONE"]:
            parts.append(f'{counts["DONE"]} done')
        return ", ".join(parts) or "idle"

    def _track_elapsed(self, sessions):
        """Stamp each session with how long it's been in its current state.

        The timer resets only when the state token changes, so a session that
        stays WORKING across a whole turn accumulates the full task duration.
        """
        now = time.time()
        for sid, rec in sessions.items():
            prev = self._since.get(sid)
            if not prev or prev[0] != rec["state"]:
                if prev and prev[0] == "WORKING":   # remember how long the finished task ran
                    self._work_took[sid] = now - prev[1]
                self._since[sid] = (rec["state"], now)
            rec["_elapsed"] = now - self._since[sid][1]
            rec["_work_took"] = self._work_took.get(sid, 0)
        self._since = {sid: v for sid, v in self._since.items() if sid in sessions}
        self._work_took = {sid: v for sid, v in self._work_took.items() if sid in sessions}

    def _recompute(self, initial=False):
        """Rescan sessions, refresh the menu, and drive the icon off the aggregate."""
        sessions = self._scan_sessions()
        self._sessions = sessions
        self._track_elapsed(sessions)
        self._rebuild_session_menu(sessions)
        self._manage_sweep(sessions)
        agg = self._aggregate_state(sessions)
        if initial:
            self.current = None
            self.red_since = None
            self.apply(agg, alert=False)
        else:
            self._process(agg)

    def _row_label(self, rec):
        """A session row: '🔴  claude-semaphore      1m04s' (loudest states get a time)."""
        icon = self.states[rec["state"]]["icon"]
        proj = rec.get("project") or "session"
        elapsed = rec.get("_elapsed", 0)
        if rec["state"] == "DONE":
            suffix = "done"
        else:
            suffix = _fmt_elapsed(elapsed) if elapsed >= 1 else ""
        pad = " " * max(2, 22 - len(proj))   # loosely right-align the time
        return f'{icon}  {proj}{pad}{suffix}'.rstrip()

    def _rebuild_session_menu(self, sessions):
        """Render one clickable row per session, inline under the Sessions header.

        Loudest first; click a row to focus that terminal. Rebuilt only when the
        set/state/elapsed-bucket changes, so it isn't churning every event.
        """
        live = sorted((s for s in sessions.values() if s.get("_sid") != "_legacy"),
                      key=lambda s: (-STATE_PRIORITY.get(s["state"], 0), s.get("project", "")))
        # elapsed bucketed to ~5s so the times refresh occasionally without thrashing
        sig = tuple((s["_sid"], s["state"], s.get("project", ""), int(s.get("_elapsed", 0)) // 5)
                    for s in live)
        if sig == self._menu_sig:
            return
        self._menu_sig = sig
        self.info.title = self._header_text()

        # drop the previous rows, then insert the fresh ones after the header
        for k in self._row_keys:
            try:
                self.menu.pop(k)
            except KeyError:
                pass
        self._row_keys = []

        if not live:
            self.hdr_sessions.title = "Sessions"
            row = rumps.MenuItem("    no active sessions")   # grayed (no callback)
            self.menu.insert_after(self._sessions_anchor, row)
            self._row_keys = [row.title]
            return

        self.hdr_sessions.title = f"Sessions ({len(live)})"
        anchor = self._sessions_anchor
        seen = {}
        for s in live:
            title = self._row_label(s)
            seen[title] = seen.get(title, 0) + 1
            if seen[title] > 1:
                title = f'{title} ·{seen[title]}'   # keep menu keys unique
            self.menu.insert_after(anchor, rumps.MenuItem(title, callback=self._make_focus_cb(s)))
            anchor = title              # keep insertion order
            self._row_keys.append(title)

    def _make_focus_cb(self, rec):
        def _cb(_sender):
            self.focus_session(rec)
        return _cb

    def _focus_top(self, _sender):
        """Focus whichever session most wants attention (WAITING before WORKING)."""
        live = [s for s in self._sessions.values() if s.get("_sid") != "_legacy"]
        if not live:
            return
        live.sort(key=lambda s: (-STATE_PRIORITY.get(s["state"], 0), -s.get("updated", 0)))
        self.focus_session(live[0])

    def focus_session(self, rec):
        script = build_focus_script(rec)
        if not script:
            return
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _manage_sweep(self, sessions):
        """Run the janitor timer only while something is worth watching."""
        need = len(sessions) > 1 or any(s["state"] != "DONE" for s in sessions.values())
        if need and not self._sweep_running:
            self._t_sweep.start()
            self._sweep_running = True
        elif not need and self._sweep_running:
            self._t_sweep.stop()
            self._sweep_running = False

    def _sweep(self, _):
        self._recompute()

    # ---- state handling ----
    def _process(self, raw):
        """React to a state token (from an event, the debounce timer, or startup)."""
        if raw not in self.states:
            return
        if raw == DEBOUNCE_STATE:
            if self.current == DEBOUNCE_STATE:
                return                      # already red
            if self.red_since is None:      # arm the debounce window (filters fast blips)
                self.red_since = time.time()
                self._t_debounce.start()
            return
        # Any non-WAITING state cancels a pending debounce and applies immediately.
        if self.red_since is not None:
            self.red_since = None
            self._t_debounce.stop()
        if raw != self.current:
            self.apply(raw)

    def _debounce_fire(self, _):
        """The WAITING window elapsed: if it's still waiting, it's a real one."""
        self._t_debounce.stop()
        self.red_since = None
        if self.current == DEBOUNCE_STATE:
            return
        sessions = self._scan_sessions()
        if self._aggregate_state(sessions) == DEBOUNCE_STATE:
            # refresh state so the notification knows which project is waiting
            self._sessions = sessions
            self._track_elapsed(sessions)
            self.apply(DEBOUNCE_STATE)

    def animate(self, _):
        if self.current != ANIM_STATE:
            return
        # Anti-stuck now lives in the sweep janitor (it expires stale sessions),
        # so this timer only advances the hourglass while WORKING.
        self.frame = (self.frame + 1) % len(self.frames)
        elapsed = ""
        if self.cfg.get("show_elapsed") and self.work_since:
            elapsed = _fmt_elapsed(time.time() - self.work_since)
        self.title = self._make_title(ANIM_STATE, frame_icon=self.frames[self.frame], elapsed=elapsed)

    def _notify(self, state):
        """Post a macOS notification naming the project and what it's about.

        The detail goes in the *body* (never empty — an empty-body notification
        won't reliably show) and the project headline in the title.
        """
        rec = self._relevant_session(state)
        project = (rec or {}).get("project", "")
        icon = self.states[state]["icon"]
        if state == DEBOUNCE_STATE:
            title = f'{icon}  {project} needs you' if project else f'{icon}  Claude needs you'
            body = "Waiting for your approval"
        elif state == "DONE":
            title = f'{icon}  {project} finished' if project else f'{icon}  Claude finished'
            took = (rec or {}).get("_work_took", 0)
            body = f"Took {_fmt_elapsed(took)}" if took >= 1 else "Done"
        else:
            title = "Claude Semaphore"
            body = f'{icon} {self.states[state].get("label", "")}'.strip()
        subprocess.Popen(
            ["osascript", "-e",
             f'display notification "{_osa_escape(body)}" with title "{_osa_escape(title)}"'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _relevant_session(self, state):
        """The most-recently-updated session driving the current state (for the notif)."""
        cands = [s for s in self._live() if s.get("state") == state]
        return max(cands, key=lambda s: s.get("updated", 0)) if cands else None

    def apply(self, state, alert=True):
        st = self.states[state]
        self.frame = 0
        # Run the animation timer only while WORKING (it also does the stale check).
        if state == ANIM_STATE:
            self.work_since = time.time()
            if not self._anim_running:
                self._t_anim.start()
                self._anim_running = True
        elif self._anim_running:
            self._t_anim.stop()
            self._anim_running = False
        self.current = state
        self.title = self._make_title(state)
        # (the header summary line is owned by _rebuild_session_menu)
        if alert:
            snd = resolve_sound(st.get("sound")) if self.sound_on else None
            if snd:
                subprocess.Popen(["afplay", snd],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if self.notif_on and st.get("notify"):
                self._notify(state)


if __name__ == "__main__":
    _lock = ensure_single_instance()
    hide_from_dock()
    Semaphore(load_config()).run()
