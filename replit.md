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

## Configuration (`config.json`)

| Key | Value | Notes |
|---|---|---|
| `base_bet` | 500 IDR | Bet awal tiap siklus |
| `base_chance` | 40 % | Win chance, fixed (tidak naik saat streak) |
| `bet_multiplier` | 1.68 | ×1.68 tiap loss (Martingale) |
| `circuit_breaker_at` | 5 | Loss ke-5 → cut loss, reset ke base_bet |
| `max_bet_multiplier` | 100 | Hard cap = base_bet × 100 = Rp 50.000 |
| `target_profit_pct` | 3 % | Auto-stop saat profit ≥ 3 % |
| `stop_loss_pct` | 5 % | Hard stop saat rugi ≥ 5 % |

## Strategy summary

- **Strategi:** Martingale ×1.68 — setiap loss, bet dikali 1.68.
- **Progression:** 500 → 840 → 1.411 → 2.371 → 3.983 IDR (5 tahap, total CB cost: Rp 7.105).
- **On win:** reset bet dan streak ke baseline.
- **Circuit breaker:** loss ke-5 → cut loss, reset semua ke base_bet.
- **Anti-bust:** bet > 10 % saldo → dipotong 50 % otomatis.
- **Session ends** saat take-profit atau stop-loss tercapai.

## Smart Random Delay

Tiga tier probabilistik meniru pola klik manusia (bukan fixed delay):

| Tier | Probabilitas | Durasi | Tujuan |
|---|---|---|---|
| Reguler | 75 % | 0.8 – 1.5 detik | Ritme santai normal |
| Agresif | 20 % | 0.4 – 0.7 detik | Klik cepat kejar momen |
| Distraksi | 5 % | 3.0 – 6.5 detik | Cek saldo / terdistraksi |

## User preferences

- Stack: Python / requests
- Currency: IDR (Indonesian Rupiah)
