"""
simulator.py — Portfolio simulation v2: smart mid-market exits
  TP     : Take Profit when unrealized >= bet * TAKE_PROFIT_MULT
  SL     : Stop Loss   when unrealized <= -(bet * STOP_LOSS_PCT)
  SIGNAL : Exit after SIGNAL_EXIT_N consecutive opposing OBI snaps
  LATE   : Exit in last LATE_EXIT_SECS seconds if any profit
  EXPIRE : Binary resolution at market expiry (fallback)
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


INITIAL_CAPITAL  = 100.0
TRADE_PCT        = 0.02      # 2% of capital per trade
MIN_CONFIDENCE   = 65
ENTRY_AFTER_N    = 4         # consecutive aligned snaps required to enter

# v2 exit parameters
TAKE_PROFIT_MULT = 3.0       # TP when unrealized >= bet * 3
STOP_LOSS_PCT    = 0.50      # SL when unrealized <= -(bet * 0.50)
SIGNAL_EXIT_N    = 3         # exit after N consecutive opposing signal snaps
LATE_EXIT_SECS   = 45        # exit in last 45 s with any profit


@dataclass
class Trade:
    id:           int
    market:       str
    direction:    str           # "UP" or "DOWN"
    entry_price:  float         # token price at entry (0–1)
    shares:       float         # bet_size / entry_price
    bet_size:     float         # USDC committed
    entry_time:   str
    exit_price:   Optional[float] = None
    pnl:          Optional[float] = None
    status:       str = "OPEN"  # OPEN | WIN | LOSS | CANCELLED
    exit_reason:  Optional[str] = None  # TP | SL | SIGNAL | LATE | EXPIRE | FORCED

    def mark_to_market(self, current_price: float) -> float:
        return round(self.shares * current_price, 4)

    def unrealized_pnl(self, current_price: float) -> float:
        return round(self.mark_to_market(current_price) - self.bet_size, 4)

    def close_binary(self, won: bool, exit_price: float, exit_reason: str = "EXPIRE") -> float:
        """Binary resolution when market expires (token resolves to 0 or 1)."""
        self.exit_price  = exit_price
        self.exit_reason = exit_reason
        if won:
            proceeds    = self.shares * 1.0
            self.pnl    = round(proceeds - self.bet_size, 4)
            self.status = "WIN"
        else:
            self.pnl    = round(-self.bet_size, 4)
            self.status = "LOSS"
        return self.pnl

    def close_market(self, exit_price: float, exit_reason: str) -> float:
        """Mid-market close at current token price."""
        self.exit_price  = round(exit_price, 4)
        self.exit_reason = exit_reason
        proceeds = self.shares * exit_price
        self.pnl = round(proceeds - self.bet_size, 4)
        self.status = "WIN" if self.pnl > 0 else "LOSS"
        return self.pnl

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "market":       self.market,
            "direction":    self.direction,
            "entry_price":  self.entry_price,
            "shares":       round(self.shares, 4),
            "bet_size":     self.bet_size,
            "entry_time":   self.entry_time,
            "exit_price":   self.exit_price,
            "pnl":          self.pnl,
            "status":       self.status,
            "exit_reason":  self.exit_reason,
        }


class Portfolio:
    def __init__(self, initial_capital: float = INITIAL_CAPITAL,
                 trade_pct: float = TRADE_PCT, db=None):
        self.initial_capital  = initial_capital
        self.capital          = initial_capital
        self.trade_pct        = trade_pct
        self.active_trade: Optional[Trade]  = None
        self.closed_trades: list[Trade]     = []
        self.pnl_history:   list[float]     = [0.0]
        self._trade_counter  = 0
        self._db             = db
        self._signal_streak: dict = {"label": None, "count": 0}
        self._opposing_streak = 0   # consecutive opposing OBI snaps while in trade

    def restore(self, saved: dict) -> None:
        self.capital         = saved["capital"]
        self.initial_capital = saved["initial_capital"]
        self.pnl_history     = saved["pnl_history"]
        self._trade_counter  = saved["trade_counter"]
        self.closed_trades   = saved["closed_trades"]

    # ── Entry ─────────────────────────────────────────────────────────────────

    def consider_entry(self, signal: dict, market_question: str,
                       up_price: float, down_price: float) -> bool:
        if self.active_trade is not None:
            return False
        if self.capital < 1.0:
            return False

        label = signal["label"]
        conf  = signal["confidence"]

        if label not in ("UP", "DOWN", "STRONG UP", "STRONG DOWN"):
            self._signal_streak = {"label": None, "count": 0}
            return False

        direction = "UP" if "UP" in label else "DOWN"

        if self._signal_streak["label"] == direction:
            self._signal_streak["count"] += 1
        else:
            self._signal_streak = {"label": direction, "count": 1}

        if self._signal_streak["count"] < ENTRY_AFTER_N:
            return False
        if conf < MIN_CONFIDENCE:
            return False

        bet_size    = round(self.capital * self.trade_pct, 2)
        entry_price = up_price if direction == "UP" else down_price
        if entry_price <= 0.01:
            return False

        self.capital = round(self.capital - bet_size, 4)
        shares = round(bet_size / entry_price, 4)
        self._trade_counter  += 1
        self._opposing_streak = 0
        self.active_trade = Trade(
            id          = self._trade_counter,
            market      = market_question,
            direction   = direction,
            entry_price = entry_price,
            shares      = shares,
            bet_size    = bet_size,
            entry_time  = datetime.utcnow().strftime("%H:%M:%S"),
        )
        self._signal_streak = {"label": None, "count": 0}
        if self._db:
            self._db.save_trade(self.active_trade)
        return True

    # ── v2: Smart exit checks ─────────────────────────────────────────────────

    def check_exits(self, signal: dict, up_price: float, down_price: float,
                    secs_left) -> Optional[str]:
        """
        Evaluate exit conditions every snapshot.
        Returns the exit reason string, or None if no exit.
        Priority: TP > SL > SIGNAL > LATE
        """
        if not self.active_trade:
            return None

        trade = self.active_trade
        upnl  = self.get_unrealized(up_price, down_price)

        # 1. Take Profit
        if upnl >= trade.bet_size * TAKE_PROFIT_MULT:
            return "TP"

        # 2. Stop Loss
        if upnl <= -(trade.bet_size * STOP_LOSS_PCT):
            return "SL"

        # 3. Signal reversal streak
        label = signal.get("label", "NEUTRAL")
        if label in ("UP", "DOWN", "STRONG UP", "STRONG DOWN"):
            direction = "UP" if "UP" in label else "DOWN"
            if direction != trade.direction:
                self._opposing_streak += 1
            else:
                self._opposing_streak = 0  # aligned signal resets counter
        # NEUTRAL: leave streak unchanged

        if self._opposing_streak >= SIGNAL_EXIT_N:
            return "SIGNAL"

        # 4. Late exit: any profit in last LATE_EXIT_SECS
        if secs_left is not None and secs_left <= LATE_EXIT_SECS and upnl > 0:
            return "LATE"

        return None

    def exit_at_market_price(self, up_price: float, down_price: float,
                             exit_reason: str) -> Optional[Trade]:
        """Close active trade at current mid-market token price."""
        if not self.active_trade:
            return None

        trade = self.active_trade
        cp    = self.current_price_for_trade(up_price, down_price)
        pnl   = trade.close_market(cp, exit_reason)

        self.capital = round(self.capital + trade.bet_size + pnl, 4)
        self.closed_trades.append(trade)
        self.active_trade      = None
        self._signal_streak    = {"label": None, "count": 0}
        self._opposing_streak  = 0

        total_pnl = round(self.capital - self.initial_capital, 4)
        self.pnl_history.append(total_pnl)

        if self._db:
            self._db.save_trade(trade)
            self._db.save_portfolio_state(
                self.capital, self.initial_capital,
                self.pnl_history, self._trade_counter,
            )
        return trade

    # ── Mark-to-market ────────────────────────────────────────────────────────

    def current_price_for_trade(self, up_price: float, down_price: float) -> float:
        if not self.active_trade:
            return 0.0
        return up_price if self.active_trade.direction == "UP" else down_price

    def get_unrealized(self, up_price: float, down_price: float) -> float:
        if not self.active_trade:
            return 0.0
        cp = self.current_price_for_trade(up_price, down_price)
        return self.active_trade.unrealized_pnl(cp)

    # ── Binary close (market expiry fallback) ─────────────────────────────────

    def close_trade(self, up_price: float, down_price: float,
                    force_winner: Optional[bool] = None) -> Optional[Trade]:
        """Binary resolution at market expiry."""
        if not self.active_trade:
            return None

        trade = self.active_trade

        if force_winner is not None:
            won = force_winner
        else:
            won = (up_price >= 0.5) if trade.direction == "UP" else (down_price >= 0.5)

        exit_price = 1.0 if won else 0.0
        pnl        = trade.close_binary(won, exit_price, "EXPIRE")
        self.capital = round(self.capital + trade.bet_size + pnl, 4)
        self.closed_trades.append(trade)
        self.active_trade      = None
        self._signal_streak    = {"label": None, "count": 0}
        self._opposing_streak  = 0

        total_pnl = round(self.capital - self.initial_capital, 4)
        self.pnl_history.append(total_pnl)

        if self._db:
            self._db.save_trade(trade)
            self._db.save_portfolio_state(
                self.capital, self.initial_capital,
                self.pnl_history, self._trade_counter,
            )
        return trade

    def cancel_active_trade(self):
        if not self.active_trade:
            return
        self.active_trade.status      = "CANCELLED"
        self.active_trade.exit_reason = "FORCED"
        self.closed_trades.append(self.active_trade)
        if self._db:
            self._db.save_trade(self.active_trade)
            self._db.save_portfolio_state(
                self.capital, self.initial_capital,
                self.pnl_history, self._trade_counter,
            )
        self.active_trade     = None
        self._opposing_streak = 0

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self, up_price: float = 0.5, down_price: float = 0.5) -> dict:
        closed   = self.closed_trades
        wins     = [t for t in closed if t.status == "WIN"]
        losses   = [t for t in closed if t.status == "LOSS"]
        n_closed = len(wins) + len(losses)
        win_rate = round(len(wins) / n_closed * 100, 1) if n_closed else 0.0

        realized_pnl   = sum(t.pnl for t in closed if t.pnl is not None)
        unrealized_pnl = self.get_unrealized(up_price, down_price)
        total_pnl      = round(realized_pnl + unrealized_pnl, 4)
        equity         = round(
            self.capital
            + (self.active_trade.bet_size if self.active_trade else 0)
            + unrealized_pnl, 4,
        )

        active = None
        if self.active_trade:
            t  = self.active_trade
            cp = self.current_price_for_trade(up_price, down_price)
            # Price levels for TP/SL
            # upnl = shares*cp - bet >= bet*TP_MULT  →  cp >= entry*(1+TP_MULT)
            # upnl = shares*cp - bet <= -bet*SL_PCT  →  cp <= entry*(1-SL_PCT)
            tp_price = round(t.entry_price * (1 + TAKE_PROFIT_MULT), 4)
            sl_price = round(t.entry_price * (1 - STOP_LOSS_PCT), 4)
            tp_pnl   = round(t.bet_size * TAKE_PROFIT_MULT, 4)
            sl_pnl   = round(-(t.bet_size * STOP_LOSS_PCT), 4)
            rng = tp_price - sl_price
            progress = round(
                max(0.0, min(1.0, (cp - sl_price) / rng)) * 100, 1
            ) if rng > 0 else 50.0
            active = {
                **t.to_dict(),
                "current_price":   round(cp, 4),
                "mark_to_market":  t.mark_to_market(cp),
                "unrealized_pnl":  t.unrealized_pnl(cp),
                "tp_price":        tp_price,
                "sl_price":        sl_price,
                "tp_pnl":          tp_pnl,
                "sl_pnl":          sl_pnl,
                "progress_pct":    progress,
                "opposing_streak": self._opposing_streak,
            }

        # Exit reason breakdown
        exit_reasons: dict = {}
        for t in closed:
            r = t.exit_reason or "EXPIRE"
            exit_reasons[r] = exit_reasons.get(r, 0) + 1

        return {
            "initial_capital": self.initial_capital,
            "capital":         round(self.capital, 4),
            "equity":          equity,
            "realized_pnl":    round(realized_pnl, 4),
            "unrealized_pnl":  round(unrealized_pnl, 4),
            "total_pnl":       total_pnl,
            "total_pnl_pct":   round(total_pnl / self.initial_capital * 100, 2),
            "total_trades":    len(closed),
            "wins":            len(wins),
            "losses":          len(losses),
            "cancelled":       len([t for t in closed if t.status == "CANCELLED"]),
            "win_rate":        win_rate,
            "best_trade":      round(max((t.pnl for t in closed if t.pnl), default=0), 4),
            "worst_trade":     round(min((t.pnl for t in closed if t.pnl), default=0), 4),
            "avg_pnl":         round(realized_pnl / n_closed, 4) if n_closed else 0,
            "pnl_history":     self.pnl_history[-50:],
            "active_trade":    active,
            "trade_log":       [t.to_dict() for t in reversed(closed[-20:])],
            "signal_streak":   self._signal_streak,
            "exit_reasons":    exit_reasons,
        }
