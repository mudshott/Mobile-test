# PredictionCryptoBot

Autonomous research and trading application for **Kalshi 15-minute crypto UP/DOWN markets**
(BTC, ETH, SOL, XRP and DOGE when Kalshi lists them), running locally on Windows with a browser dashboard.

> **No profitability is claimed.** The software runs; whether the strategy deserves real money is a
> separate, statistical question answered (or not yet) by the **Strategy Validation** panel. Today it
> reports `INSUFFICIENT_DATA`, so LIVE trading is blocked by default.

## Quick start (Windows)

1. **Create a Kalshi API key** at kalshi.com → Account → API Keys, with the *narrowest* scopes:
   `read` + **`write::trade`** only. Do **not** grant `write` (broad) or `write::transfer`.
   If you use a subaccount, create a key restricted to that subaccount. Save the private key `.pem`
   somewhere outside this folder (e.g. `C:\Users\you\kalshi\trading-key.pem`).
2. Run `dist\PredictionCryptoBot.exe` once. It creates
   `%LOCALAPPDATA%\PredictionCryptoBot\config.toml` and opens the dashboard at http://127.0.0.1:8765/.
3. Edit `config.toml`:
   ```toml
   [kalshi]
   environment = "production"
   api_key_id = "your-key-id"
   private_key_path = "C:/Users/you/kalshi/trading-key.pem"
   # subaccount = 1          # only if the key is restricted to a subaccount
   ```
   (Alternatively set the environment variables `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`.)
4. Restart the EXE. Wait for the header to show **READY**. The model needs about 5 minutes of live
   CF Benchmarks ticks after the first start; later restarts reuse persisted ticks.
5. Choose **PAPER**, **SHADOW** or **LIVE**, choose a risk profile, and press **START BOT**.
   **STOP BOT** stops new orders immediately. It does *not* liquidate positions.

The application **always starts STOPPED**, on every launch.

### LIVE mode

LIVE requires all of the following:
- operational readiness: credentials, authenticated reconciliation, WebSocket, books, CF, clock,
  no unresolved orders;
- selecting LIVE, reading the risk disclosure and typing `LIVE`, then pressing START (the confirmation
  is valid for one START only);
- **Strategy Validation = PASS**. To override this deliberately, set
  `allow_live_without_validation = true` under `[app]` in `config.toml`. That is your decision; the
  software will not make it for you.

An **EMERGENCY STOP** button appears whenever the bot runs or LIVE is selected. It stops the bot and
engages a kill switch, which must be reset manually.

## Modes and risk profiles

| Mode | What happens |
|---|---|
| PAPER | Real market data. Simulated IOC orders are matched against the live book 250 ms later, assuming only 50% of displayed depth is reachable, with Kalshi taker fees. Simulated capital. |
| SHADOW | Records what it *would* submit, with a hypothetical fill estimate. No capital moves, simulated or real. |
| LIVE | Real Kalshi V2 orders (`POST /portfolio/events/orders`, immediate-or-cancel). |

| Profile | Min adjusted edge | Kelly fraction | Max single order | Cash reserve | Session DD stop |
|---|---|---|---|---|---|
| SAFE RETURNS | $0.05 | 0.10 | 5% | 60% | 5% |
| BALANCED (default) | $0.03 | 0.20 | 12% | 35% | 10% |
| AGGRESSIVE RESEARCHED | $0.02 | 0.33 | 30% (hard ceiling) | 20% | 20% |

All three profiles share the same data, model, execution and safety. They differ only in the risk
envelope. Poor conditions automatically **downgrade** the effective profile, and it never goes above
the one you selected.

## Account security

The program contains **no code** for deposits, withdrawals, transfers, payment methods, subaccount
management or API-key management. A local firewall (`kalshi/firewall.py`) also rejects those endpoints
before any HTTP request is made, as defense in depth. The dashboard's *Security* panel lists the
complete allowlist.

## Developer guide

```bash
pip install -e ".[dev]"
pytest                       # 150+ unit + integration tests (no network)
ruff check src tests scripts && ruff format --check src tests scripts
mypy
python scripts/secret_scan.py
python scripts/build.py                                  # → dist/PredictionCryptoBot(.exe)
python -m predictionbot.sim.mock_exchange --port 8799    # local protocol-faithful MOCK exchange
PCB_ENV=mock KALSHI_API_KEY_ID=x KALSHI_PRIVATE_KEY_PATH=test.pem python -m predictionbot.main
python scripts/verify_packaged.py --exe dist/PredictionCryptoBot.exe --key test.pem --wait-settlement
PredictionCryptoBot.exe --self-test    # offline proof of the order path and firewall inside the EXE
```

Runtime data (database, logs, backups, config) lives in `%LOCALAPPDATA%\PredictionCryptoBot`
(override with `PCB_DATA_DIR`). It is never inside `dist/`, so rebuilding cannot destroy history.

See `docs/FINAL_DEVELOPMENT_REPORT.md` for architecture, API verification, validation methodology,
test results and known limitations.
