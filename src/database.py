from datetime import datetime, timezone


def _utcnow() -> datetime:
    """Helper: UTC-aware datetime (reemplaza datetime.utcnow() deprecado)."""
    return datetime.now(timezone.utc)
from sqlalchemy import (
    create_engine, Column, Integer, Float, String, DateTime, Boolean, Text, event
)
from sqlalchemy.orm import DeclarativeBase, Session
from src.config import DB_PATH


# WAL mode permite concurrent readers + 1 writer (bot escribe, API lee)
# timeout=30s evita "database is locked" en momentos de carga
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    connect_args={"timeout": 30, "check_same_thread": False},
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class Base(DeclarativeBase):
    pass


class Candle(Base):
    """Cada vela analizada por el bot."""
    __tablename__ = "candles"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False)
    timeframe = Column(String, nullable=False)
    timestamp = Column(DateTime, nullable=False, index=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    ema_fast = Column(Float)
    ema_slow = Column(Float)
    rsi = Column(Float)
    macd = Column(Float)
    macd_signal = Column(Float)
    macd_hist = Column(Float)
    bb_upper = Column(Float)
    bb_mid = Column(Float)
    bb_lower = Column(Float)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class Signal(Base):
    """Señales generadas (no todas derivan en trade)."""
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False)
    timestamp = Column(DateTime, nullable=False, index=True)
    signal_type = Column(String, nullable=False)  # "BUY" | "SELL" | "NONE"
    reason = Column(Text)
    close_price = Column(Float)
    rsi = Column(Float)
    ema_fast = Column(Float)
    ema_slow = Column(Float)
    acted_on = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class Trade(Base):
    """Cada trade ejecutado."""
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)       # "BUY" | "SELL"
    status = Column(String, default="OPEN")     # "OPEN" | "CLOSED"
    entry_price = Column(Float)
    exit_price = Column(Float)
    quantity = Column(Float)
    stop_loss = Column(Float)
    take_profit = Column(Float)
    entry_time = Column(DateTime)
    exit_time = Column(DateTime)
    pnl_usdt = Column(Float)
    pnl_pct = Column(Float)
    exit_reason = Column(String)                # "STOP_LOSS" | "TAKE_PROFIT" | "SIGNAL" | "TRAILING_STOP" | "MANUAL"
    order_id = Column(String)
    highest_price = Column(Float)               # para trailing stop: precio máximo alcanzado
    created_at = Column(DateTime(timezone=True), default=_utcnow)


def _migrate_add_columns():
    """Migración idempotente: añade columnas nuevas a tablas existentes."""
    with engine.connect() as conn:
        try:
            cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(trades)").fetchall()]
            if "highest_price" not in cols:
                conn.exec_driver_sql("ALTER TABLE trades ADD COLUMN highest_price FLOAT")
                conn.commit()
        except Exception:
            # Tabla no existe aún, create_all la creará con la columna
            pass


def init_db():
    Base.metadata.create_all(engine)
    _migrate_add_columns()


def get_session() -> Session:
    return Session(engine)
