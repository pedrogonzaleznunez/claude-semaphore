# Claude Semaphore 🚦

A tiny macOS **menu bar traffic light** for [Claude Code](https://claude.com/claude-code). See at a glance whether Claude is working, done, or waiting for you — without keeping the terminal in focus.

![Claude Semaphore demo](assets/demo.gif)

| Menu bar | State | When |
|---|---|---|
| ✅ Claude finished | done | Claude finished responding |
| ⌛️/⏳ Claude working | working | Claude is thinking / running tools (the hourglass flips) |
| ‼️ Claude needs you | waiting | Claude is asking for your confirmation |

On **finished** and **needs you** it also plays a sound and posts a macOS notification. **Working** is silent on purpose (it fires constantly).

> **Status:** beta, macOS only. Linux/Windows are on the roadmap — PRs welcome.

---

## How it works

Two halves talk through per-session state files:

```
Claude Code ──(hooks)──▶ ~/.claude-semaphore/sessions/<id>.json ──(kqueue events)──▶ menu bar app
```

- **The brain — hooks.** Claude Code [hooks](https://docs.claude.com/en/docs/claude-code/hooks) call a tiny `hook.py` on each event. It reads the hook's JSON (session id, cwd, terminal identity) and writes one small file **per Claude session** — so several projects can run at once without stepping on each other.
- **The face — `main.py`.** A [`rumps`](https://github.com/jaredks/rumps) menu bar app watches the `sessions/` dir with **`kqueue`** hooked into the main run loop (no polling) — updates arrive by event on write. It aggregates every session into one icon (**the loudest state wins**: needs-you ▸ working ▸ done), lists them in the menu, plays sounds and posts notifications.

**Multi-project.** Each row in the menu is one session (`icon project`). Click a row — or the status line at the top — to **jump straight to that terminal**: it focuses the exact tab in Terminal.app / iTerm2 (matched by tty), or brings the right app forward for other terminals.

**Detecting "needs you".** `WAITING` comes straight from Claude Code's `Notification` hook with `matcher: "permission_prompt"` — the real "I need your approval" signal, which fires whether or not the terminal is focused. (Earlier versions guessed it from `PreToolUse`/`PostToolUse` timing, which false-alarmed on any tool that ran longer than the debounce — e.g. a slow command. That heuristic is gone.)

**No moving text.** The working animation swaps only the emoji (⌛️ ↔ ⏳). Both emojis are the same width, so the label next to it never shifts.

**Anti-stuck.** If you cancel Claude with `Escape`, no hook fires and a session would stay `WORKING` forever — so a light janitor (running only while sessions are active) drops any session that hasn't updated in `stale_seconds`, and finished sessions linger for `done_ttl_seconds` before they leave the menu.

---

## Install

```bash
git clone https://github.com/pedrogonzaleznunez/claude-semaphore.git
cd claude-semaphore
./install.sh
```

The installer:

1. Creates `~/.claude-semaphore/` with its own Python venv + `rumps`.
2. Copies `main.py` + `hook.py` and creates `config.json` (an existing one is respected).
3. **Safely merges** the hooks into `~/.claude/settings.json` — it backs the file up first and never overwrites your existing settings (idempotent: re-running won't duplicate).
4. Installs a LaunchAgent so the app starts at login, and launches it now.
5. Drops a `Claude Semaphore.app` into `~/Applications` so you can [reopen it manually](#reopening-it-manually) from Spotlight/Finder.

**Then restart Claude Code** so it picks up the hooks (accept the hook-review prompt if it appears).

---

## Menu

Click the icon:

- **Status** — a one-line summary (e.g. *1 needs you, 2 working*). Click it to jump to the session that most wants your attention.
- **Sessions** — one row per active Claude session (`icon project`). Click a row to focus that session's terminal tab/window.
- **Notifications** — toggle the macOS notifications.
- **Play sounds** — master switch for all sounds.
- **Sound per state** — pick the sound for each state (**Finished** / **Working** / **Needs you**) from the macOS system sounds, or **None**. It previews the sound as you pick it and remembers your choice across restarts (overriding `config.json`).
- **Restart (hard reset)** — clears the state and relaunches the process if anything gets stuck.
- **Quit Claude Semaphore**.

Both toggles persist across restarts.

### Reopening it manually

Quit the app (or it isn't running)? The installer drops a **`Claude Semaphore.app`** into `~/Applications`, so you can reopen it like any other app — search "Claude Semaphore" in **Spotlight** (⌘Space), or double-click it in Finder / Launchpad. It just wakes the background agent back up (and does nothing if it's already running).

Prefer the terminal? `launchctl kickstart gui/$(id -u)/com.claude-semaphore.agent` does the same thing.

---

## Configure

Everything visible lives in `~/.claude-semaphore/config.json` (see [`config.example.json`](config/config.example.json)):

| Key | What |
|---|---|
| `states.*.icon` / `.label` | emoji + text per state |
| `states.*.sound` | a `/System/Library/Sounds` name (e.g. `Glass.aiff`), an absolute path, or `null` |
| `states.*.notify` | whether that state posts a notification |
| `cooking_frames` | the emojis the "working" state alternates through |
| `red_debounce_seconds` | how long `WAITING` must persist to count as a real wait (filters an instant approval) |
| `anim_seconds` | animation speed |
| `stale_seconds` | idle time before a stuck session is dropped |
| `done_ttl_seconds` | how long a finished session lingers in the menu before it's removed |
| `sweep_seconds` | janitor cadence while sessions are active |
| `show_elapsed` | append an elapsed timer while working (note: may slightly shift the text) |

Edit it, then **Restart (hard reset)** from the menu.

---

## Uninstall

```bash
./uninstall.sh
```

Removes the LaunchAgent and the `Claude Semaphore.app`, and cleanly strips only *its own* hooks from `settings.json` (with a backup). It asks before deleting `~/.claude-semaphore/`.

---

## Troubleshooting

- **Icon doesn't appear** → check `/tmp/claude-semaphore.err.log`.
- **Never turns red / "needs you"** → you're in *bypass permissions* mode (Claude auto-approves everything, so it never asks). Exit it with `Shift+Tab`.
- **Stuck after cancel (Escape)** → expected briefly; the anti-stuck reset returns it to ✅ after `stale_seconds`. Use **Restart (hard reset)** to skip the wait.
- **Changed hooks and nothing happens** → restart the Claude Code session.

---

## Roadmap

- 🐧🪟 Linux (`pystray` + `notify-send`) and Windows support
- 🍎 `.app` bundle / Homebrew formula (install without Python)
- 🖱️ Click to focus the Claude window
- 🧵 Multi-session awareness (today a single global state file is shared across sessions)

Contributions welcome!

---

## License

[MIT](LICENSE) © 2026 Pedro Gonzalez Nuñez

Not affiliated with Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.
