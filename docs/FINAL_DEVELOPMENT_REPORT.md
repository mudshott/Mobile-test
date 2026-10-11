# PredictionCryptoBot — Final Development Report

_Date: 2026-10-11 · Branch: `claude/kalshi-crypto-trading-bot-owsbov` · App 1.0.0 · Model `settle-gauss-avg-1.0` · Strategy `ev-taker-ioc-1.0` · Schema v1_

## 1. Executive summary

- I built a complete, coherent application: Kalshi REST and WebSocket connectivity, order books, CF
  Benchmarks settlement reference, a settlement-aware probability model, an EV strategy, a risk governor,
  PAPER/SHADOW/LIVE execution, reconciliation, settlement and P&L, persistence, validation and research
  tooling, a dashboard, and a Windows EXE.
- **Verified here:** 153 automated tests pass. ruff, mypy (strict-ish) and the secret scan are clean.
  `dist\PredictionCryptoBot.exe` was built (PE32+ x86-64), launched under Wine, and driven through the
  operational checklist end to end against a protocol-faithful **mock** exchange. That includes a real
  quarter-hour rollover, PAPER fills and settlement (see §15).
- **Not verified here:** connectivity to the *real* Kalshi production hosts. This build container's
  network policy blocks `*.kalshi.com` (HTTP 403 from the egress proxy). The API contract was verified
  against Kalshi's official SDK and spec instead (§4). The first launch on your PC with your key is the
  first real-exchange run.
- **Strategy validation: INSUFFICIENT_DATA.** No profitability is claimed. LIVE is blocked by default
  until the pre-registered validation policy (§11) passes.

## 2. Starting repository state (audit)

| Item | Finding |
|---|---|
| Branch / HEAD | `claude/kalshi-crypto-trading-bot-owsbov`, **no commits**. Remote `mudshott/Mobile-test` had **no branches** (verified via GitHub API). |
| Files | None: an empty working tree, no untracked files. |
| Existing code, duplicates, stubs, fake health, stale API code, EXE drift, DB, research data, tests, running processes | None existed. Nothing needed preserving, migrating or backing up. |

This was therefore a greenfield build. The "preserve useful components" instruction had nothing to
apply to. The migration and backup machinery (§10) is in place for the future.

## 3. Architecture

```
src/predictionbot/
  config.py            settings (data dir outside dist/, env/config.toml, endpoint pinning per environment)
  buildinfo.py         provenance: app/model/strategy/schema versions, git commit, build time
  fixedpoint.py        exact Decimal handling of *_dollars / *_fp strings
  kalshi/auth.py       RSA-PSS / Ed25519 request signing (matches official SDK)
  kalshi/firewall.py   deny-by-default endpoint + WS command firewall
  kalshi/rest.py       async REST client: firewall → sign → rate-limit → classify outcomes
  kalshi/ws.py         WebSocket: reader task → queue → dispatcher; reconnect/backoff; resubscribe; seq tracking
  market/models.py     MarketInfo: timing, tick grid from price_ranges, strike semantics
  market/orderbook.py  YES/NO bid book, complement pricing, integrity state machine, depth walk
  market/books.py      per-market subscriptions, gap → get_snapshot → resubscribe ladder, REST display backstop
  market/discovery.py  series allowlist → verified current markets → ACTIVE/CLOSED/SETTLED rotation
  reference/cf.py      cfbenchmarks_value ingest, freshness, final-minute window averages
  model/               features (removable; trading vs research) + settlement probability model
  strategy/            fees, evaluator (executable EV), machine-readable reasons
  risk/                profiles, capped fractional Kelly sizing, independent governor
  execution/           PAPER (latency + haircut IOC), SHADOW, LIVE (V2, ambiguity reconciliation)
  portfolio/ledger.py  cash/positions/exposure/drawdown; settlement P&L
  persistence/         SQLite WAL + versioned migrations + pre-migration backups
  research/            partitions, validation policy & studies, statistics, knowledge base
  app/engine.py        single authoritative runtime owner: loops, health, readiness, controls
  api/server.py        localhost FastAPI + dashboard (backend authoritative)
  ui/static/           dashboard (vanilla JS, 1 s polling of /api/state)
  selftest.py          offline proof of order path + firewall inside the shipped binary
  sim/mock_exchange.py TEST HARNESS ONLY: protocol-faithful local exchange simulator
```

The engine runs independent supervised loops: discovery/rotation (every 20 s and right after each
quarter-hour boundary), evaluation (1 s), book maintenance (1 s), portfolio reconciliation (25 s, or 3 s
while an order is unresolved), settlement polling (10 s), persistence and health transitions (2 s), and
validation (every 30 min). A crashed loop is logged and restarted.

## 4. Kalshi API: what was verified and how

`docs.kalshi.com` and all `*.kalshi.com` hosts are unreachable from this container: WebFetch fails DNS
and curl gets proxy 403. I therefore verified against first-party artifacts that *were* reachable:

1. **`kalshi_python_sync` 3.33.0** (Kalshi's official generated SDK, published to PyPI 2026-10-07).
   I read it for endpoints, models, hosts and signing. It was not imported into the app.
2. **`asyncapi.yaml` and `openapi.yaml` (v3.34.0)** vendored in the TexasCoding Kalshi SDK repository,
   synced 2026-10-10. These are Kalshi's published specs. I read them for WS channels, payloads, error
   codes and API-key scopes.

| Topic | Implemented per spec |
|---|---|
| REST base | `https://external-api.kalshi.com/trade-api/v2` (demo `https://external-api.demo.kalshi.co/trade-api/v2`) |
| WS | `wss://external-api-ws.kalshi.com/trade-api/ws/v2`; the handshake is a signed `GET /trade-api/ws/v2` |
| Signing | `base64(sign(ts_ms + METHOD + path_without_query))`; RSA-PSS/SHA-256 (salt = digest length) or Ed25519; headers `KALSHI-ACCESS-KEY/-TIMESTAMP/-SIGNATURE` |
| Create order | `POST /portfolio/events/orders` (V2): `ticker, client_order_id, side (bid|ask on the YES book), count "N.00", price "0.XXXX", time_in_force, self_trade_prevention_type, cancel_order_on_pause, subaccount` |
| Cancel / decrease / amend | `DELETE /portfolio/events/orders/{id}`, `POST …/{id}/decrease`, `POST …/{id}/amend`. Cancel is used by the STOP policy; decrease/amend are allowlisted but the IOC strategy does not need them. |
| Reads | `/portfolio/balance, /positions, /orders, /fills, /settlements`, `/markets`, `/markets/{t}`, `/markets/{t}/orderbook`, `/series/{t}`, `/exchange/status`, `/historical/*` |
| Fixed point | prices `*_dollars` strings (2–4 dp in requests), counts `*_fp` (0.01 granularity); parsed with Decimal only |
| Price grid | `price_ranges[{start,end,step}]` per market (supports sub-cent structures) |
| Book semantics | bids only per outcome: YES ask = 1 − best NO bid, NO ask = 1 − best YES bid |
| WS channels | `orderbook_delta` (snapshot then deltas, `get_snapshot` action), `trade`, `ticker`, `fill`, `user_orders`, `market_positions`, `market_lifecycle_v2`, `cfbenchmarks_value` (`index_ids`) |
| Keep-alive | Kalshi pings every 10 s and the client auto-pongs; the client also pings every 10 s (20 s timeout) |
| Scopes | `read`, `write`, child scopes `write::trade`, `write::transfer`, `read::portfolio_balance` (confirmed in the OpenAPI `ApiKeyScope` enum) |
| Fees | series `fee_type` quadratic: `0.07·C·P·(1−P)·fee_multiplier`. We round **up to the cent per order/level**, which is never cheaper than Kalshi's finer rounding, so EV is conservative. |
| Rate limits | token buckets (Basic tier: order create = 10 tokens, cancel = 2). The client uses conservative 10 read/s and 5 write/s buckets and never retries writes. |
| Historical | `/historical/cutoff`, `/historical/markets` (settled markets with `expiration_value`, strike, result) |

Legacy fields (`side`/`action`, integer cents, `POST /portfolio/orders`) are not used. The legacy order
endpoint is blocked by the firewall.

## 5. WebSocket behaviour

- The reader task only parses and enqueues (bounded 20k queue). A dispatcher runs handlers, so a slow
  handler can never block the socket (tested: 50 frames consumed while the handler was blocked).
- Queue overflow forces a reconnect and full resync rather than dropping data.
- Reconnect uses exponential backoff with jitter (0.5 s to 30 s). Subscriptions are **desired state**,
  and every one is re-sent after reconnect (`WS_RESUBSCRIBED`).
- Per-sid sequence tracking: duplicates are dropped and counted; a gap emits `WS_SEQUENCE_GAP` and marks
  the frame for the book manager. A fresh connection restarts sequences without false gaps.
- Terminal channel errors (codes 10 and 25) re-create the subscription.
- Diagnostics: `WS_CONNECTED/DISCONNECTED/RECONNECTING/RECONNECTED/SUBSCRIBED/RESUBSCRIBED/ERROR/SEQUENCE_GAP`
  are shown in the UI and persisted to `health_events`.

## 6. Order-book behaviour

- One `orderbook_delta` subscription **per market**, so each market has its own sequence and a gap in
  one never invalidates the others.
- State machine: `AWAITING_SNAPSHOT → VALID → INVALID(reason)`. Reasons include SEQUENCE_GAP,
  NEGATIVE_LEVEL, MALFORMED_*, CROSSED_BOOK and WS_DISCONNECTED.
- Recovery ladder: `get_snapshot` on the same sid → after 6 s, unsubscribe and resubscribe (fresh
  snapshot, fresh sequence) → retried every maintenance pass. A market is never permanently disabled.
- A quiet book is re-confirmed with `get_snapshot` every 20 s. Over 45 s without confirmation it is STALE.
- REST snapshots are taken only while the WS is down (every 10 s per market), and **only for display**.
  REST-sourced books are never tradeable.
- Executable prices come from walking the opposite side's bids, with depth at or better than the limit.

## 7. CF Benchmarks behaviour

- Series → asset → index mapping: `KXBTC15M→BRTI`, `KXETH15M→ETHUSD_RTI`, `KXSOL15M→SOLUSD_RTI`,
  `KXXRP15M→XRPUSD_RTI`, `KXDOGE15M→DOGEUSD_RTI`. Each market must reference CF Benchmarks in its rules
  or its series' `settlement_sources`, or it stays `MARKET_NOT_VERIFIED`.
- Ingests `data` (the raw CF frame: value + source time), `received_at`, `avg_60s_data` and
  `last_60s_windowed_average_15min` (official running final-minute average with `window_size`).
- Tracked per index: value, source timestamp, Kalshi receipt, local receipt, age, observation count,
  errors and out-of-order count. Stale after 5 s means **no new risk**. If an index stays stale more
  than 10 s after subscription, the CF subscription is re-created (rate-limited to once per 30 s).
- Values are never fabricated. Malformed or non-positive frames are rejected. After a restart the
  volatility buffer is re-seeded from **persisted real ticks** (≤ 30 min old), but freshness only
  returns when a new live tick arrives.
- Pyth and exchange prices are not used, and could never replace the settlement reference.

## 8. Model architecture

- Separate concepts: forecast/fair probability (model) → mispricing (raw edge) → adjusted EV → decision
  (thresholds + sizing + governor).
- **Settlement geometry.** YES iff A = mean of the 60 one-second RTI ticks in (close−60 s, close]
  satisfies the strike rule (greater, greater_or_equal, less, less_or_equal or between).
- **Distribution of A.** Zero-drift arithmetic random walk with per-second volatility σ·S:
  - E[A] = (k·m_obs + (60−k)·S)/60
  - Var[A] = σ²S²·ΣΣmin(uᵢ,uⱼ)/3600, using the closed forms 3600·(τ−60) + 73810 before the final
    minute, and n(n+1)(2n+1)/6 inside it.
  - Inside the final minute it uses Kalshi's official running average (k ticks, mean m_obs).
- **σ** comes from realized 1 s CF log returns: EWMA (half-life 5 min) and a flat 30-min window.
  It needs at least 300 returns and at least 80% coverage.
- **Ensemble.** {Gaussian, unit-variance Student-t(ν=4)} × {σ_short, σ_long}.
  - Raw = mean of the four. Disagreement = their range.
  - Uncertainty adds a σ-estimation band of ±2/√(2N).
  - Conservative P(side) = the least favourable bound − 0.005, capped at 0.97 until high-confidence
    calibration exists.
- **Outputs:** raw, lower/upper, conservative YES/NO, uncertainty, disagreement, model version, data
  quality (n_returns, coverage, spot age, window ticks), and state WARMING_UP/READY/DEGRADED/FAILED with
  exact missing items (e.g. `INSUFFICIENT_HISTORY:49/300 one-second returns`).
- **No indicator soup.** Trading features are only `settlement_geometry` and `realized_vol_1s`.
  Momentum and vol-ratio are computed and persisted as **research** features (hypotheses H-MOM-001 and
  H-MICRO-002) and never affect trades until they pass held-out tests.
- The model **never** takes Kalshi prices as input and never substitutes zero.

## 9. Strategy and risk

- **Per side** (YES = UP, NO = DOWN):
  - executable ask; the limit is the ask + 1 tick (latency allowance);
  - depth at the limit × 50% haircut;
  - fee per contract at the limit price;
  - adjusted EV = p_cons − limit − fee/contract;
  - return on cost = EV / all-in cost.
- **Gates,** each with a machine-readable reason: market state and verification, CF mapping and
  freshness, time window, book missing/gap/stale/REST-sourced, model state, disagreement, uncertainty,
  spread, liquidity, price bounds, `EDGE_TOO_LOW`, `NO_POSITIVE_ADJUSTED_EV`.
- **Sizing:** f* = (p−c)/(1−c); stake = kelly_fraction·f*·bankroll, capped by the profile's single-order
  ceiling (≤ 30% hard), then by depth, in whole contracts. The bankroll is the **current reconciled
  equity**: exchange balance for LIVE, the paper ledger for PAPER. No balance is hard-coded. The
  high-confidence override exists in the schema but is disabled; even when calibration allows it,
  sizing cannot exceed 30%.
- **Governor** (runs immediately before submission, so limit changes between signal and submission are
  honoured).
  - Rejects on: kill switch, DB/API/WS unhealthy, LIVE reconciliation stale, any unresolved order,
    per-market order count, session/daily/drawdown limits.
  - Reduces to fit: single-order, single-market, asset, **correlated same-direction** exposure across
    all crypto assets, total exposure, and cash reserve.
  - Downgrades the effective profile on drawdown ≥ ½ of the limit, consecutive losses, or a poor LIVE
    reject ratio. It never upgrades, and nothing ever sizes up after losses.

## 10. Execution, reconciliation, settlement, persistence

- **PAPER.** The order is persisted first (`PENDING_SUBMIT`). After 250 ms it is matched against the
  *current* book: IOC at the limit, 50% depth haircut, partial fills, taker fee per level. If the book
  is invalid or stale, or the market closed on arrival, there is no fill. Orders, fills and positions
  are persisted.
- **SHADOW.** The intended order and a hypothetical fill estimate are persisted. No capital moves.
- **LIVE.**
  - Requires arming: START in LIVE after the typed confirmation.
  - Every order gets a unique `client_order_id` (`pcb-l-<uuid>`) and is persisted before sending.
  - Outcomes:
    - **201:** status from the response, then fills fetched by `order_id`.
    - **Timeout, connection drop, 5xx or malformed response after send:** `AMBIGUOUS`. **Never
      resubmitted.** Capital stays reserved and the governor blocks all new risk. Reconciliation lists
      exchange orders by ticker and time and matches `client_order_id`. If the order isn't found within
      120 s it becomes `NOT_FOUND`.
    - **409 duplicate client_order_id:** reconciled the same way as ambiguous.
    - **429 or other 4xx:** `REJECTED`, capital released.
  - Unresolved orders survive restarts and are recovered from the DB.
  - Fills from WS and REST are de-duplicated by fill id. Cash is debited provisionally and overwritten
    at each reconciliation.
- **Reconciliation** (exchange authoritative): balance, positions, resting orders and recent fills at
  startup and every 25 s. Realtime fills, user orders and positions arrive via WS. The UI shows separate
  ages for portfolio reconciliation, WS, books, CF and order/fill updates.
- **Settlement.** Closed markets are polled until the official `result` is final (`finalized`/`settled`).
  Then paper and live positions are settled, realized P&L is recorded, and the market row (result,
  `expiration_value`) is updated for validation.
- **Persistence:** SQLite (WAL) in `%LOCALAPPDATA%\PredictionCryptoBot`, holding:
  - markets, CF ticks, predictions (observations), evaluations (signals and NO-TRADE reasons), risk
    decisions, orders, fills, positions, settlements, balance snapshots, health events;
  - validation partitions and studies, research hypotheses, and sessions (with model/strategy versions).
  Migrations are versioned, a backup is written before migrating an existing DB, and a newer-schema DB
  is refused.
- **Counters** are derived from those records and keep observation, signal, order, fill, position,
  settled trade, unique market, evaluated market and OOS market distinct. Win rate is `N/A` with zero
  settled trades.

## 11. Validation methodology (system health ≠ strategy validation)

- **Partitions** are assigned once per market at first sight and never moved. The independence unit is
  the 15-minute **window**, because markets on different assets in the same window are correlated, so
  they share a partition.
  - First seen during a running PAPER/LIVE session → `LIVE_FORWARD`.
  - Otherwise `sha256(window)` → TRAIN 50% / VALIDATION 20% / OOS_TEST 30%.
  - Studies use OOS_TEST + LIVE_FORWARD only, one prediction per market (closest to 180 s before close),
    clustered by window.
- **Study artifacts** (`validation_studies`): id, type, time, model/strategy version, dataset hash,
  partition, unique markets, observations, fills, assumptions, fee/slippage/latency models, metrics,
  verdict and reason.
  - Types: CALIBRATION (Brier, log loss, ECE, Wilson-CI buckets, market-implied baseline, ≥0.90 region),
    PAPER_PNL (cluster-bootstrap CI), EXECUTION_SENSITIVITY (prices 0–3¢ worse, 25% missed fills), and
    SETTLEMENT_RULE_AUDIT (historical settled markets: does our strike logic reproduce Kalshi's results?).
- **ValidationPolicy:** pre-registered numbers, each derived from a stated formula rather than carried
  over as a magic number:

| Requirement | Value | Derivation |
|---|---|---|
| P&L: independent traded windows | **1,583** at the prior σ=0.48; recomputed with the observed σ (floor 0.30 → 619) | n = ((z₀.₉₅ + z₀.₈₀)·σ / MDE)², MDE = $0.03/contract, one-sided α=0.05, power 0.80 |
| P&L: significance | 95% cluster-bootstrap lower bound of mean P&L/contract > 0 | |
| Calibration | ≥ 400 independent windows, ECE ≤ 0.03, no bucket (n ≥ 30) outside its **Bonferroni-adjusted** Wilson interval ± 0.02 | Bonferroni keeps the family-wise false-fail rate ≤ 5% (0/40 seeds false-failed in testing) |
| ≥ 0.90 high-confidence region | n ≥ **385**, Wilson lower bound ≥ mean prediction − 0.03; until then the high-confidence size override stays disabled | n = 1.96²·0.9·0.1/0.03² |
| Paper execution | ≥ **97** fills | ±0.10 fill-rate precision at 95%: 1.96²·0.25/0.1² |
| Execution stress | mean P&L/contract > 0 with prices 1¢ worse | |

Historical magic numbers (500 markets, 100 trades, 200 OOS) were **not** used. Thresholds must not be
tuned on OOS_TEST or LIVE_FORWARD.

## 12. Current research evidence

- **No genuine settled-market evidence exists yet**, because nothing has run against the real exchange.
  The studies currently return `INSUFFICIENT_DATA`, and `live_capital_justified = false`.
- The knowledge base (`research/knowledge.json` → `research_hypotheses`) holds 9 hypotheses, all
  **UNTESTED**: settlement-window modelling, settlement-rule audit, EWMA vol, fat tails, momentum,
  favourite-longshot bias, book imbalance, depth reachability and fractional-Kelly drawdown. Each has a
  source, credibility and test methodology.
- **What historical data can support.** Kalshi `/historical/markets` and settled `/markets` give strike,
  `expiration_value` and result per market. That supports the settlement-rule audit and base rates.
  Candlesticks give coarse market prices.
- **What it cannot support.** It does **not** provide historical order-book depth or our CF 1 s paths.
  Kalshi documents a CF Benchmarks REST passthrough, but its paths were not in the OpenAPI I could
  access, so it is not implemented. Model calibration and execution evidence therefore come from
  **live-forward PAPER/SHADOW collection**. About 96 windows/day means roughly 4 days to the calibration
  minimum, and weeks to months for the P&L requirement, depending on trade frequency.

## 13. Account-permission security

- **No code path** exists for deposits, withdrawals, transfers, payment methods, subaccount creation or
  transfers, or API-key create/delete/rotate. The REST client has no such methods (tested).
- **Firewall** (`kalshi/firewall.py`): deny by default, keyword denylist first (deposit, withdraw,
  transfer, subaccount, api_key, bank, payment, card, paypal, ach, wire, funding, fcm, communications,
  order_groups, batched…), then an exact allowlist of read endpoints plus the 4 V2 single-order endpoints.
  - Cancel-ALL (`DELETE /portfolio/events/orders`) is blocked, so STOP cancels only bot-owned orders.
  - Path traversal, percent-encoding, `//` and inline query strings are rejected.
  - WS commands, channels and actions are allowlisted (no `communications`).
  - **Tests prove** 30 forbidden endpoints raise `FirewallViolation`, and an HTTP transport records
    **zero requests** for them. The shipped EXE re-proves this offline with `--self-test`.
- **Secrets:** only the key *id* and the *path* are stored. The PEM is read once and never logged
  (`repr` is redacted, and a log filter scrubs PEM blocks and signatures). `config.toml`, `*.pem`/`*.key`
  and the DB are git-ignored. `scripts/secret_scan.py` runs before every build. The UI binds to
  127.0.0.1 only, and mutating API calls need a custom header plus a localhost Origin, which blocks CSRF.
- **Your key:** create it with `read` + `write::trade` only, without `write::transfer` or broad `write`,
  restricted to a subaccount if you want. The app never creates or modifies keys.

## 14. Tests

| Suite | Count | Covers |
|---|---|---|
| test_security | 72 | firewall allow/deny, no-HTTP-before-block, WS firewall, RSA/Ed25519 signatures verified with public keys, secret redaction |
| test_strategy_risk | 22 | **TRADE UP / TRADE DOWN / NO TRADE fixtures**, every NO-TRADE gate, profiles, Kelly caps, governor fail-closed, correlated exposure, loss limits, no martingale, fees |
| test_persistence_validation | 18 | migrations + backup, schema guard, restart with open position, settlement math, partitions, policy derivations, calibration PASS/FAIL/INSUFFICIENT, P&L, sensitivity, knowledge base, REST retry/no-retry/429/clock skew, discovery rotation and exclusions |
| test_execution | 15 | paper partial fills/haircut/latency re-read/invalid book, shadow, V2 body construction, LIVE success, timeout → ambiguous → reconcile (exactly 1 POST), not-found give-up, 409 duplicate, 400 reject, restart recovery, fill de-dup, provisional cash |
| test_cf_model | 11 | official CF example payload, stale/recovery, malformed/out-of-order, warming reasons, probability geometry, strike types, variance closed form, final-window use, degraded state |
| test_orderbook | 8 | complement pricing, depth walk, deltas, negative level, gap → snapshot recovery, malformed, crossed |
| test_ws | 5 | subscribe/ack, duplicate, gap, malformed, reconnect + resubscribe, no false gap after reconnect, slow handler isolation, backoff, null-id frames |
| integration/test_end_to_end | 2 | real Engine + API vs the in-process mock: startup STOPPED → READY, PAPER fill, WS drop, seq gap, CF outage, STOP, restart STOPPED, API guards, LIVE gating |

Commands and results on the final tree: `pytest` → **153 passed**; `ruff check` and
`ruff format --check` → clean; `mypy` → **no issues in 50 files**; `scripts/secret_scan.py` → 0
problems; schema check: migration applied from empty, version guard and backup tested. No real
production order was placed at any point.

## 15. Packaging and packaged verification

- `python scripts/build.py --python "wine C:/…/python.exe"` produced **`dist\PredictionCryptoBot.exe`**:
  PE32+ x86-64 console, about 20 MB, one file. It bundles the static UI, SQL migrations, the knowledge
  base and the provenance JSON.
  - It was built with Windows CPython 3.12.15 and PyInstaller 6.22.3 under Wine 9.0, because this
    container is Linux.
  - Runtime data is created in `%LOCALAPPDATA%\PredictionCryptoBot`, not under `dist\`.
  - A Linux build of the same spec is at `dist/linux/PredictionCryptoBot` (used only for an extra
    native-binary check).
- Build provenance is exposed at `/api/build` and in the UI's *Build info* card (`source: packaged`).
- `scripts/verify_packaged.py` launched the EXE and drove it through the checklist. Full results are in
  **`docs/PACKAGED_VERIFICATION_WINDOWS.md`**: **29/29 checks passed** on the EXE built from
  commit `a41f18c` (clean tree). Checks included:
  - offline self-test of the V2 order body, with forbidden endpoints blocked and 0 HTTP requests;
  - launch; build matches source; starts STOPPED;
  - credentials, auth, reconciliation, REST, WS, discovery, books and CF all healthy, staying fresh;
  - WS drop → reconnect + resubscribe; CF outage → stale → recovery;
  - model READY; EV computed (healthy `NO_POSITIVE_ADJUSTED_EV` on fair quotes);
  - PAPER start and loop; a deterministic mispricing produced an IOC paper fill (120 NO @ 0.3352),
    which was persisted;
  - two quarter-hour rollovers, with the position settled on the official result;
  - STOP; header guard; LIVE blocked (mock + validation);
  - restart returns STOPPED with history intact.
- The final EXE was then rebuilt from the final source commit and re-verified (no settlement wait):
  see `docs/PACKAGED_VERIFICATION_FINAL_BUILD.md`. That binary is committed as
  `release/PredictionCryptoBot.exe` (SHA-256 in `release/SHA256SUMS.txt`), because `dist/` is a
  disposable build output.
- The dashboard was rendered from the packaged EXE in headless Chromium and checked visually.

## 16. Remaining genuine external limitations

**1 — Real-exchange connectivity**
- COMPONENT: REST/WS connection to Kalshi production (and demo)
- EXACT FAILURE: `curl: (56) CONNECT tunnel failed, response 403` for `external-api.kalshi.com`,
  `api.elections.kalshi.com` and `docs.kalshi.com`; WebFetch `getaddrinfo ENOTFOUND docs.kalshi.com`
- ROOT CAUSE: this build container's egress policy does not allow Kalshi hosts
- EVIDENCE: §4 probes; GitHub, PyPI and astral-sh downloads succeed from the same container
- CAN CODE FIX IT? NO
- USER ACTION REQUIRED: run the EXE on your PC with your API key. Production auth, discovery, books, CF
  and reconciliation are exercised on first launch, and the dashboard shows each component's exact
  status. Nothing trades until you press START.

**2 — API key**
- COMPONENT: authentication and portfolio reconciliation
- EXACT FAILURE: no credentials exist in this environment (correctly: secrets are never committed)
- ROOT CAUSE: the key must be created by you
- EVIDENCE: health shows `CREDENTIALS: API key id / private key path not configured` without one
- CAN CODE FIX IT? NO, by design. The bot must not create keys.
- USER ACTION REQUIRED: create a `read` + `write::trade` key (no `write::transfer`) and set it in
  `config.toml`

**3 — Series coverage**
- COMPONENT: market discovery
- EXACT FAILURE: only `KXBTC15M` was confirmed to exist (in Kalshi's spec examples). ETH/SOL/XRP/DOGE
  15-minute series tickers are assumed by analogy.
- ROOT CAUSE: no access to live `GET /series`
- EVIDENCE: §4
- CAN CODE FIX IT? Partly. Discovery checks each series via `GET /series/{ticker}` and reports missing
  ones (re-checked every 10 min) without failing.
- USER ACTION REQUIRED: none. If Kalshi uses different tickers, add them under `[[series]]` in
  `config.toml`.

**4 — CF historical passthrough**
- COMPONENT: historical backfill / backtesting
- EXACT FAILURE: the CF Benchmarks REST passthrough paths are not in the accessible OpenAPI
- ROOT CAUSE: documentation not reachable
- EVIDENCE: no `cfbenchmarks` path in `openapi.yaml` v3.34.0
- CAN CODE FIX IT? YES, once the path is known
- USER ACTION REQUIRED: none. Validation uses live-forward data, which is the more legitimate source
  for execution evidence anyway.

**5 — Strategy evidence**
- COMPONENT: strategy validation
- EXACT FAILURE: verdict `INSUFFICIENT_DATA`
- ROOT CAUSE: no settled real markets have been observed yet
- EVIDENCE: §11 and §12
- CAN CODE FIX IT? NO, it needs time and data
- USER ACTION REQUIRED: run PAPER (or SHADOW) for weeks, and watch *Strategy Validation*. Do not enable
  LIVE before it PASSES, unless you deliberately accept unvalidated risk.

## 17. Exact user instructions

1. Copy `dist\PredictionCryptoBot.exe` anywhere (e.g. `C:\Apps\PredictionCryptoBot\`).
2. In Kalshi → Account → API keys, create a key with scopes **`read` and `write::trade` only**
   (optionally restricted to a subaccount). Download the private key to e.g.
   `C:\Users\<you>\kalshi\trading-key.pem`.
3. Double-click the EXE once, then close it. Edit
   `%LOCALAPPDATA%\PredictionCryptoBot\config.toml`: set `api_key_id` and `private_key_path`, plus
   `subaccount` if applicable.
4. Start the EXE. The dashboard opens at http://127.0.0.1:8765/ and shows **STOPPED**.
5. Check the *System health* card: everything should turn green within about 30 s, and MODEL within
   about 5 minutes of the first launch. If any item is red, its detail line says exactly why.
6. Select **PAPER** (or SHADOW), select **BALANCED**, and press **START BOT**. Leave it running. NO TRADE
   with `NO_POSITIVE_ADJUSTED_EV` / `EDGE_TOO_LOW` is the normal, healthy outcome most of the time.
7. Periodically press *Run validation studies now* and read *Strategy Validation*.
8. Only after it PASSES: select **LIVE**, read the disclosure, type `LIVE`, and press **START BOT**.
   Use **STOP BOT** or **EMERGENCY STOP** at any time. Open positions are tracked to settlement, not
   liquidated.
9. Rebuild from source on Windows with `pip install -e ".[dev]"` and then `python scripts\build.py`.
   Verify with `dist\PredictionCryptoBot.exe --self-test`.
