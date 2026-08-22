#!/usr/bin/env bash

set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${repo_root}/install.sh"

fail() {
    echo "installer helper test failed: $*" >&2
    exit 1
}

TURRET_NVIDIA_DRIVER_VERSION="580.173.02"
[[ "$(nvidia_kernel_driver_version)" == "580.173.02" ]] ||
    fail "explicit driver version was not accepted"

TURRET_NVIDIA_DRIVER_VERSION="not-a-version"
if nvidia_kernel_driver_version >/dev/null 2>&1; then
    fail "invalid explicit driver version was accepted"
fi
unset TURRET_NVIDIA_DRIVER_VERSION

FAKE_COMPUTE_CAPABILITY="6.1"
nvidia-smi() {
    if [[ "$*" == *"--query-gpu=compute_cap"* ]]; then
        printf '%s\n' "${FAKE_COMPUTE_CAPABILITY}"
        return 0
    fi
    return 1
}

nvidia_requires_legacy_cuda || fail "Pascal was not classified as legacy CUDA"

FAKE_COMPUTE_CAPABILITY="7.0"
nvidia_requires_legacy_cuda || fail "Volta was not classified as legacy CUDA"

FAKE_COMPUTE_CAPABILITY="7.5"
if nvidia_requires_legacy_cuda; then
    fail "Turing was incorrectly classified as legacy CUDA"
elif [[ $? -ne 1 ]]; then
    fail "Turing capability returned an unexpected status"
fi

FAKE_COMPUTE_CAPABILITY="8.6"
if nvidia_requires_legacy_cuda; then
    fail "Ampere was incorrectly classified as legacy CUDA"
elif [[ $? -ne 1 ]]; then
    fail "Ampere capability returned an unexpected status"
fi

FAKE_COMPUTE_CAPABILITY="unknown"
if nvidia_requires_legacy_cuda; then
    fail "an unknown capability was classified as legacy CUDA"
elif [[ $? -ne 2 ]]; then
    fail "an unknown capability did not produce the diagnostic status"
fi

test_root="$(mktemp -d "${repo_root}/.installer-test.XXXXXX")"
trap 'rm -rf -- "${test_root}"' EXIT
VENV_DIR="${test_root}/venv"
SERVER_DIR="${test_root}/server"
mkdir -p "${VENV_DIR}/bin" "${SERVER_DIR}/requirements"
printf '# test requirements\n' > "${SERVER_DIR}/requirements/ai.txt"
printf '#!/usr/bin/env bash\nexit 0\n' > "${VENV_DIR}/bin/pip"
chmod +x "${VENV_DIR}/bin/pip"

nvidia_device_exposed() { return 0; }
ensure_nvidia_userspace() { return 0; }
try_install_cuda_pytorch() { return 1; }
ensure_cpu_pytorch() { touch "${test_root}/cpu-fallback"; }
AI_DEVICE_OVERRIDE="auto"

install_ai_stack >/dev/null 2>&1
[[ -f "${test_root}/cpu-fallback" ]] || fail "CUDA failure did not select CPU fallback"

rm -f "${test_root}/cpu-fallback"
try_install_cuda_pytorch() { return 0; }
install_ai_stack >/dev/null 2>&1
[[ ! -f "${test_root}/cpu-fallback" ]] || fail "working CUDA incorrectly selected CPU fallback"

rm -f "${test_root}/cpu-fallback"
AI_DEVICE_OVERRIDE="cpu"
ensure_nvidia_userspace() { fail "CPU mode attempted NVIDIA userspace setup"; }
install_ai_stack >/dev/null 2>&1
[[ -f "${test_root}/cpu-fallback" ]] || fail "CPU mode did not verify CPU PyTorch"

DATA_DIR="${test_root}/data"
mkdir -p "${DATA_DIR}"
test_python="python3"
if [[ -x "${repo_root}/server/.venv/Scripts/python.exe" ]]; then
    test_python="${repo_root}/server/.venv/Scripts/python.exe"
fi
"${test_python}" - "${DATA_DIR}/turret.db" <<'PY'
import json
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("CREATE TABLE settings (section TEXT PRIMARY KEY, data TEXT)")
connection.execute(
    "INSERT INTO settings(section, data) VALUES (?, ?)",
    ("detector", json.dumps({"device": "cuda"})),
)
connection.commit()
connection.close()
PY
python3() { "${test_python}" "$@"; }
AI_DEVICE_OVERRIDE=""
[[ "$(configured_ai_device)" == "cuda" ]] || fail "saved detector device was not read"

echo "installer helper tests passed"
