# Packaged executable verification — FINAL dist/PredictionCryptoBot.exe (Windows, under Wine 9.0)

- Executable: `dist/PredictionCryptoBot.exe` (launcher: `wine`)
- Date (UTC): 2026-10-15 00:31:46
- Environment: **mock** exchange simulator on localhost (real Kalshi hosts are not reachable from the build machine)
- Result: **28/28 checks passed**

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | production V2 order request constructed (no order sent) | PASS | `{"ticker": "KXBTC15M-SELFTEST-00", "client_order_id": "pcb-l-selftest1", "side": "bid", "count": "3.00", "price": "0.5600", "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross", "cancel_order_on_pause": true}` |
| 2 | funding/deposit/withdraw/transfer/API-key blocked before any HTTP request | PASS | `{"blocked": {"POST /portfolio/withdrawals": "FUNDING_WITHDRAWAL", "POST /portfolio/deposits": "FUNDING_DEPOSIT", "POST /portfolio/subaccounts/transfer": "FUNDS_TRANSFER", "POST /portfolio/intra_exchange_instance_transfer": "FUNDS_TRANSFER", "POST /portfolio/subaccounts": "SUBACCOUNT_MANAGEMENT", "POST /api_keys": "API_KEY_MANAGEMENT", "POST /api_keys/generate": "API_KEY_MANAGEMENT", "DELETE /api_k…` |
| 3 | executable launches and API responds | PASS | `{"app_version": "1.0.0", "model_version": "settle-gauss-avg-1.0", "strategy_version": "ev-taker-ioc-1.0", "schema_version": 1, "git_commit": "2fb4b08ee8790f3fc90cabeabc41895b7cd77d7a", "git_dirty": false, "build_timestamp": "2026-10-15T00:24:38+00:00", "source": "packaged"}` |
| 4 | build matches source (git commit) | PASS | `{"exe": "2fb4b08ee8790f3fc90cabeabc41895b7cd77d7a", "head": "2fb4b08ee8790f3fc90cabeabc41895b7cd77d7a", "dirty": false}` |
| 5 | starts STOPPED | PASS | `"STOPPED"` |
| 6 | CREDENTIALS healthy | PASS | `{"status": "OK", "detail": "API key loaded", "age_s": null}` |
| 7 | AUTHENTICATION healthy | PASS | `{"status": "OK", "detail": "authenticated (GET /portfolio/balance)", "age_s": 0.1}` |
| 8 | RECONCILIATION healthy | PASS | `{"status": "OK", "detail": "ok", "age_s": 0.1}` |
| 9 | REST healthy | PASS | `{"status": "OK", "detail": "recent error (1s ago): 404 on GET /series/KXDOGE15M", "age_s": 0.1}` |
| 10 | WEBSOCKET healthy | PASS | `{"status": "OK", "detail": "WS_CONNECTED; reconnects=0", "age_s": 0.5}` |
| 11 | MARKET_DISCOVERY healthy | PASS | `{"status": "OK", "detail": "3 verified open market(s)", "age_s": 1.1}` |
| 12 | ORDER_BOOKS healthy | PASS | `{"status": "OK", "detail": "3/3 valid & fresh ", "age_s": null}` |
| 13 | CF_BENCHMARKS healthy | PASS | `{"status": "OK", "detail": "BRTI=0.5s, ETHUSD_RTI=0.5s, SOLUSD_RTI=0.5s", "age_s": null}` |
| 14 | open 15-minute crypto markets discovered | PASS | `["KXBTC15M-26OCT150030-00", "KXETH15M-26OCT150030-00", "KXSOL15M-26OCT150030-00"]` |
| 15 | books + CF remain fresh | PASS | `{"portfolio_reconciliation_s": 5.133310079574585, "websocket_s": 0.4797823429107666, "orderbook_max_s": 0.47969889640808105, "cf_max_s": 0.4803280830383301, "order_fill_update_s": null}` |
| 16 | WebSocket reconnect + resubscribe | PASS | `["WS_SUBSCRIBED", "WS_SUBSCRIBED", "WS_SUBSCRIBED", "WS_SUBSCRIBED", "WS_SUBSCRIBED", "WS_SUBSCRIBED"]` |
| 17 | CF outage → no new risk → recovery | PASS | `["CF_STALE:BRTI", "CF_STALE:ETHUSD_RTI", "CF_STALE:SOLUSD_RTI"]` |
| 18 | model warms up; probability available | PASS | `[["KXBTC15M-26OCT150045-00", "READY", 0.7214600532354457], ["KXETH15M-26OCT150045-00", "READY", 0.676563263194357], ["KXSOL15M-26OCT150045-00", "READY", 0.36963607096114615]]` |
| 19 | strategy computes adjusted EV | PASS | `[["KXBTC15M-26OCT150045-00", "NO_POSITIVE_ADJUSTED_EV:-0.047580"], ["KXETH15M-26OCT150045-00", "NO_POSITIVE_ADJUSTED_EV:-0.066901"], ["KXSOL15M-26OCT150045-00", "NO_POSITIVE_ADJUSTED_EV:-0.057540"]]` |
| 20 | PAPER starts | PASS | `{"ok": true, "session_id": 1}` |
| 21 | PAPER loop keeps running | PASS | `"READY"` |
| 22 | deterministic qualifying opportunity reaches PAPER fill | PASS | `{"orders": [{"client_order_id": "pcb-p-7675a72f51ab4bd19964fd042c133d9f", "exchange_order_id": null, "mode": "PAPER", "ticker": "KXBTC15M-26OCT150045-00", "outcome": "yes", "book_side": "bid", "limit_price": "0.47", "count": "109", "filled_count": "109.00", "avg_fill_price": "0.468578", "fees": "1.91", "status": "FILLED", "created_ts": 1792024294.0277243, "error": null}], "session": {"session_id":…` |
| 23 | fills + positions persisted | PASS | `{"observations": 198, "signals": 1, "orders": 1, "shadow_orders": 0, "fills": 2, "open_positions": 1, "settled_trades": 0, "unique_markets": 6, "evaluated_markets": 3, "oos_markets": 0, "live_forward_markets": 0, "settled_markets": 3}` |
| 24 | STOP works (no new orders, positions kept) | PASS | `{"stop": {"ok": true, "cancelled_resting": 0}, "bot": "STOPPED"}` |
| 25 | control API refuses requests without dashboard header | PASS | `{"http_status": 403, "detail": "missing X-PCB-Client header"}` |
| 26 | LIVE blocked in mock env / without validation | PASS | `"LIVE blocked: STRATEGY_VALIDATION: INSUFFICIENT_DATA (LIVE requires statistical evidence; see Strategy Validation); ENVIRONMENT: mock simulator \u2014 LIVE is disabled"` |
| 27 | restart returns STOPPED | PASS | `{"bot": "STOPPED", "mode": "PAPER"}` |
| 28 | history survives restart | PASS | `{"observations": 199, "signals": 1, "orders": 1, "shadow_orders": 0, "fills": 2, "open_positions": 1, "settled_trades": 0, "unique_markets": 6, "evaluated_markets": 3, "oos_markets": 0, "live_forward_markets": 0, "settled_markets": 3}` |
