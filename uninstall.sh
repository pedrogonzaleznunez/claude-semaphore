#!/usr/bin/env bash
#
# Claude Semaphore — uninstaller (macOS)
#
set -euo pipefail

APP_ID="com.claude-semaphore.agent"
APP_DIR="$HOME/.claude-semaphore"
PLIST="$HOME/Library/LaunchAgents/$APP_ID.plist"
APP_BUNDLE="$HOME/Applications/Claude Semaphore.app"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"

echo "🧹 Desinstalando Claude Semaphore…"

# 1. Parar y quitar el auto-arranque
launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"

# 1b. Quitar la app de "abrir manualmente"
rm -rf "$APP_BUNDLE"

# 2. Sacar NUESTROS hooks del settings.json, dejando el resto intacto (con backup)
if [[ -f "$CLAUDE_SETTINGS" ]]; then
  python3 - "$CLAUDE_SETTINGS" <<'PY'
import json, os, sys, time, shutil
p = sys.argv[1]
try:
    with open(p) as f:
        s = json.load(f)
except Exception:
    sys.exit(0)
shutil.copy(p, p + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
MARK = ".claude-semaphore/"  # matchea el viejo …/state y el nuevo …/hook.py
hooks = s.get("hooks", {})
for event in list(hooks.keys()):
    hooks[event] = [e for e in hooks[event]
                    if not any(MARK in h.get("command", "") for h in e.get("hooks", []))]
    if not hooks[event]:
        del hooks[event]
if not hooks:
    s.pop("hooks", None)
with open(p, "w") as f:
    json.dump(s, f, indent=2, ensure_ascii=False)
    f.write("\n")
print("✅ Hooks removidos del settings.json (con backup).")
PY
fi

# 3. Datos de la app (config + estado)
read -r -p "¿Borrar también $APP_DIR (incluye tu config.json)? [y/N] " ans
if [[ "${ans:-N}" =~ ^[Yy]$ ]]; then
  rm -rf "$APP_DIR"
  echo "🗑  $APP_DIR borrado."
else
  echo "📁 $APP_DIR conservado."
fi

echo "Listo. Reiniciá Claude Code para soltar los hooks."
