#!/bin/bash
# MotoGParr Entrypoint

SCRIPT_DIR="/app"
PYTHON="/opt/venv/bin/python3"
CONFIG_DIR="${CONFIG_DIR:-/config}"
SETTINGS_FILE="${CONFIG_DIR}/settings.yml"
IMAGE_CALENDARS="/app/config"

echo "=================================================="
echo "  MotoGParr - MotoGP Automated Recording"
echo "=================================================="

if [ -n "${MOTOGPARR_VERSION}" ]; then
    echo "  Version: ${MOTOGPARR_VERSION}"
    echo "--------------------------------------------------"
fi

# Ensure config directory exists
mkdir -p "${CONFIG_DIR}"

# ---------------------------------------------------------------------------
# Calendars
#
# /config is a persistent volume, so the calendars living there survive image
# updates. That means a new image does NOT automatically update them - and a
# stale calendar silently produces wrong round numbers. We therefore compare
# every season and either refresh it or warn loudly. A calendar is only replaced
# automatically when it can prove it is the same season (same set of venues), so
# a file for a different championship is never overwritten. Note that "auto"
# will also roll back a local edit to a calendar, which is why a .bak is kept.
#
#   MOTOGPARR_CALENDAR_UPDATE=auto | warn | off   (default: auto)
# ---------------------------------------------------------------------------
CALENDAR_UPDATE="${MOTOGPARR_CALENDAR_UPDATE:-auto}"

echo ""
echo "  Calendars (${CALENDAR_UPDATE}):"

for calendar in "${IMAGE_CALENDARS}"/motogp_*.json; do
    [ -f "$calendar" ] || continue
    filename=$(basename "$calendar")
    target="${CONFIG_DIR}/${filename}"

    if [ ! -f "$target" ]; then
        echo "    + ${filename}: copying into config"
        cp "$calendar" "$target"
        continue
    fi

    if cmp -s "$calendar" "$target"; then
        echo "    = ${filename}: up to date"
        continue
    fi

    # Files differ: decide whether replacing the config copy is safe.
    case "$CALENDAR_UPDATE" in
        off)
            echo "    ? ${filename}: differs from image, left alone (updates off)"
            continue
            ;;
    esac

    $PYTHON "${SCRIPT_DIR}/utils/calendar_sync.py" \
        --image "$calendar" --config "$target" --json > /tmp/calendar_status.json 2>/dev/null

    summary=$(sed -n 's/.*"change_summary": "\(.*\)",/\1/p' /tmp/calendar_status.json)

    if [ "$CALENDAR_UPDATE" = "auto" ] && grep -q '"same_season": true' /tmp/calendar_status.json; then
        if $PYTHON "${SCRIPT_DIR}/utils/calendar_sync.py" \
                --image "$calendar" --config "$target" --apply > /dev/null 2>&1; then
            echo "    ^ ${filename}: updated from image"
            echo "        ${summary}"
            echo "        previous copy saved as ${filename}.bak"
            continue
        fi
    fi

    # Either updates are off, or the files disagree about which season this is.
    echo "    ! ${filename}: DIFFERS from the calendar shipped in this image"
    echo "        ${summary}"
    if grep -q '"same_season": false' /tmp/calendar_status.json; then
        echo "        These do not look like the same season - not replacing it."
    else
        echo "        Your calendar is probably out of date after an update."
        echo "        To refresh it (a .bak copy is kept):"
        echo "          cp ${target} ${target}.bak && cp ${calendar} ${target}"
        echo "        Or start with MOTOGPARR_CALENDAR_UPDATE=auto to allow this."
    fi
done

# Create settings.yml template if it doesn't exist. The WebUI can fill it in
# afterwards, but having a real file on disk means the container validates and
# starts cleanly on a fresh install instead of complaining about every setting.
if [ ! -f "$SETTINGS_FILE" ]; then
    echo ""
    echo "  No settings.yml found - creating a starter template."
    if $PYTHON -c "from utils.config_loader import create_template; raise SystemExit(0 if create_template() else 1)"; then
        echo "  Created ${SETTINGS_FILE} - fill in the blanks in the WebUI (port 5679)."
    else
        echo "  ⚠️  Could not create ${SETTINGS_FILE}; check that ${CONFIG_DIR} is writable."
    fi
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
echo "=================================================="

# Start WebUI in background
echo "Starting WebUI..."
$PYTHON "$SCRIPT_DIR/motogp_webui.py" &
WEBUI_PID=$!
echo "  WebUI started (PID: $WEBUI_PID)"

sleep 2

# Run scheduler in foreground
echo "Starting Scheduler..."
exec $PYTHON "$SCRIPT_DIR/motogp_scheduler.py"
