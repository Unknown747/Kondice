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
    total_wager  = 0.0
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
        total_wager += bet
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
        "total_wager": total_wager,
        "end_reason": end_reason,
    }


def smart_random_delay(rng: random.Random) -> float:
    """Sama persis dengan smart_random_delay() di dice_bot.py — 3 tier probabilistik."""
    r = rng.random()
    if r < 0.75:
        return rng.uniform(0.8, 1.5)
    elif r < 0.95:
        return rng.uniform(0.4, 0.7)
    else:
        return rng.uniform(3.0, 6.5)


def spins_in_time_budget(budget_seconds: float, rng: random.Random, api_latency: float = 0.0) -> int:
    """Berapa banyak roll yang muat dalam budget_seconds detik, memakai delay asli dari skrip
    ditambah estimasi latency jaringan per request (round-trip ke server Stake)."""
    t = 0.0
    n = 0
    while True:
        d = smart_random_delay(rng) + api_latency
        if t + d > budget_seconds:
            break
        t += d
        n += 1
    return n


def simulate_day(cfg: dict, start_balance: float, run_minutes: int, pause_minutes: int,
                  hours: float, api_latency: float, rng: random.Random) -> dict:
    """
    Simulasikan 1 hari penuh mengikuti pola scheduler.py: run_minutes jalan / pause_minutes jeda,
    berulang selama `hours` jam. Tiap fase JALAN = proses dice_bot.py baru (state bet/streak/CB
    di-reset ke awal, TAPI saldo dilanjutkan dari fase sebelumnya — persis seperti scheduler
    yang me-restart proses dice_bot.py tiap siklus).
    Begitu satu fase berakhir dengan HARD_STOP, sisa hari dianggap tidak ada bet lagi (saldo
    sudah di bawah batas aman sehingga guardrail langsung memicu lagi di awal tiap sesi baru).
    """
    cycle_seconds  = (run_minutes + pause_minutes) * 60
    total_seconds  = hours * 3600
    num_cycles     = int(total_seconds // cycle_seconds)

    balance        = start_balance
    peak_balance   = start_balance
    max_drawdown   = 0.0
    total_wager    = 0.0
    wins = losses  = 0
    total_cb       = 0
    hard_stopped_at_phase = None
    phase_results  = []

    for phase in range(1, num_cycles + 1):
        if hard_stopped_at_phase is not None:
            phase_results.append({"phase": phase, "spins": 0, "net": 0.0,
                                   "balance": balance, "reason": "HALTED_BY_HARD_STOP"})
            continue

        spins_budget = spins_in_time_budget(run_minutes * 60, rng, api_latency)
        res = run_session(cfg, balance, spins_budget, rng)

        balance      = res["end_balance"]
        total_wager += res["total_wager"]
        wins        += res["wins"]
        losses      += res["losses"]
        total_cb    += res["cb_count"]
        peak_balance = max(peak_balance, balance)
        max_drawdown = max(max_drawdown, peak_balance - balance)

        phase_results.append({"phase": phase, "spins": res["spins"], "net": res["net"],
                               "balance": balance, "reason": res["end_reason"]})

        if res["end_reason"] in ("HARD_STOP", "BUSTED"):
            hard_stopped_at_phase = phase

    return {
        "end_balance": balance,
        "net": balance - start_balance,
        "total_wager": total_wager,
        "wins": wins,
        "losses": losses,
        "spins": wins + losses,
        "cb_count": total_cb,
        "max_drawdown": max_drawdown,
        "num_cycles": num_cycles,
        "hard_stopped_at_phase": hard_stopped_at_phase,
        "phases": phase_results,
    }


def run_day_mode(args):
    cfg = load_cfg()
    if args.base_bet is not None:
        cfg["base_bet"] = args.base_bet
    rng = random.Random(args.seed)

    trials = []
    for _ in range(args.trials):
        res = simulate_day(cfg, args.balance, args.run_minutes, args.pause_minutes,
                            args.hours, args.api_latency, rng)
        trials.append(res)

    nets       = [t["net"] for t in trials]
    spins      = [t["spins"] for t in trials]
    wagers     = [t["total_wager"] for t in trials]
    drawdowns  = [t["max_drawdown"] for t in trials]
    hard_stops = sum(1 for t in trials if t["hard_stopped_at_phase"] is not None)
    profitable = sum(1 for n in nets if n > 0)

    idx_sorted   = sorted(range(len(trials)), key=lambda i: nets[i])
    worst_i      = idx_sorted[0]
    best_i       = idx_sorted[-1]
    median_i     = idx_sorted[len(idx_sorted) // 2]

    def describe(label, i):
        t = trials[i]
        print(f"  [{label}] net Rp {t['net']:>+13,.0f}  |  saldo akhir Rp {t['end_balance']:>13,.0f}  |  "
              f"spin {t['spins']:>6,}  |  wager Rp {t['total_wager']:>14,.0f}  |  "
              f"CB {t['cb_count']:>3}x  |  drawdown Rp {t['max_drawdown']:>12,.0f}  |  "
              f"{'HARD STOP di fase #' + str(t['hard_stopped_at_phase']) if t['hard_stopped_at_phase'] else 'selesai 24 jam penuh'}")

    print("=" * 78)
    print(f"  SIMULASI 24 JAM — {args.trials} trial | run {args.run_minutes}mnt/jeda {args.pause_minutes}mnt "
          f"| {args.hours:.0f} jam | saldo awal Rp {args.balance:,.0f}")
    print(f"  Delay: smart_random_delay skrip asli + estimasi latency API {args.api_latency:.2f} dtk/request")
    print("=" * 78)
    print(f"  Config: base_bet=Rp{cfg['base_bet']:,.0f}  chance={cfg['base_chance']}%  mult={cfg['bet_multiplier']}  "
          f"CB@{cfg['circuit_breaker_at']}  hard_stop=Rp{cfg.get('hard_stop_balance',0):,.0f}  "
          f"max_cb/sesi={cfg.get('max_cb_per_session',0)}  siklus/hari={trials[0]['num_cycles']}")
    print("-" * 78)
    print(f"  Spin/hari      : rata2 {stats.mean(spins):>8,.0f}  |  min {min(spins):>8,}  |  max {max(spins):>8,}")
    print(f"  Wager/hari     : rata2 Rp {stats.mean(wagers):>14,.0f}  |  min Rp {min(wagers):>14,.0f}  |  max Rp {max(wagers):>14,.0f}")
    print(f"  Net/hari       : rata2 Rp {stats.mean(nets):>+14,.0f}  |  median Rp {stats.median(nets):>+14,.0f}")
    print(f"  Hari profit    : {profitable}/{args.trials} ({100*profitable/args.trials:.1f}%)")
    print(f"  Hari kena HARD STOP (berhenti total sebelum 24 jam): {hard_stops}/{args.trials} ({100*hard_stops/args.trials:.1f}%)")
    print(f"  Max drawdown   : rata2 Rp {stats.mean(drawdowns):>14,.0f}  |  terburuk Rp {max(drawdowns):>14,.0f}")
    print("-" * 78)
    print("  SKENARIO REPRESENTATIF DARI HASIL TRIAL:")
    describe("TERBURUK", worst_i)
    describe("SEDANG  ", median_i)
    describe("TERBAIK ", best_i)
    print("=" * 78)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["session", "day"], default="session",
                     help="'session' = backtest N sesi spin tetap, 'day' = simulasi 24 jam scheduler")
    ap.add_argument("--sessions", type=int, default=100)
    ap.add_argument("--spins", type=int, default=1000, help="target spins per session (avg)")
    ap.add_argument("--spins-jitter", type=int, default=150,
                    help="+/- random jitter around --spins to mimic 'rata-rata N spin per sesi'")
    ap.add_argument("--balance", type=float, default=700_000.0)
    ap.add_argument("--base-bet", type=float, default=None, help="override cfg base_bet for this run")
    ap.add_argument("--seed", type=int, default=None)
    # --mode day khusus
    ap.add_argument("--trials", type=int, default=200, help="jumlah hari yang disimulasikan (mode day)")
    ap.add_argument("--run-minutes", type=int, default=30, help="durasi jalan scheduler per siklus")
    ap.add_argument("--pause-minutes", type=int, default=10, help="durasi jeda scheduler per siklus")
    ap.add_argument("--hours", type=float, default=24.0, help="total durasi simulasi per hari")
    ap.add_argument("--api-latency", type=float, default=0.3,
                    help="estimasi latency network per request roll (detik), di luar delay skrip")
    args = ap.parse_args()

    if args.mode == "day":
        run_day_mode(args)
        return

    cfg = load_cfg()
    if args.base_bet is not None:
        cfg["base_bet"] = args.base_bet
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
    total_wager    = sum(r["total_wager"] for r in results)
    total_rolls    = total_wins + total_losses
    avg_bet_per_roll = total_wager / max(total_rolls, 1)
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
    print(f"  Total roll         : {total_rolls:,}  (WR {100*total_wins/max(total_rolls,1):.2f}%)")
    print(f"  Total wager        : Rp {total_wager:,.0f}  |  Rata2 bet/roll: Rp {avg_bet_per_roll:,.2f}")
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
