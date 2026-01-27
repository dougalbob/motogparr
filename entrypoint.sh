#!/bin/bash
# MotoGParr Entrypoint

SCRIPT_DIR="/app"
PYTHON="/opt/venv/bin/python3"
CONFIG_DIR="${CONFIG_DIR:-/config}"
SETTINGS_FILE="${CONFIG_DIR}/settings.yml"

echo "================================================"
echo "  MotoGParr - MotoGP Automated Recording"
echo "================================================"

# Ensure config directory exists
mkdir -p "${CONFIG_DIR}"

# Copy calendar files if they don't exist (first run)
for calendar in /app/config/*.json; do
    if [ -f "$calendar" ]; then
        filename=$(basename "$calendar")
        if [ ! -f "${CONFIG_DIR}/${filename}" ]; then
            echo "  Copying ${filename} to config..."
            cp "$calendar" "${CONFIG_DIR}/"
        fi
    fi
done

# Create settings.yml template if it doesn't exist
if [ ! -f "$SETTINGS_FILE" ]; then
    echo "  Creating settings.yml template..."
    # (template content same as before)
fi

# Validate configuration
echo ""
echo "  Checking configuration..."
$PYTHON -c "
from utils.config_loader import validate
v = validate()
if v['valid']:
    print('  ✅ All required settings configured')
else:
    print('  ⚠️  Missing settings:', ', '.join(v['missing']))
"

echo ""
echo "  Config Dir:     ${CONFIG_DIR}"
echo "  Settings File:  ${SETTINGS_FILE}"
echo "  WebUI Port:     5679"
echo "================================================"

# Start WebUI in background
echo "Starting WebUI..."
$PYTHON "$SCRIPT_DIR/motogp_webui.py" &
WEBUI_PID=$!
echo "  WebUI started (PID: $WEBUI_PID)"

sleep 2

# Run scheduler in foreground
echo "Starting Scheduler..."
exec $PYTHON "$SCRIPT_DIR/motogp_scheduler.py"