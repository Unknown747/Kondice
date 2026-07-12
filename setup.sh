#!/usr/bin/env bash
# ============================================================
#  setup.sh — Stake Dice Bot VPS Setup
#  Supported: Ubuntu 20.04/22.04/24.04, Debian 11/12
# ============================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()  { echo -e "${CYAN}[INFO]${RESET}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${RESET}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error() { echo -e "${RED}[ERR ]${RESET}  $*" >&2; }

echo -e "\n${BOLD}${CYAN}══════════════════════════════════════════${RESET}"
echo -e "${BOLD}${CYAN}  Stake Dice Bot — Setup VPS${RESET}"
echo -e "${BOLD}${CYAN}══════════════════════════════════════════${RESET}\n"

# ── Cek OS ──────────────────────────────────────────────────
if ! command -v apt-get &>/dev/null; then
    error "Script ini butuh sistem Debian/Ubuntu (apt-get tidak ditemukan)."
    exit 1
fi

# ── 1. Install paket sistem ──────────────────────────────────
info "Update package list..."
sudo apt-get update -qq

info "Install Python3, pip, screen, curl..."
sudo apt-get install -y -qq python3 python3-pip screen curl
ok "Paket sistem siap."

# ── 2. Copy file bot ─────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "Folder bot: ${SCRIPT_DIR}"

for FILE in dice_bot.py scheduler.py config.json; do
    if [[ -f "${SCRIPT_DIR}/${FILE}" ]]; then
        ok "Ditemukan: ${FILE}"
    else
        warn "Tidak ditemukan: ${FILE} — copy manual ke folder ini sebelum jalankan bot."
    fi
done

# ── 3. Install requests ───────────────────────────────────────
info "Install requests..."
pip3 install requests \
    --timeout 30 --retries 3 \
    2>&1 | grep -v "^Requirement already"
ok "requests siap."

# ── 4. API Token ─────────────────────────────────────────────
ENV_FILE="${SCRIPT_DIR}/.env"

echo
echo -e "${YELLOW}Cara dapat token Stake:${RESET}"
echo -e "  1. Login Stake.com di browser"
echo -e "  2. DevTools (F12) → Application → Cookies → cari ${BOLD}x-access-token${RESET}"
echo -e "  3. Atau: DevTools → Network → request GraphQL → Headers → ${BOLD}x-access-token${RESET}"
echo

EXISTING=""
if [[ -f "${ENV_FILE}" ]]; then
    EXISTING=$(grep -E "^STAKE_API_KEY=" "${ENV_FILE}" 2>/dev/null | cut -d'=' -f2- || true)
fi

if [[ -n "${EXISTING}" ]]; then
    warn "Token sudah tersimpan di ${ENV_FILE}"
    read -r -p "  Ganti dengan token baru? [y/N]: " REPLACE
    if [[ ! "${REPLACE}" =~ ^[Yy]$ ]]; then
        ok "Token tetap dipakai."
    else
        EXISTING=""
    fi
fi

if [[ -z "${EXISTING}" ]]; then
    while true; do
        read -r -s -p "  Paste token Stake (tersembunyi): " TOKEN
        echo
        if [[ -z "${TOKEN}" ]]; then
            error "Token tidak boleh kosong."
        elif [[ ${#TOKEN} -lt 20 ]]; then
            error "Token terlalu pendek (${#TOKEN} karakter)."
        else
            break
        fi
    done

    TMP="$(mktemp "${SCRIPT_DIR}/.env.XXXXXX")"
    chmod 600 "${TMP}"
    printf 'STAKE_API_KEY=%s\n' "${TOKEN}" > "${TMP}"
    mv "${TMP}" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    ok "Token disimpan di ${ENV_FILE}"
fi

# ── 5. Selesai ───────────────────────────────────────────────
echo
echo -e "${BOLD}${CYAN}══════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  Setup selesai!${RESET}"
echo -e "${BOLD}${CYAN}══════════════════════════════════════════${RESET}"
echo
echo -e "  Jalankan scheduler (30 mnt jalan / 10 mnt jeda):"
echo -e "  ${BOLD}python3 scheduler.py${RESET}"
echo
echo -e "  Atau langsung botnya saja:"
echo -e "  ${BOLD}python3 dice_bot.py${RESET}"
echo
echo -e "  Pakai screen biar aman saat SSH putus:"
echo -e "  ${BOLD}screen -S bot${RESET}"
echo -e "  ${BOLD}python3 scheduler.py${RESET}"
echo -e "  Ctrl+A lalu D untuk detach"
echo
