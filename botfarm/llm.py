"""Claude schlaegt neue Strategie-Varianten vor (optional, braucht ANTHROPIC_API_KEY).

Claude liefert nur Parameter innerhalb der festen Strategie-Familien, keinen Code.
Jeder Vorschlag muss danach denselben Backtest bestehen wie alle anderen Kandidaten.
"""

from __future__ import annotations

import json

import pandas as pd

from .strategies import COMMON, PARAM_SPACE, normalize

MODEL = "claude-opus-5-5"

SCHEMA = {
    "type": "object",
    "properties": {
        "genomes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "family": {"type": "string", "enum": list(PARAM_SPACE)},
                    "params": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}, "value": {"type": "number"}},
                            "required": ["name", "value"],
                            "additionalProperties": False,
                        },
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["family", "params", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["genomes"],
    "additionalProperties": False,
}


def _market_summary(window: pd.DataFrame) -> dict:
    close = window["close"].astype(float)
    returns = close.pct_change().dropna()
    return {
        "bars": len(close),
        "total_return": round(float(close.iloc[-1] / close.iloc[0] - 1), 4),
        "last_200_bars_return": round(float(close.iloc[-1] / close.iloc[-min(200, len(close))] - 1), 4),
        "volatility_per_bar": round(float(returns.std()), 5),
        "max_drawdown": round(float((1 - close / close.cummax()).max()), 4),
    }


def propose(parent: dict | None, parent_stats: dict | None, window: pd.DataFrame, count: int = 4) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic()
    prompt = (
        "You design parameter sets for simple long-only spot trading strategies. The proposals are "
        "backtested afterwards on the same market window, and only profitable ones with limited "
        "drawdown are used, so favour robust settings over extreme ones.\n\n"
        f"Strategy families and parameter ranges (min, max, integer?): {json.dumps(PARAM_SPACE)}\n"
        f"Every family also takes: {json.dumps(COMMON)} - 'trend' is the length of a moving-average "
        "trend filter; 0 turns it off.\n"
        f"Recent market window: {json.dumps(_market_summary(window))}\n"
        f"Parent strategy: {json.dumps(parent)}\n"
        f"Parent backtest on this window: {json.dumps(parent_stats)}\n\n"
        f"Propose {count} different parameter sets likely to do well in this market. Include every "
        "parameter of the chosen family plus 'trend'."
    )
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"Antwort unvollstaendig: {response.stop_reason}")
    text = next(b.text for b in response.content if b.type == "text")
    genomes = []
    for item in json.loads(text)["genomes"]:
        try:
            genomes.append(normalize({"family": item["family"], "params": {p["name"]: p["value"] for p in item["params"]}}))
        except (ValueError, KeyError, TypeError):
            continue
    return genomes
