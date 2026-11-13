"""Kursdaten: echte Boersendaten (ccxt), CSV-Datei oder kuenstliche Testdaten."""

from __future__ import annotations

import numpy as np
import pandas as pd

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def synthetic(bars: int = 6000, seed: int = 1, start_price: float = 100.0) -> pd.DataFrame:
    """Kuenstlicher Kursverlauf mit wechselnden Marktphasen. Nur zum Testen."""
    rng = np.random.default_rng(seed)
    drift = np.zeros(bars)
    vol = np.zeros(bars)
    i = 0
    while i < bars:
        length = int(rng.integers(100, 500))
        drift[i : i + length] = rng.choice([-0.0006, 0.0, 0.0003, 0.0009])
        vol[i : i + length] = rng.choice([0.004, 0.008, 0.014])
        i += length
    returns = drift + vol * rng.standard_normal(bars)
    close = start_price * np.exp(np.cumsum(returns))
    open_ = np.concatenate([[start_price], close[:-1]])
    spread = np.abs(vol * rng.standard_normal(bars)) * close
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=bars, freq="h"),
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.uniform(10, 100, bars),
        }
    )


def from_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV fehlt Spalten: {missing}")
    return df[COLUMNS].reset_index(drop=True)


def from_exchange(exchange: str, symbol: str, timeframe: str = "1h", bars: int = 1500) -> pd.DataFrame:
    """Oeffentliche Kursdaten einer Boerse laden. Braucht keinen API-Schluessel.

    Die letzte, noch laufende Kerze wird weggelassen.
    """
    import ccxt

    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    step = ex.parse_timeframe(timeframe) * 1000
    since = ex.milliseconds() - (bars + 1) * step
    rows: list[list] = []
    while len(rows) < bars + 1:
        chunk = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not chunk:
            break
        rows.extend(chunk)
        since = chunk[-1][0] + step
        if len(chunk) < 2:
            break
    df = pd.DataFrame(rows, columns=COLUMNS).drop_duplicates("timestamp")
    df = df[df["timestamp"] + step <= ex.milliseconds()]  # nur abgeschlossene Kerzen
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.tail(bars).reset_index(drop=True)


def frames_from_exchange(exchange: str, symbols, timeframe: str = "1h", bars: int = 1500) -> dict:
    """Kursdaten mehrerer Coins auf derselben Zeitachse. Nicht abrufbare Coins werden ausgelassen."""
    frames = {}
    for symbol in symbols:
        try:
            frames[symbol] = from_exchange(exchange, symbol, timeframe, bars)
        except Exception as exc:
            print(f"  {symbol} nicht abrufbar ({type(exc).__name__}), wird ausgelassen.")
    if not frames:
        raise ConnectionError("Kein Coin abrufbar")
    return align(frames)


def align(frames: dict) -> dict:
    """Nur Kerzen behalten, die es bei allen Coins gibt, damit Index i ueberall dieselbe Zeit meint."""
    common = None
    for df in frames.values():
        stamps = set(df["timestamp"])
        common = stamps if common is None else common & stamps
    return {s: df[df["timestamp"].isin(common)].sort_values("timestamp").reset_index(drop=True)
            for s, df in frames.items()}


def synthetic_frames(symbols, bars: int = 6000, seed: int = 1) -> dict:
    return {symbol: synthetic(bars, seed=seed + k) for k, symbol in enumerate(symbols)}


_tickers: dict = {}


def last_prices(exchange: str, symbols) -> dict:
    """Aktuelle Kurse (oeffentlich, ohne Schluessel)."""
    import ccxt

    if exchange not in _tickers:
        _tickers[exchange] = getattr(ccxt, exchange)({"enableRateLimit": True})
    tickers = _tickers[exchange].fetch_tickers(list(symbols))
    return {s: float(t["last"]) for s, t in tickers.items() if s in symbols and t.get("last")}
