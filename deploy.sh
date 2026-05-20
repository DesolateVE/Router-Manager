#!/bin/bash
# Deploy mihomo_helper to OpenWrt via SSH
# Target: root@10.0.8.84  Password: weiyi

set -e

TARGET_HOST="10.0.8.84"
TARGET_USER="root"
TARGET_PASS="weiyi"
TARGET_DIR="/opt/mihomo_helper"
DATA_DIR="/etc/mihomo_helper"
SERVICE_PORT="8080"

# ---- helper functions -------------------------------------------------------

ssh_run() {
    sshpass -p "${TARGET_PASS}" ssh \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=10 \
        "${TARGET_USER}@${TARGET_HOST}" "$@"
}

scp_put() {
    sshpass -p "${TARGET_PASS}" scp \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=10 \
        "$@"
}

log() { echo "[deploy] $*"; }

# ---- preflight --------------------------------------------------------------

if ! command -v sshpass &>/dev/null; then
    echo "Error: sshpass is required."
    echo "  Ubuntu/Debian: sudo apt install sshpass"
    echo "  macOS:         brew install sshpass"
    exit 1
fi

log "Connecting to ${TARGET_USER}@${TARGET_HOST} ..."
ssh_run "uname -a"

# ---- transfer files ---------------------------------------------------------

log "Creating remote directories: ${TARGET_DIR}  ${DATA_DIR}"
ssh_run "mkdir -p '${TARGET_DIR}/api' '${TARGET_DIR}/web' '${DATA_DIR}'"

log "Uploading Python source files ..."
scp_put main.py models.py config_gen.py import_engine.py \
        process_mgr.py store.py requirements.txt \
        "${TARGET_USER}@${TARGET_HOST}:${TARGET_DIR}/"

log "Uploading api/ ..."
scp_put api/__init__.py api/routes.py \
        "${TARGET_USER}@${TARGET_HOST}:${TARGET_DIR}/api/"

log "Uploading web/ ..."
scp_put web/index.html web/app.js web/style.css \
        "${TARGET_USER}@${TARGET_HOST}:${TARGET_DIR}/web/"

log "Uploading resource files to ${DATA_DIR} ..."
for f in resource/*; do
    [[ -f "$f" ]] || continue
    log "  -> $f"
    scp_put "$f" "${TARGET_USER}@${TARGET_HOST}:${DATA_DIR}/"
done

log "Setting execute permission on firewall scripts ..."
ssh_run "chmod +x '${DATA_DIR}/mihomo_firewall.sh' '${DATA_DIR}/mihomo_cleanup.sh' 2>/dev/null; true"

# ---- install Python & dependencies ------------------------------------------

# log "Updating opkg and installing python3 / python3-pip ..."
# ssh_run "opkg update && opkg install python3 python3-pip"

# log "Installing Python dependencies ..."
# ssh_run "pip3 install -r '${TARGET_DIR}/requirements.txt'"

# ---- install procd init script ----------------------------------------------

log "Writing /etc/init.d/mihomo_helper ..."

# Build the init script locally, then pipe it to the remote host.
INIT_SCRIPT=$(cat <<INIT
#!/bin/sh /etc/rc.common

USE_PROCD=1
APP_NAME=mihomo_helper
START=95
STOP=01

start_service() {
    # 初始化防火墙/TProxy 规则
    if [ -x ${DATA_DIR}/mihomo_firewall.sh ]; then
        ${DATA_DIR}/mihomo_firewall.sh
    fi

    procd_open_instance
    procd_set_param command python3 ${TARGET_DIR}/main.py \\
        --data-dir ${DATA_DIR} \\
        --port ${SERVICE_PORT}
    procd_set_param respawn \${respawn_threshold:-3600} \${respawn_timeout:-5} \${respawn_retry:-5}
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_close_instance
}

stop_service() {
    # procd 停止主进程后清理防火墙/TProxy 规则
    if [ -x ${DATA_DIR}/mihomo_cleanup.sh ]; then
        ${DATA_DIR}/mihomo_cleanup.sh
    fi
}

reload_service() {
    stop
    start
}
INIT
)

echo "${INIT_SCRIPT}" | ssh_run "cat > /etc/init.d/mihomo_helper"
ssh_run "chmod +x /etc/init.d/mihomo_helper"

# ---- enable & start ---------------------------------------------------------

log "Enabling service (auto-start on boot) ..."
ssh_run "/etc/init.d/mihomo_helper enable"

log "Starting service ..."
ssh_run "/etc/init.d/mihomo_helper start"

# ---- verify -----------------------------------------------------------------

sleep 2
STATUS=$(ssh_run "/etc/init.d/mihomo_helper status" 2>/dev/null || echo "unknown")
log "Service status: ${STATUS}"

log "Done! Web UI:  http://${TARGET_HOST}:${SERVICE_PORT}"
