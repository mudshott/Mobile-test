# Packaged executable verification — FINAL dist/PredictionCryptoBot.exe (Windows, under Wine 9.0)

- Executable: `dist/PredictionCryptoBot.exe` (launcher: `wine`)
- Date (UTC): 2026-10-11 15:52:38
- Environment: **mock** exchange simulator on localhost (real Kalshi hosts are not reachable from the build machine)
- Result: **28/28 checks passed**

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | production V2 order request constructed (no order sent) | PASS | `{"ticker": "KXBTC15M-SELFTEST-00", "client_order_id": "pcb-l-selftest1", "side": "bid", "count": "3.00", "price": "0.5600", "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross", "cancel_order_on_pause": true}` |
| 2 | funding/deposit/withdraw/transfer/API-key blocked before any HTTP request | PASS | `{"blocked": {"POST /portfolio/withdrawals": "FUNDING_WITHDRAWAL", "POST /portfolio/deposits": "FUNDING_DEPOSIT", "POST /portfolio/subaccounts/transfer": "FUNDS_TRANSFER", "POST /portfolio/intra_exchange_instance_transfer": "FUNDS_TRANSFER", "POST /portfolio/subaccounts": "SUBACCOUNT_MANAGEMENT", "POST /api_keys": "API_KEY_MANAGEMENT", "POST /api_keys/generate": "API_KEY_MANAGEMENT", "DELETE /api_k…` |
| 3 | executable launches and API responds | PASS | `{"app_version": "1.0.0", "model_version": "settle-gauss-avg-1.0", "strategy_version": "ev-taker-ioc-1.0", "schema_version": 1, "git_commit": "95402a025418a64a127273516a7e32eb0302679e", "git_dirty": false, "build_timestamp": "2026-10-11T15:45:51+00:00", "source": "packaged"}` |
| 4 | build matches source (git commit) | PASS | `{"exe": "95402a025418a64a127273516a7e32eb0302679e", "head": "95402a025418a64a127273516a7e32eb0302679e", "dirty": false}` |
| 5 | starts STOPPED | PASS | `"STOPPED"` |
| 6 | CREDENTIALS healthy | PASS | `{"status": "OK", "detail": "API key loaded", "age_s": null}` |
| 7 | AUTHENTICATION healthy | PASS | `{"status": "OK", "detail": "authenticated (GET /portfolio/balance)", "age_s": 0.7}` |
| 8 | RECONCILIATION healthy | PASS | `{"status": "OK", "detail": "ok", "age_s": 0.7}` |
| 9 | REST healthy | PASS | `{"status": "OK", "detail": "last error: 404 on GET /series/KXDOGE15M", "age_s": 0.7}` |
| 10 | WEBSOCKET healthy | PASS | `{"status": "OK", "detail": "WS_CONNECTED; reconnects=0", "age_s": 0.4}` |
| 11 | MARKET_DISCOVERY healthy | PASS | `{"status": "OK", "detail": "3 verified open market(s)", "age_s": 1.7}` |
| 12 | ORDER_BOOKS healthy | PASS | `{"status": "OK", "detail": "3/3 valid & fresh ", "age_s": null}` |
| 13 | CF_BENCHMARKS healthy | PASS | `{"status": "OK", "detail": "BRTI=0.4s, ETHUSD_RTI=0.4s, SOLUSD_RTI=0.4s", "age_s": null}` |
| 14 | open 15-minute crypto markets discovered | PASS | `["KXBTC15M-26OCT111600-00", "KXETH15M-26OCT111600-00", "KXSOL15M-26OCT111600-00"]` |
| 15 | books + CF remain fresh | PASS | `{"portfolio_reconciliation_s": 5.734858512878418, "websocket_s": 0.3911418914794922, "orderbook_max_s": 0.39111924171447754, "cf_max_s": 0.3916594982147217, "order_fill_update_s": null}` |
| 16 | WebSocket reconnect + resubscribe | PASS | `["WS_SUBSCRIBED", "WS_SUBSCRIBED", "WS_SUBSCRIBED", "WS_SUBSCRIBED", "WS_SUBSCRIBED", "WS_SUBSCRIBED"]` |
| 17 | CF outage → no new risk → recovery | PASS | `["INSUFFICIENT_HISTORY:6/300 one-second returns", "INSUFFICIENT_HISTORY:6/300 one-second returns", "INSUFFICIENT_HISTORY:6/300 one-second returns"]` |
| 18 | model warms up; probability available | PASS | `[["KXBTC15M-26OCT111600-00", "READY", 0.3145965600437637], ["KXETH15M-26OCT111600-00", "READY", 0.7797482493801461], ["KXSOL15M-26OCT111600-00", "READY", 0.6514281197854837]]` |
| 19 | strategy computes adjusted EV | PASS | `[["KXBTC15M-26OCT111600-00", "NO_POSITIVE_ADJUSTED_EV:-0.050179"], ["KXETH15M-26OCT111600-00", "NO_POSITIVE_ADJUSTED_EV:-0.061283"], ["KXSOL15M-26OCT111600-00", "NO_POSITIVE_ADJUSTED_EV:-0.058663"]]` |
| 20 | PAPER starts | PASS | `{"ok": true, "session_id": 1}` |
| 21 | PAPER loop keeps running | PASS | `"READY"` |
| 22 | deterministic qualifying opportunity reaches PAPER fill | PASS | `{"orders": [{"client_order_id": "pcb-p-50d3d0eff53142f194fb6dccdf751304", "exchange_order_id": null, "mode": "PAPER", "ticker": "KXBTC15M-26OCT111600-00", "outcome": "no", "book_side": "ask", "limit_price": "0.46", "count": "123", "filled_count": "123.00", "avg_fill_price": "0.455894", "fees": "2.15", "status": "FILLED", "created_ts": 1791733944.860152, "error": null}], "session": {"session_id": 1…` |
| 23 | fills + positions persisted | PASS | `{"observations": 187, "signals": 1, "orders": 1, "shadow_orders": 0, "fills": 2, "open_positions": 1, "settled_trades": 0, "unique_markets": 3, "evaluated_markets": 3, "oos_markets": 3, "live_forward_markets": 0, "settled_markets": 0}` |
| 24 | STOP works (no new orders, positions kept) | PASS | `{"stop": {"ok": true, "cancelled_resting": 0}, "bot": "STOPPED"}` |
| 25 | control API refuses requests without dashboard header | PASS | `{"http_status": 403, "detail": "missing X-PCB-Client header"}` |
| 26 | LIVE blocked in mock env / without validation | PASS | `"LIVE blocked: STRATEGY_VALIDATION: INSUFFICIENT_DATA (LIVE requires statistical evidence; see Strategy Validation); ENVIRONMENT: mock simulator \u2014 LIVE is disabled"` |
| 27 | restart returns STOPPED | PASS | `{"bot": "STOPPED", "mode": "PAPER"}` |
| 28 | history survives restart | PASS | `{"observations": 190, "signals": 1, "orders": 1, "shadow_orders": 0, "fills": 2, "open_positions": 1, "settled_trades": 0, "unique_markets": 3, "evaluated_markets": 3, "oos_markets": 3, "live_forward_markets": 0, "settled_markets": 0}` |
