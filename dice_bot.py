"""
Stake Dice Bot — IDR
Strategi  : Modulo-2 chance scaling + Modulo-3 stake compounding
Config    : config.json (hot-reload tiap sesi baru)
Log       : dice_bot.log (auto-rotate 5 MB)
"""

import os, sys, time, uuid, json, re, logging
from datetime import datetime
from logging.handlers import RotatingFileHandler

import requests
from requests.exceptions import HTTPError, ConnectionError as ReqConnError, Timeout as ReqTimeout

# ═══════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════
API_ENDPOINT  = "https://stake.com/_api/graphql"
CONFIG_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
LOG_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dice_bot.log")

# ═══════════════════════════════════════════════════════════
#  LOGGING (terminal + file dengan rotate 5 MB)
# ═══════════════════════════════════════════════════════════
def _setup_logger() -> logging.Logger:
    fmt = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)-5s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    logger = logging.getLogger("dice_bot")
    logger.setLevel(logging.DEBUG)

    # File handler — rotate at 5 MB, keep 1 backup
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=1, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)

    # Terminal handler — INFO and above only
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

log = _setup_logger()

# ═══════════════════════════════════════════════════════════
#  CONFIG — baca config.json, reload tiap sesi baru
# ═══════════════════════════════════════════════════════════
_CONFIG_DEFAULTS = {
    "currency"            : "idr",
    "base_bet"            : 100.0,
    "base_chance"         : 5.0,
    "max_chance_cap"      : 45.0,
    "target_profit_pct"   : 20.0,
    "stop_loss_pct"       : 20.0,
    "recovery_factor"     : 1.02,
    "roll_delay_ms"       : 500,
    "max_api_retries"     : 10,
    "auto_restart_session": True,
    "max_sessions"        : 0,
}

def load_config() -> dict:
    """Baca config.json, fallback ke default kalau key tidak ada."""
    cfg = dict(_CONFIG_DEFAULTS)
    if not os.path.exists(CONFIG_FILE):
        log.warning(f"config.json tidak ditemukan — pakai nilai default. "
                    f"Buat file di: {CONFIG_FILE}")
        return cfg
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
        for k, v in _CONFIG_DEFAULTS.items():
            if k in data:
                cfg[k] = type(v)(data[k])  # cast ke tipe default
        log.debug(f"Config dimuat dari {CONFIG_FILE}")
    except Exception as exc:
        log.error(f"Gagal baca config.json: {exc} — pakai nilai default")
    return cfg

# ═══════════════════════════════════════════════════════════
#  SESSION (persistent HTTP session + headers)
# ═══════════════════════════════════════════════════════════
def _load_api_key() -> str:
    key = os.environ.get("STAKE_API_KEY", "").strip()
    if not key:
        log.error("STAKE_API_KEY belum diset. Set di .env atau export dulu.")
        sys.exit(1)
    return key

API_KEY = _load_api_key()

_session = requests.Session()
_session.headers.update({
    "Content-Type"                : "application/json",
    "Accept"                      : "*/*",
    "Accept-Language"             : "en-US,en;q=0.9",
    "Accept-Encoding"             : "gzip, deflate",
    "Origin"                      : "https://stake.com",
    "Referer"                     : "https://stake.com/",
    "User-Agent"                  : (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "x-access-token"              : API_KEY,
    "x-language"                  : "en",
    "apollographql-client-name"   : "web",
    "apollographql-client-version": "1.0.0",
    "Connection"                  : "keep-alive",
})

# ═══════════════════════════════════════════════════════════
#  GRAPHQL
# ═══════════════════════════════════════════════════════════
BALANCE_QUERY = """
query UserBalances {
  user {
    balances {
      available { amount currency }
    }
  }
}
"""

DICE_MUTATION = """
mutation DiceRoll(
  $amount: Float!, $target: Float!,
  $condition: CasinoGameDiceConditionEnum!,
  $currency: CurrencyEnum!, $identifier: String!
) {
  diceRoll(
    amount: $amount, target: $target,
    condition: $condition, currency: $currency, identifier: $identifier
  ) {
    id active payoutMultiplier amountMultiplier amount payout updatedAt currency game
    ... on CasinoGameDice { result target condition }
    user {
      id
      balances { available { amount currency } }
    }
  }
}
"""

_RATE_LIMIT_RE = re.compile(r"rate.?limit|too many request|throttl", re.IGNORECASE)

class RateLimitError(Exception):
    pass

class AuthError(Exception):
    pass

def gql(query: str, variables: dict = None) -> dict:
    """Kirim GraphQL request, return data payload."""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    try:
        resp = _session.post(API_ENDPOINT, json=payload, timeout=15)
    except ReqConnError as exc:
        raise RuntimeError(f"Koneksi terputus: {exc}") from exc
    except ReqTimeout:
        raise RuntimeError("Request timeout setelah 15 detik")

    if resp.status_code == 401:
        raise AuthError("Token expired atau tidak valid (HTTP 401)")
    if resp.status_code == 429:
        raise RateLimitError("Rate limit dari server (HTTP 429)")

    try:
        resp.raise_for_status()
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {resp.status_code}: {exc}") from exc

    body = resp.json()

    if "errors" in body:
        err_str = str(body["errors"])
        if _RATE_LIMIT_RE.search(err_str):
            raise RateLimitError(f"Rate limit dari GraphQL: {err_str}")
        raise RuntimeError(f"GraphQL error: {err_str}")

    return body.get("data", {})


def fetch_idr_balance(currency: str) -> float:
    """Ambil saldo wallet dari API."""
    try:
        data     = gql(BALANCE_QUERY)
        balances = data["user"]["balances"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Gagal parse response saldo: {exc}") from exc

    for entry in balances:
        avail = entry.get("available", {})
        if avail.get("currency", "").lower() == currency:
            return float(avail["amount"])

    raise RuntimeError(
        f"Wallet {currency.upper()} tidak ditemukan di akun ini."
    )


def extract_balance_from_roll(roll_data: dict, currency: str) -> float | None:
    """Ambil saldo terbaru yang ada di response diceRoll (hindari extra API call)."""
    try:
        balances = roll_data["diceRoll"]["user"]["balances"]
        for entry in balances:
            avail = entry.get("available", {})
            if avail.get("currency", "").lower() == currency:
                return float(avail["amount"])
    except (KeyError, TypeError):
        pass
    return None


def place_dice_bet(bet_amount: float, win_chance: float, currency: str) -> dict:
    """
    Pasang satu bet dice.
    Roll Over (above): menang jika result > target
    target = 100 - win_chance  (e.g. chance=5% → target=95)
    """
    target     = round(100.0 - win_chance, 4)
    identifier = str(uuid.uuid4())
    variables  = {
        "amount"    : round(bet_amount, 2),
        "target"    : target,
        "condition" : "above",
        "currency"  : currency,
        "identifier": identifier,
    }
    return gql(DICE_MUTATION, variables)


# ═══════════════════════════════════════════════════════════
#  STATE MACHINE — strategi modulo-2 / modulo-3
# ═══════════════════════════════════════════════════════════
def on_win(state: dict) -> dict:
    state["current_bet"]      = state["base_bet"]
    state["current_chance"]   = state["base_chance"]
    state["streak_loss"]      = 0
    state["accumulated_loss"] = 0.0   # KOREKSI: hutang terbayar
    log.info("WIN  → state di-reset ke baseline. Hutang terlunasi.")
    return state


def on_loss(state: dict) -> dict:
    state["accumulated_loss"] += state["current_bet"]   # KOREKSI: catat kerugian
    state["streak_loss"]      += 1
    streak = state["streak_loss"]

    # Modulo-2: naikkan win chance tiap 2 loss berturut-turut (TETAP)
    if streak % 2 == 0:
        state["current_chance"] = min(
            state["current_chance"] + 2.50,
            state["max_chance_cap"]
        )

    # KOREKSI: Recovery-based bet — gantikan ×1.35 modulo-3
    # Hitung bet minimum agar SATU kemenangan menutup semua kerugian + margin
    payout_mult = 99.0 / state["current_chance"]
    if payout_mult > 1 and state["accumulated_loss"] > 0:
        recovery_bet = (state["accumulated_loss"] * state["recovery_factor"]) / (payout_mult - 1)
        state["current_bet"] = max(state["base_bet"], round(recovery_bet, 2))

    return state


# ═══════════════════════════════════════════════════════════
#  GUARDRAILS — dicek SEBELUM setiap bet
# ═══════════════════════════════════════════════════════════
def apply_guardrails(state: dict) -> dict:
    # 1. Circuit breaker: 15 loss berturut-turut
    # KOREKSI: hanya reset chance & streak — accumulated_loss DIPERTAHANKAN
    # agar recovery bet tetap aktif menutup hutang kerugian
    if state["streak_loss"] >= 15:
        state["current_chance"] = state["base_chance"]
        state["streak_loss"]    = 0
        # Recalculate recovery bet dengan payout tinggi (chance kembali ke base = 19.8x)
        payout_mult = 99.0 / state["current_chance"]
        if state["accumulated_loss"] > 0:
            recovery_bet = (state["accumulated_loss"] * state["recovery_factor"]) / (payout_mult - 1)
            state["current_bet"] = max(state["base_bet"], round(recovery_bet, 2))
        log.warning(f"⚠  Circuit Breaker! Chance reset, recovery bet: Rp {state['current_bet']:,.2f} "
                    f"(hutang: Rp {state['accumulated_loss']:,.2f})")

    # 2. Anti-bust: bet > 10% saldo → potong 50%
    if state["current_balance"] > 0 and \
            state["current_bet"] > state["current_balance"] * 0.10:
        state["current_bet"] *= 0.50
        log.warning("⚠  Anti-bust: bet > 10% saldo, dikurangi 50%.")

    # 3. Take-profit / Stop-loss per sesi
    net          = state["current_balance"] - state["session_start_balance"]
    tp_threshold =  state["session_start_balance"] * (state["target_profit_pct"] / 100)
    sl_threshold = -state["session_start_balance"] * (state["stop_loss_pct"]     / 100)

    if net >= tp_threshold:
        state["session_end_reason"] = "TAKE_PROFIT"
        state["session_active"]     = False
    elif net <= sl_threshold:
        state["session_end_reason"] = "STOP_LOSS"
        state["session_active"]     = False

    return state


# ═══════════════════════════════════════════════════════════
#  TELEMETRY — log tiap roll
# ═══════════════════════════════════════════════════════════
def log_roll(state: dict, result: str, roll_num: int):
    net    = state["current_balance"] - state["session_start_balance"]
    payout = round(99 / state["current_chance"], 4)
    log.info(
        f"[#{roll_num:>5}] {result} | "
        f"Chance:{state['current_chance']:>5.2f}% | "
        f"Payout:{payout:.4f}x | "
        f"Bet:{state['current_bet']:>10.2f} IDR | "
        f"Streak:{state['streak_loss']:>2} | "
        f"Hutang:{state['accumulated_loss']:>10.2f} IDR | "
        f"Net:{net:>+10.2f} IDR"
    )


# ═══════════════════════════════════════════════════════════
#  SESSION SUMMARY
# ═══════════════════════════════════════════════════════════
def print_session_summary(session_num: int, state: dict, cum: dict):
    net = state["current_balance"] - state["session_start_balance"]
    reason = state.get("session_end_reason", "MANUAL_STOP")
    sep = "═" * 60
    log.info(sep)
    log.info(f"  SESI #{session_num} SELESAI — {reason}")
    log.info(f"  Roll sesi     : {state['roll_count']}")
    log.info(f"  Saldo awal    : Rp {state['session_start_balance']:>12,.2f}")
    log.info(f"  Saldo akhir   : Rp {state['current_balance']:>12,.2f}")
    log.info(f"  Net sesi      : Rp {net:>+12,.2f}")
    log.info(sep)
    log.info(f"  KUMULATIF {cum['sessions']} SESI")
    log.info(f"  Total roll    : {cum['total_rolls']}")
    log.info(f"  Total net     : Rp {cum['total_net']:>+12,.2f}")
    log.info(f"  Win rate      : {cum['wins']}/{cum['wins']+cum['losses']} "
             f"({100*cum['wins']/max(cum['wins']+cum['losses'],1):.1f}%)")
    log.info(sep)


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════
def new_session_state(cfg: dict, balance: float) -> dict:
    return {
        # Config snapshot untuk sesi ini
        "base_bet"            : cfg["base_bet"],
        "base_chance"         : cfg["base_chance"],
        "max_chance_cap"      : cfg["max_chance_cap"],
        "target_profit_pct"   : cfg["target_profit_pct"],
        "stop_loss_pct"       : cfg["stop_loss_pct"],
        "recovery_factor"     : cfg["recovery_factor"],   # KOREKSI: faktor recovery
        # Dynamic
        "session_start_balance": balance,
        "current_balance"     : balance,
        "current_bet"         : cfg["base_bet"],
        "current_chance"      : cfg["base_chance"],
        "streak_loss"         : 0,
        "accumulated_loss"    : 0.0,                      # KOREKSI: pelacak hutang
        "roll_count"          : 0,
        "session_active"      : True,
        "session_end_reason"  : None,
    }


def main():
    sep = "═" * 60
    log.info(sep)
    log.info("  Stake Dice Bot — IDR Mode")
    log.info(f"  Log file: {LOG_FILE}")
    log.info(sep)

    # ── Kumulatif lintas sesi ────────────────────────────────
    cum = {
        "sessions"   : 0,
        "total_rolls": 0,
        "total_net"  : 0.0,
        "wins"       : 0,
        "losses"     : 0,
    }

    session_num = 0

    try:
        while True:
            # ── Load / reload config tiap sesi baru ─────────
            cfg = load_config()

            # ── Fetch saldo awal sesi ────────────────────────
            log.info("Mengambil saldo IDR...")
            try:
                balance = fetch_idr_balance(cfg["currency"])
            except Exception as exc:
                log.error(f"Gagal ambil saldo: {exc}")
                sys.exit(1)

            session_num += 1
            cum["sessions"] = session_num
            state = new_session_state(cfg, balance)

            log.info(sep)
            log.info(f"  SESI #{session_num} DIMULAI")
            log.info(f"  Saldo       : Rp {balance:>12,.2f}")
            log.info(f"  Base bet    : Rp {cfg['base_bet']:>12,.2f}")
            log.info(f"  Win chance  : {cfg['base_chance']:.2f}%")
            log.info(f"  Take-profit : +{cfg['target_profit_pct']:.1f}%  "
                     f"(Rp {balance * cfg['target_profit_pct']/100:,.2f})")
            log.info(f"  Stop-loss   : -{cfg['stop_loss_pct']:.1f}%  "
                     f"(Rp {balance * cfg['stop_loss_pct']/100:,.2f})")
            if cfg["max_sessions"] > 0:
                log.info(f"  Sesi        : {session_num}/{cfg['max_sessions']}")
            log.info(sep)

            consecutive_errors = 0

            # ── Roll loop ────────────────────────────────────
            while state["session_active"]:
                state = apply_guardrails(state)
                if not state["session_active"]:
                    break

                state["roll_count"] += 1
                roll_num_global = cum["total_rolls"] + state["roll_count"]

                # ── Pasang bet ───────────────────────────────
                try:
                    roll_data = place_dice_bet(
                        state["current_bet"],
                        state["current_chance"],
                        cfg["currency"]
                    )
                    consecutive_errors = 0

                except RateLimitError as exc:
                    state["roll_count"] -= 1
                    log.warning(f"Rate limit — tunggu 5 detik... ({exc})")
                    time.sleep(5)
                    continue

                except AuthError as exc:
                    log.error(str(exc))
                    log.error("Update token dengan: ./update_token.sh lalu restart bot.")
                    sys.exit(1)

                except RuntimeError as exc:
                    consecutive_errors += 1
                    state["roll_count"] -= 1
                    max_r = cfg["max_api_retries"]
                    log.warning(f"API error #{consecutive_errors}/{max_r}: {exc} — retry 3 detik...")
                    if consecutive_errors >= max_r:
                        log.error(f"{max_r} error berturut-turut. Bot berhenti.")
                        state["session_end_reason"] = "API_ERROR"
                        state["session_active"]     = False
                    else:
                        time.sleep(3)
                    continue

                # ── Validasi response ────────────────────────
                dice = roll_data.get("diceRoll")
                if not dice:
                    consecutive_errors += 1
                    state["roll_count"] -= 1
                    log.warning(f"Response kosong #{consecutive_errors} — retry 3 detik...")
                    if consecutive_errors >= cfg["max_api_retries"]:
                        state["session_end_reason"] = "API_ERROR"
                        state["session_active"]     = False
                    else:
                        time.sleep(3)
                    continue

                result_val = dice.get("result")
                target_val = dice.get("target")
                if result_val is None or target_val is None:
                    consecutive_errors += 1
                    state["roll_count"] -= 1
                    log.warning(f"Data roll tidak lengkap #{consecutive_errors} — retry 3 detik...")
                    if consecutive_errors >= cfg["max_api_retries"]:
                        state["session_end_reason"] = "API_ERROR"
                        state["session_active"]     = False
                    else:
                        time.sleep(3)
                    continue

                # ── Update saldo ─────────────────────────────
                new_bal = extract_balance_from_roll(roll_data, cfg["currency"])
                if new_bal is not None:
                    state["current_balance"] = new_bal
                else:
                    try:
                        state["current_balance"] = fetch_idr_balance(cfg["currency"])
                    except RuntimeError:
                        pass

                # ── Evaluasi hasil roll ──────────────────────
                # State diupdate DULU, baru log — supaya Streak di log
                # mencerminkan nilai yang sudah benar setelah roll ini.
                won = float(result_val) > float(target_val)
                if won:
                    cum["wins"] += 1
                    state = on_win(state)
                else:
                    cum["losses"] += 1
                    state = on_loss(state)
                log_roll(state, "WIN " if won else "LOSS", roll_num_global)

                # ── Delay antar roll ─────────────────────────
                delay = cfg["roll_delay_ms"] / 1000.0
                if delay > 0:
                    time.sleep(delay)

            # ── Sesi selesai ─────────────────────────────────
            cum["total_rolls"] += state["roll_count"]
            cum["total_net"]   += state["current_balance"] - state["session_start_balance"]
            print_session_summary(session_num, state, cum)

            reason = state.get("session_end_reason", "")

            # Berhenti total jika API error atau manual stop
            if reason == "API_ERROR":
                log.error("Bot berhenti karena error API berulang.")
                break

            # Cek batas maksimal sesi
            if cfg["max_sessions"] > 0 and session_num >= cfg["max_sessions"]:
                log.info(f"Batas {cfg['max_sessions']} sesi tercapai. Bot selesai.")
                break

            # Auto-restart sesi berikutnya
            if cfg["auto_restart_session"]:
                log.info("Auto-restart sesi baru dalam 3 detik... (Ctrl+C untuk berhenti)")
                time.sleep(3)
            else:
                log.info("auto_restart_session = false. Bot selesai.")
                break

    except KeyboardInterrupt:
        log.info("\nBot dihentikan manual (Ctrl+C).")
        log.info(f"Total sesi  : {cum['sessions']}")
        log.info(f"Total roll  : {cum['total_rolls']}")
        log.info(f"Total net   : Rp {cum['total_net']:>+,.2f}")
        log.info(f"Win rate    : {cum['wins']}/{cum['wins']+cum['losses']} "
                 f"({100*cum['wins']/max(cum['wins']+cum['losses'],1):.1f}%)")


if __name__ == "__main__":
    main()
