-- Schema v1: canonical records for recovery and research.
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts REAL NOT NULL,
    ended_ts REAL,
    mode TEXT NOT NULL,
    profile TEXT NOT NULL,
    environment TEXT NOT NULL,
    app_version TEXT, git_commit TEXT, model_version TEXT, strategy_version TEXT,
    starting_bankroll TEXT,
    stop_reason TEXT
);

CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    series TEXT NOT NULL, asset TEXT NOT NULL, cf_index TEXT NOT NULL,
    event_ticker TEXT, open_ts REAL, close_ts REAL,
    strike_type TEXT, floor_strike TEXT, cap_strike TEXT,
    status TEXT, result TEXT, expiration_value TEXT, settled_ts REAL,
    verified INTEGER NOT NULL DEFAULT 0, verify_notes TEXT,
    first_seen_ts REAL NOT NULL, updated_ts REAL NOT NULL,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS ix_markets_close ON markets(close_ts);

CREATE TABLE IF NOT EXISTS cf_ticks (
    index_id TEXT NOT NULL, source_ts_ms INTEGER NOT NULL, value REAL NOT NULL, recv_ts REAL NOT NULL,
    PRIMARY KEY (index_id, source_ts_ms)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, ticker TEXT NOT NULL, session_id INTEGER,
    model_version TEXT NOT NULL, state TEXT NOT NULL,
    raw_prob_yes REAL, cons_prob_yes REAL, cons_prob_no REAL,
    lower_prob_yes REAL, upper_prob_yes REAL, uncertainty REAL, disagreement REAL,
    seconds_to_close REAL, spot REAL, expected_avg REAL, sd_avg REAL,
    sigma_short REAL, sigma_long REAL, n_returns INTEGER, coverage REAL, window_observed INTEGER,
    yes_bid TEXT, yes_ask TEXT, no_bid TEXT, no_ask TEXT, spread TEXT,
    missing TEXT, research TEXT
);
CREATE INDEX IF NOT EXISTS ix_predictions_ticker_ts ON predictions(ticker, ts);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, ticker TEXT NOT NULL, session_id INTEGER, prediction_id INTEGER,
    mode TEXT, profile TEXT, decision TEXT NOT NULL, primary_reason TEXT, reasons TEXT,
    side TEXT, best_ask TEXT, limit_price TEXT, raw_edge REAL, adjusted_ev TEXT, p_cons REAL, depth TEXT,
    seconds_to_close REAL, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_evaluations_ticker_ts ON evaluations(ticker, ts);
CREATE INDEX IF NOT EXISTS ix_evaluations_decision ON evaluations(decision);

CREATE TABLE IF NOT EXISTS risk_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, evaluation_id INTEGER, ticker TEXT NOT NULL, session_id INTEGER, mode TEXT,
    verdict TEXT NOT NULL, requested_count TEXT, approved_count TEXT,
    selected_profile TEXT, effective_profile TEXT, bankroll TEXT, reasons TEXT, snapshot TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL UNIQUE,
    exchange_order_id TEXT,
    mode TEXT NOT NULL, session_id INTEGER, ticker TEXT NOT NULL,
    outcome TEXT NOT NULL, book_side TEXT NOT NULL,
    limit_price TEXT NOT NULL, count TEXT NOT NULL,
    filled_count TEXT NOT NULL DEFAULT '0', avg_fill_price TEXT, fees TEXT NOT NULL DEFAULT '0',
    status TEXT NOT NULL, time_in_force TEXT NOT NULL,
    created_ts REAL NOT NULL, updated_ts REAL NOT NULL,
    evaluation_id INTEGER, risk_decision_id INTEGER,
    bot_owned INTEGER NOT NULL DEFAULT 1,
    error TEXT, request TEXT, response TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS ix_orders_ticker ON orders(ticker);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_id TEXT NOT NULL UNIQUE,
    client_order_id TEXT, exchange_order_id TEXT,
    mode TEXT NOT NULL, ticker TEXT NOT NULL, outcome TEXT NOT NULL,
    count TEXT NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL, is_taker INTEGER,
    ts REAL NOT NULL, raw TEXT
);
CREATE INDEX IF NOT EXISTS ix_fills_ticker ON fills(mode, ticker);

CREATE TABLE IF NOT EXISTS positions (
    mode TEXT NOT NULL, ticker TEXT NOT NULL, outcome TEXT NOT NULL,
    count TEXT NOT NULL, cost TEXT NOT NULL, fees TEXT NOT NULL,
    status TEXT NOT NULL, opened_ts REAL NOT NULL, updated_ts REAL NOT NULL,
    settled_ts REAL, result TEXT, payout TEXT, realized_pnl TEXT,
    asset TEXT,
    PRIMARY KEY (mode, ticker, outcome)
);

CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL, ticker TEXT NOT NULL, outcome TEXT NOT NULL, result TEXT NOT NULL,
    settled_ts REAL NOT NULL, count TEXT NOT NULL, cost TEXT NOT NULL, fees TEXT NOT NULL,
    payout TEXT NOT NULL, pnl TEXT NOT NULL, source TEXT NOT NULL, raw TEXT,
    UNIQUE (mode, ticker, outcome)
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, mode TEXT NOT NULL, cash TEXT NOT NULL, portfolio_value TEXT, source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, component TEXT NOT NULL, code TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_health_ts ON health_events(ts);

CREATE TABLE IF NOT EXISTS validation_partitions (
    ticker TEXT PRIMARY KEY, partition TEXT NOT NULL, assigned_ts REAL NOT NULL, rule TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS validation_studies (
    study_id TEXT PRIMARY KEY, study_type TEXT NOT NULL, created_ts REAL NOT NULL,
    model_version TEXT, strategy_version TEXT, dataset_hash TEXT,
    time_start REAL, time_end REAL, partition TEXT,
    unique_markets INTEGER, observations INTEGER, fills INTEGER,
    assumptions TEXT, fee_model TEXT, slippage_model TEXT, latency_model TEXT,
    metrics TEXT, verdict TEXT NOT NULL, reason TEXT
);

CREATE TABLE IF NOT EXISTS research_hypotheses (
    id TEXT PRIMARY KEY, topic TEXT, source TEXT, claim TEXT, quality TEXT, credibility TEXT,
    hypothesis TEXT, methodology TEXT, result TEXT, status TEXT NOT NULL, updated_ts REAL NOT NULL
);
