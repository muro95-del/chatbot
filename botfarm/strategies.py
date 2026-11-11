"""Strategie-Familien. Ein "Genom" ist {"family": ..., "params": {...}}.

Jede Strategie liefert pro Kerze 1 (investiert) oder 0 (nicht investiert).
Alle Berechnungen nutzen nur vergangene Daten.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Parameter: (Minimum, Maximum, ist_ganzzahlig)
PARAM_SPACE: dict[str, dict[str, tuple[float, float, bool]]] = {
    "ema_cross": {"fast": (3, 60, True), "slow": (20, 300, True)},
    "rsi_reversion": {"period": (5, 30, True), "buy_below": (15, 45, False), "sell_above": (50, 85, False)},
    "donchian": {"entry": (10, 200, True), "exit": (5, 100, True)},
    "bollinger": {"period": (10, 100, True), "width": (1.0, 3.5, False)},
}
COMMON: dict[str, tuple[float, float, bool]] = {"trend": (0, 400, True)}  # 0 = kein Trendfilter


def _space(family: str) -> dict[str, tuple[float, float, bool]]:
    return {**PARAM_SPACE[family], **COMMON}


def normalize(genome: dict) -> dict:
    """Genom pruefen und alle Werte in den erlaubten Bereich bringen."""
    family = genome.get("family")
    if family not in PARAM_SPACE:
        raise ValueError(f"Unbekannte Strategie-Familie: {family!r}")
    params = {}
    given = genome.get("params", {})
    for name, (lo, hi, is_int) in _space(family).items():
        value = float(given.get(name, lo))
        if not np.isfinite(value):
            raise ValueError(f"Ungueltiger Wert fuer {name}")
        value = min(max(value, lo), hi)
        params[name] = int(round(value)) if is_int else round(value, 3)
    if family == "ema_cross" and params["fast"] >= params["slow"]:
        params["slow"] = min(300, params["fast"] * 2 + 1)
    if 0 < params["trend"] < 20:
        params["trend"] = 0
    return {"family": family, "params": params}


def random_genome(rng: np.random.Generator) -> dict:
    family = str(rng.choice(list(PARAM_SPACE)))
    params = {n: rng.uniform(lo, hi) for n, (lo, hi, _) in PARAM_SPACE[family].items()}
    params["trend"] = 0 if rng.random() < 0.5 else rng.uniform(50, 400)
    return normalize({"family": family, "params": params})


def mutate(genome: dict, rng: np.random.Generator, strength: float = 0.25) -> dict:
    """Leicht veraenderte Kopie einer Strategie."""
    params = dict(genome["params"])
    for name, (lo, hi, _) in _space(genome["family"]).items():
        if rng.random() < 0.7:
            params[name] = params[name] + rng.normal(0, strength) * (hi - lo) * 0.5
    if rng.random() < 0.15:
        params["trend"] = 0 if params["trend"] else rng.uniform(50, 400)
    return normalize({"family": genome["family"], "params": params})


def _latch(enter: pd.Series, leave: pd.Series) -> pd.Series:
    """1 ab Einstiegssignal, 0 ab Ausstiegssignal, dazwischen unveraendert."""
    state = pd.Series(np.nan, index=enter.index)
    state[enter] = 1.0
    state[leave] = 0.0
    return state.ffill().fillna(0.0)


def signal(genome: dict, df: pd.DataFrame) -> np.ndarray:
    """Zielposition (0 oder 1) am Schluss jeder Kerze."""
    p = genome["params"]
    close = df["close"].astype(float).reset_index(drop=True)
    family = genome["family"]

    if family == "ema_cross":
        fast = close.ewm(span=p["fast"], adjust=False).mean()
        slow = close.ewm(span=p["slow"], adjust=False).mean()
        pos = (fast > slow).astype(float)
        pos[: p["slow"]] = 0.0
    elif family == "rsi_reversion":
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / p["period"], adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / p["period"], adjust=False).mean()
        rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
        rsi = rsi.fillna(50)
        rsi[: p["period"] * 3] = 50
        pos = _latch(rsi < p["buy_below"], rsi > p["sell_above"])
    elif family == "donchian":
        upper = close.rolling(p["entry"]).max().shift(1)
        lower = close.rolling(p["exit"]).min().shift(1)
        pos = _latch(close > upper, close < lower)
    elif family == "bollinger":
        mid = close.rolling(p["period"]).mean()
        std = close.rolling(p["period"]).std(ddof=0)
        pos = _latch(close < mid - p["width"] * std, close > mid)
    else:
        raise ValueError(family)

    if p["trend"]:
        pos = pos * (close > close.rolling(p["trend"]).mean()).astype(float)
    return pos.to_numpy(dtype=float, copy=True)


def describe(genome: dict) -> str:
    params = ", ".join(f"{k}={v}" for k, v in genome["params"].items())
    return f"{genome['family']}({params})"
