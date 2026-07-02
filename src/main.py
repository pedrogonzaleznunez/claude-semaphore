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
STATE_FILE = os.path.join(APP_DIR, "state")
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

# kqueue file-watching constants (no polling).
O_EVTONLY = 0x8000          # macOS: open a fd only to receive kqueue events
KQ_READ_CB = 1              # kCFFileDescriptorReadCallBack
_FILE_FFLAGS = (select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_DELETE
                | select.KQ_NOTE_RENAME | select.KQ_NOTE_ATTRIB)
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

        self.info = rumps.MenuItem("Starting…")
        self.item_notif = rumps.MenuItem("Notifications", callback=self.toggle_notif)
        self.item_notif.state = 1 if self.notif_on else 0
        self.item_sound = rumps.MenuItem("Play sounds", callback=self.toggle_sound)
        self.item_sound.state = 1 if self.sound_on else 0
        self.item_reset = rumps.MenuItem("Restart (hard reset)", callback=self.restart)
        self.menu = [self.info, None, self.item_notif, self.item_sound,
                     self._build_sound_menu(), None, self.item_reset]

        self.current = None        # last shown state token
        self.red_since = None      # when DEBOUNCE_STATE started (for debounce)
        self.frame = 0             # hourglass frame
        self.work_since = 0.0      # when WORKING started (for the elapsed timer)

        # Timers are event-driven now: the animation timer runs *only* while
        # WORKING (it also does the anti-stuck check); the debounce timer is a
        # one-shot armed when WAITING appears. Neither polls the state file.
        self._t_anim = rumps.Timer(self.animate, cfg["anim_seconds"])
        self._anim_running = False
        self._t_debounce = rumps.Timer(self._debounce_fire, cfg["red_debounce_seconds"])

        # Show the initial state (silently, no sound/notification on launch).
        initial = read_state()
        if initial in self.states:
            self.apply(initial, alert=False)

        # Watch the state file by event (kqueue on the main run loop); fall back
        # to a light poll only if the CoreFoundation bridge isn't available.
        self._file_fd = -1
        self._dir_fd = -1
        if CFFileDescriptorCreate is not None:
            self._setup_watch()
        else:
            self._t_poll = rumps.Timer(lambda _: self._process(read_state()), 0.25)
            self._t_poll.start()

        # Re-assert menu-bar-only mode shortly after launch (run() can reset it).
        self._t_dock = rumps.Timer(self._hide_dock_once, 0.5)
        self._t_dock.start()

    # ---- helpers ----
    def _make_title(self, state, frame_icon=None, elapsed=""):
        st = self.states[state]
        icon = frame_icon if frame_icon else st["icon"]
        suffix = f" {elapsed}" if elapsed else ""
        return f'{icon} {st["label"]}{suffix}'

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

    # ---- state file watcher (kqueue on the main run loop; no polling) ----
    def _setup_watch(self):
        self._kq = select.kqueue()
        # Watch the dir (stable inode) so we notice the state file being (re)created,
        # and the file itself for in-place writes (the hooks do `printf > file`).
        try:
            self._dir_fd = os.open(APP_DIR, O_EVTONLY)
            self._kq_register(self._dir_fd, _DIR_FFLAGS)
        except OSError:
            self._dir_fd = -1
        self._open_state_file()
        self._cffd = CFFileDescriptorCreate(None, self._kq.fileno(), False, self._on_kq, None)
        src = CFFileDescriptorCreateRunLoopSource(None, self._cffd, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), src, kCFRunLoopDefaultMode)
        CFFileDescriptorEnableCallBacks(self._cffd, KQ_READ_CB)
        self._cffd_src = src  # keep a ref so it isn't collected

    def _kq_register(self, fd, fflags):
        ev = select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                           flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR, fflags=fflags)
        self._kq.control([ev], 0, 0)

    def _open_state_file(self):
        """(Re)attach the file watch to the current state-file inode."""
        if self._file_fd >= 0:
            try:
                os.close(self._file_fd)
            except OSError:
                pass
            self._file_fd = -1
        try:
            fd = os.open(STATE_FILE, O_EVTONLY)
            self._kq_register(fd, _FILE_FFLAGS)
            self._file_fd = fd
        except OSError:
            self._file_fd = -1

    def _on_kq(self, cffd, callback_types, info):
        try:
            events = self._kq.control(None, 16, 0)  # drain, non-blocking
        except OSError:
            events = []
        file_gone = any(e.ident == self._file_fd
                        and (e.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME))
                        for e in events)
        dir_touched = any(e.ident == self._dir_fd for e in events)
        if file_gone or (dir_touched and self._file_fd < 0):
            self._open_state_file()  # reattach to the recreated file
        self._process(read_state())
        CFFileDescriptorEnableCallBacks(cffd, KQ_READ_CB)  # callbacks are one-shot; re-arm

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
        if self.current != DEBOUNCE_STATE and read_state() == DEBOUNCE_STATE:
            self.apply(DEBOUNCE_STATE)

    def animate(self, _):
        if self.current != ANIM_STATE:
            return
        # Anti-stuck: a WORKING that hasn't changed in stale_seconds (e.g. cancelled
        # with Escape, so no hook fired) auto-resets to done. This runs only while
        # WORKING, which is exactly when the animation timer is active.
        try:
            age = time.time() - os.path.getmtime(STATE_FILE)
        except OSError:
            age = 0.0
        if age > self.cfg["stale_seconds"] and read_state() == ANIM_STATE:
            self.apply("DONE", alert=False)
            return
        self.frame = (self.frame + 1) % len(self.frames)
        elapsed = ""
        if self.cfg.get("show_elapsed") and self.work_since:
            elapsed = _fmt_elapsed(time.time() - self.work_since)
        self.title = self._make_title(ANIM_STATE, frame_icon=self.frames[self.frame], elapsed=elapsed)

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
        self.title = self._make_title(state)
        self.info.title = f'Status: {st["label"]}'
        if alert:
            snd = resolve_sound(st.get("sound")) if self.sound_on else None
            if snd:
                subprocess.Popen(["afplay", snd],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if self.notif_on and st.get("notify"):
                subprocess.Popen(
                    ["osascript", "-e",
                     f'display notification "{st["label"]}" with title "Claude Semaphore"'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.current = state


if __name__ == "__main__":
    _lock = ensure_single_instance()
    hide_from_dock()
    Semaphore(load_config()).run()
