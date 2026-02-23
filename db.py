"""
db.py — SQLite persistence for portfolio state and trade history.

Storage path:
  - Railway:  /data/portfolio.db   (Volume montado en /data)
  - Local:    ./data/portfolio.db  (creado automáticamente)

Se controla con la variable de entorno DATA_DIR.
"""

import json
import logging
import os
import sqlite3
import threading

log = logging.getLogger("db")

def _resolve_data_dir() -> str:
    """
    Resuelve el directorio de datos con fallback seguro.
    Railway con Volume: /data  (persistente)
    Sin Volume / local: ./data (efímero pero no crashea)
    """
    candidate = os.environ.get("DATA_DIR", "/data")
    try:
        os.makedirs(candidate, exist_ok=True)
        # Verificar que es escribible
        test = os.path.join(candidate, ".write_test")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        return candidate
    except OSError:
        fallback = "./data"
        os.makedirs(fallback, exist_ok=True)
        log.warning(f"DATA_DIR '{candidate}' no escribible, usando fallback: {fallback}")
        return fallback


DATA_DIR = _resolve_data_dir()
DB_PATH  = os.path.join(DATA_DIR, "portfolio.db")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")   # mejor concurrencia
        _conn.execute("PRAGMA synchronous=NORMAL") # buen balance durabilidad/velocidad
        _conn.row_factory = sqlite3.Row
        log.info(f"DB conectada: {DB_PATH}")
    return _conn


def init_db() -> None:
    """Crea las tablas si no existen. Llamar al inicio del servidor."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY,
            market      TEXT    NOT NULL,
            direction   TEXT    NOT NULL,
            entry_price REAL    NOT NULL,
            shares      REAL    NOT NULL,
            bet_size    REAL    NOT NULL,
            entry_time  TEXT    NOT NULL,
            exit_price  REAL,
            pnl         REAL,
            status      TEXT    NOT NULL DEFAULT 'OPEN',
            exit_reason TEXT,
            created_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS portfolio_state (
            id              INTEGER PRIMARY KEY CHECK (id = 1),
            capital         REAL    NOT NULL,
            initial_capital REAL    NOT NULL,
            pnl_history     TEXT    NOT NULL DEFAULT '[0.0]',
            trade_counter   INTEGER NOT NULL DEFAULT 0,
            updated_at      TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT DEFAULT (datetime('now')),
            note       TEXT
        );
    """)
    conn.commit()

    # Migration: add exit_reason column if missing (for existing DBs)
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN exit_reason TEXT")
        conn.commit()
    except Exception:
        pass  # Column already exists

    # Registrar sesión de inicio
    conn.execute("INSERT INTO sessions (note) VALUES ('server start')")
    conn.commit()
    log.info("DB inicializada correctamente")


# ── Escritura ─────────────────────────────────────────────────────────────────

def save_trade(trade) -> None:
    """
    Inserta o actualiza un trade en la DB.
    Llamar al abrir (status=OPEN) y al cerrar (status=WIN/LOSS/CANCELLED).
    """
    with _lock:
        _get_conn().execute(
            """
            INSERT OR REPLACE INTO trades
                (id, market, direction, entry_price, shares, bet_size,
                 entry_time, exit_price, pnl, status, exit_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.id, trade.market, trade.direction,
                trade.entry_price, trade.shares, trade.bet_size,
                trade.entry_time, trade.exit_price, trade.pnl, trade.status,
                getattr(trade, "exit_reason", None),
            ),
        )
        _get_conn().commit()


def save_portfolio_state(capital: float, initial_capital: float,
                         pnl_history: list, trade_counter: int) -> None:
    """Guarda el estado del portafolio (upsert en fila única id=1)."""
    with _lock:
        _get_conn().execute(
            """
            INSERT OR REPLACE INTO portfolio_state
                (id, capital, initial_capital, pnl_history, trade_counter, updated_at)
            VALUES (1, ?, ?, ?, ?, datetime('now'))
            """,
            (capital, initial_capital, json.dumps(pnl_history), trade_counter),
        )
        _get_conn().commit()


# ── Lectura ───────────────────────────────────────────────────────────────────

def load_state() -> dict:
    """
    Carga el estado guardado al arrancar.
    Retorna un dict con capital, pnl_history, trade_counter y closed_trades.
    Si no hay datos previos, retorna valores por defecto.
    """
    from simulator import Trade   # import local para evitar circular

    conn = _get_conn()

    # Estado del portafolio
    row = conn.execute(
        "SELECT * FROM portfolio_state WHERE id = 1"
    ).fetchone()

    if row:
        capital         = row["capital"]
        initial_capital = row["initial_capital"]
        pnl_history     = json.loads(row["pnl_history"])
        trade_counter   = row["trade_counter"]
        log.info(
            f"Estado cargado: capital=${capital:.2f}, "
            f"trades={trade_counter}, "
            f"P&L={pnl_history[-1]:+.4f}"
        )
    else:
        capital         = 100.0
        initial_capital = 100.0
        pnl_history     = [0.0]
        trade_counter   = 0
        log.info("Sin estado previo — iniciando desde cero ($100)")

    # Trades cerrados (historial completo)
    rows = conn.execute(
        "SELECT * FROM trades WHERE status != 'OPEN' ORDER BY id"
    ).fetchall()

    closed_trades = []
    for r in rows:
        row_dict = dict(r)
        t = Trade(
            id          = row_dict["id"],
            market      = row_dict["market"],
            direction   = row_dict["direction"],
            entry_price = row_dict["entry_price"],
            shares      = row_dict["shares"],
            bet_size    = row_dict["bet_size"],
            entry_time  = row_dict["entry_time"],
            exit_price  = row_dict["exit_price"],
            pnl         = row_dict["pnl"],
            status      = row_dict["status"],
            exit_reason = row_dict.get("exit_reason"),
        )
        closed_trades.append(t)

    if closed_trades:
        log.info(f"Trades históricos cargados: {len(closed_trades)}")

    return {
        "capital":         capital,
        "initial_capital": initial_capital,
        "pnl_history":     pnl_history,
        "trade_counter":   trade_counter,
        "closed_trades":   closed_trades,
    }


def db_path() -> str:
    return DB_PATH
