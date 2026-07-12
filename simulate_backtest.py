#!/usr/bin/env python3
"""
simulate_backtest.py — Monte Carlo backtest untuk strategi dice_bot.py

PENTING: Ini simulasi matematis murni (random number generator lokal).
TIDAK menghubungi API Stake, TIDAK memakai STAKE_API_KEY, TIDAK memasang
bet sungguhan. Tujuannya hanya menguji ketahanan strategi Martingale
(base_bet, bet_multiplier, circuit_breaker, hard_stop, dst.) memakai
parameter yang sama seperti config.json.

Model per-roll:
  - win_chance %   -> probabilitas menang tiap roll
  - payout on win  = bet * (99 / win_chance)   (house edge 1%, sama seperti Stake)
  - net on win     = bet * (99 / win_chance - 1)
  - net on loss    = -bet

Jalankan:
  python3 simulate_backtest.py --sessions 100 --spins 1000 --balance 700000
"""
from __future__ import annotations
import argparse
import json
import random
import statistics as stats

DEFAULT_CFG_FILE = "config.json"


def load_cfg():
    with open(DEFAULT_CFG_FILE) as f:
        data = json.load(f)
    return data


def run_session(cfg: dict, start_balance: float, target_spins: int, rng: random.Random) -> dict:
    base_bet      = cfg["base_bet"]
    base_chance   = cfg["base_chance"]
    bet_mult      = cfg["bet_multiplier"]
    max_bet_mult  = cfg["max_bet_multiplier"]
    cb_at         = cfg["circuit_breaker_at"]
    hard_stop     = cfg.get("hard_stop_balance", 0.0)
    max_cb        = cfg.get("max_cb_per_session", 0)
    max_bet       = base_bet * max_bet_mult

    balance      = start_balance
    peak_balance = start_balance
    max_drawdown = 0.0

    current_bet  = base_bet
    streak_loss  = 0
    cb_count     = 0
    wins = losses = 0
    spins_done   = 0
    end_reason   = "SPIN_TARGET"

    for _ in range(target_spins):
        # Circuit breaker check (mirrors apply_guardrails order)
        if streak_loss >= cb_at:
            current_bet = base_bet
            streak_loss = 0
            cb_count += 1
            if max_cb > 0 and cb_count >= max_cb:
                end_reason = "MAX_CB_SESSION"
                break

        if hard_stop > 0 and balance <= hard_stop:
            end_reason = "HARD_STOP"
            break

        # Anti-bust: bet > 10% saldo -> potong 50%
        if balance > 0 and current_bet > balance * 0.10:
            current_bet = round(current_bet * 0.50, 2)

        # Can't bet more than we have
        bet = min(current_bet, max(balance, 0))
        if bet <= 0:
            end_reason = "BALANCE_ZERO"
            break

        spins_done += 1
        won = rng.random() < (base_chance / 100.0)

        if won:
            wins += 1
            net = bet * (99.0 / base_chance - 1.0)
            balance += net
            current_bet = base_bet
            streak_loss = 0
        else:
            losses += 1
            balance -= bet
            streak_loss += 1
            current_bet = min(round(current_bet * bet_mult, 2), max_bet)

        peak_balance = max(peak_balance, balance)
        max_drawdown = max(max_drawdown, peak_balance - balance)

        if hard_stop > 0 and balance <= hard_stop:
            end_reason = "HARD_STOP"
            break
        if balance <= 0:
            end_reason = "BUSTED"
            break

    return {
        "end_balance": balance,
        "net": balance - start_balance,
        "spins": spins_done,
        "wins": wins,
        "losses": losses,
        "cb_count": cb_count,
        "max_drawdown": max_drawdown,
        "end_reason": end_reason,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=100)
    ap.add_argument("--spins", type=int, default=1000, help="target spins per session (avg)")
    ap.add_argument("--spins-jitter", type=int, default=150,
                    help="+/- random jitter around --spins to mimic 'rata-rata N spin per sesi'")
    ap.add_argument("--balance", type=float, default=700_000.0)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    cfg = load_cfg()
    rng = random.Random(args.seed)

    results = []
    for i in range(args.sessions):
        jitter = rng.randint(-args.spins_jitter, args.spins_jitter)
        target_spins = max(1, args.spins + jitter)
        res = run_session(cfg, args.balance, target_spins, rng)
        results.append(res)

    nets           = [r["net"] for r in results]
    end_balances   = [r["end_balance"] for r in results]
    drawdowns      = [r["max_drawdown"] for r in results]
    total_wins     = sum(r["wins"] for r in results)
    total_losses   = sum(r["losses"] for r in results)
    busted         = sum(1 for r in results if r["end_reason"] == "BUSTED")
    hard_stopped   = sum(1 for r in results if r["end_reason"] == "HARD_STOP")
    max_cb_hit     = sum(1 for r in results if r["end_reason"] == "MAX_CB_SESSION")
    reached_target = sum(1 for r in results if r["end_reason"] == "SPIN_TARGET")
    profitable     = sum(1 for n in nets if n > 0)

    print("=" * 64)
    print(f"  BACKTEST — {args.sessions} sesi | ~{args.spins} spin/sesi (±{args.spins_jitter}) | saldo awal Rp {args.balance:,.0f}")
    print("=" * 64)
    print(f"  Config dipakai   : base_bet=Rp{cfg['base_bet']:,.0f}  chance={cfg['base_chance']}%  "
          f"mult={cfg['bet_multiplier']}  CB@{cfg['circuit_breaker_at']}  "
          f"hard_stop=Rp{cfg.get('hard_stop_balance',0):,.0f}  max_cb/sesi={cfg.get('max_cb_per_session',0)}")
    print("-" * 64)
    print(f"  Total roll         : {total_wins + total_losses:,}  (WR {100*total_wins/max(total_wins+total_losses,1):.2f}%)")
    print(f"  Sesi profit        : {profitable}/{args.sessions} ({100*profitable/args.sessions:.1f}%)")
    print(f"  Sesi capai target spin (tidak kena rem): {reached_target}/{args.sessions}")
    print(f"  Sesi kena HARD_STOP (saldo <= Rp{cfg.get('hard_stop_balance',0):,.0f}): {hard_stopped}/{args.sessions}")
    print(f"  Sesi kena MAX_CB_SESSION: {max_cb_hit}/{args.sessions}")
    print(f"  Sesi BUSTED (saldo habis): {busted}/{args.sessions}")
    print("-" * 64)
    print(f"  Net per sesi   : rata2 Rp {stats.mean(nets):>+14,.0f} | median Rp {stats.median(nets):>+14,.0f} | "
          f"min Rp {min(nets):>+14,.0f} | max Rp {max(nets):>+14,.0f}")
    print(f"  Saldo akhir    : rata2 Rp {stats.mean(end_balances):>14,.0f} | min Rp {min(end_balances):>14,.0f} | "
          f"max Rp {max(end_balances):>14,.0f}")
    print(f"  Max drawdown   : rata2 Rp {stats.mean(drawdowns):>14,.0f} | terburuk Rp {max(drawdowns):>14,.0f}")
    print(f"  Total net (100 sesi digabung, saldo awal Rp{args.balance:,.0f} tiap sesi): Rp {sum(nets):>+,.0f}")
    print("=" * 64)


if __name__ == "__main__":
    main()
