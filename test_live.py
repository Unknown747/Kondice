"""
Test live: 5 percobaan × maks 100 roll per sesi.
Setiap sesi berhenti di TP, SL, atau 100 roll.
Di akhir, analisis strategi ditampilkan.
"""

import os, sys, time, uuid, json, re
import requests
from requests.exceptions import HTTPError, ConnectionError as ReqConnError, Timeout as ReqTimeout

# ── Config ────────────────────────────────────────────────
MAX_SESSIONS   = 5
MAX_ROLLS      = 100
BASE_BET       = 100.0
BASE_CHANCE    = 40.0        # % — payout 2.475x, chance FIXED (tidak naik saat streak)
MAX_CHANCE_CAP = 49.5        # tidak aktif (chance fixed)
TP_PCT         = 2.0         # exit lebih cepat — 2% = ~Rp1.850, kurangi paparan CB cluster
SL_PCT         = 5.0         # stop sesi saat loss >= 5%. Dengan CB=4, butuh 6 CB untuk trigger SL
CB_AT          = 4           # CB cost Rp844 (bukan Rp1.485). 5 CB = Rp4.220 < SL Rp4.600
MAX_BET_MULT   = 100         # hard cap bet = BASE_BET × MAX_BET_MULT
CURRENCY       = "idr"
ROLL_DELAY     = 0.0         # 0 untuk test cepat (VPS pakai 0.5)
MAX_RETRIES    = 5

# ── API ───────────────────────────────────────────────────
API_ENDPOINT = "https://stake.com/_api/graphql"
API_KEY      = os.environ.get("STAKE_API_KEY", "").strip()
if not API_KEY:
    print("ERROR: STAKE_API_KEY belum diset.")
    sys.exit(1)

sess = requests.Session()
sess.headers.update({
    "Content-Type"                : "application/json",
    "Accept"                      : "*/*",
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
})

BALANCE_QUERY = """
query UserBalances {
  user { balances { available { amount currency } } }
}
"""

DICE_MUTATION = """
mutation DiceRoll(
  $amount: Float!, $target: Float!,
  $condition: CasinoGameDiceConditionEnum!,
  $currency: CurrencyEnum!, $identifier: String!
) {
  diceRoll(amount: $amount, target: $target, condition: $condition,
           currency: $currency, identifier: $identifier) {
    id active payoutMultiplier amountMultiplier amount payout updatedAt currency game
    user { id balances { available { amount currency } } }
  }
}
"""

# ── Helpers ───────────────────────────────────────────────
def gql(query, variables=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    r = sess.post(API_ENDPOINT, json=payload, timeout=15)
    if r.status_code == 401:
        raise RuntimeError("Token expired (HTTP 401)")
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        raise RuntimeError(f"GraphQL: {body['errors']}")
    return body.get("data", {})

def fetch_balance():
    data = gql(BALANCE_QUERY)
    for b in data["user"]["balances"]:
        av = b.get("available", {})
        if av.get("currency", "").lower() == CURRENCY:
            return float(av["amount"])
    raise RuntimeError(f"Wallet {CURRENCY.upper()} tidak ditemukan")

def place_bet(amount, chance):
    target = round(100.0 - chance, 4)
    data = gql(DICE_MUTATION, {
        "amount"    : round(amount, 2),
        "target"    : target,
        "condition" : "above",
        "currency"  : CURRENCY,
        "identifier": str(uuid.uuid4()),
    })
    return data

def balance_from_roll(data):
    try:
        for b in data["diceRoll"]["user"]["balances"]:
            av = b.get("available", {})
            if av.get("currency", "").lower() == CURRENCY:
                return float(av["amount"])
    except (KeyError, TypeError):
        pass
    return None

# ── Strategi ──────────────────────────────────────────────
def init_cycle():
    return {
        "bet"        : BASE_BET,
        "chance"     : BASE_CHANCE,
        "streak"     : 0,
        "cycle_spent": 0.0,
    }

def after_win(cycle):
    return init_cycle()

def after_loss(cycle, bet_placed):
    cycle["streak"]      += 1
    cycle["cycle_spent"] += bet_placed

    # Chance TIDAK dinaikkan — fixed di BASE_CHANCE agar payout stabil
    # dan recovery bet tidak membengkak melebihi batas Circuit Breaker.

    # True Martingale: bet agar 1 WIN = tutup semua kerugian + BASE_BET profit
    payout = 99.0 / cycle["chance"]
    recovery = (cycle["cycle_spent"] + BASE_BET) / (payout - 1.0)
    cycle["bet"] = min(round(recovery, 2), BASE_BET * MAX_BET_MULT)
    return cycle

def circuit_breaker(cycle):
    """Terima rugi siklus, mulai ulang dari baseline."""
    return init_cycle()

# ── Satu sesi ─────────────────────────────────────────────
def run_session(sesi_num, start_balance):
    balance = start_balance
    tp_thr  =  start_balance * TP_PCT / 100
    sl_thr  = -start_balance * SL_PCT / 100
    cycle   = init_cycle()

    rolls, wins, losses = 0, 0, 0
    cb_count = 0
    peak_net = 0.0
    trough_net = 0.0
    stop_reason = "MAX_ROLL"
    errors = 0

    print(f"\n{'═'*62}")
    print(f"  SESI #{sesi_num}  |  Saldo: Rp {start_balance:,.2f}")
    print(f"  TP: +Rp {tp_thr:,.2f}  |  SL: -Rp {abs(sl_thr):,.2f}  |  Maks: {MAX_ROLLS} roll")
    print(f"{'═'*62}")

    while rolls < MAX_ROLLS:

        # Circuit breaker
        if cycle["streak"] >= CB_AT:
            cb_count += 1
            print(f"  [#{rolls+1:>3}] ⚠ CB #{cb_count} — defisit siklus Rp {cycle['cycle_spent']:,.2f}, reset baseline")
            cycle = circuit_breaker(cycle)

        bet_used    = cycle["bet"]
        chance_used = cycle["chance"]

        # Anti-bust: bet > 10% saldo → kurangi
        if balance > 0 and bet_used > balance * 0.10:
            bet_used = round(balance * 0.05, 2)

        # Pasang bet (dengan retry)
        data = None
        for attempt in range(MAX_RETRIES):
            try:
                data = place_bet(bet_used, chance_used)
                errors = 0
                break
            except Exception as exc:
                errors += 1
                print(f"  [API error #{errors}] {exc} — retry...")
                time.sleep(3)

        if data is None:
            print(f"  Gagal setelah {MAX_RETRIES} percobaan. Sesi dihentikan.")
            stop_reason = "API_ERROR"
            break

        rolls += 1
        dice  = data.get("diceRoll", {})
        won   = float(dice.get("payout", 0)) > 0

        # Update saldo
        new_bal = balance_from_roll(data)
        if new_bal is not None:
            balance = new_bal

        net = balance - start_balance
        peak_net   = max(peak_net, net)
        trough_net = min(trough_net, net)

        # Update state
        if won:
            wins  += 1
            cycle  = after_win(cycle)
            label  = "WIN "
        else:
            losses += 1
            cycle   = after_loss(cycle, bet_used)
            label   = "LOSS"

        payout_disp = round(99 / chance_used, 4)
        print(f"  [#{rolls:>3}] {label} | "
              f"{chance_used:>5.2f}% ({payout_disp:.3f}x) | "
              f"Bet: {bet_used:>8.2f} | "
              f"Streak: {cycle['streak']:>2} | "
              f"Net: Rp {net:>+9.2f}")

        # TP / SL
        if net >= tp_thr:
            stop_reason = "TAKE_PROFIT"
            break
        if net <= sl_thr:
            stop_reason = "STOP_LOSS"
            break

        time.sleep(ROLL_DELAY)

    net_final = balance - start_balance
    win_rate  = 100 * wins / max(rolls, 1)

    print(f"\n  ── HASIL SESI #{sesi_num} ─────────────────────────────")
    print(f"  Stop    : {stop_reason}")
    print(f"  Roll    : {rolls}  |  WIN: {wins}  LOSS: {losses}  ({win_rate:.1f}%)")
    print(f"  Net     : Rp {net_final:>+,.2f}")
    print(f"  Peak    : Rp +{peak_net:,.2f}  |  Trough: Rp {trough_net:,.2f}")
    print(f"  CB fired: {cb_count}x")

    return {
        "sesi"       : sesi_num,
        "rolls"      : rolls,
        "wins"       : wins,
        "losses"     : losses,
        "win_rate"   : win_rate,
        "net"        : net_final,
        "peak"       : peak_net,
        "trough"     : trough_net,
        "cb_count"   : cb_count,
        "stop_reason": stop_reason,
        "end_balance": balance,
    }

# ── Main ──────────────────────────────────────────────────
def main():
    print("═" * 62)
    print(f"  LIVE TEST: {MAX_SESSIONS} sesi × maks {MAX_ROLLS} roll")
    print(f"  Strategi : True Martingale | Chance {BASE_CHANCE}% base | CB={CB_AT}")
    print(f"  TP/SL    : {TP_PCT}% / {SL_PCT}%")
    print("═" * 62)

    try:
        balance = fetch_balance()
    except Exception as exc:
        print(f"Gagal ambil saldo: {exc}")
        sys.exit(1)

    print(f"\n  Saldo awal total: Rp {balance:,.2f}")

    results    = []
    start_total = balance

    for i in range(1, MAX_SESSIONS + 1):
        try:
            r = run_session(i, balance)
        except KeyboardInterrupt:
            print("\n\nDihentikan manual.")
            break
        results.append(r)
        balance = r["end_balance"]
        time.sleep(1.5)

    # ── Analisis keseluruhan ──────────────────────────────
    if not results:
        return

    total_rolls  = sum(r["rolls"]  for r in results)
    total_wins   = sum(r["wins"]   for r in results)
    total_losses = sum(r["losses"] for r in results)
    total_net    = sum(r["net"]    for r in results)
    total_cb     = sum(r["cb_count"] for r in results)
    sesi_profit  = sum(1 for r in results if r["net"] > 0)
    sesi_loss    = sum(1 for r in results if r["net"] < 0)
    sesi_tp      = sum(1 for r in results if r["stop_reason"] == "TAKE_PROFIT")
    sesi_sl      = sum(1 for r in results if r["stop_reason"] == "STOP_LOSS")
    sesi_maxroll = sum(1 for r in results if r["stop_reason"] == "MAX_ROLL")

    print(f"\n{'═'*62}")
    print(f"  RINGKASAN {len(results)} SESI")
    print(f"{'═'*62}")
    print(f"  Saldo awal      : Rp {start_total:>12,.2f}")
    print(f"  Saldo akhir     : Rp {balance:>12,.2f}")
    print(f"  Total net       : Rp {total_net:>+12,.2f}")
    print(f"  Total roll      : {total_rolls}  (WIN {total_wins} / LOSS {total_losses})")
    print(f"  Win rate        : {100*total_wins/max(total_rolls,1):.1f}%  (target ≈ 35%)")
    print(f"  Circuit breaker : {total_cb}x total")
    print(f"  Sesi profit     : {sesi_profit}/{len(results)}")
    print(f"  Sesi rugi       : {sesi_loss}/{len(results)}")
    print(f"  Stop TP/SL/MAX  : {sesi_tp}/{sesi_sl}/{sesi_maxroll}")
    print()

    # ── Per-sesi tabel ───────────────────────────────────
    print(f"  {'Sesi':>4} {'Roll':>5} {'Win%':>6} {'Net (IDR)':>12} {'CB':>4} {'Stop'}")
    print(f"  {'─'*4} {'─'*5} {'─'*6} {'─'*12} {'─'*4} {'─'*12}")
    for r in results:
        mark = "✅" if r["net"] > 0 else "❌"
        print(f"  {r['sesi']:>4} {r['rolls']:>5} {r['win_rate']:>5.1f}% "
              f"{r['net']:>+12,.2f} {r['cb_count']:>4}  {r['stop_reason']} {mark}")

    # ── Evaluasi strategi ─────────────────────────────────
    print(f"\n{'═'*62}")
    print("  EVALUASI STRATEGI")
    print(f"{'═'*62}")

    avg_net = total_net / len(results)
    avg_win_rate = 100 * total_wins / max(total_rolls, 1)

    notes = []
    if avg_win_rate < 30:
        notes.append("⚠  Win rate di bawah 30% — variance tinggi, kemungkinan RNG streak buruk")
    elif avg_win_rate > 40:
        notes.append("✅ Win rate di atas 40% — lebih baik dari ekspektasi 35%")
    else:
        notes.append(f"✅ Win rate {avg_win_rate:.1f}% — sesuai ekspektasi (~35%)")

    if total_cb == 0:
        notes.append("✅ Tidak ada circuit breaker — siklus selalu recover sebelum CB")
    elif total_cb <= len(results):
        notes.append(f"ℹ  CB {total_cb}x ({total_cb/len(results):.1f}x/sesi) — normal untuk streak panjang")
    else:
        notes.append(f"⚠  CB {total_cb}x ({total_cb/len(results):.1f}x/sesi) — terlalu sering, pertimbangkan naikkan CB_AT")

    if sesi_profit > sesi_loss:
        notes.append(f"✅ {sesi_profit}/{len(results)} sesi profit — strategi menang lebih sering")
    elif sesi_profit == sesi_loss:
        notes.append(f"⚖  {sesi_profit}/{len(results)} sesi profit — hasil seimbang")
    else:
        notes.append(f"⚠  Hanya {sesi_profit}/{len(results)} sesi profit — pertimbangkan turunkan SL% atau naikkan chance")

    if sesi_sl > sesi_tp:
        notes.append("⚠  Stop-loss lebih sering dari take-profit — pertimbangkan turunkan base_bet atau naikkan max_chance_cap")

    if total_net > 0:
        notes.append(f"✅ Net keseluruhan PROFIT Rp {total_net:,.2f}")
    else:
        notes.append(f"❌ Net keseluruhan RUGI Rp {abs(total_net):,.2f}  — {abs(total_net)/start_total*100:.2f}% dari modal")

    for n in notes:
        print(f"  {n}")

    print(f"\n  Avg net/sesi : Rp {avg_net:>+,.2f}")
    print(f"{'═'*62}\n")

if __name__ == "__main__":
    main()
