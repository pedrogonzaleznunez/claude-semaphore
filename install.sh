#!/usr/bin/env bash
#
# Claude Semaphore — installer (macOS beta)
#
# Qué hace:
#   1. Crea ~/.claude-semaphore/ (app dir), con su propio venv + rumps.
#   2. Copia main.py y crea config.json (respeta uno existente).
#   3. Integra los hooks en ~/.claude/settings.json de forma SEGURA
#      (backup + merge idempotente, sin pisar tu config).
#   4. Genera el LaunchAgent con tus rutas reales y lo carga (auto-arranque).
#
set -euo pipefail

APP_ID="com.claude-semaphore.agent"
APP_NAME="Claude Semaphore"
APP_DIR="$HOME/.claude-semaphore"
STATE_FILE="$APP_DIR/state"
CONFIG_FILE="$APP_DIR/config.json"
VENV="$APP_DIR/venv"
PLIST="$HOME/Library/LaunchAgents/$APP_ID.plist"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🚦 Instalando ${APP_NAME}…"

# 1. Solo macOS (beta)
if [[ "$(uname)" != "Darwin" ]]; then
  echo "❌ Esta beta es solo para macOS. Linux/Windows están en el roadmap."
  exit 1
fi

# 2. python3
if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ Necesitás python3. Instalalo (p.ej. 'brew install python') y reintentá."
  exit 1
fi

# 3. Directorios
mkdir -p "$APP_DIR" "$(dirname "$PLIST")"

# 4. venv + rumps
if [[ ! -d "$VENV" ]]; then
  echo "📦 Creando entorno virtual en ${VENV}…"
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
echo "📦 Instalando rumps…"
"$VENV/bin/pip" install --quiet rumps

# 5. Copiar app y config
cp "$SCRIPT_DIR/src/main.py" "$APP_DIR/main.py"
if [[ ! -f "$CONFIG_FILE" ]]; then
  cp "$SCRIPT_DIR/config/config.example.json" "$CONFIG_FILE"
  echo "🛠  Config creada en $CONFIG_FILE"
else
  echo "🛠  Ya existía config; se respeta: $CONFIG_FILE"
fi

# 6. Estado inicial
printf DONE > "$STATE_FILE"

# 7. Merge SEGURO de hooks en settings.json
echo "🪝 Integrando hooks en ${CLAUDE_SETTINGS}…"
mkdir -p "$(dirname "$CLAUDE_SETTINGS")"
HOOKS_JSON="$SCRIPT_DIR/config/hooks.json" python3 - "$CLAUDE_SETTINGS" <<'PY'
import json, os, sys, time, shutil

settings_path = sys.argv[1]
with open(os.environ["HOOKS_JSON"]) as f:
    our_hooks = json.load(f)

settings = {}
if os.path.exists(settings_path):
    shutil.copy(settings_path, settings_path + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except Exception:
        print("⚠️  settings.json no era JSON válido; se guardó backup y se recrea el bloque de hooks.")
        settings = {}

hooks = settings.setdefault("hooks", {})
MARK = ".claude-semaphore/state"  # firma de NUESTROS comandos

def is_ours(entry):
    return any(MARK in h.get("command", "") for h in entry.get("hooks", []))

for event, entries in our_hooks.items():
    kept = [e for e in hooks.get(event, []) if not is_ours(e)]  # idempotente
    hooks[event] = kept + entries

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2, ensure_ascii=False)
    f.write("\n")
print("✅ Hooks integrados (con backup del settings.json previo).")
PY

# 8. Generar el LaunchAgent con rutas reales
echo "🚀 Configurando auto-arranque…"
sed -e "s|{{PYTHON}}|$VENV/bin/python3|g" \
    -e "s|{{SCRIPT}}|$APP_DIR/main.py|g" \
    -e "s|{{APP_ID}}|$APP_ID|g" \
    "$SCRIPT_DIR/config/com.claude-semaphore.plist.template" > "$PLIST"

# 9. Cargar
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

cat <<EOF

🎉 ¡Listo!
   • El ícono debería aparecer arriba en la barra de menú.
   • Reiniciá Claude Code para que tome los hooks (aceptá el cartel de revisión si aparece).
   • Config editable en: $CONFIG_FILE
   • Desinstalar:        ./uninstall.sh
EOF
