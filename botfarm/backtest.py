"""Schneller Backtest einer Strategie auf vergangenen Kursen."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .strategies import signal


def backtest(genome: dict, df: pd.DataFrame, fee: float, slippage: float, warmup: int = 0) -> dict:
    """Ergebnis der Strategie, bewertet nur auf den Kerzen ab `warmup`."""
    close = df["close"].to_numpy(dtype=float)
    pos = signal(genome, df)
    pos[:warmup] = 0.0
    held = np.concatenate([[0.0], pos[:-1]])  # Position wirkt erst ab der naechsten Kerze
    price_ret = np.concatenate([[0.0], close[1:] / close[:-1] - 1])
    trades = np.abs(np.diff(np.concatenate([[0.0], pos])))
    returns = held * price_ret - trades * (fee + slippage)
    equity = np.cumprod(1 + returns)
    peak = np.maximum.accumulate(equity)
    max_dd = float(np.max(1 - equity / peak)) if len(equity) else 0.0
    total = float(equity[-1] - 1) if len(equity) else 0.0
    n_trades = int(trades.sum() // 2)
    std = returns.std()
    sharpe = float(returns.mean() / std * np.sqrt(len(returns))) if std > 0 else 0.0
    return {
        "return": total,
        "max_drawdown": max_dd,
        "trades": n_trades,
        "sharpe": sharpe,
        "score": total - 0.5 * max_dd,  # Gewinn zaehlt, Einbrueche kosten Punkte
    }
