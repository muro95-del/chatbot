"""Schneller Backtest einer Strategie auf vergangenen Kursen."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .bot import Account
from .strategies import signal


def backtest(genome: dict, df: pd.DataFrame, fee: float, slippage: float, warmup: int = 0,
             funding_per_bar: float = 0.0) -> dict:
    """Ergebnis der Strategie, bewertet nur auf den Kerzen ab `warmup`."""
    close = df["close"].to_numpy(dtype=float)
    pos = signal(genome, df)
    pos[:warmup] = 0.0
    leverage, side = int(genome.get("leverage", 1)), (-1 if genome.get("side") == "short" else 1)
    if leverage > 1 or side < 0:
        return _backtest_leveraged(df, pos, fee, slippage, warmup, funding_per_bar, leverage, side)
    held = np.concatenate([[0.0], pos[:-1]])  # Position wirkt erst ab der naechsten Kerze
    price_ret = np.concatenate([[0.0], close[1:] / close[:-1] - 1])
    trades = np.abs(np.diff(np.concatenate([[0.0], pos])))
    returns = held * price_ret - trades * (fee + slippage)
    equity = np.cumprod(1 + returns)
    n_trades = int(trades.sum() // 2)
    return _stats(equity, n_trades)


def _backtest_leveraged(df, pos, fee, slippage, warmup, funding_per_bar, leverage, side) -> dict:
    """Hebel und Short werden Kerze fuer Kerze mit demselben Konto gerechnet wie im Betrieb,
    einschliesslich Liquidation am unguenstigsten Kurs der Kerze."""
    close = df["close"].to_numpy(dtype=float)
    adverse = (df["low"] if side > 0 else df["high"]).to_numpy(dtype=float)
    acct = Account(cash=1.0)
    equity, trades = [], 0
    for t in range(warmup, len(close)):
        price = close[t]
        if acct.units:
            if acct.liquidated(adverse[t]):
                acct.liquidate()
                equity.append(acct.equity(price))
                break
            acct.funding(price, funding_per_bar)
        flat = acct.units == 0
        acct.set_target(pos[t], price, fee, slippage, leverage, side)
        trades += flat and acct.units != 0
        equity.append(acct.equity(price))
    return _stats(np.asarray(equity or [1.0]), int(trades))


def _stats(equity: np.ndarray, n_trades: int) -> dict:
    returns = np.diff(np.concatenate([[1.0], equity])) / np.concatenate([[1.0], equity[:-1]]).clip(min=1e-12)
    peak = np.maximum.accumulate(np.concatenate([[1.0], equity]))[1:]
    max_dd = float(np.max(1 - equity / peak)) if len(equity) else 0.0
    total = float(equity[-1] - 1) if len(equity) else 0.0
    std = returns.std()
    sharpe = float(returns.mean() / std * np.sqrt(len(returns))) if std > 0 else 0.0
    return {
        "return": total,
        "max_drawdown": max_dd,
        "trades": n_trades,
        "sharpe": sharpe,
        "score": total - 0.5 * max_dd,  # Gewinn zaehlt, Einbrueche kosten Punkte
    }
