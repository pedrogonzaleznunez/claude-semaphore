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
import subprocess
import sys
import time

import rumps

try:
    import AppKit  # ships with pyobjc (a rumps dependency)
except Exception:
    AppKit = None

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

DEFAULTS = {
    "states": {
        "DONE":    {"icon": "✅", "label": "Claudia finished",  "sound": "Glass.aiff",  "notify": True},
        "WORKING": {"icon": "⌛️", "label": "Claudia working",   "sound": None,          "notify": False},
        "WAITING": {"icon": "‼️", "label": "Claudia needs you", "sound": "Sosumi.aiff", "notify": True},
    },
    "cooking_frames": ["⌛️", "⏳"],
    "poll_seconds": 0.25,
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
        self._dock_hidden = False

        initial = read_state()
        if initial in self.states:
            self.current = initial
            self.title = self._make_title(initial)
            self.info.title = f'Status: {self.states[initial]["label"]}'

        # Timers with intervals from config (programmatic, not decorators).
        self._t_tick = rumps.Timer(self.tick, cfg["poll_seconds"])
        self._t_anim = rumps.Timer(self.animate, cfg["anim_seconds"])
        self._t_tick.start()
        self._t_anim.start()

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

    # ---- timers ----
    def tick(self, _):
        if not self._dock_hidden:
            hide_from_dock()
            self._dock_hidden = True

        raw = read_state()
        if raw not in self.states:
            return

        silent = False
        # Anti-stuck auto-reset: stale WORKING with no activity => treat as done.
        if raw == ANIM_STATE:
            try:
                age = time.time() - os.path.getmtime(STATE_FILE)
            except OSError:
                age = 0.0
            if age > self.cfg["stale_seconds"]:
                raw = "DONE"
                silent = True

        if raw == DEBOUNCE_STATE:
            if self.red_since is None:
                self.red_since = time.time()
            if time.time() - self.red_since < self.cfg["red_debounce_seconds"]:
                return  # not stable yet; ignore the blip from fast tools
            new = DEBOUNCE_STATE
        else:
            self.red_since = None
            new = raw

        if new != self.current:
            self.apply(new, alert=not silent)

    def animate(self, _):
        if self.current != ANIM_STATE:
            return
        self.frame = (self.frame + 1) % len(self.frames)
        elapsed = ""
        if self.cfg.get("show_elapsed") and self.work_since:
            elapsed = _fmt_elapsed(time.time() - self.work_since)
        self.title = self._make_title(ANIM_STATE, frame_icon=self.frames[self.frame], elapsed=elapsed)

    def apply(self, state, alert=True):
        st = self.states[state]
        self.frame = 0
        if state == ANIM_STATE:
            self.work_since = time.time()
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
