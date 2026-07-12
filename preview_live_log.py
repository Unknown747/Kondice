"""
Preview Live Log — simulasi tampilan terminal dice_bot.py
═══════════════════════════════════════════════════════════
TUJUAN : Menunjukkan PERSIS bagaimana log terminal terlihat saat bot
         jalan sungguhan — tanpa menyentuh API Stake atau uang asli.

CARA KERJA:
  - Memakai ULANG fungsi asli dari dice_bot.py: load_config, new_session_state,
    on_win, on_loss, apply_guardrails, log_roll, print_session_summary,
    smart_random_delay — jadi format & logika guardrail-nya identik 100%.
  - Satu-satunya bagian yang diganti: place_dice_bet() (panggilan API asli)
    diganti dengan roll dadu lokal berbasis random.random() < base_chance,
    memakai RNG Python biasa (bukan RNG server Stake).

Jalankan: python3 preview_live_log.py [jumlah_roll] [saldo_awal]
Default : 60 roll, saldo Rp 700.000
"""
import os
import sys
import random

# Dummy — dice_bot.py butuh STAKE_API_KEY ada saat import, tapi TIDAK PERNAH
# dipakai untuk request nyata di preview ini (place_dice_bet asli tidak dipanggil).
os.environ.setdefault("STAKE_API_KEY", "preview-only-not-a-real-key")

import dice_bot as bot  # noqa: E402  (import setelah env var di-set)


def simulate_roll(chance: float) -> bool:
    """Ganti place_dice_bet() asli — roll lokal, bukan API Stake."""
    return random.random() < (chance / 100.0)


def main():
    n_rolls = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    balance = float(sys.argv[2]) if len(sys.argv) > 2 else 700_000.0

    cfg = bot.load_config()
    state = bot.new_session_state(cfg, balance)

    bot.log.info("═" * 60)
    bot.log.info("  [PREVIEW] Stake Dice Bot — Gigi 200 | Mode Berbasis Waktu")
    bot.log.info("  [PREVIEW] Roll disimulasikan lokal — BUKAN API Stake asli")
    bot.log.info("═" * 60)
    bot.log.info(f"  SESI #1 DIMULAI")
    bot.log.info(f"  Saldo       : Rp {balance:>12,.2f}")
    bot.log.info(f"  Base bet    : Rp {cfg['base_bet']:>12,.2f}")
    bot.log.info(f"  Win chance  : {cfg['base_chance']:.2f}%")
    bot.log.info("═" * 60)

    cum = {"sessions": 1, "total_rolls": 0, "total_net": 0.0, "wins": 0, "losses": 0}

    for _ in range(n_rolls):
        state = bot.apply_guardrails(state)
        if not state["session_active"]:
            break

        state["roll_count"] += 1
        bet_used = state["current_bet"]
        chance_used = state["current_chance"]

        won = simulate_roll(chance_used)
        # anti-bust bisa memotong bet jadi lebih kecil dari saldo — cukup untuk preview
        state["current_balance"] = max(0.0, state["current_balance"] + (
            bet_used * (99.0 / chance_used - 1) if won else -bet_used
        ))

        if won:
            cum["wins"] += 1
            roll_net = round(bet_used * (99.0 / chance_used - 1), 2)
            state = bot.on_win(state)
        else:
            cum["losses"] += 1
            roll_net = -bet_used
            state = bot.on_loss(state, bet_used)

        bot.log_roll(state, "WIN " if won else "LOSS", state["roll_count"],
                     bet_used, chance_used, roll_net)

        interval = state.get("realtime_log_interval_rolls", 0)
        if interval > 0 and state["roll_count"] % interval == 0:
            _wr = 100 * cum["wins"] / max(cum["wins"] + cum["losses"], 1)
            _net = state["current_balance"] - state["session_start_balance"]
            _cb_rate = state["session_cb_count"] / state["roll_count"] * 100
            bot.log.info(
                f"[CHECKPOINT #{state['roll_count']:>4}] "
                f"CB sesi: {state['session_cb_count']}x ({_cb_rate:.1f}/100roll) | "
                f"WR: {_wr:.1f}% | "
                f"Net sesi: Rp {_net:>+,.0f} | "
                f"Saldo: Rp {state['current_balance']:,.0f}"
            )

    cum["total_rolls"] += state["roll_count"]
    cum["total_net"] += state["current_balance"] - state["session_start_balance"]
    bot.print_session_summary(1, state, cum)
    bot.log.info("[PREVIEW] Selesai — ini contoh tampilan, bukan sesi bot asli.")


if __name__ == "__main__":
    main()
