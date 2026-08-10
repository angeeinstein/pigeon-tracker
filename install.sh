#!/usr/bin/env bash
#
# turret-control installer / updater.
#
#   curl -fsSL https://raw.githubusercontent.com/angeeinstein/pigeon-tracker/main/install.sh | sudo bash
#
# The same command installs, updates and repairs: it detects what is already
# there and does the right thing. Running it twice must never damage anything,
# and user data (configuration, calibration, zones, event history) is never
# touched except by an explicit migration.
#
# Flags:
#   --install            force a fresh installation
#   --update             update an existing installation
#   --repair             reinstall dependencies without changing the code
#   --restart            restart the service and exit
#   --status             show status and exit
#   --uninstall          remove the service and code (keeps data unless --purge)
#   --purge              with --uninstall, also remove configuration and data
#   --yes                never prompt (for automation)
#   --branch <name>      git branch/tag to install (default: main)
#   --repo <url>         source repository
#   --port <n>           HTTP port (default: 8080)
#   --no-ai              skip the AI stack (torch/ultralytics) - much smaller
#   --no-frontend        skip building the web UI
#   --with-gstreamer     use the system OpenCV so GStreamer capture is available

set -Eeuo pipefail

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
REPO_URL="${TURRET_REPO_URL:-https://github.com/angeeinstein/pigeon-tracker.git}"
BRANCH="${TURRET_BRANCH:-main}"

APP_USER="turret"
APP_GROUP="turret"
APP_DIR="/opt/turret-control"
CONFIG_DIR="/etc/turret-control"
DATA_DIR="/var/lib/turret-control"
BACKUP_DIR="/var/backups/turret-control"
VENV_DIR="${APP_DIR}/venv"
SERVER_DIR="${APP_DIR}/server"
SERVICE_NAME="turret-control"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="${CONFIG_DIR}/turret.env"
HTTP_PORT="${TURRET_PORT:-8080}"

MODE=""
ASSUME_YES=0
INSTALL_AI=1
BUILD_FRONTEND=1
USE_SYSTEM_GSTREAMER=0
PURGE=0

MIN_NODE_MAJOR=18
NODESOURCE_VERSION=20

export DEBIAN_FRONTEND=noninteractive

# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------
if [[ -t 1 ]]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'
else
    C_RESET=""; C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""
fi

step()  { printf '%s==>%s %s\n' "${C_BLUE}${C_BOLD}" "${C_RESET}" "$*"; }
info()  { printf '    %s\n' "$*"; }
ok()    { printf '    %s✓%s %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn()  { printf '    %s!%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()   { printf '%sERROR:%s %s\n' "${C_RED}${C_BOLD}" "${C_RESET}" "$*" >&2; exit 1; }

on_error() {
    local line=$1
    printf '\n%sInstallation failed (line %s).%s\n' "${C_RED}${C_BOLD}" "${line}" "${C_RESET}" >&2
    if [[ -n "${ROLLBACK_COMMIT:-}" ]]; then
        warn "attempting to restore the previous version (${ROLLBACK_COMMIT})"
        if git -C "${APP_DIR}" checkout --quiet "${ROLLBACK_COMMIT}" 2>/dev/null; then
            systemctl restart "${SERVICE_NAME}" 2>/dev/null || true
            warn "restored the previous code; the service was restarted"
        else
            warn "automatic rollback failed - the installation may be inconsistent"
        fi
    fi
    printf '\nDiagnostics:\n  journalctl -u %s -n 80 --no-pager\n' "${SERVICE_NAME}" >&2
    exit 1
}
trap 'on_error $LINENO' ERR

confirm() {
    [[ ${ASSUME_YES} -eq 1 ]] && return 0
    [[ -t 0 ]] || return 0
    local answer
    read -r -p "$1 [Y/n] " answer || true
    [[ -z "${answer}" || "${answer}" =~ ^[Yy] ]]
}

# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --install)        MODE="install" ;;
            --update)         MODE="update" ;;
            --repair)         MODE="repair" ;;
            --restart)        MODE="restart" ;;
            --status)         MODE="status" ;;
            --uninstall)      MODE="uninstall" ;;
            --purge)          PURGE=1 ;;
            --yes|-y)         ASSUME_YES=1 ;;
            --branch)         BRANCH="${2:?--branch needs a value}"; shift ;;
            --repo)           REPO_URL="${2:?--repo needs a value}"; shift ;;
            --port)           HTTP_PORT="${2:?--port needs a value}"; shift ;;
            --no-ai)          INSTALL_AI=0 ;;
            --no-frontend)    BUILD_FRONTEND=0 ;;
            --with-gstreamer) USE_SYSTEM_GSTREAMER=1 ;;
            -h|--help)        sed -n '3,30p' "$0"; exit 0 ;;
            *)                die "unknown option: $1 (try --help)" ;;
        esac
        shift
    done
}

# --------------------------------------------------------------------------
# environment checks
# --------------------------------------------------------------------------
require_root() {
    [[ ${EUID} -eq 0 ]] || die "this script must run as root (use: sudo bash install.sh)"
}

check_os() {
    [[ -r /etc/os-release ]] || die "unsupported system: /etc/os-release is missing"
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}${ID_LIKE:-}" in
        *debian*|*ubuntu*) ;;
        *) die "unsupported distribution '${PRETTY_NAME:-unknown}'. Debian/Ubuntu is required." ;;
    esac
    command -v systemctl >/dev/null 2>&1 || die "systemd is required"
    ok "${PRETTY_NAME:-Debian-like system} detected"
}

is_installed() {
    [[ -d "${APP_DIR}/.git" ]] || [[ -f "${SERVICE_FILE}" ]]
}

# --------------------------------------------------------------------------
# packages
# --------------------------------------------------------------------------
apt_packages() {
    local packages=(
        ca-certificates curl git jq
        python3 python3-venv python3-dev python3-pip
        build-essential pkg-config
        ffmpeg
        libgl1 libglib2.0-0
        gstreamer1.0-plugins-base gstreamer1.0-plugins-good
        gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-tools
    )
    if [[ ${USE_SYSTEM_GSTREAMER} -eq 1 ]]; then
        # The distro OpenCV is built with GStreamer support; the PyPI wheels
        # are not. Only pulled in when the user asks for it.
        packages+=(python3-opencv python3-numpy)
    fi
    printf '%s\n' "${packages[@]}"
}

install_packages() {
    step "Installing system packages"
    apt-get update -qq
    # shellcheck disable=SC2046
    apt-get install -y -qq --no-install-recommends $(apt_packages | tr '\n' ' ') >/dev/null
    ok "system packages installed"

    if [[ ${BUILD_FRONTEND} -eq 1 ]]; then
        install_node
    fi
}

node_major() {
    command -v node >/dev/null 2>&1 || { echo 0; return; }
    node --version 2>/dev/null | sed 's/^v\([0-9]*\).*/\1/'
}

install_node() {
    local major
    major="$(node_major)"
    if [[ "${major}" -ge ${MIN_NODE_MAJOR} ]]; then
        ok "Node.js $(node --version) is present"
        return
    fi

    info "installing Node.js (found major version '${major:-none}', need >= ${MIN_NODE_MAJOR})"
    if apt-get install -y -qq --no-install-recommends nodejs npm >/dev/null 2>&1 &&
       [[ "$(node_major)" -ge ${MIN_NODE_MAJOR} ]]; then
        ok "Node.js $(node --version) installed from the distribution"
        return
    fi

    info "distribution Node.js is too old; using NodeSource ${NODESOURCE_VERSION}.x"
    curl -fsSL "https://deb.nodesource.com/setup_${NODESOURCE_VERSION}.x" -o /tmp/nodesource.sh
    bash /tmp/nodesource.sh >/dev/null
    rm -f /tmp/nodesource.sh
    apt-get install -y -qq nodejs >/dev/null
    [[ "$(node_major)" -ge ${MIN_NODE_MAJOR} ]] ||
        die "Node.js >= ${MIN_NODE_MAJOR} could not be installed (use --no-frontend to skip the UI build)"
    ok "Node.js $(node --version) installed"
}

# --------------------------------------------------------------------------
# user, directories, code
# --------------------------------------------------------------------------
create_user() {
    if id -u "${APP_USER}" >/dev/null 2>&1; then
        ok "user '${APP_USER}' exists"
    else
        step "Creating system user '${APP_USER}'"
        useradd --system --home-dir "${DATA_DIR}" --shell /usr/sbin/nologin \
                --comment "turret-control service" "${APP_USER}"
        ok "user created"
    fi
}

create_directories() {
    step "Creating directories"
    install -d -o root -g root -m 0755 "${APP_DIR}"
    install -d -o root -g "${APP_GROUP}" -m 0750 "${CONFIG_DIR}"
    install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 "${DATA_DIR}"
    install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 "${DATA_DIR}/models"
    install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 "${DATA_DIR}/snapshots"
    install -d -o root -g root -m 0700 "${BACKUP_DIR}"
    ok "directories ready"
}

fetch_code() {
    step "Fetching the application (${BRANCH})"
    if [[ -d "${APP_DIR}/.git" ]]; then
        ROLLBACK_COMMIT="$(git -C "${APP_DIR}" rev-parse HEAD)"
        git -C "${APP_DIR}" remote set-url origin "${REPO_URL}"
        git -C "${APP_DIR}" fetch --quiet --depth 50 origin "${BRANCH}"
        # Discard local modifications to tracked files, never to data: the
        # database, config and secrets live outside the repository.
        git -C "${APP_DIR}" checkout --quiet -B "${BRANCH}" "origin/${BRANCH}"
        git -C "${APP_DIR}" reset --hard --quiet "origin/${BRANCH}"
        ok "updated to $(git -C "${APP_DIR}" rev-parse --short HEAD)"
    else
        # A local checkout (developer running ./install.sh from the repo) is
        # copied rather than cloned, so testing an uncommitted change works.
        local script_dir
        script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        if [[ -d "${script_dir}/server/app" && "${script_dir}" != "${APP_DIR}" ]]; then
            info "installing from the local checkout at ${script_dir}"
            mkdir -p "${APP_DIR}"
            tar -C "${script_dir}" --exclude=.git --exclude=node_modules --exclude=.venv \
                --exclude=venv -cf - . | tar -C "${APP_DIR}" -xf -
        else
            git clone --quiet --depth 50 --branch "${BRANCH}" "${REPO_URL}" "${APP_DIR}"
        fi
        ok "source installed in ${APP_DIR}"
    fi

    # Record the commit so /api/version reports it even without git present.
    local commit="unknown"
    commit="$(git -C "${APP_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    printf 'GIT_COMMIT = "%s"\n' "${commit}" > "${SERVER_DIR}/app/_build_info.py"
}

# --------------------------------------------------------------------------
# python environment
# --------------------------------------------------------------------------
has_nvidia_gpu() {
    command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1
}

setup_venv() {
    step "Setting up the Python environment"
    local venv_args=()
    [[ ${USE_SYSTEM_GSTREAMER} -eq 1 ]] && venv_args+=(--system-site-packages)

    if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
        python3 -m venv "${venv_args[@]}" "${VENV_DIR}"
        ok "virtual environment created"
    else
        ok "virtual environment exists"
    fi

    "${VENV_DIR}/bin/python" -m pip install --quiet --upgrade pip wheel setuptools

    info "installing runtime dependencies"
    "${VENV_DIR}/bin/pip" install --quiet -r "${SERVER_DIR}/requirements/base.txt"

    if [[ ${USE_SYSTEM_GSTREAMER} -eq 1 ]]; then
        # The pip wheel would shadow the GStreamer-enabled system build.
        "${VENV_DIR}/bin/pip" uninstall -y -q opencv-python-headless >/dev/null 2>&1 || true
    fi

    if [[ ${INSTALL_AI} -eq 1 ]]; then
        if has_nvidia_gpu; then
            info "NVIDIA GPU detected - installing the CUDA build of the AI stack"
            "${VENV_DIR}/bin/pip" install --quiet -r "${SERVER_DIR}/requirements/ai.txt"
        else
            info "no GPU detected - installing the CPU-only AI stack (this takes a while)"
            "${VENV_DIR}/bin/pip" install --quiet \
                --extra-index-url https://download.pytorch.org/whl/cpu \
                -r "${SERVER_DIR}/requirements/ai.txt"
        fi
        ok "AI stack installed"
    else
        warn "skipping the AI stack (--no-ai): detection falls back to the mock detector"
    fi
}

# --------------------------------------------------------------------------
# frontend
# --------------------------------------------------------------------------
build_frontend() {
    if [[ ${BUILD_FRONTEND} -eq 0 ]]; then
        warn "skipping the web UI build (--no-frontend)"
        return
    fi
    step "Building the web interface"
    pushd "${SERVER_DIR}/frontend" >/dev/null
    if [[ -f package-lock.json ]]; then
        npm ci --silent --no-audit --no-fund
    else
        npm install --silent --no-audit --no-fund
    fi
    npm run build --silent
    popd >/dev/null
    [[ -f "${SERVER_DIR}/app/static/index.html" ]] ||
        die "the frontend build produced no index.html"
    ok "web interface built"
}

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
write_config() {
    step "Configuring"
    if [[ -f "${ENV_FILE}" ]]; then
        ok "keeping the existing ${ENV_FILE}"
    else
        local controller_token
        controller_token="$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | xxd -p)"
        cat > "${ENV_FILE}" <<EOF
# turret-control environment - created by install.sh on $(date -Is)
# This file contains secrets. Do not commit it anywhere.

TURRET_HOST=0.0.0.0
TURRET_PORT=${HTTP_PORT}
TURRET_DATA_DIR=${DATA_DIR}
TURRET_LOG_LEVEL=INFO
TURRET_LOG_FORMAT=console

# Web UI login. Disabled by default for trusted LAN use.
TURRET_AUTH_ENABLED=false
TURRET_AUTH_USERNAME=admin
TURRET_AUTH_PASSWORD=

# Pre-shared token the ESP32 must present. Copy this into
# firmware/include/secrets.h as TURRET_CONTROLLER_TOKEN.
TURRET_CONTROLLER_TOKEN=${controller_token}

# Camera credentials referenced from camera URLs as \${CAM_PASSWORD}
# CAM_PASSWORD=
EOF
        ok "created ${ENV_FILE} with a generated controller token"
    fi
    chown root:"${APP_GROUP}" "${ENV_FILE}"
    chmod 0640 "${ENV_FILE}"

    chown -R "${APP_USER}:${APP_GROUP}" "${DATA_DIR}"
    # The application never writes into its own code directory.
    chown -R root:root "${APP_DIR}"
    chmod -R go-w "${APP_DIR}"
}

backup_data() {
    [[ -d "${DATA_DIR}" ]] || return 0
    step "Backing up configuration and data"
    local stamp archive
    stamp="$(date +%Y%m%d-%H%M%S)"
    archive="${BACKUP_DIR}/turret-${stamp}.tar.gz"
    tar -czf "${archive}" \
        -C / \
        --exclude="${DATA_DIR#/}/snapshots" \
        --exclude="${DATA_DIR#/}/models" \
        "${DATA_DIR#/}" \
        $([[ -f "${ENV_FILE}" ]] && echo "${ENV_FILE#/}") 2>/dev/null || true
    chmod 0600 "${archive}" 2>/dev/null || true
    ok "backup written to ${archive}"

    # Keep the five most recent backups; older ones are not worth the disk.
    ls -1t "${BACKUP_DIR}"/turret-*.tar.gz 2>/dev/null | tail -n +6 | xargs -r rm -f
}

# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------
install_service() {
    step "Installing the systemd service"
    local source="${APP_DIR}/deploy/${SERVICE_NAME}.service"
    [[ -f "${source}" ]] || die "service template missing at ${source}"

    local changed=0
    if ! cmp -s "${source}" "${SERVICE_FILE}"; then
        install -m 0644 "${source}" "${SERVICE_FILE}"
        changed=1
    fi
    systemctl daemon-reload
    systemctl enable --quiet "${SERVICE_NAME}"
    [[ ${changed} -eq 1 ]] && ok "service file updated" || ok "service file unchanged"
}

restart_service() {
    step "Restarting the service"
    systemctl restart "${SERVICE_NAME}"
    ok "restart requested"
}

wait_for_health() {
    step "Verifying"
    local url="http://127.0.0.1:${HTTP_PORT}/api/health"
    local attempt
    for attempt in $(seq 1 60); do
        if ! systemctl is-active --quiet "${SERVICE_NAME}"; then
            sleep 1
            continue
        fi
        if curl -fsS --max-time 3 "${url}" >/tmp/turret-health.json 2>/dev/null; then
            local status
            status="$(jq -r '.status' </tmp/turret-health.json 2>/dev/null || echo unknown)"
            ok "service is up (health: ${status})"
            if [[ "${status}" != "ok" ]]; then
                info "degraded checks: $(jq -r '.checks | to_entries[] | select(.value==false) | .key' \
                    </tmp/turret-health.json 2>/dev/null | tr '\n' ' ')"
                info "this is normal before the camera and controller are configured"
            fi
            rm -f /tmp/turret-health.json
            return 0
        fi
        sleep 1
    done

    warn "the service did not become healthy within 60 s"
    journalctl -u "${SERVICE_NAME}" -n 40 --no-pager >&2 || true
    return 1
}

show_status() {
    systemctl status "${SERVICE_NAME}" --no-pager --lines 15 || true
    echo
    curl -fsS --max-time 3 "http://127.0.0.1:${HTTP_PORT}/api/health" 2>/dev/null |
        jq . 2>/dev/null || echo "health endpoint not reachable"
}

primary_address() {
    hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1"
}

summary() {
    local address token
    address="$(primary_address)"
    token="$(grep -E '^TURRET_CONTROLLER_TOKEN=' "${ENV_FILE}" 2>/dev/null | cut -d= -f2- || true)"

    cat <<EOF

${C_GREEN}${C_BOLD}turret-control is installed.${C_RESET}

  Web interface   ${C_BOLD}http://${address}:${HTTP_PORT}/${C_RESET}
  Health          http://${address}:${HTTP_PORT}/api/health
  Version         $(git -C "${APP_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown) (${BRANCH})

  Configuration   ${ENV_FILE}
  Data            ${DATA_DIR}
  Code            ${APP_DIR}
  Backups         ${BACKUP_DIR}

  Logs            journalctl -u ${SERVICE_NAME} -f
  Restart         systemctl restart ${SERVICE_NAME}
  Update          sudo bash ${APP_DIR}/install.sh --update

${C_BOLD}Next steps${C_RESET}
  1. Open the web interface and add your camera under Settings > Camera.
  2. Flash the ESP32 (firmware/README.md). Put this token in
     firmware/include/secrets.h:
         #define TURRET_CONTROLLER_TOKEN "${token:-<see ${ENV_FILE}>}"
  3. Home the turret, then measure calibration points under Calibration.
  4. Draw zones, then enable automatic targeting when you are happy with it.

${C_DIM}Water output is disabled and the system is disarmed until you turn them on.${C_RESET}
EOF
}

# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------
do_install() {
    step "Installing turret-control"
    check_os
    install_packages
    create_user
    create_directories
    fetch_code
    setup_venv
    build_frontend
    write_config
    install_service
    restart_service
    wait_for_health
    summary
}

do_update() {
    step "Updating turret-control"
    check_os
    backup_data
    install_packages
    create_directories
    fetch_code
    setup_venv
    build_frontend
    write_config
    install_service
    restart_service
    wait_for_health
    ok "update complete"
    summary
}

do_repair() {
    step "Repairing the installation"
    check_os
    install_packages
    create_user
    create_directories
    setup_venv
    build_frontend
    write_config
    install_service
    restart_service
    wait_for_health
    ok "repair complete"
}

do_uninstall() {
    confirm "Remove the turret-control service and application?" || { info "cancelled"; exit 0; }
    step "Uninstalling"
    systemctl disable --now "${SERVICE_NAME}" 2>/dev/null || true
    rm -f "${SERVICE_FILE}"
    systemctl daemon-reload
    rm -rf "${APP_DIR}"
    ok "service and code removed"
    if [[ ${PURGE} -eq 1 ]]; then
        confirm "Also delete configuration, calibration and event history in ${DATA_DIR}?" || {
            info "keeping ${DATA_DIR} and ${CONFIG_DIR}"
            return
        }
        rm -rf "${DATA_DIR}" "${CONFIG_DIR}"
        ok "configuration and data removed"
    else
        info "kept ${DATA_DIR} and ${CONFIG_DIR} (use --purge to remove them)"
    fi
}

interactive_menu() {
    cat <<EOF

${C_BOLD}turret-control is already installed.${C_RESET}
  Location: ${APP_DIR}   Service: $(systemctl is-active "${SERVICE_NAME}" 2>/dev/null || echo unknown)

  1) Update installation
  2) Repair / reinstall dependencies
  3) Restart service
  4) Show service status
  5) Cancel

EOF
    local choice
    read -r -p "Choice [1]: " choice || true
    case "${choice:-1}" in
        1) MODE="update" ;;
        2) MODE="repair" ;;
        3) MODE="restart" ;;
        4) MODE="status" ;;
        *) info "cancelled"; exit 0 ;;
    esac
}

main() {
    parse_args "$@"
    require_root

    if [[ -z "${MODE}" ]]; then
        if is_installed; then
            if [[ ${ASSUME_YES} -eq 1 || ! -t 0 ]]; then
                # Piped from curl or run unattended: updating is the safe,
                # expected default for an existing installation.
                MODE="update"
            else
                interactive_menu
            fi
        else
            MODE="install"
        fi
    fi

    # An existing install always reuses its configured port unless overridden.
    if [[ -f "${ENV_FILE}" && -z "${TURRET_PORT:-}" ]]; then
        HTTP_PORT="$(grep -E '^TURRET_PORT=' "${ENV_FILE}" | cut -d= -f2 | tr -d '[:space:]' || true)"
        HTTP_PORT="${HTTP_PORT:-8080}"
    fi

    case "${MODE}" in
        install)   if is_installed; then do_update; else do_install; fi ;;
        update)    is_installed || die "nothing to update - run without flags to install"
                   do_update ;;
        repair)    is_installed || die "nothing to repair - run without flags to install"
                   do_repair ;;
        restart)   restart_service; wait_for_health ;;
        status)    show_status ;;
        uninstall) do_uninstall ;;
        *)         die "unknown mode: ${MODE}" ;;
    esac
}

main "$@"
