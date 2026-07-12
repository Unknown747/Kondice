"""
Test Bot — Simulasi dice_bot.py dengan uang tidak nyata
═══════════════════════════════════════════════════════════
Semua logika strategi IDENTIK dengan dice_bot.py (Gigi 300 | Mode Berbasis Waktu).
Tidak ada API call, tidak butuh token.

Penggunaan:
  python3 test_bot.py                              # pakai config.json, saldo default
  python3 test_bot.py --balance 342526             # mulai dengan saldo real saat ini
  python3 test_bot.py --sessions 10                # jalankan 10 sesi simulasi
  python3 test_bot.py --rolls 1800                 # ~30 mnt simulasi (1 roll/detik)
  python3 test_bot.py --seed 42                    # seed tetap (hasil reproducible)
  python3 test_bot.py --fast                       # tanpa delay, secepat mungkin
  python3 test_bot.py --balance 342526 --rolls 1800 --sessions 3 --fast --seed 99
"""

import os, sys, time, json, random, logging, argparse
from logging.handlers import RotatingFileHandler

# ═══════════════════════════════════════════════════════════
#  PATHS
# ═══════════════════════════════════════════════════════════
_DIR         = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE  = os.path.join(_DIR, "config.json")
LOG_FILE     = os.path.join(_DIR, "test_bot.log")

# ═══════════════════════════════════════════════════════════
#  LOGGER — sama persis dengan dice_bot.py
# ═══════════════════════════════════════════════════════════
def _setup_logger() -> logging.Logger:
    fmt = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)-5s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    logger = logging.getLogger("test_bot")
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
#  CONFIG — identik dengan dice_bot.py
# ═══════════════════════════════════════════════════════════
_CONFIG_DEFAULTS = {
    "currency"            : "idr",
    "base_bet"            : 300.0,    # Gigi 300 — CB cost: Rp 5.463
    "base_chance"         : 40.0,     # FIXED — payout 2.475x
    "max_chance_cap"      : 49.5,     # tidak aktif
    "target_profit_pct"   : 0.0,      # 0 = NONAKTIF (mode berbasis waktu)
    "stop_loss_pct"       : 0.0,      # 0 = NONAKTIF
    "bet_multiplier"      : 1.68,
    "max_bet_multiplier"  : 100,
    "circuit_breaker_at"  : 5,
    "roll_delay_ms"       : 500,      # dipakai test_bot saat --fast tidak aktif
    "max_api_retries"     : 10,
    "auto_restart_session": False,
    "max_sessions"        : 0,
}

def load_config() -> dict:
    cfg = dict(_CONFIG_DEFAULTS)
    if not os.path.exists(CONFIG_FILE):
        log.warning("config.json tidak ditemukan — pakai nilai default.")
        return cfg
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
        for k, v in _CONFIG_DEFAULTS.items():
            if k in data:
                cfg[k] = type(v)(data[k])
    except Exception as exc:
        log.error(f"Gagal baca config.json: {exc} — pakai nilai default")
    return cfg

# ═══════════════════════════════════════════════════════════
#  SIMULASI ROLL — pengganti API call
# ═══════════════════════════════════════════════════════════
def simulate_roll(win_chance: float, rng: random.Random) -> tuple[float, float, bool]:
    """
    Roll Over: menang jika result > target (100 - win_chance)
    Return: (result, target, won)
    """
    target = round(100.0 - win_chance, 4)
    result = round(rng.uniform(0.0, 100.0), 4)
    won    = result > target
    return result, target, won

# ═══════════════════════════════════════════════════════════
#  STATE MACHINE — identik dengan dice_bot.py
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
#  GUARDRAILS — identik dengan dice_bot.py (termasuk fix TP/SL=0)
# ═══════════════════════════════════════════════════════════
def apply_guardrails(state: dict) -> dict:
    # 1. Circuit Breaker
    if state["streak_loss"] >= state["circuit_breaker_at"]:
        log.warning(
            f"⚠  Circuit Breaker ({state['circuit_breaker_at']} loss)! "
            f"Defisit siklus Rp {state['cycle_spent']:,.2f} — reset ke baseline Rp {state['base_bet']:,.0f}."
        )
        state["current_bet"]    = state["base_bet"]
        state["current_chance"] = state["base_chance"]
        state["streak_loss"]    = 0
        state["cycle_spent"]    = 0.0
        state["sim"]["circuit_breaker_count"] += 1

    # 2. Anti-bust
    if state["current_balance"] > 0 and \
            state["current_bet"] > state["current_balance"] * 0.10:
        state["current_bet"] = round(state["current_bet"] * 0.50, 2)
        log.warning("⚠  Anti-bust: bet > 10% saldo, dikurangi 50%.")
        state["sim"]["antibust_count"] += 1

    # 3. Take-profit / Stop-loss — HANYA aktif jika pct > 0
    #    FIX: tanpa guard ini pct=0 → threshold=0 → trigger di roll pertama (bug lama).
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
#  TELEMETRY — identik dengan dice_bot.py
# ═══════════════════════════════════════════════════════════
def log_roll(state: dict, result: str, roll_num: int,
             bet_used: float, chance_used: float,
             dice_result: float, target: float):
    net    = state["current_balance"] - state["session_start_balance"]
    payout = round(99 / chance_used, 4)
    log.info(
        f"[#{roll_num:>5}] {result} | "
        f"Chance:{chance_used:>5.2f}% | "
        f"Payout:{payout:.4f}x | "
        f"Bet:{bet_used:>10.2f} IDR | "
        f"Streak:{state['streak_loss']:>2} | "
        f"Net:{net:>+10.2f} IDR | "
        f"Roll:{dice_result:>6.2f}/Tgt:{target:.2f}"
    )

# ═══════════════════════════════════════════════════════════
#  SESSION STATE
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
        "session_active"       : True,
        "session_end_reason"   : None,
        # Sim-only tracking
        "sim": {
            "circuit_breaker_count": 0,
            "antibust_count"       : 0,
            "max_streak"           : 0,
            "max_bet"              : cfg["base_bet"],
            "min_balance"          : balance,
            "max_balance"          : balance,
        }
    }

# ═══════════════════════════════════════════════════════════
#  SESSION SUMMARY
# ═══════════════════════════════════════════════════════════
def print_session_summary(session_num: int, state: dict, cum: dict):
    net    = state["current_balance"] - state["session_start_balance"]
    reason = state.get("session_end_reason", "STOPPED")
    sep    = "═" * 64

    log.info(sep)
    log.info(f"  SESI #{session_num} SELESAI — {reason}")
    log.info(f"  Roll sesi       : {state['roll_count']}")
    log.info(f"  Saldo awal      : Rp {state['session_start_balance']:>12,.2f}")
    log.info(f"  Saldo akhir     : Rp {state['current_balance']:>12,.2f}")
    log.info(f"  Net sesi        : Rp {net:>+12,.2f}")
    log.info(f"  Streak maks     : {state['sim']['max_streak']}")
    log.info(f"  Bet tertinggi   : Rp {state['sim']['max_bet']:>12,.2f}")
    log.info(f"  Saldo terendah  : Rp {state['sim']['min_balance']:>12,.2f}")
    log.info(f"  Circuit breaker : {state['sim']['circuit_breaker_count']}x")
    log.info(f"  Anti-bust       : {state['sim']['antibust_count']}x")
    log.info(sep)
    log.info(f"  KUMULATIF {cum['sessions']} SESI")
    log.info(f"  Total roll      : {cum['total_rolls']}")
    log.info(f"  Total net       : Rp {cum['total_net']:>+12,.2f}")
    log.info(f"  TP / SL / Manual: {cum['tp_count']} / {cum['sl_count']} / {cum['manual_count']}")
    total_bets = cum['wins'] + cum['losses']
    log.info(f"  Win rate        : {cum['wins']}/{total_bets} "
             f"({100*cum['wins']/max(total_bets,1):.1f}%)")
    log.info(sep)


def print_final_report(cum: dict, args):
    sep        = "═" * 64
    total_bets = cum['wins'] + cum['losses']
    log.info("")
    log.info(sep)
    log.info("  LAPORAN AKHIR SIMULASI")
    log.info(sep)
    log.info(f"  Saldo awal sim  : Rp {args.balance:>12,.2f}")
    log.info(f"  Saldo akhir sim : Rp {cum['final_balance']:>12,.2f}")
    log.info(f"  Total net       : Rp {cum['total_net']:>+12,.2f}")
    roi = 100 * cum['total_net'] / args.balance if args.balance > 0 else 0
    log.info(f"  ROI simulasi    : {roi:>+.2f}%")
    log.info(f"  Total sesi      : {cum['sessions']}")
    log.info(f"  Total roll      : {cum['total_rolls']}")
    log.info(f"  Win rate        : {cum['wins']}/{total_bets} "
             f"({100*cum['wins']/max(total_bets,1):.1f}%)")
    log.info(f"  TP / SL / Manual: {cum['tp_count']} / {cum['sl_count']} / {cum['manual_count']}")
    log.info(f"  Streak maks     : {cum['overall_max_streak']}")
    log.info(f"  Bet maks pernah : Rp {cum['overall_max_bet']:>12,.2f}")
    log.info(f"  Circuit breaker : {cum['total_circuit_breaker']}x (total)")
    log.info(f"  Anti-bust       : {cum['total_antibust']}x (total)")
    if cum['bust_count'] > 0:
        log.warning(f"  ⚠ BANGKRUT      : {cum['bust_count']}x (saldo < base_bet)")
    log.info(sep)
    log.info(f"  Log tersimpan di: {LOG_FILE}")
    log.info(sep)

# ═══════════════════════════════════════════════════════════
#  ARGPARSE
# ═══════════════════════════════════════════════════════════
def parse_args():
    p = argparse.ArgumentParser(
        description="Simulasi dice_bot.py Gigi 300 | Mode Berbasis Waktu"
    )
    p.add_argument("--balance",  type=float, default=342_526.0,
                   help="Saldo awal simulasi dalam IDR (default: 342526)")
    p.add_argument("--sessions", type=int,   default=0,
                   help="Jumlah sesi (0 = tidak terbatas, default: 0)")
    p.add_argument("--rolls",    type=int,   default=0,
                   help="Batas roll per sesi — setara durasi (0 = tidak terbatas)")
    p.add_argument("--seed",     type=int,   default=None,
                   help="Random seed untuk hasil reproducible (default: acak)")
    p.add_argument("--fast",     action="store_true",
                   help="Nonaktifkan delay antar roll")
    return p.parse_args()

# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════
def main():
    args = parse_args()
    rng  = random.Random(args.seed)
    seed_info = f"seed={args.seed}" if args.seed is not None else "seed=acak"

    sep = "═" * 64
    log.info(sep)
    log.info("  SIMULASI Stake Dice Bot — Gigi 300 | Mode Berbasis Waktu")
    log.info(sep)
    log.info(f"  Saldo awal      : Rp {args.balance:>12,.2f}")
    log.info(f"  Mode            : {'FAST (tanpa delay)' if args.fast else 'NORMAL'}")
    log.info(f"  Random          : {seed_info}")
    if args.sessions: log.info(f"  Batas sesi      : {args.sessions}")
    if args.rolls:    log.info(f"  Batas roll/sesi : {args.rolls}  (setara ~{args.rolls} detik jalan)")
    log.info(f"  Log file        : {LOG_FILE}")
    log.info(sep)

    cum = {
        "sessions"             : 0,
        "total_rolls"          : 0,
        "total_net"            : 0.0,
        "wins"                 : 0,
        "losses"               : 0,
        "tp_count"             : 0,
        "sl_count"             : 0,
        "manual_count"         : 0,
        "bust_count"           : 0,
        "final_balance"        : args.balance,
        "overall_max_streak"   : 0,
        "overall_max_bet"      : 0.0,
        "total_circuit_breaker": 0,
        "total_antibust"       : 0,
    }

    balance     = args.balance
    session_num = 0

    try:
        while True:
            cfg = load_config()

            # Override batas sesi dari CLI
            if args.sessions > 0:
                cfg["max_sessions"] = args.sessions

            # Cek bangkrut
            if balance < cfg["base_bet"]:
                log.warning(f"Saldo Rp {balance:,.2f} < base_bet Rp {cfg['base_bet']:,.2f}. "
                            f"Simulasi berhenti (bangkrut).")
                cum["bust_count"] += 1
                break

            session_num += 1
            cum["sessions"] = session_num
            state           = new_session_state(cfg, balance)

            # ── Header sesi ──────────────────────────────────
            cb_cost = sum(cfg["base_bet"] * cfg["bet_multiplier"]**i
                          for i in range(cfg["circuit_breaker_at"]))
            log.info(sep)
            log.info(f"  SESI #{session_num} DIMULAI  [VIRTUAL]")
            log.info(f"  Saldo       : Rp {balance:>12,.2f}")
            log.info(f"  Base bet    : Rp {cfg['base_bet']:>12,.2f}")
            log.info(f"  Win chance  : {cfg['base_chance']:.2f}%")
            log.info(f"  CB cost 1x  : Rp {cb_cost:>10,.2f}")
            log.info(f"  Ketahanan CB: >{int(balance / cb_cost):,} nyawa")

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
                if args.rolls:
                    log.info(f"  Durasi sim  : {args.rolls} roll per sesi")
            log.info(sep)

            # ── Roll loop ────────────────────────────────────
            while state["session_active"]:
                # Batas roll dari CLI (setara durasi scheduler)
                if args.rolls > 0 and state["roll_count"] >= args.rolls:
                    state["session_end_reason"] = "ROLL_LIMIT"
                    state["session_active"]     = False
                    break

                state = apply_guardrails(state)
                if not state["session_active"]:
                    break

                state["roll_count"] += 1
                roll_num_global = cum["total_rolls"] + state["roll_count"]

                bet_used    = state["current_bet"]
                chance_used = state["current_chance"]

                dice_result, target, won = simulate_roll(chance_used, rng)

                if won:
                    payout_mult = 99.0 / chance_used
                    profit      = bet_used * (payout_mult - 1)
                    state["current_balance"] += profit
                else:
                    state["current_balance"] -= bet_used

                # Tracking sim
                state["sim"]["max_bet"]     = max(state["sim"]["max_bet"],     bet_used)
                state["sim"]["min_balance"] = min(state["sim"]["min_balance"], state["current_balance"])
                state["sim"]["max_balance"] = max(state["sim"]["max_balance"], state["current_balance"])

                if won:
                    cum["wins"] += 1
                    state = on_win(state)
                else:
                    cum["losses"] += 1
                    state = on_loss(state, bet_used)
                    state["sim"]["max_streak"] = max(
                        state["sim"]["max_streak"], state["streak_loss"]
                    )

                log_roll(state, "WIN " if won else "LOSS",
                         roll_num_global, bet_used, chance_used, dice_result, target)

                if not args.fast and cfg["roll_delay_ms"] > 0:
                    time.sleep(cfg["roll_delay_ms"] / 1000.0)

            # ── Akhir sesi ───────────────────────────────────
            cum["total_rolls"]           += state["roll_count"]
            cum["total_net"]             += state["current_balance"] - state["session_start_balance"]
            cum["final_balance"]          = state["current_balance"]
            cum["overall_max_streak"]     = max(cum["overall_max_streak"], state["sim"]["max_streak"])
            cum["overall_max_bet"]        = max(cum["overall_max_bet"],    state["sim"]["max_bet"])
            cum["total_circuit_breaker"] += state["sim"]["circuit_breaker_count"]
            cum["total_antibust"]        += state["sim"]["antibust_count"]

            reason = state.get("session_end_reason", "STOPPED")
            if   reason == "TAKE_PROFIT": cum["tp_count"]     += 1
            elif reason == "STOP_LOSS"  : cum["sl_count"]     += 1
            else                        : cum["manual_count"] += 1

            balance = state["current_balance"]
            print_session_summary(session_num, state, cum)

            # Cek batas sesi
            if cfg["max_sessions"] > 0 and session_num >= cfg["max_sessions"]:
                log.info(f"Batas {cfg['max_sessions']} sesi tercapai. Simulasi selesai.")
                break

            if not cfg["auto_restart_session"] and not (tp_pct > 0 or sl_pct > 0):
                # Mode berbasis waktu: satu sesi per run (kecuali --sessions diberikan)
                if args.sessions == 0 or session_num >= args.sessions:
                    break

            if balance < cfg["base_bet"]:
                log.warning("Saldo tidak cukup untuk sesi berikutnya. Simulasi berhenti.")
                cum["bust_count"] += 1
                break

    except KeyboardInterrupt:
        log.info("\nSimulasi dihentikan manual (Ctrl+C).")
        cum["manual_count"] += 1

    print_final_report(cum, args)


if __name__ == "__main__":
    main()
