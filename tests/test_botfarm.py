import json
import types

import numpy as np
import pandas as pd
import pytest

from botfarm import Farm, FarmConfig, data, strategies
from botfarm.backtest import backtest
from botfarm.bot import Account, Bot


def frame(prices):
    prices = np.asarray(prices, dtype=float)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=len(prices), freq="h"),
            "open": prices, "high": prices, "low": prices, "close": prices, "volume": 1.0,
        }
    )


def trending(bars=4000, seed=0, drift=0.0012, vol=0.006):
    rng = np.random.default_rng(seed)
    return frame(100 * np.exp(np.cumsum(drift + vol * rng.standard_normal(bars))))


ALWAYS_IN = {"family": "ema_cross", "params": {"fast": 3, "slow": 20, "trend": 0}}


def one_bot_farm(config, capital=100.0, genome=ALWAYS_IN):
    """Farm mit einem Bot, der immer voll investiert bleibt."""
    farm = Farm(config)
    farm.treasury = 0.0
    farm._signal = lambda bot, df: np.ones(len(df))
    farm._add_bot((genome, {"return": 0.0, "max_drawdown": 0.0}), capital, parent=None, probation=False)
    return farm


def run(farm, df, start=0):
    for i in range(start, len(df)):
        farm.step(df, i)
    return farm


# ---------------------------------------------------------------- Strategien
@pytest.mark.parametrize("family", list(strategies.PARAM_SPACE))
def test_signals_use_only_past_data(family):
    df = data.synthetic(900, seed=3)
    rng = np.random.default_rng(1)
    for _ in range(5):
        genome = strategies.random_genome(rng)
        genome = strategies.normalize({"family": family, "params": genome["params"] if genome["family"] == family else {}})
        full = strategies.signal(genome, df)
        part = strategies.signal(genome, df.iloc[:600])
        assert np.array_equal(full[:600], part)
        assert set(np.unique(full)) <= {0.0, 1.0}


def test_normalize_clips_and_rejects():
    g = strategies.normalize({"family": "ema_cross", "params": {"fast": 500, "slow": 1, "trend": 5}})
    assert g["params"]["fast"] == 60 and g["params"]["slow"] > g["params"]["fast"] and g["params"]["trend"] == 0
    with pytest.raises(ValueError):
        strategies.normalize({"family": "martingale", "params": {}})
    with pytest.raises(ValueError):
        strategies.normalize({"family": "donchian", "params": {"entry": float("nan")}})


def test_backtest_charges_costs():
    df = trending(1500)
    free = backtest(ALWAYS_IN, df, 0.0, 0.0)
    paid = backtest(ALWAYS_IN, df, 0.001, 0.0005)
    assert paid["trades"] == free["trades"] > 0
    assert paid["return"] < free["return"]


# ---------------------------------------------------------------- Konto
def test_account_trading_and_withdraw():
    acct = Account(cash=100.0)
    acct.set_target(1, 10.0, 0.001, 0.0)
    assert acct.cash == 0 and acct.units == pytest.approx(9.99)
    out = acct.withdraw(20.0, 10.0, 0.001, 0.0)
    assert out == pytest.approx(20.0) and acct.cash == pytest.approx(0.0, abs=1e-9)
    acct.set_target(0, 10.0, 0.001, 0.0)
    assert acct.units == 0 and acct.cash < 80.0
    assert Account(cash=5.0).withdraw(50.0, 10.0, 0, 0) == 5.0


# ---------------------------------------------------------------- Gewinnsicherung
def test_milestone_secures_between_min_and_max():
    cfg = FarmConfig(fee=0, slippage=0, milestones=(3.0, 10.0), giveback=0.99, epoch_bars=10**9,
                     kill_drawdown=0.9)
    prices = np.concatenate([np.full(30, 100.0), np.linspace(100, 420, 200)])
    farm = run(one_bot_farm(cfg), frame(prices))
    bot = farm.bots[0]
    secures = [e for e in farm.events if e["type"] == "secure"]
    assert len(secures) == 1 and bot.milestone_idx == 1
    profit_in_bot_at_hit = secures[0]["amount"] / cfg.secure_min
    assert 300 <= profit_in_bot_at_hit < 310  # gute Aussicht: nur der Mindestanteil wird gesichert
    assert farm.vault == pytest.approx(secures[0]["amount"]) == pytest.approx(bot.secured)
    assert farm.total_wealth() == pytest.approx(100 + bot.total_profit(farm.last_price))


def test_weak_outlook_secures_more():
    cfg = FarmConfig(fee=0, slippage=0, milestones=(1.0,), giveback=0.99, epoch_bars=10**9, kill_drawdown=0.9,
                     outlook_bars=20)
    # Hoch auf +95 %, zurueck, dann langsam ueber die Stufe: Aussicht ist nur maessig.
    prices = np.concatenate([np.full(30, 100.0), np.linspace(100, 195, 60), np.linspace(195, 180, 30),
                             np.linspace(180, 201, 400)])
    farm = run(one_bot_farm(cfg), frame(prices))
    amount = next(e for e in farm.events if e["type"] == "secure")["amount"]
    assert amount > cfg.secure_min * 101 * 1.5


def test_giveback_guard_secures_when_profit_falls_back():
    cfg = FarmConfig(fee=0, slippage=0, milestones=(10.0,), epoch_bars=10**9, kill_drawdown=0.9)
    prices = np.concatenate([np.full(30, 100.0), np.linspace(100, 180, 80), np.linspace(180, 150, 60)])
    farm = run(one_bot_farm(cfg), frame(prices))
    secures = [e for e in farm.events if e["type"] == "secure"]
    assert len(secures) == 1 and "zurueck" in secures[0]["message"]
    # Hoechststand +80, Sicherung bei 30 % Ruecklauf (Gewinn rund 56): die Haelfte davon wandert in den Tresor
    assert 27 < farm.vault < 28.1


def test_secured_money_survives_a_crash():
    cfg = FarmConfig(fee=0, slippage=0, milestones=(0.5,), epoch_bars=10**9)
    prices = np.concatenate([np.full(30, 100.0), np.linspace(100, 200, 100), np.linspace(200, 40, 100)])
    farm = run(one_bot_farm(cfg), frame(prices))
    assert farm.bots[0].status == "dead"
    assert farm.vault > 0 and farm.total_wealth() >= farm.vault


# ---------------------------------------------------------------- Leben, Sterben, Vermehrung
def test_drawdown_kills_bot_and_returns_money():
    cfg = FarmConfig(fee=0, slippage=0, epoch_bars=10**9, global_stop=0.1)
    prices = np.concatenate([np.full(30, 100.0), np.linspace(100, 110, 20), np.linspace(110, 60, 60)])
    farm = run(one_bot_farm(cfg), frame(prices))
    bot = farm.bots[0]
    assert bot.status == "dead" and "Verlust" in bot.death_reason
    assert not farm.alive and farm.treasury + farm.vault == pytest.approx(farm.total_wealth())
    assert 70 < farm.treasury < 80  # bei rund -30 % vom Hoechststand war Schluss


def test_global_stop_halts_everything():
    cfg = FarmConfig(fee=0, slippage=0, epoch_bars=10**9, kill_drawdown=0.95, global_stop=0.6)
    prices = np.concatenate([np.full(30, 100.0), np.linspace(100, 101, 20), np.linspace(101, 30, 100)])
    farm = run(one_bot_farm(cfg), frame(prices))
    assert farm.halted and all(b.account.units == 0 for b in farm.bots)
    wealth = farm.total_wealth()
    farm.step(frame(prices * 0.5), 10)
    assert farm.total_wealth() == wealth  # nach dem Halt passiert nichts mehr


def test_profitable_bot_funds_children_from_profit_only():
    cfg = FarmConfig(max_bots=4, probation_bars=48, epoch_bars=96)
    df = trending(4000, seed=2)
    farm = Farm.start(cfg, df, 1400)
    run(farm, df, 1401)
    children = [b for b in farm.bots if b.parent_id is not None]
    assert children, "im klaren Aufwaertstrend muss es Nachwuchs geben"
    births = {e["bot"]: e["bar"] for e in farm.events if e["type"] == "birth"}
    assert all(c.status != "probation" or c.account.units == 0 for c in children)
    assert len(farm.alive) <= cfg.max_bots
    assert max(b.generation for b in farm.bots) >= 1
    first = farm.bots[0]
    assert first.given >= 0 and births[children[0].id] > 0


def test_failed_probation_returns_escrow_to_parent():
    cfg = FarmConfig(fee=0, slippage=0, probation_bars=5, epoch_bars=10**9, kill_drawdown=0.9, global_stop=0.1)
    farm = one_bot_farm(cfg)
    parent = farm.bots[0]
    df = frame(np.concatenate([np.full(30, 100.0), np.linspace(100, 90, 20)]))
    run(farm, df.iloc[:30])
    out = parent.account.withdraw(20.0, 100.0, 0, 0)
    parent.given += out
    farm._after_flow(parent, 100.0, -out)
    child = farm._add_bot((ALWAYS_IN, {"return": 0.0, "max_drawdown": 0.0}), out, parent=parent)
    run(farm, df, 30)
    assert child.status == "dead" and "Probezeit" in child.death_reason
    assert parent.given == pytest.approx(0.0)
    assert farm.total_wealth() == pytest.approx(parent.account.equity(farm.last_price))


@pytest.mark.parametrize("seed", [1, 2, 5, 7])
def test_no_money_is_created_or_lost(seed):
    cfg = FarmConfig(seed=seed)
    df = data.synthetic(6000, seed=seed)
    farm = Farm.start(cfg, df, 1399)
    run(farm, df, 1400)
    price = farm.last_price
    assert sum(b.total_profit(price) for b in farm.bots) == pytest.approx(farm.total_wealth() - cfg.start_capital)
    assert farm.vault == pytest.approx(sum(b.secured for b in farm.bots))
    assert farm.vault >= 0 and farm.treasury >= 0
    assert all(b.account.cash >= -1e-9 and b.account.units >= 0 for b in farm.bots)


def test_save_and_load_continue_identically(tmp_path):
    cfg = FarmConfig(seed=5)
    df = data.synthetic(5000, seed=5)
    straight = run(Farm.start(cfg, df, 1399), df, 1400)
    farm = Farm.start(cfg, df, 1399)
    for i in range(1400, 3000):
        farm.step(df, i)
    path = str(tmp_path / "state.json")
    farm.save(path)
    resumed = run(Farm.load(path), df, 3000)
    assert resumed.total_wealth() == pytest.approx(straight.total_wealth())
    assert resumed.vault == pytest.approx(straight.vault)
    assert [b.status for b in resumed.bots] == [b.status for b in straight.bots]


# ---------------------------------------------------------------- Claude-Vorschlaege
def test_llm_proposals_are_validated(monkeypatch):
    import anthropic

    from botfarm import llm

    payload = {"genomes": [
        {"family": "ema_cross", "params": [{"name": "fast", "value": 9999}, {"name": "slow", "value": 50},
                                           {"name": "trend", "value": 0}], "rationale": "x"},
        {"family": "donchian", "params": [{"name": "entry", "value": 55}, {"name": "exit", "value": 20},
                                          {"name": "trend", "value": 200}], "rationale": "y"},
    ]}
    seen = {}

    class FakeMessages:
        def create(self, **kwargs):
            seen.update(kwargs)
            return types.SimpleNamespace(stop_reason="end_turn",
                                         content=[types.SimpleNamespace(type="text", text=json.dumps(payload))])

    fake = types.SimpleNamespace(beta=types.SimpleNamespace(messages=FakeMessages()))
    monkeypatch.setattr(anthropic, "Anthropic", lambda: fake)
    genomes = llm.propose(ALWAYS_IN, {"return": 0.1}, data.synthetic(500), count=2)
    assert [g["family"] for g in genomes] == ["ema_cross", "donchian"]
    assert genomes[0]["params"]["fast"] == 60  # ausserhalb des Bereichs wird begrenzt
    assert seen["model"] == "claude-opus-5-5" and "thinking" not in seen


def test_farm_keeps_running_when_llm_fails(monkeypatch):
    from botfarm import llm

    def boom(*a, **k):
        raise RuntimeError("kein Schluessel")

    monkeypatch.setattr(llm, "propose", boom)
    df = trending(2500, seed=2)
    farm = Farm.start(FarmConfig(use_llm=True), df, 1400)
    assert farm.bots and any(e["type"] == "llm_error" for e in farm.events)


# ---------------------------------------------------------------- Dauerbetrieb
def test_paper_loop_resumes_and_obeys_stop_file(tmp_path, monkeypatch, capsys):
    from botfarm import cli

    full = data.synthetic(2200, seed=5)
    visible = {"n": 1500}
    monkeypatch.setattr(cli, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli.data, "from_exchange", lambda *a, **k: full.iloc[: visible["n"]].tail(1500).reset_index(drop=True))
    argv = ["paper", "--exchange", "kraken", "--once"]

    cli.main(argv)
    assert Farm.load(str(tmp_path / "state.json")).bar_count == 0
    visible["n"] = 2000
    cli.main(argv)
    farm = Farm.load(str(tmp_path / "state.json"))
    assert farm.bar_count == 500 and farm.last_ts == str(full["timestamp"].iloc[1999])
    cli.main(argv)  # keine neuen Kerzen: nichts wird doppelt verarbeitet
    assert Farm.load(str(tmp_path / "state.json")).bar_count == 500

    (tmp_path / "STOP").write_text("")
    visible["n"] = 2100
    cli.main(argv)
    farm = Farm.load(str(tmp_path / "state.json"))
    assert farm.halted and all(b.account.units == 0 for b in farm.bots)
    cli.main(["status"])
    assert "ANGEHALTEN" in capsys.readouterr().out
