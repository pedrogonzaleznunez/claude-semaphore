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
APP_BUNDLE="$HOME/Applications/$APP_NAME.app"
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

# 5. Copiar app, hook emitter y config
cp "$SCRIPT_DIR/src/main.py" "$APP_DIR/main.py"
cp "$SCRIPT_DIR/src/hook.py" "$APP_DIR/hook.py"
mkdir -p "$APP_DIR/sessions"   # una entrada por sesión de Claude (multi-proyecto)
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
MARK = ".claude-semaphore/"  # firma de NUESTROS comandos (matchea el viejo …/state y el nuevo …/hook.py)

def is_ours(entry):
    return any(MARK in h.get("command", "") for h in entry.get("hooks", []))

# 1) Sacar NUESTROS hooks de TODOS los eventos (idempotente, y así se limpian
#    eventos que ya no usamos, p.ej. el viejo "Notification").
for event in list(hooks.keys()):
    hooks[event] = [e for e in hooks[event] if not is_ours(e)]
    if not hooks[event]:
        del hooks[event]

# 2) Agregar los hooks actuales de hooks.json.
for event, entries in our_hooks.items():
    hooks.setdefault(event, [])
    hooks[event] += entries

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

# 10. App para abrir manualmente (Spotlight / Finder / Launchpad)
#     Es un .app mínimo que revive el LaunchAgent con `launchctl kickstart`
#     (sin -k: si ya está corriendo, es un no-op).
echo "🖱  Creando app para abrir manualmente en ${APP_BUNDLE}…"
MACOS_DIR="$APP_BUNDLE/Contents/MacOS"
RES_DIR="$APP_BUNDLE/Contents/Resources"
mkdir -p "$MACOS_DIR" "$RES_DIR"

# Ícono de la app (para Spotlight/Finder): genera un .icns desde el PNG.
ICON_SRC="$SCRIPT_DIR/assets/icono-semaforo-512.png"
if [[ -f "$ICON_SRC" ]] && command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  echo "🎨 Generando ícono de la app…"
  ICONSET="$(mktemp -d)/AppIcon.iconset"
  mkdir -p "$ICONSET"
  for spec in "16:16x16" "32:16x16@2x" "32:32x32" "64:32x32@2x" \
              "128:128x128" "256:128x128@2x" "256:256x256" "512:256x256@2x" "512:512x512"; do
    px="${spec%%:*}"; name="${spec##*:}"
    sips -z "$px" "$px" "$ICON_SRC" --out "$ICONSET/icon_${name}.png" >/dev/null 2>&1
  done
  iconutil -c icns "$ICONSET" -o "$RES_DIR/AppIcon.icns" 2>/dev/null \
    || echo "⚠️  No se pudo generar el ícono; se usará el genérico."
  rm -rf "$(dirname "$ICONSET")"
fi

cat > "$APP_BUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>$APP_NAME</string>
    <key>CFBundleDisplayName</key><string>$APP_NAME</string>
    <key>CFBundleIdentifier</key><string>com.claude-semaphore.launcher</string>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1.0</string>
</dict>
</plist>
EOF

cat > "$MACOS_DIR/launcher" <<EOF
#!/bin/bash
# Claude Semaphore — abre la app de la barra de menú a pedido.
APP_ID="$APP_ID"
PLIST="$PLIST"
DOMAIN="gui/\$(id -u)"

if launchctl print "\$DOMAIN/\$APP_ID" >/dev/null 2>&1; then
  # Agente ya cargado: lo arranca si está muerto; no-op (sin -k) si ya corre.
  launchctl kickstart "\$DOMAIN/\$APP_ID"
else
  # No está cargado (p.ej. se desactivó): lo carga, y RunAtLoad lo arranca.
  launchctl bootstrap "\$DOMAIN" "\$PLIST" 2>/dev/null || launchctl load "\$PLIST"
fi
EOF
chmod +x "$MACOS_DIR/launcher"

# Refrescar LaunchServices e invalidar el cache de íconos (si no, Finder/Spotlight
# pueden seguir mostrando el ícono genérico).
touch "$APP_BUNDLE"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP_BUNDLE" 2>/dev/null || true

cat <<EOF

🎉 ¡Listo!
   • El ícono debería aparecer arriba en la barra de menú.
   • Reiniciá Claude Code para que tome los hooks (aceptá el cartel de revisión si aparece).
   • Para abrirla manualmente: buscá "$APP_NAME" en Spotlight (⌘Espacio) o abrila desde $APP_BUNDLE.
   • Config editable en: $CONFIG_FILE
   • Desinstalar:        ./uninstall.sh
EOF
