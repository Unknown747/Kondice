# Stake Dice Bot (IDR)

Automated dice-betting script targeting the Stake platform GraphQL API, using Indonesian Rupiah (IDR) as the active currency wallet.

## How to run

```
python dice_bot.py
```

## Required secret

| Secret | Description |
|--------|-------------|
| `STAKE_API_KEY` | Your Stake session/access token (`x-access-token` header) |

Set it via Replit Secrets before running.

## Configuration (top of `dice_bot.py`)

| Variable | Default | Notes |
|---|---|---|
| `BASE_BET` | 100 IDR | Minimum bet per roll |
| `BASE_CHANCE` | 5 % | Starting win probability |
| `MAX_CHANCE_CAP` | 45 % | Win-chance ceiling on losing streaks |
| `TARGET_PROFIT_PCT` | 15 % | Auto-stop on net +15 % gain |
| `STOP_LOSS_PCT` | 25 % | Hard stop on net −25 % loss |
| `ROLL_DELAY` | 0.5 s | Pause between rolls (set 0 for max speed) |

## Strategy summary

- **On win:** reset bet, chance, and streak to baseline.
- **On loss:** every 2 consecutive losses → expand win chance +2.5 %; every 3 consecutive losses → multiply bet ×1.35.
- **Circuit breaker:** 15 straight losses resets all state.
- **Anti-bust:** if a single bet exceeds 10 % of remaining balance, it's halved automatically.
- **Session ends** when take-profit or stop-loss threshold is hit.

## Telemetry format

```
[Roll #    1] | Result:  W  | Chance: 5.00% | Payout: 19.8000x | Bet: 100.00 IDR | Streak: 0 | Net: +0.00 IDR
```

## User preferences

- Stack: Python / requests
- Currency: IDR (Indonesian Rupiah)
