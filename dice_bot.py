"""
Stake Dice Bot — IDR  |  Gigi 200  |  Mode Berbasis Waktu
═══════════════════════════════════════════════════════════
Strategi   : Martingale ×1.68 — tiap loss bet dikali 1.68, chance fixed 40%.
             Progression: 200 → 336 → 564 → 948 → 1.593 IDR
             CB cost 1x: Rp 3.642 | Ketahanan saldo Rp 342k: >93 CB
Rem darurat: Circuit Breaker di loss ke-5 → reset ke base_bet (Rp 200), lanjut.
             TP/SL per sesi DINONAKTIFKAN (0) — tidak ada restart loop.
Durasi     : Dikendalikan scheduler.py (30 mnt jalan / 10 mnt jeda).
Delay      : Smart Random Delay — 3 tier probabilistik meniru pola klik manusia.
Config     : config.json (hot-reload tiap sesi baru)
Log        : dice_bot.log (auto-rotate 5 MB)
"""

from __future__ import annotations
import os, sys, time, uuid, json, re, logging, random
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
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=1, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)

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
    "base_bet"            : 200.0,    # Rp 200 — CB cost: Rp 3.642 | >93 nyawa CB
    "base_chance"         : 40.0,     # chance FIXED — payout 2.475x
    "max_chance_cap"      : 49.5,     # tidak aktif, disimpan untuk kompatibilitas
    "target_profit_pct"   : 0.0,      # 0 = NONAKTIF — mode berbasis waktu
    "stop_loss_pct"       : 0.0,      # 0 = NONAKTIF — rem hanya CB
    "bet_multiplier"      : 1.68,     # ×1.68 tiap loss
    "max_bet_multiplier"  : 100,
    "circuit_breaker_at"  : 5,        # 5 loss beruntun → reset, lanjut (tidak restart sesi)
    "max_api_retries"     : 10,
    "auto_restart_session": False,    # tidak relevan saat TP/SL = 0
    "max_sessions"        : 0,
    # ── Manajemen Risiko Dinamis ──────────────────────────────
    "hard_stop_balance"           : 0.0,  # > 0 = bot mati total jika saldo ≤ angka ini
    "max_cb_per_session"          : 0,    # > 0 = hentikan sesi jika CB ≥ N kali dalam satu run
    "realtime_log_interval_rolls" : 100,  # cetak ringkasan tiap N roll (0 = nonaktif)
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
                cfg[k] = type(v)(data[k])
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
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("STAKE_API_KEY=") and not line.startswith("#"):
                        key = line.split("=", 1)[1].strip()
                        break
    if not key:
        log.error("STAKE_API_KEY belum diset.")
        log.error("Buat file .env di folder yang sama dengan bot, isi:")
        log.error("  STAKE_API_KEY=token_kamu_disini")
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
    target = 100 - win_chance  (e.g. chance=40% → target=60)
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
#  SMART RANDOM DELAY — meniru pola klik manusia
# ═══════════════════════════════════════════════════════════
def smart_random_delay() -> float:
    """
    Tier 1 (75%): Jeda reguler   0.8 – 1.5 detik  → ritme santai normal
    Tier 2 (20%): Jeda agresif   0.4 – 0.7 detik  → klik cepat kejar momen
    Tier 3 ( 5%): Jeda distraksi 3.0 – 6.5 detik  → cek saldo / terdistraksi
    """
    r = random.random()
    if r < 0.75:
        return random.uniform(0.8, 1.5)
    elif r < 0.95:
        return random.uniform(0.4, 0.7)
    else:
        return random.uniform(3.0, 6.5)


# ═══════════════════════════════════════════════════════════
#  STATE MACHINE — Martingale ×1.68
# ═══════════════════════════════════════════════════════════
def on_win(state: dict) -> dict:
    state["current_bet"]    = state["base_bet"]
    state["current_chance"] = state["base_chance"]
    state["streak_loss"]    = 0
    state["cycle_spent"]    = 0.0
    return state


def on_loss(state: dict, bet_placed: float) -> dict:
    state["streak_loss"] += 1
    state["cycle_spent"] += bet_placed

    max_bet = state["base_bet"] * state["max_bet_multiplier"]
    state["current_bet"] = min(
        round(state["current_bet"] * state["bet_multiplier"], 2), max_bet
    )
    return state


# ═══════════════════════════════════════════════════════════
#  GUARDRAILS — dicek SEBELUM setiap bet
# ═══════════════════════════════════════════════════════════
def apply_guardrails(state: dict) -> dict:
    # 1. Circuit Breaker — satu-satunya rem aktif di mode berbasis waktu
    #    Tidak merestart sesi, hanya reset bet ke baseline dan lanjut rolling.
    if state["streak_loss"] >= state["circuit_breaker_at"]:
        log.warning(
            f"⚠  Circuit Breaker ({state['circuit_breaker_at']} loss)! "
            f"Defisit siklus Rp {state['cycle_spent']:,.2f} — reset ke baseline Rp {state['base_bet']:,.0f}."
        )
        state["current_bet"]    = state["base_bet"]
        state["current_chance"] = state["base_chance"]
        state["streak_loss"]    = 0
        state["cycle_spent"]    = 0.0
        state["session_cb_count"] += 1

        # max_cb_per_session — hentikan sesi jika CB terlalu banyak dalam satu run
        max_cb = state.get("max_cb_per_session", 0)
        if max_cb > 0 and state["session_cb_count"] >= max_cb:
            log.warning(
                f"⚠  MAX CB/SESI: {state['session_cb_count']}x Circuit Breaker ≥ batas {max_cb}x "
                f"— sesi dihentikan lebih awal untuk mencegah drain ekstrem."
            )
            state["session_end_reason"] = "MAX_CB_SESSION"
            state["session_active"]     = False
            return state

    # 2. hard_stop_balance — rem permanen, bot mati total jika saldo menyentuh batas aman
    hard_stop = state.get("hard_stop_balance", 0.0)
    if hard_stop > 0 and state["current_balance"] <= hard_stop:
        log.warning(
            f"🛑  HARD STOP! Saldo Rp {state['current_balance']:,.2f} ≤ batas aman "
            f"Rp {hard_stop:,.0f} — bot dihentikan total untuk mengamankan modal inti."
        )
        state["session_end_reason"] = "HARD_STOP"
        state["session_active"]     = False
        return state

    # 3. Anti-bust — bet > 10% saldo → potong 50%
    if state["current_balance"] > 0 and \
            state["current_bet"] > state["current_balance"] * 0.10:
        state["current_bet"] = round(state["current_bet"] * 0.50, 2)
        log.warning("⚠  Anti-bust: bet > 10% saldo, dikurangi 50%.")

    # 4. Take-profit / Stop-loss — HANYA aktif jika pct > 0
    #    Nilai 0 = mode berbasis waktu, cek ini dilompati sepenuhnya.
    #    FIX: tanpa guard ini, pct=0 → threshold=0 → langsung trigger di roll pertama.
    tp_pct = state["target_profit_pct"]
    sl_pct = state["stop_loss_pct"]

    if tp_pct > 0 or sl_pct > 0:
        net = state["current_balance"] - state["session_start_balance"]
        if tp_pct > 0:
            tp_threshold = state["session_start_balance"] * (tp_pct / 100)
            if net >= tp_threshold:
                state["session_end_reason"] = "TAKE_PROFIT"
                state["session_active"]     = False
                return state
        if sl_pct > 0:
            sl_threshold = -state["session_start_balance"] * (sl_pct / 100)
            if net <= sl_threshold:
                state["session_end_reason"] = "STOP_LOSS"
                state["session_active"]     = False

    return state


# ═══════════════════════════════════════════════════════════
#  TELEMETRY — log tiap roll
# ═══════════════════════════════════════════════════════════
def log_roll(state: dict, result: str, roll_num: int,
             bet_used: float, chance_used: float, roll_net: float):
    """Log satu roll. bet_used/chance_used/roll_net adalah nilai SEBELUM update state."""
    log.info(
        f"[#{roll_num:>5}] {result} | "
        f"Chance:{chance_used:>5.2f}% | "
        f"Bet:{bet_used:>10.2f} IDR | "
        f"Net:{roll_net:>+10.2f} IDR | "
        f"Saldo: Rp {state['current_balance']:,.2f}"
    )


# ═══════════════════════════════════════════════════════════
#  SESSION SUMMARY
# ═══════════════════════════════════════════════════════════
def print_session_summary(session_num: int, state: dict, cum: dict):
    net    = state["current_balance"] - state["session_start_balance"]
    reason = state.get("session_end_reason", "STOPPED")
    sep    = "═" * 60
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
        "base_bet"             : cfg["base_bet"],
        "base_chance"          : cfg["base_chance"],
        "target_profit_pct"    : cfg["target_profit_pct"],
        "stop_loss_pct"        : cfg["stop_loss_pct"],
        "bet_multiplier"       : cfg["bet_multiplier"],
        "max_bet_multiplier"   : cfg["max_bet_multiplier"],
        "circuit_breaker_at"   : cfg["circuit_breaker_at"],
        "session_start_balance": balance,
        "current_balance"      : balance,
        "current_bet"          : cfg["base_bet"],
        "current_chance"       : cfg["base_chance"],
        "streak_loss"          : 0,
        "cycle_spent"          : 0.0,
        "roll_count"           : 0,
        "session_active"             : True,
        "session_end_reason"         : None,
        # ── Manajemen Risiko Dinamis ──────────────────────────
        "hard_stop_balance"          : cfg["hard_stop_balance"],
        "max_cb_per_session"         : cfg["max_cb_per_session"],
        "realtime_log_interval_rolls": cfg["realtime_log_interval_rolls"],
        "session_cb_count"           : 0,
    }


def main():
    sep = "═" * 60
    log.info(sep)
    log.info("  Stake Dice Bot — Gigi 200 | Mode Berbasis Waktu")
    log.info(f"  Log file: {LOG_FILE}")
    log.info(sep)

    cum = {
        "sessions"   : 0,
        "total_rolls": 0,
        "total_net"  : 0.0,
        "wins"       : 0,
        "losses"     : 0,
    }

    session_num = 0
    state       = None   # pastikan terdefinisi sebelum try untuk handler except

    try:
        while True:
            cfg = load_config()

            # ── Fetch saldo awal ─────────────────────────────
            log.info("Mengambil saldo IDR...")
            balance = None
            for _attempt in range(3):
                try:
                    balance = fetch_idr_balance(cfg["currency"])
                    break
                except Exception as exc:
                    log.warning(f"Gagal ambil saldo (percobaan {_attempt+1}/3): {exc} — retry 10 detik...")
                    time.sleep(10)
            if balance is None:
                log.error("Gagal ambil saldo setelah 3 percobaan. Bot berhenti.")
                sys.exit(1)

            session_num += 1
            cum["sessions"] = session_num
            state = new_session_state(cfg, balance)

            # ── Header sesi ──────────────────────────────────
            log.info(sep)
            log.info(f"  SESI #{session_num} DIMULAI")
            log.info(f"  Saldo       : Rp {balance:>12,.2f}")
            log.info(f"  Base bet    : Rp {cfg['base_bet']:>12,.2f}")
            log.info(f"  Win chance  : {cfg['base_chance']:.2f}%")
            log.info(f"  CB cost 1x  : Rp {sum(cfg['base_bet'] * cfg['bet_multiplier']**i for i in range(cfg['circuit_breaker_at'])):>10,.2f}")
            log.info(f"  Ketahanan CB: >{int(balance / sum(cfg['base_bet'] * cfg['bet_multiplier']**i for i in range(cfg['circuit_breaker_at']))):,} nyawa")

            tp_pct = cfg["target_profit_pct"]
            sl_pct = cfg["stop_loss_pct"]
            if tp_pct > 0 or sl_pct > 0:
                if tp_pct > 0:
                    log.info(f"  Take-profit : +{tp_pct:.1f}%  (Rp {balance * tp_pct/100:,.2f})")
                if sl_pct > 0:
                    log.info(f"  Stop-loss   : -{sl_pct:.1f}%  (Rp {balance * sl_pct/100:,.2f})")
            else:
                log.info("  Mode        : BERBASIS WAKTU — TP/SL dinonaktifkan")
                log.info(f"  Rem darurat : CB tiap {cfg['circuit_breaker_at']} loss → reset ke Rp {cfg['base_bet']:,.0f}, lanjut")
                log.info("  Durasi      : Dikendalikan scheduler (SIGTERM saat waktunya berhenti)")
            # ── Manajemen Risiko Dinamis ──────────────────────
            hs = cfg["hard_stop_balance"]
            log.info(f"  Hard Stop   : Saldo ≤ Rp {hs:,.0f} → bot berhenti total" if hs > 0 else "  Hard Stop   : NONAKTIF (0)")
            mcb = cfg["max_cb_per_session"]
            log.info(f"  Max CB/sesi : {mcb}x → sesi berhenti lebih awal" if mcb > 0 else "  Max CB/sesi : NONAKTIF (0)")
            rti = cfg["realtime_log_interval_rolls"]
            log.info(f"  RT Log      : setiap {rti} roll" if rti > 0 else "  RT Log      : NONAKTIF (0)")
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
                    log.error("Token tidak valid — perbarui STAKE_API_KEY lalu restart bot.")
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
                bet_used    = state["current_bet"]
                chance_used = state["current_chance"]

                won = float(dice.get("payout", 0)) > 0
                if won:
                    cum["wins"] += 1
                    roll_net = round(bet_used * (99.0 / chance_used - 1), 2)
                    state = on_win(state)
                else:
                    cum["losses"] += 1
                    roll_net = -bet_used
                    state = on_loss(state, bet_used)

                log_roll(state, "WIN " if won else "LOSS", roll_num_global,
                         bet_used, chance_used, roll_net)

                # ── Realtime log ringkasan setiap N roll ─────
                interval = state.get("realtime_log_interval_rolls", 0)
                if interval > 0 and state["roll_count"] % interval == 0:
                    _wr      = 100 * cum["wins"] / max(cum["wins"] + cum["losses"], 1)
                    _net     = state["current_balance"] - state["session_start_balance"]
                    _cb_rate = state["session_cb_count"] / state["roll_count"] * 100
                    log.info(
                        f"[CHECKPOINT #{state['roll_count']:>4}] "
                        f"CB sesi: {state['session_cb_count']}x ({_cb_rate:.1f}/100roll) | "
                        f"WR: {_wr:.1f}% | "
                        f"Net sesi: Rp {_net:>+,.0f} | "
                        f"Saldo: Rp {state['current_balance']:,.0f}"
                    )

                # ── Smart Random Delay antar roll ────────────
                time.sleep(smart_random_delay())

            # ── Sesi selesai (hanya jika TP/SL aktif atau API error) ─
            cum["total_rolls"] += state["roll_count"]
            cum["total_net"]   += state["current_balance"] - state["session_start_balance"]
            print_session_summary(session_num, state, cum)

            reason = state.get("session_end_reason", "")

            if reason == "HARD_STOP":
                log.error("🛑  Bot dihentikan PERMANEN — hard_stop_balance tercapai.")
                log.error(f"    Saldo: Rp {state['current_balance']:,.2f} | Batas: Rp {cfg['hard_stop_balance']:,.0f}")
                log.error("    Restart manual diperlukan sebelum bot dapat dijalankan kembali.")
                break

            if reason == "API_ERROR":
                log.error("Bot berhenti karena error API berulang.")
                break

            # Dalam mode berbasis waktu (TP/SL=0), titik ini tidak akan pernah
            # tercapai secara normal — bot akan dihentikan via SIGTERM dari scheduler
            # yang akan ditangkap oleh blok except KeyboardInterrupt di bawah.
            if cfg["auto_restart_session"] and (tp_pct > 0 or sl_pct > 0):
                log.info("Auto-restart sesi baru dalam 3 detik...")
                time.sleep(3)
            else:
                log.info("Bot selesai.")
                break

    except KeyboardInterrupt:
        log.info("\nBot dihentikan (SIGTERM / Ctrl+C).")
        # Cetak ringkasan sesi yang sedang berjalan saat scheduler menghentikan bot
        if state is not None and state.get("roll_count", 0) > 0:
            cum["total_rolls"] += state["roll_count"]
            cum["total_net"]   += state["current_balance"] - state["session_start_balance"]
            print_session_summary(session_num, state, cum)
        else:
            log.info(f"Total sesi  : {cum['sessions']}")
            log.info(f"Total roll  : {cum['total_rolls']}")
            log.info(f"Total net   : Rp {cum['total_net']:>+,.2f}")
            log.info(f"Win rate    : {cum['wins']}/{cum['wins']+cum['losses']} "
                     f"({100*cum['wins']/max(cum['wins']+cum['losses'],1):.1f}%)")


if __name__ == "__main__":
    main()
