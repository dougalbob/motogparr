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

# ---------------------------------------------------------------------------
# Resolved paths
#
# A wrong path here is indistinguishable from an unmounted share: the app
# starts, logs nothing unusual, and quietly does nothing for hours. So print
# what was actually resolved, say where each value came from (an env var set by
# the container template beats settings.yml, which surprises people), and stop
# if a path that must exist does not.
# ---------------------------------------------------------------------------
echo ""
echo "  Paths:"
$PYTHON -c "
import sys
from utils.config_loader import startup_report
r = startup_report()
for e in r['paths']:
    if e['value'] is None:
        state = 'not set'
    else:
        state = 'ok' if e['exists'] else 'MISSING'
    src = e['source'] + (f\" via {e['env_var']}\" if e['source'] == 'env' else '')
    print(f\"    {e['label']:<15} {e['value']}  [{src}, {state}]\")
m = r['plex_path_mapping']
if m['container'] and m['plex']:
    print(f\"    {'Plex mapping':<15} {m['container']} -> {m['plex']}\")
else:
    print(f\"    {'Plex mapping':<15} not configured\")
if r['warnings']:
    print()
    for w in r['warnings']:
        print(f'    ⚠️  {w}')
" || echo "    ⚠️  Could not read configuration"

# ---------------------------------------------------------------------------
# Path severity
#
# A missing library path is worth refusing to start: nothing can be sorted, the
# watcher can never see a completed download, and the app would appear healthy
# while doing nothing for hours.
#
# A missing downloads path is only worth a warning. It is routine for that
# folder to not exist yet on a fresh install or after a share is reorganised,
# and refusing to boot the WebUI over it makes the problem impossible to fix
# through the UI - which is how a guard ends up causing the outage it was
# written to prevent.
# ---------------------------------------------------------------------------
PATH_STATUS=$($PYTHON -c "
import os
from utils.config_loader import get_motogp_library_path, get_downloads_path
for label, path in (('library', get_motogp_library_path()),
                    ('downloads', get_downloads_path())):
    if path and not os.path.isdir(str(path)):
        print(f'{label}={path}')
" 2>/dev/null)

LIBRARY_MISSING=$(echo "$PATH_STATUS" | sed -n 's/^library=//p')
DOWNLOAD_MISSING=$(echo "$PATH_STATUS" | sed -n 's/^downloads=//p')

if [ -n "$LIBRARY_MISSING" ]; then
    echo ""
    echo "  ❌ Library path does not exist inside the container:"
    echo "       ${LIBRARY_MISSING}"
    echo ""
    echo "     This is almost always a path that has moved, or an unmounted share."
    echo "     Nothing can be sorted until it is fixed, so the container is stopping."
    echo ""
    echo "     Set MOTOGP_LIBRARY_PATH (container template) or library.motogp_path"
    echo "     (settings.yml) to a folder that exists inside the container. Note the"
    echo "     environment variable wins, so check the template as well."
    echo "     To start anyway, set MOTOGPARR_SKIP_PATH_CHECK=1."
    echo "=================================================="
    if [ "${MOTOGPARR_SKIP_PATH_CHECK:-0}" != "1" ]; then
        exit 1
    fi
    echo "  (continuing because MOTOGPARR_SKIP_PATH_CHECK=1)"
fi

if [ -n "$DOWNLOAD_MISSING" ]; then
    echo ""
    echo "  ⚠️  Downloads path does not exist inside the container:"
    echo "       ${DOWNLOAD_MISSING}"
    echo "     No completed downloads can be found until it exists, but the WebUI"
    echo "     and scheduler are starting normally so you can fix it there."
fi

echo ""
echo "  Config Dir:     ${CONFIG_DIR}"
echo "  Settings File:  ${SETTINGS_FILE}"
echo "  WebUI Port:     5679"
echo "=================================================="

# ---------------------------------------------------------------------------
# Start the WebUI, supervised
#
# The WebUI used to be backgrounded with nothing watching it. If it died - a
# port clash, a crash on a bad route - the scheduler kept running, the container
# stayed "Up", and the only symptom was a blank page. That failure mode has cost
# real time, so the WebUI now gets restarted if it exits, and its death is
# logged loudly rather than swallowed.
# ---------------------------------------------------------------------------
WEBUI_PID=""
WEBUI_STARTED_AT=0
WEBUI_RESTARTS=0
MAX_WEBUI_RESTARTS=5

start_webui() {
    $PYTHON "$SCRIPT_DIR/motogp_webui.py" &
    WEBUI_PID=$!
    WEBUI_STARTED_AT=$(date +%s)
    echo "  WebUI started (PID: ${WEBUI_PID})"
}

# Ask bash's job table rather than kill -0: a background child that has exited
# may still be addressable as a zombie, which makes kill -0 report a dead
# process as alive.
is_running() {
    jobs -pr | grep -qx "$1"
}

echo "Starting WebUI..."
start_webui

sleep 2

# Run scheduler in the background too, so this loop can watch both.
echo "Starting Scheduler..."
$PYTHON "$SCRIPT_DIR/motogp_scheduler.py" &
SCHEDULER_PID=$!

shutdown() {
    echo ""
    echo "  Received signal, stopping..."
    kill "$WEBUI_PID" 2>/dev/null
    kill "$SCHEDULER_PID" 2>/dev/null
    wait
    exit 0
}
trap shutdown TERM INT

# Supervise both halves.
#
# The WebUI is restarted if it dies, because a dead UI used to be invisible: the
# scheduler kept running and the container stayed "Up". The scheduler is not
# restarted - if it dies the container has stopped doing its actual job, and
# pretending otherwise is worse than stopping.
while true; do
    sleep 10

    if ! is_running "$SCHEDULER_PID"; then
        wait "$SCHEDULER_PID"
        SCHEDULER_EXIT=$?
        echo ""
        echo "  ❌ Scheduler exited (code ${SCHEDULER_EXIT}). Stopping the container."
        kill "$WEBUI_PID" 2>/dev/null
        exit "${SCHEDULER_EXIT}"
    fi

    [ -z "$WEBUI_PID" ] && continue
    is_running "$WEBUI_PID" && continue

    wait "$WEBUI_PID" 2>/dev/null
    WEBUI_EXIT=$?
    RAN_FOR=$(( $(date +%s) - WEBUI_STARTED_AT ))

    echo ""
    echo "  ❌ WebUI exited (code ${WEBUI_EXIT}) after ${RAN_FOR}s."

    if [ "$RAN_FOR" -lt 10 ]; then
        echo "     It failed almost immediately, so this is a startup fault rather"
        echo "     than a transient crash. The usual cause is port 5679 already"
        echo "     being used by another container."
        echo "     The scheduler keeps running; the WebUI will not be retried."
        WEBUI_PID=""
        continue
    fi

    WEBUI_RESTARTS=$(( WEBUI_RESTARTS + 1 ))
    if [ "$WEBUI_RESTARTS" -gt "$MAX_WEBUI_RESTARTS" ]; then
        echo "     Restarted ${MAX_WEBUI_RESTARTS} times already; giving up."
        echo "     The scheduler keeps running. Check the logs above for the cause."
        WEBUI_PID=""
        continue
    fi

    echo "     Restarting it (attempt ${WEBUI_RESTARTS}/${MAX_WEBUI_RESTARTS});"
    echo "     the scheduler is unaffected."
    start_webui
done
