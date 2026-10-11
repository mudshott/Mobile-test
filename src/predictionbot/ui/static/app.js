"use strict";
// Thin renderer of backend state. No independent trading state lives here.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtAge = (s) => (s === null || s === undefined ? "n/a" : `${Number(s).toFixed(1)}s`);
const fmtP = (p) => (p === null || p === undefined ? "—" : `${(p * 100).toFixed(1)}%`);
const money = (s) => (s === null || s === undefined ? "—" : `$${Number(s).toFixed(2)}`);
let S = null;

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-PCB-Client": "dashboard" },
    body: body ? JSON.stringify(body) : "{}",
  });
  const j = await r.json().catch(() => ({}));
  $("ctlMsg").textContent = j.ok === false || !r.ok ? `⚠ ${j.error || j.detail || r.status}` : "";
  $("ctlMsg").className = "msg " + (j.ok === false || !r.ok ? "fail" : "ok");
  refresh();
  return j;
}

function kv(obj) {
  return `<div class="kv">${Object.entries(obj).map(([k, v]) => `<div>${esc(k)}</div><div>${v}</div>`).join("")}</div>`;
}

function render(s) {
  S = s;
  const bot = s.bot;
  document.body.classList.toggle("live", bot.mode === "LIVE");
  $("env").textContent = s.settings.environment.toUpperCase();
  $("phase").textContent = s.health.readiness.phase;
  $("phase").className = "pill " + (s.health.readiness.phase === "READY" ? "ok" : "warn");
  $("botstate").textContent = `${bot.state} · ${bot.mode} · ${bot.profile}` + (bot.effective_profile !== bot.profile ? ` (effective ${bot.effective_profile})` : "");
  $("botstate").className = "pill big " + (bot.state === "RUNNING" ? "ok" : "muted");
  $("estop").classList.toggle("hidden", !(bot.mode === "LIVE" || bot.state === "RUNNING"));
  document.querySelectorAll("#modeSeg button").forEach((b) => b.classList.toggle("active", b.dataset.mode === bot.mode));
  document.querySelectorAll("#profSeg button").forEach((b) => b.classList.toggle("active", b.dataset.prof === bot.profile));
  $("liveBox").classList.toggle("hidden", bot.mode !== "LIVE");
  $("liveConfirmed").textContent = bot.live_confirmed ? "✔ confirmed for next START" : "";
  $("start").disabled = bot.state === "RUNNING";
  $("stop").disabled = bot.state !== "RUNNING";
  $("resetKill").classList.toggle("hidden", !bot.kill_switch);
  const rd = s.health.readiness;
  const blockers = bot.mode === "LIVE" ? rd.live_blockers : rd.paper_blockers;
  $("readiness").innerHTML = `<b>Operational readiness (${esc(bot.mode)}):</b> ` +
    (blockers.length ? `<span class="fail">BLOCKED</span><ul>${blockers.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>` : `<span class="ok">READY</span>`) +
    `<div class="muted">Tradeable now: ${rd.tradeable_markets.length ? esc(rd.tradeable_markets.join(", ")) : "none"}</div>`;

  const c = s.connection, a = s.ages;
  $("connBody").innerHTML = kv({
    Environment: esc(c.environment),
    Authentication: `<span class="${c.auth.ok ? "ok" : "fail"}">${esc(c.auth.detail)}</span>`,
    REST: `${esc(c.rest_base)} · ${c.rest.requests} req · ${c.rest.errors} err`,
    WebSocket: `${esc(c.ws.state)} · reconnects ${c.ws.reconnects} · gaps ${c.ws.gaps} · dup ${c.ws.duplicates}`,
    "Portfolio reconciliation age": fmtAge(a.portfolio_reconciliation_s),
    "WebSocket message age": fmtAge(a.websocket_s),
    "Order-book age (max)": fmtAge(a.orderbook_max_s),
    "CF age (max)": fmtAge(a.cf_max_s),
    "Order/fill update age": fmtAge(a.order_fill_update_s),
    "Clock skew vs exchange": c.rest.server_clock_skew_s == null ? "n/a" : `${Number(c.rest.server_clock_skew_s).toFixed(2)}s`,
  });

  $("healthBody").innerHTML = `<table>${Object.entries(s.health.components).map(([k, v]) =>
    `<tr><td><span class="dot ${v.status}"></span>${esc(k)}</td><td class="reason">${esc(v.detail)}${v.age_s != null ? ` <span class="muted">(${fmtAge(v.age_s)})</span>` : ""}</td></tr>`).join("")}</table>`;

  const led = bot.mode === "LIVE" ? s.portfolio.live : s.portfolio.paper;
  const ex = s.portfolio.exchange;
  $("portBody").innerHTML = kv({
    "Ledger": `${esc(led.mode)} (${esc(led.cash_source)})`,
    "Cash / balance": money(led.cash),
    "Available capital": money(led.available),
    "Reserved": money(led.reserved),
    "Deployed": money(led.deployed),
    "Equity (at cost)": money(led.equity),
    "Session P&L": money(led.session_pnl),
    "Drawdown": `${(Number(led.drawdown_pct) * 100).toFixed(2)}%`,
    "Daily loss": `${(Number(led.daily_loss_pct) * 100).toFixed(2)}%`,
    "Kalshi balance (reconciled)": ex.balance ? money(ex.balance) : "n/a",
    "Kalshi positions / resting": `${ex.positions ?? "n/a"} / ${ex.resting_orders ?? "n/a"}`,
  });

  $("watchBody").innerHTML = s.watch.length ? `<table><tr><th>Market</th><th>Close in</th><th>Strike</th><th>CF ref</th><th>Model P(YES)</th><th>Cons YES/NO</th><th>YES ask</th><th>NO ask</th><th>Raw edge</th><th>Adj. EV</th><th>Decision</th><th>Exact reason</th></tr>` +
    s.watch.map((w) => {
      const e = w.evaluation || {};
      const best = [e.yes, e.no].filter((x) => x && x.adjusted_ev != null).sort((x, y) => Number(y.adjusted_ev) - Number(x.adjusted_ev))[0];
      const trade = (w.decision || "").startsWith("TRADE");
      return `<tr><td>${esc(w.ticker)}${w.verified ? "" : ' <span class="warn" title="' + esc((w.verify_notes || []).join("; ")) + '">unverified</span>'}</td>
        <td>${w.seconds_to_close != null ? Math.max(0, w.seconds_to_close).toFixed(0) + "s" : "—"}</td>
        <td>${esc(w.strike_type || "")} ${esc(w.floor_strike || w.cap_strike || "")}</td>
        <td>${w.spot != null ? Number(w.spot).toFixed(4) : "—"}</td>
        <td>${w.model_state === "READY" ? fmtP(w.raw_prob_yes) : `<span class="warn">${esc(w.model_state)}</span>`}</td>
        <td>${fmtP(w.cons_prob_yes)} / ${fmtP(w.cons_prob_no)}</td>
        <td>${esc(w.yes_ask ?? "—")}</td><td>${esc(w.no_ask ?? "—")}</td>
        <td>${best && best.raw_edge != null ? best.outcome.toUpperCase() + " " + Number(best.raw_edge).toFixed(3) : "—"}</td>
        <td>${best ? best.outcome.toUpperCase() + " " + esc(best.adjusted_ev) : "—"}</td>
        <td class="${trade ? "trade" : "notrade"}">${esc(w.action || w.decision)}</td>
        <td class="reason">${esc(trade ? "" : (e.reasons || []).join(" | ") || w.reason)}</td></tr>`;
    }).join("") + "</table>" : `<span class="muted">No active verified markets yet. ${esc(s.health.components.MARKET_DISCOVERY.detail)}</span>`;

  $("posBody").innerHTML = led.positions.length ? `<table><tr><th>Market</th><th>Side</th><th>Direction</th><th>Qty</th><th>Avg entry</th><th>Current (bid)</th><th>Cost</th><th>Fees</th><th>Unrealized (bid)</th></tr>` +
    led.positions.map((p) => {
      const b = (s.books.books || {})[p.ticker];
      const bid = b ? (p.outcome === "yes" ? b.best_yes_bid : b.best_no_bid) : null;
      const unreal = bid != null ? (Number(bid) * Number(p.count) - Number(p.cost) - Number(p.fees)).toFixed(2) : "—";
      return `<tr><td>${esc(p.ticker)}</td><td>${esc(p.outcome.toUpperCase())}</td><td>${esc(p.direction)}</td><td>${esc(p.count)}</td><td>${esc(p.avg_entry)}</td><td>${esc(bid ?? "—")}</td><td>${money(p.cost)}</td><td>${money(p.fees)}</td><td>${unreal}</td></tr>`;
    }).join("") + "</table>" : '<span class="muted">No open positions.</span>';

  $("ordersBody").innerHTML = s.orders.length ? `<table><tr><th>Time</th><th>Mode</th><th>Market</th><th>Side</th><th>Limit</th><th>Qty</th><th>Filled</th><th>Avg</th><th>Fees</th><th>Status</th><th>Note</th></tr>` +
    s.orders.map((o) => `<tr><td>${new Date(o.created_ts * 1000).toLocaleTimeString()}</td><td>${esc(o.mode)}</td><td>${esc(o.ticker)}</td><td>${esc(o.outcome.toUpperCase())}</td><td>${esc(o.limit_price)}</td><td>${esc(o.count)}</td><td>${esc(o.filled_count)}</td><td>${esc(o.avg_fill_price ?? "—")}</td><td>${esc(o.fees)}</td><td>${esc(o.status)}</td><td class="reason">${esc(o.error ?? "")}</td></tr>`).join("") + "</table>" : '<span class="muted">No orders yet (resting orders: none — the strategy uses immediate-or-cancel).</span>';

  const ss = s.counters.session, t = s.counters.totals;
  $("sessionBody").innerHTML = kv({
    Session: ss.session_id ?? "none",
    Orders: ss.orders ?? 0, Fills: ss.fills ?? 0, "Open/Unsettled": ss.open_unsettled ?? 0,
    Settled: ss.settled ?? 0, Wins: ss.wins ?? 0, Losses: ss.losses ?? 0,
    "Win rate": ss.win_rate ?? "N/A", "Realized P&L": ss.realized_pnl != null ? money(ss.realized_pnl) : "—",
    "— All-time —": "",
    Observations: t.observations, Signals: t.signals, "Orders (paper+live)": t.orders, "Shadow orders": t.shadow_orders,
    Fills: t.fills, "Settled trades": t.settled_trades, "Unique markets": t.unique_markets,
    "Evaluated markets": t.evaluated_markets, "OOS markets": t.oos_markets, "Live-forward markets": t.live_forward_markets,
  });

  const v = s.validation || {};
  const verd = (x) => `<span class="${x === "PASS" ? "ok" : x === "FAIL" ? "fail" : "warn"}">${esc(x)}</span>`;
  const st = v.studies || {};
  $("valBody").innerHTML = kv({
    Overall: verd(v.overall || "NOT_RUN"),
    "LIVE capital justified": v.live_capital_justified ? '<span class="ok">yes</span>' : '<span class="warn">no</span>',
    Calibration: (v.verdicts ? verd(v.verdicts.CALIBRATION) : "") + ` ${esc(st.CALIBRATION?.reason ?? "")}`,
    "Paper P&L": (v.verdicts ? verd(v.verdicts.PAPER_PNL) : "") + ` ${esc(st.PAPER_PNL?.reason ?? "")}`,
    "Execution stress": (v.verdicts ? verd(v.verdicts.EXECUTION_SENSITIVITY) : "") + ` ${esc(st.EXECUTION_SENSITIVITY?.reason ?? "")}`,
    "High-confidence size override": v.high_confidence_override_allowed ? "allowed by calibration" : "DISABLED (insufficient calibration evidence)",
  }) + `<p class="small muted">System health ≠ strategy validation. PAPER/SHADOW only need operational readiness.</p>`;

  $("events").innerHTML = s.events.map((e) => `<div>${new Date(e.ts * 1000).toLocaleTimeString()} <b>${esc(e.component)}</b> ${esc(e.code)} <span class="muted">${esc(e.detail)}</span></div>`).join("");
  $("wsDiag").innerHTML = `<pre>${esc(JSON.stringify(c.subscriptions, null, 1))}</pre>` + c.ws_events.slice().reverse().map((e) => `<div>${new Date(e.ts * 1000).toLocaleTimeString()} ${esc(e.code)} <span class="muted">${esc(e.detail)}</span></div>`).join("");
  $("booksDiag").innerHTML = `<pre>${esc(JSON.stringify(s.books, null, 1))}</pre>`;
  $("cfDiag").innerHTML = `<pre>${esc(JSON.stringify(s.cf, null, 1))}</pre>`;
  $("profDiag").innerHTML = `<pre>${esc(JSON.stringify(s.profiles, null, 1))}</pre>`;
  $("secDiag").innerHTML = `<p>${esc(s.security.note)}</p><pre>${esc(s.security.allowed_endpoints.join("\n"))}</pre>`;
  $("buildBody").innerHTML = kv(Object.fromEntries(Object.entries(s.build).map(([k, x]) => [k, esc(x)])));
  $("lastUpdate").textContent = "updated " + new Date(s.ts * 1000).toLocaleTimeString();
}

async function refresh() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    if (r.ok) render(await r.json());
  } catch (e) {
    $("lastUpdate").textContent = "backend unreachable: " + e;
  }
}

document.querySelectorAll("#modeSeg button").forEach((b) => b.addEventListener("click", () => post("/api/bot/mode", { mode: b.dataset.mode })));
document.querySelectorAll("#profSeg button").forEach((b) => b.addEventListener("click", () => post("/api/bot/profile", { profile: b.dataset.prof })));
$("liveConfirm").addEventListener("click", () => post("/api/bot/confirm_live", { text: $("liveText").value }));
$("start").addEventListener("click", () => post("/api/bot/start"));
$("stop").addEventListener("click", () => post("/api/bot/stop"));
$("estop").addEventListener("click", () => post("/api/bot/emergency_stop"));
$("resetKill").addEventListener("click", () => post("/api/bot/reset_kill_switch"));
$("runVal").addEventListener("click", () => post("/api/validation/run"));
refresh();
setInterval(refresh, 1000);
