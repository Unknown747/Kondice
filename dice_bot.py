"""
Dice Automation Bot — Stake API (IDR)
Implements the full spec: state machines, guardrails, telemetry.
Requires: STAKE_API_KEY environment variable (your Stake session token).
"""

import os
import sys
import time
import uuid
import requests
from requests.exceptions import HTTPError

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
API_ENDPOINT = "https://api.stake.com/graphql"
CURRENCY     = "idr"

BASE_BET          = 100.00
BASE_CHANCE       = 5.00
MAX_CHANCE_CAP    = 45.00
TARGET_PROFIT_PCT = 15.00
STOP_LOSS_PCT     = 25.00

# Delay between rolls (seconds) — set to 0 for max speed
ROLL_DELAY = 0.5

# Max consecutive API failures before aborting (prevents silent infinite retry)
MAX_API_RETRIES = 10

# ─────────────────────────────────────────────
# GRAPHQL QUERIES
# ─────────────────────────────────────────────
BALANCE_QUERY = """
query UserBalances {
  user {
    balances {
      available {
        amount
        currency
      }
    }
  }
}
"""

DICE_MUTATION = """
mutation DiceRoll(
  $amount: Float!,
  $target: Float!,
  $condition: CasinoGameDiceConditionEnum!,
  $currency: CurrencyEnum!,
  $identifier: String!
) {
  diceRoll(
    amount: $amount,
    target: $target,
    condition: $condition,
    currency: $currency,
    identifier: $identifier
  ) {
    id
    active
    payoutMultiplier
    amountMultiplier
    amount
    payout
    updatedAt
    currency
    game
    ... on CasinoGameDice {
      result
      target
      condition
    }
    user {
      id
      balances {
        available {
          amount
          currency
        }
      }
    }
  }
}
"""

# ─────────────────────────────────────────────
# API HELPERS
# ─────────────────────────────────────────────

# FIX: read token once at startup; fail loudly if missing
def _load_api_key() -> str:
    key = os.environ.get("STAKE_API_KEY", "").strip()
    if not key:
        print("[ERROR] STAKE_API_KEY environment variable not set.")
        print("        Set it in .env or export it before running.")
        sys.exit(1)
    return key

API_KEY = _load_api_key()

HEADERS = {
    "Content-Type": "application/json",
    "x-access-token": API_KEY,
}


def gql(query: str, variables: dict = None) -> dict:
    """Execute a GraphQL request and return the data payload."""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    try:
        resp = requests.post(API_ENDPOINT, json=payload, headers=HEADERS, timeout=15)
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(f"Network error: {exc}") from exc
    except requests.exceptions.Timeout:
        raise RuntimeError("Request timed out after 15 s")

    # FIX: detect 401 explicitly — token expired, no point retrying
    if resp.status_code == 401:
        print("\n[AUTH ERROR] Stake returned 401 Unauthorized.")
        print("  Your session token has expired. Get a fresh token and")
        print("  update it with:  ./update_token.sh  then restart the bot.")
        sys.exit(1)

    try:
        resp.raise_for_status()
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {resp.status_code}: {exc}") from exc

    body = resp.json()

    if "errors" in body:
        raise RuntimeError(f"GraphQL error: {body['errors']}")

    return body.get("data", {})


def fetch_idr_balance() -> float:
    """Return the current available IDR balance."""
    # FIX: wrap KeyError so the user gets a clear message
    try:
        data = gql(BALANCE_QUERY)
        balances = data["user"]["balances"]["available"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            f"Could not parse balance response from Stake API: {exc}\n"
            "Check that your token is for the correct account."
        ) from exc

    for entry in balances:
        if entry.get("currency", "").lower() == CURRENCY:
            return float(entry["amount"])

    raise RuntimeError(
        f"No {CURRENCY.upper()} wallet found on this account. "
        "Make sure the account has an IDR balance."
    )


def fetch_current_balance() -> float:
    """Fresh balance fetch — used when embedded balance is unavailable."""
    return fetch_idr_balance()


def extract_balance_from_roll(roll_data: dict) -> float | None:
    """Pull the updated IDR balance embedded in the diceRoll response."""
    try:
        balances = roll_data["diceRoll"]["user"]["balances"]["available"]
        for entry in balances:
            if entry.get("currency", "").lower() == CURRENCY:
                return float(entry["amount"])
    except (KeyError, TypeError):
        pass
    return None


def place_dice_bet(bet_amount: float, win_chance: float) -> dict:
    """
    Place a single dice bet.
    Uses Roll Over: win if result > target.
    target = 100 - win_chance  (e.g. chance=5 → target=95)
    """
    target = round(100.0 - win_chance, 4)
    identifier = str(uuid.uuid4())

    variables = {
        "amount":     round(bet_amount, 2),
        "target":     target,
        "condition":  "above",   # Roll Over
        "currency":   CURRENCY,
        "identifier": identifier,
    }
    return gql(DICE_MUTATION, variables)


# ─────────────────────────────────────────────
# STATE MACHINE
# ─────────────────────────────────────────────
def on_win(state: dict) -> dict:
    state["CURRENT_BET"]    = state["BASE_BET"]
    state["CURRENT_CHANCE"] = state["BASE_CHANCE"]
    state["STREAK_LOSS"]    = 0
    print("  → Win registered. System state flushed to baseline.")
    return state


def on_loss(state: dict) -> dict:
    state["STREAK_LOSS"] += 1
    streak = state["STREAK_LOSS"]

    # Modulo-2: expand win chance every 2 consecutive losses
    if streak % 2 == 0:
        state["CURRENT_CHANCE"] = min(
            state["CURRENT_CHANCE"] + 2.50,
            state["MAX_CHANCE_CAP"]
        )

    # Modulo-3: geometric stake increase every 3 consecutive losses
    if streak % 3 == 0:
        state["CURRENT_BET"] *= 1.35

    return state


# ─────────────────────────────────────────────
# GUARDRAILS (evaluated BEFORE each new bet)
# ─────────────────────────────────────────────
def apply_guardrails(state: dict) -> dict:
    # 1. Circuit breaker — 15 consecutive losses
    if state["STREAK_LOSS"] >= 15:
        state["CURRENT_BET"]    = state["BASE_BET"]
        state["CURRENT_CHANCE"] = state["BASE_CHANCE"]
        state["STREAK_LOSS"]    = 0
        print("  ⚠ Circuit Breaker Tripped at 15 Losses. Deficit absorbed. Baseline restored.")

    # 2. Anti-bust: bet > 10 % of remaining balance → halve it
    #    FIX: guard against zero/negative balance to avoid dividing by 0 or
    #         the guardrail never triggering on a near-busted account
    if state["CURRENT_BALANCE"] > 0 and \
            state["CURRENT_BET"] > state["CURRENT_BALANCE"] * 0.10:
        state["CURRENT_BET"] *= 0.50
        print("  ⚠ Risk mitigation triggered. Compounding IDR bet scaled down by 50%.")

    # 3. Take-profit / stop-loss
    net           = state["CURRENT_BALANCE"] - state["INITIAL_BALANCE"]
    tp_threshold  =  state["INITIAL_BALANCE"] * (state["TARGET_PROFIT_PCT"] / 100)
    sl_threshold  = -state["INITIAL_BALANCE"] * (state["STOP_LOSS_PCT"]     / 100)

    if net >= tp_threshold:
        print(f"\n✅ Target Profit Reached Successfully in IDR. Net: {net:+.2f} IDR")
        _terminate(state, "Target Profit Reached Successfully in IDR.")

    if net <= sl_threshold:
        print(f"\n🛑 Hard Stop-Loss Triggered. Protecting remaining IDR assets. Net: {net:+.2f} IDR")
        _terminate(state, "Hard Stop-Loss Triggered. Protecting remaining IDR assets.")

    return state


def _terminate(state: dict, reason: str):
    """Print session summary and exit cleanly."""
    print(f"\n{'='*60}")
    print(f"  SESSION TERMINATED: {reason}")
    print(f"  Total Rolls  : {state['ROLL_COUNT']}")
    print(f"  Final Balance: {state['CURRENT_BALANCE']:.2f} IDR")
    net = state["CURRENT_BALANCE"] - state["INITIAL_BALANCE"]
    print(f"  Net P&L      : {net:+.2f} IDR")
    print(f"{'='*60}\n")
    sys.exit(0)


# ─────────────────────────────────────────────
# TELEMETRY
# ─────────────────────────────────────────────
def log_roll(state: dict, result: str):
    net    = state["CURRENT_BALANCE"] - state["INITIAL_BALANCE"]
    payout = round(99 / state["CURRENT_CHANCE"], 4)
    print(
        f"[Roll #{state['ROLL_COUNT']:>5}] | "
        f"Result: {result} | "
        f"Chance: {state['CURRENT_CHANCE']:.2f}% | "
        f"Payout: {payout:.4f}x | "
        f"Bet: {state['CURRENT_BET']:.2f} IDR | "
        f"Streak: {state['STREAK_LOSS']} | "
        f"Net: {net:+.2f} IDR"
    )


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  Stake Dice Bot — IDR Mode")
    print("=" * 60)

    # ── Initialisation ──────────────────────────────────────────
    print("\n[INIT] Fetching IDR balance...")
    initial_balance = fetch_idr_balance()
    print(f"[INIT] Initial IDR balance: {initial_balance:.2f} IDR")

    state = {
        # Config (immutable references)
        "BASE_BET":          BASE_BET,
        "BASE_CHANCE":       BASE_CHANCE,
        "MAX_CHANCE_CAP":    MAX_CHANCE_CAP,
        "TARGET_PROFIT_PCT": TARGET_PROFIT_PCT,
        "STOP_LOSS_PCT":     STOP_LOSS_PCT,
        # Dynamic state
        "INITIAL_BALANCE":   initial_balance,
        "CURRENT_BALANCE":   initial_balance,
        "CURRENT_BET":       BASE_BET,
        "CURRENT_CHANCE":    BASE_CHANCE,
        "STREAK_LOSS":       0,
        "ROLL_COUNT":        0,
    }

    print(f"\n[CONFIG] Base bet    : {BASE_BET:.2f} IDR")
    print(f"[CONFIG] Base chance : {BASE_CHANCE:.2f}%")
    print(f"[CONFIG] Take-profit : +{TARGET_PROFIT_PCT:.1f}%  ({initial_balance * TARGET_PROFIT_PCT / 100:.2f} IDR)")
    print(f"[CONFIG] Stop-loss   : -{STOP_LOSS_PCT:.1f}%  ({initial_balance * STOP_LOSS_PCT / 100:.2f} IDR)")
    print("\n[RUNNING] Starting roll loop. Press Ctrl+C to stop.\n")

    # ── Roll Loop ────────────────────────────────────────────────
    consecutive_errors = 0

    try:
        while True:
            # Guardrails before each bet
            state = apply_guardrails(state)

            state["ROLL_COUNT"] += 1
            try:
                roll_data = place_dice_bet(state["CURRENT_BET"], state["CURRENT_CHANCE"])
                consecutive_errors = 0  # reset on success
            except RuntimeError as exc:
                consecutive_errors += 1
                # FIX: cap retries — abort after MAX_API_RETRIES consecutive failures
                print(f"  [API ERROR #{consecutive_errors}/{MAX_API_RETRIES}] {exc} — retrying in 3 s…")
                if consecutive_errors >= MAX_API_RETRIES:
                    print(f"\n[FATAL] {MAX_API_RETRIES} consecutive API failures. Aborting to protect balance.")
                    _terminate(state, f"Aborted after {MAX_API_RETRIES} consecutive API errors.")
                time.sleep(3)
                state["ROLL_COUNT"] -= 1  # don't count failed requests
                continue

            dice = roll_data.get("diceRoll")

            # FIX: if diceRoll key is missing entirely, treat as an API error
            # (don't silently count it as a loss and compound the bet)
            if not dice:
                consecutive_errors += 1
                print(f"  [API ERROR #{consecutive_errors}/{MAX_API_RETRIES}] Empty diceRoll in response — retrying in 3 s…")
                if consecutive_errors >= MAX_API_RETRIES:
                    print(f"\n[FATAL] {MAX_API_RETRIES} consecutive empty responses. Aborting.")
                    _terminate(state, f"Aborted after {MAX_API_RETRIES} consecutive empty API responses.")
                time.sleep(3)
                state["ROLL_COUNT"] -= 1
                continue

            result_val = dice.get("result")
            target_val = dice.get("target")

            # FIX: determine win/loss only from result vs target (Roll Over = above)
            # Removed the payout fallback — if result is absent, treat as API error
            if result_val is None or target_val is None:
                consecutive_errors += 1
                print(f"  [API ERROR #{consecutive_errors}/{MAX_API_RETRIES}] Missing result/target in response — retrying in 3 s…")
                if consecutive_errors >= MAX_API_RETRIES:
                    _terminate(state, f"Aborted after {MAX_API_RETRIES} consecutive malformed responses.")
                time.sleep(3)
                state["ROLL_COUNT"] -= 1
                continue

            won = float(result_val) > float(target_val)

            # Update balance from embedded response (avoids an extra API call)
            new_bal = extract_balance_from_roll(roll_data)
            if new_bal is not None:
                state["CURRENT_BALANCE"] = new_bal
            else:
                # FIX: if balance isn't in the roll response, fetch it explicitly
                # so anti-bust guardrail always works with current data
                try:
                    state["CURRENT_BALANCE"] = fetch_current_balance()
                except RuntimeError:
                    pass  # keep last known balance; guardrail will still apply

            if won:
                log_roll(state, " W ")
                state = on_win(state)
            else:
                log_roll(state, " L ")
                state = on_loss(state)

            if ROLL_DELAY > 0:
                time.sleep(ROLL_DELAY)

    except KeyboardInterrupt:
        print("\n\n[STOPPED] Manual interrupt.")
        _terminate(state, "Manually stopped by user.")


if __name__ == "__main__":
    main()
