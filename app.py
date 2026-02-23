"""
app.py — FastAPI server: WebSocket broadcast + strategy background loop
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("strategy")

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from strategy_core import (
    find_active_sol_market,
    get_order_book_metrics,
    compute_signal,
    seconds_remaining,
    fetch_clob_market,
    build_market_info,
    fetch_gamma_market,
    get_current_slot_ts,
    SLOT_STEP,
)
from simulator import Portfolio
import db as database

# ── Config ────────────────────────────────────────────────────────────────────
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3"))
OBI_THRESHOLD = float(os.environ.get("OBI_THRESHOLD", "0.15"))
WINDOW_SIZE   = int(os.environ.get("WINDOW_SIZE", "8"))
PORT          = int(os.environ.get("PORT", "8000"))

# ── Shared state ──────────────────────────────────────────────────────────────
connected: set[WebSocket] = set()

state: dict = {
    "market":    {},
    "orderbook": {},
    "signal":    {},
    "portfolio": {},
    "config":    {
        "threshold":     OBI_THRESHOLD,
        "window_size":   WINDOW_SIZE,
        "poll_interval": POLL_INTERVAL,
        "initial_capital": 100.0,
        "trade_pct":     0.02,
    },
    "status":    "initializing",
    "error":     None,
}


# ── WebSocket broadcast ───────────────────────────────────────────────────────

async def broadcast(data: dict):
    dead: set[WebSocket] = set()
    for ws in connected.copy():
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    connected.difference_update(dead)


# ── Strategy loop ─────────────────────────────────────────────────────────────

async def strategy_loop():
    # Restaurar portafolio desde DB
    saved     = database.load_state()
    portfolio = Portfolio(
        initial_capital = saved["initial_capital"],
        db              = database,
    )
    portfolio.restore(saved)
    log.info(
        f"Portafolio restaurado: capital=${portfolio.capital:.2f}, "
        f"trades históricos={len(portfolio.closed_trades)}"
    )

    obi_window  = deque(maxlen=WINDOW_SIZE)
    market_info = None
    snap        = 0
    last_market_id = None

    while True:
        try:
            # ── Find / refresh market ─────────────────────────────────────────
            if market_info is None:
                state["status"] = "searching"
                await broadcast(state)
                log.info("Searching for active SOL 5min market...")
                market_info = await asyncio.to_thread(find_active_sol_market)
                if market_info is None:
                    state["error"] = "No active SOL market found. Retrying in 15s..."
                    log.warning("No market found, retrying in 15s")
                    await asyncio.sleep(15)
                    continue
                state["error"] = None
                log.info(f"Found market: {market_info['question']}")

            # ── Detect new market cycle ───────────────────────────────────────
            if market_info["condition_id"] != last_market_id:
                last_market_id = market_info["condition_id"]
                obi_window.clear()
                snap = 0
                log.info(f"New market cycle: {market_info['question']}")
                # Close any open trade (market expired)
                if portfolio.active_trade:
                    up_p   = market_info["up_price"]
                    down_p = market_info["down_price"]
                    portfolio.close_trade(up_p, down_p)

            # ── Get order book ────────────────────────────────────────────────
            snap += 1
            ob, err = await asyncio.to_thread(
                get_order_book_metrics, market_info["up_token_id"]
            )

            if err or ob is None:
                # 404 means market expired → find next slot
                if err and ("404" in str(err) or "No orderbook" in str(err)):
                    if portfolio.active_trade:
                        portfolio.close_trade(
                            market_info["up_price"],
                            market_info["down_price"],
                        )
                    market_info = None
                    state["status"] = "searching"
                    state["error"]  = "Mercado expirado, buscando siguiente..."
                    await broadcast(state)
                    await asyncio.sleep(6)
                else:
                    state["status"] = "error"
                    state["error"]  = err or "Empty order book"
                    await broadcast(state)
                    await asyncio.sleep(POLL_INTERVAL)
                continue

            # Update market prices
            market_info["up_price"]   = ob["vwap_mid"]
            market_info["down_price"] = round(1 - ob["vwap_mid"], 4)
            obi_window.append(ob["obi"])

            # ── Signal ────────────────────────────────────────────────────────
            signal = compute_signal(ob["obi"], list(obi_window), OBI_THRESHOLD)

            # ── Simulation ────────────────────────────────────────────────────
            secs_left = seconds_remaining(market_info)

            # v2: smart exits — check every snapshot while a trade is open
            if portfolio.active_trade and secs_left is not None and secs_left > 0:
                reason = portfolio.check_exits(
                    signal,
                    market_info["up_price"],
                    market_info["down_price"],
                    secs_left,
                )
                if reason:
                    exited = portfolio.exit_at_market_price(
                        market_info["up_price"],
                        market_info["down_price"],
                        reason,
                    )
                    if exited:
                        log.info(
                            f"Smart exit [{reason}] #{exited.id}: "
                            f"entry={exited.entry_price:.4f} "
                            f"exit={exited.exit_price:.4f} "
                            f"pnl={exited.pnl:+.4f}"
                        )

            # Try entering a trade (only when market has >60s remaining)
            if secs_left is not None and secs_left > 60:
                portfolio.consider_entry(
                    signal,
                    market_info["question"],
                    market_info["up_price"],
                    market_info["down_price"],
                )

            # Emergency binary close: market expiring with open trade
            if secs_left is not None and secs_left < 5 and portfolio.active_trade:
                portfolio.close_trade(
                    market_info["up_price"],
                    market_info["down_price"],
                )

            # Detect market expiry → search next
            if secs_left is not None and secs_left <= 0:
                market_info = None
                await asyncio.sleep(5)
                continue

            # ── Refresh accepting_orders status ───────────────────────────────
            if snap % 5 == 0:
                fresh = await asyncio.to_thread(
                    fetch_clob_market, market_info["condition_id"]
                )
                if fresh:
                    market_info["accepting_orders"] = bool(fresh.get("accepting_orders"))

            # ── Build state snapshot ──────────────────────────────────────────
            portfolio_stats = portfolio.stats(
                market_info["up_price"],
                market_info["down_price"],
            )

            # ── Realistic price data (display only, algo unchanged) ────────────
            # In a real trade: you BUY at best_ask, you SELL at best_bid.
            # The algo still uses vwap_mid internally — this is only for the dashboard.
            up_ask   = ob["best_ask"]
            up_bid   = ob["best_bid"]
            down_ask = round(1 - ob["best_bid"], 4)   # cost to buy DOWN token
            down_bid = round(1 - ob["best_ask"], 4)   # proceeds selling DOWN token

            if portfolio.active_trade and portfolio_stats.get("active_trade"):
                at = portfolio.active_trade
                # Current price you'd actually receive if you sold right now
                real_cp   = up_bid if at.direction == "UP" else down_bid
                real_upnl = round(at.shares * real_cp - at.bet_size, 4)
                sim_upnl  = portfolio_stats["active_trade"].get("unrealized_pnl", 0)
                portfolio_stats["active_trade"]["real_current_price"] = round(real_cp, 4)
                portfolio_stats["active_trade"]["real_unrealized_pnl"] = real_upnl
                portfolio_stats["active_trade"]["spread_impact"] = round(sim_upnl - real_upnl, 4)
                portfolio_stats["active_trade"]["real_entry_cost"] = (
                    up_ask if at.direction == "UP" else down_ask
                )

            state["status"]    = "running"
            state["error"]     = None
            state["snapshot"]  = snap
            state["timestamp"] = datetime.utcnow().isoformat() + "Z"
            state["market"]    = {
                **market_info,
                "seconds_remaining": round(secs_left, 1) if secs_left is not None else None,
                "up_ask":   up_ask,    # precio real para COMPRAR UP
                "up_bid":   up_bid,    # precio real al VENDER UP
                "down_ask": down_ask,  # precio real para COMPRAR DOWN
                "down_bid": down_bid,  # precio real al VENDER DOWN
            }
            state["orderbook"] = ob
            state["signal"]    = signal
            state["portfolio"] = portfolio_stats

            await broadcast(state)

        except Exception as exc:
            log.exception(f"Strategy loop error: {exc}")
            state["status"] = "error"
            state["error"]  = str(exc)
            market_info = None  # force re-search on next iteration
            await broadcast(state)

        await asyncio.sleep(POLL_INTERVAL)


# ── App lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Inicializar DB antes de arrancar el loop
    database.init_db()
    log.info(f"DB path: {database.db_path()}")
    task = asyncio.create_task(strategy_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app       = FastAPI(title="Polymarket SOL Strategy v3", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Endpoint de diagnóstico — útil para verificar Railway."""
    return {
        "status":    "ok",
        "db_path":   database.db_path(),
        "data_dir":  database.DATA_DIR,
        "strategy":  state.get("status"),
        "market":    state.get("market", {}).get("question"),
        "snapshot":  state.get("snapshot"),
        "error":     state.get("error"),
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/state")
async def get_state():
    return state


@app.get("/api/trades")
async def get_all_trades():
    """Historial completo de trades desde la DB (útil para análisis externo)."""
    saved = await asyncio.to_thread(database.load_state)
    return {
        "capital":       saved["capital"],
        "initial_capital": saved["initial_capital"],
        "total_trades":  saved["trade_counter"],
        "trades":        [t.to_dict() for t in saved["closed_trades"]],
        "db_path":       database.db_path(),
    }


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected.add(websocket)
    # Send current state immediately
    try:
        await websocket.send_json(state)
        while True:
            # Keep connection alive; client sends pings
            await asyncio.wait_for(websocket.receive_text(), timeout=30)
    except (WebSocketDisconnect, asyncio.TimeoutError, Exception):
        connected.discard(websocket)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, log_level="warning")
