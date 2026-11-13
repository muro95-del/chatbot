import json
import types

import numpy as np
import pandas as pd
import pytest

from botfarm import Farm, FarmConfig, data, strategies
from botfarm.backtest import backtest
from botfarm.bot import Account, Bot


@pytest.fixture(autouse=True)
def no_network_news(monkeypatch):
    """Tests lesen nie echte Nachrichten aus dem Netz."""
    from botfarm import news

    monkeypatch.setattr(news, "fetch_feeds", lambda: ([], []))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


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


# ---------------------------------------------------------------- Echte Orders
class FakeExchange:
    """Boerse zum Testen: fuehrt Market-Orders zum gesetzten Kurs mit 0,1 % Gebuehr aus."""

    has = {"createMarketBuyOrderWithCost": True}

    def __init__(self, price=100.0, free=1000.0):
        self.price, self.free, self.sent = price, free, []

    def market(self, symbol):
        return {"base": "BTC", "quote": "USDT", "limits": {"amount": {"min": 0.0001}, "cost": {"min": 1.0}}}

    def amount_to_precision(self, symbol, amount):
        return f"{int(amount * 1e6) / 1e6:.6f}"

    def cost_to_precision(self, symbol, cost):
        return f"{cost:.4f}"

    def fetch_balance(self):
        return {"free": {"USDT": self.free}}

    def create_market_buy_order_with_cost(self, symbol, cost):
        self.sent.append(("buy", cost))
        return {"id": str(len(self.sent)), "filled": cost / self.price, "cost": cost,
                "fee": {"cost": cost * 0.001, "currency": "USDT"}}

    def create_order(self, symbol, kind, side, amount):
        self.sent.append((side, amount))
        cost = amount * self.price
        return {"id": str(len(self.sent)), "filled": amount, "cost": cost,
                "fee": {"cost": cost * 0.001, "currency": "USDT"}}


def farm_holding(units, halted="", symbol="BTC/USDT"):
    bots = [types.SimpleNamespace(account=types.SimpleNamespace(units=u), genome={"symbol": symbol}) for u in units]
    return types.SimpleNamespace(alive=bots, halted=halted, last_ts="2024-01-01")


P = {"BTC/USDT": 100.0}


def test_live_executor_follows_net_position_within_budget(tmp_path):
    from botfarm.live import LiveExecutor

    ex = FakeExchange(price=100.0)
    live = LiveExecutor(ex, "BTC/USDT", 50.0, str(tmp_path / "live.json"), real_orders=True)
    live.check_balance()

    (order,) = live.sync(farm_holding([0.2, 0.1]), P)  # Bots halten zusammen 0,3 = 30 USDT
    assert order["side"] == "buy" and live.state["units"]["BTC/USDT"] == pytest.approx(0.3)
    assert live.state["cash"] == pytest.approx(50 - 30 - 0.03)
    assert live.sync(farm_holding([0.2, 0.1]), P) == []  # nichts zu tun: keine zweite Order

    live.sync(farm_holding([5.0]), P)  # Buch will 500 USDT, Budget erlaubt nur den Rest
    assert live.state["cash"] >= 0 and live.state["units"]["BTC/USDT"] < 0.5
    spent = sum(c for side, c in ex.sent if side == "buy")
    assert spent <= 50.0

    live.sync(farm_holding([0.3], halted="Not-Aus"), P)  # Halt: alles verkaufen
    assert live.state["units"]["BTC/USDT"] == pytest.approx(0.0, abs=1e-6) and ex.sent[-1][0] == "sell"
    assert live.value(P) == pytest.approx(50 - live.state["fees"], abs=1e-3)

    resumed = LiveExecutor(ex, "BTC/USDT", 999.0, str(tmp_path / "live.json"), real_orders=True)
    assert resumed.state["budget"] == 50.0 and resumed.state["cash"] == pytest.approx(live.state["cash"])


def test_live_executor_handles_several_coins_and_ignores_leverage(tmp_path):
    from botfarm.live import LiveExecutor

    ex = FakeExchange(price=100.0)
    live = LiveExecutor(ex, ["BTC/USDT", "ETH/USDT"], 100.0, str(tmp_path / "live.json"), real_orders=True)
    prices = {"BTC/USDT": 100.0, "ETH/USDT": 100.0}
    bots = farm_holding([0.3]).alive + farm_holding([0.4], symbol="ETH/USDT").alive
    bots += farm_holding([-2.0], symbol="ETH/USDT").alive  # Short: nie als Spot-Order
    farm = types.SimpleNamespace(alive=bots, halted="", last_ts="t")
    orders = live.sync(farm, prices)
    assert {o["symbol"]: o["side"] for o in orders} == {"BTC/USDT": "buy", "ETH/USDT": "buy"}
    assert live.state["units"] == pytest.approx({"BTC/USDT": 0.3, "ETH/USDT": 0.4})

    farm.alive = farm_holding([0.9]).alive  # ETH raus, BTC aufstocken: erst verkaufen, dann kaufen
    orders = live.sync(farm, prices)
    assert [o["side"] for o in orders] == ["sell", "buy"]
    assert live.state["units"]["ETH/USDT"] == pytest.approx(0, abs=1e-6) and live.state["cash"] >= 0

    with pytest.raises(SystemExit):  # ein noch gehaltener Coin darf nicht aus der Liste fallen
        LiveExecutor(ex, ["ETH/USDT"], 100.0, str(tmp_path / "live.json"), real_orders=True)

    old = {"symbol": "BTC/USDT", "budget": 50, "cash": 20, "units": 0.3, "fees": 0, "orders": []}
    json.dump(old, open(tmp_path / "old.json", "w"))
    migrated = LiveExecutor(ex, "BTC/USDT", 50.0, str(tmp_path / "old.json"), real_orders=True)
    assert migrated.state["units"] == {"BTC/USDT": 0.3}  # Stand der Ein-Coin-Version wird uebernommen


def test_live_executor_dry_run_and_safety_checks(tmp_path, monkeypatch):
    from botfarm.live import LiveExecutor

    ex = FakeExchange(price=100.0, free=20.0)
    dry = LiveExecutor(ex, "BTC/USDT", 50.0, str(tmp_path / "dry.json"), real_orders=False)
    (order,) = dry.sync(farm_holding([0.2]), P)
    assert order["real"] is False and ex.sent == []  # Probelauf schickt nichts ab
    assert dry.sync(farm_holding([0.200001]), P) == []  # unter der Mindestgroesse

    real = LiveExecutor(ex, "BTC/USDT", 50.0, str(tmp_path / "real.json"), real_orders=True)
    with pytest.raises(SystemExit):
        real.check_balance()  # nur 20 frei, Bot braeuchte 50

    ex.create_market_buy_order_with_cost = lambda symbol, cost: {"id": "x"}
    ex.fetch_order = lambda oid, symbol: {"id": "x"}
    import botfarm.live as live_module
    monkeypatch.setattr(live_module.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError):
        real.sync(farm_holding([0.2]), P)  # unklare Ausfuehrung: anhalten statt raten
    assert real.state["units"] == {} and real.state["cash"] == 50.0


def test_live_command_runs_and_refuses_to_mix_with_paper(tmp_path, monkeypatch, capsys):
    from botfarm import cli
    import botfarm.live as live_module

    full = data.synthetic(2200, seed=5)
    visible = {"n": 1500}
    ex = FakeExchange(price=float(full["close"].iloc[1999]))
    monkeypatch.setattr(cli, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli.data, "from_exchange", lambda *a, **k: full.iloc[: visible["n"]].tail(1500).reset_index(drop=True))
    monkeypatch.setattr(live_module, "connect", lambda name, uta=False: ex)
    argv = ["live", "--exchange", "bitget", "--symbol", "BTC/USDT", "--capital", "100", "--once", "--real-orders"]

    cli.main(argv)
    visible["n"] = 2000
    cli.main(argv)
    real = json.load(open(tmp_path / "live.json"))
    farm = Farm.load(str(tmp_path / "state.json"))
    booked = sum(b.account.units for b in farm.alive)
    assert booked > 0 and farm.bar_count == 1  # verpasste Kerzen werden nicht nachgespielt
    assert real["units"]["BTC/USDT"] == pytest.approx(booked, rel=0.01)
    assert real["cash"] >= 0 and sum(c for s, c in ex.sent if s == "buy") <= 100.0
    assert real["cash"] + real["units"]["BTC/USDT"] * farm.last_price == pytest.approx(farm.total_wealth(), rel=0.01)
    cli.main(["status"])
    assert "Anteil des Bots" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["paper", "--exchange", "bitget", "--once"])


def test_unclear_order_locks_live_mode_until_checked(tmp_path, monkeypatch):
    from botfarm import cli
    import botfarm.live as live_module

    full = data.synthetic(2200, seed=5)
    visible = {"n": 1500}
    ex = FakeExchange(price=float(full["close"].iloc[1999]))
    ex.create_market_buy_order_with_cost = lambda symbol, cost: {"id": "x"}
    ex.fetch_order = lambda oid, symbol: {"id": "x"}
    monkeypatch.setattr(live_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli.data, "from_exchange", lambda *a, **k: full.iloc[: visible["n"]].tail(1500).reset_index(drop=True))
    monkeypatch.setattr(live_module, "connect", lambda name, uta=False: ex)
    argv = ["live", "--exchange", "bitget", "--symbol", "BTC/USDT", "--once", "--real-orders"]
    cli.main(argv)
    visible["n"] = 2000
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert (tmp_path / "BOERSE_PRUEFEN").exists()
    with pytest.raises(SystemExit, match="Gesperrt"):
        cli.main(argv)  # kein zweiter Kaufversuch, solange niemand nachgesehen hat


def test_connect_switches_bitget_to_unified_account(monkeypatch):
    import ccxt

    from botfarm import live

    class FakeBitget:
        requiredCredentials = {"password": True}

        def __init__(self, config):
            self.config, self.options, self.password = config, {}, None

        def load_markets(self):
            self.loaded_with = dict(self.options)

    monkeypatch.setattr(ccxt, "bitget", FakeBitget)
    monkeypatch.setenv("BOTFARM_API_KEY", "k")
    monkeypatch.setenv("BOTFARM_API_SECRET", "s")
    monkeypatch.delenv("BOTFARM_API_PASSPHRASE", raising=False)
    with pytest.raises(SystemExit, match="Passphrase"):
        live.connect("bitget", uta=True)
    monkeypatch.setenv("BOTFARM_API_PASSPHRASE", "p")
    ex = live.connect("bitget", uta=True)
    assert ex.password == "p" and ex.loaded_with == {"uta": True}
    assert live.connect("bitget").loaded_with == {}


def test_dry_run_state_is_archived_not_reused_for_real_money(tmp_path, monkeypatch):
    from botfarm import cli
    import botfarm.live as live_module

    full = data.synthetic(2200, seed=5)
    state_dir = tmp_path / "botfarm_state"
    ex = FakeExchange(price=float(full["close"].iloc[1499]))
    monkeypatch.setattr(cli, "STATE_DIR", str(state_dir))
    monkeypatch.setattr(cli.data, "from_exchange", lambda *a, **k: full.iloc[:1500].tail(1500).reset_index(drop=True))
    monkeypatch.setattr(live_module, "connect", lambda name, uta=False: ex)
    base = ["live", "--exchange", "bitget", "--symbol", "BTC/USDT", "--capital", "100", "--once"]

    cli.main(base)  # Probelauf
    assert json.load(open(state_dir / "live.json"))["real_orders"] is False
    cli.main(base + ["--real-orders"])  # Echtgeld: Probelauf-Stand wird verschoben, frischer Start
    archived = list(tmp_path.glob("botfarm_state_probelauf_*"))
    assert len(archived) == 1 and (archived[0] / "live.json").exists()
    real = json.load(open(state_dir / "live.json"))
    assert real["real_orders"] is True and real["cash"] + sum(real["units"].values()) * ex.price == pytest.approx(100, abs=1)
    with pytest.raises(SystemExit, match="ECHTEM"):
        cli.main(base)  # Probelauf darf einen Echtgeld-Stand nicht ueberschreiben


# ---------------- Echtzeit
def test_tick_kills_and_secures_between_candles():
    cfg = FarmConfig(fee=0, slippage=0, milestones=(0.5,), epoch_bars=10**9, global_stop=0.1)
    farm = run(one_bot_farm(cfg), frame(np.full(30, 100.0)))
    bot = farm.bots[0]
    assert bot.account.units == pytest.approx(1.0)

    farm.tick(160.0, "t1")  # +60 %: Stufe 0,5 sofort erreicht, ohne auf die Kerze zu warten
    assert farm.vault > 0 and farm.last_price == 160.0 and farm.tick_ts == "t1"
    secured = farm.vault
    farm.tick(160.0, "t2")
    assert farm.vault == secured  # derselbe Kurs sichert nicht doppelt

    farm.tick(100.0, "t3")  # Absturz zwischen zwei Kerzen: sofort raus
    assert bot.status == "dead" and bot.account.units == 0
    assert farm.treasury + farm.vault == pytest.approx(farm.total_wealth())
    assert sum(b.total_profit(100.0) for b in farm.bots) == pytest.approx(farm.total_wealth() - 100)

    state = Farm.from_dict(farm.to_dict())
    assert state.tick_ts == "t3"
    old = farm.to_dict(); old.pop("tick_ts")
    assert Farm.from_dict(old).tick_ts is None  # aeltere gespeicherte Staende laden weiter


def test_live_loop_follows_price_in_real_time(tmp_path, monkeypatch):
    from botfarm import cli
    import botfarm.live as live_module

    full = data.synthetic(2200, seed=5)
    visible = {"n": 1500}
    close = float(full["close"].iloc[1999])
    ex = FakeExchange(price=close)
    prices = iter([close, close * 0.5])  # zweiter Tick: Kurs halbiert sich
    clock = {"t": 0.0}
    monkeypatch.setattr(cli, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli.data, "from_exchange", lambda *a, **k: full.iloc[: visible["n"]].tail(1500).reset_index(drop=True))
    monkeypatch.setattr(cli.data, "last_prices", lambda *a: {"BTC/USDT": next(prices)})
    monkeypatch.setattr(cli.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(cli.time, "time", lambda: clock["t"])
    monkeypatch.setattr(live_module, "connect", lambda name, uta=False: ex)
    argv = ["live", "--exchange", "bitget", "--symbol", "BTC/USDT", "--capital", "100", "--real-orders"]

    cli.main(argv + ["--once"])
    visible["n"] = 2000
    cli.main(argv + ["--tick", "10", "--interval", "60"])  # endet von selbst: der Absturz loest den Stopp aus
    farm = Farm.load(str(tmp_path / "state.json"))
    real = json.load(open(tmp_path / "live.json"))
    assert farm.halted and not any(b.account.units for b in farm.bots)
    assert real["units"]["BTC/USDT"] == pytest.approx(0, abs=1e-6) and ex.sent[-1][0] == "sell"
    assert clock["t"] == 20  # reagiert beim zweiten Tick, nicht erst bei der naechsten Kerze


def test_live_mode_reads_real_account_balance(tmp_path, monkeypatch, capsys):
    from botfarm import cli
    import botfarm.live as live_module

    full = data.synthetic(2200, seed=5)
    ex = FakeExchange(price=float(full["close"].iloc[1499]), free=321.5)
    ex.fetch_balance = lambda: {"free": {"USDT": 321.5}, "total": {"USDT": 400.0, "BTC": 0.25}}
    monkeypatch.setattr(cli, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli.data, "from_exchange", lambda *a, **k: full.iloc[:1500].reset_index(drop=True))
    monkeypatch.setattr(live_module, "connect", lambda name, uta=False: ex)
    argv = ["live", "--exchange", "bitget", "--symbol", "BTC/USDT", "--capital", "100", "--once"]

    cli.main(argv)  # Probelauf reicht: der Kontostand wird nur gelesen
    acct = json.load(open(tmp_path / "live.json"))["account"]
    assert (acct["quote_free"], acct["quote_total"], acct["coins"]) == (321.5, 400.0, {"BTC": 0.25})
    assert "Bitget-Konto: 400.00 USDT" in capsys.readouterr().out and ex.sent == []

    def broken():
        raise ConnectionError("offline")

    ex.fetch_balance = broken
    cli.main(argv)  # Abruf scheitert: Bot laeuft weiter, alter Stand bleibt
    assert json.load(open(tmp_path / "live.json"))["account"]["quote_total"] == 400.0
    assert "Kontostand nicht abrufbar" in capsys.readouterr().out


# ---------------- Hebel, Short, mehrere Coins
def test_leveraged_and_short_account_math():
    long3 = Account(cash=100.0)
    long3.set_target(1, 100.0, 0.0, 0.0, leverage=3, side=1)
    assert long3.units == pytest.approx(3.0) and long3.margin == 100 and long3.cash == 0
    assert long3.equity(110.0) == pytest.approx(130.0)  # +10 % Kurs = +30 %
    assert long3.equity(60.0) == 0.0  # isoliert: nie unter null
    assert not long3.liquidated(70.0) and long3.liquidated(66.9)
    out = long3.withdraw(13.0, 110.0, 0.0, 0.0)  # Gewinn entnehmen schliesst einen Teil
    assert out == pytest.approx(13.0) and long3.equity(110.0) == pytest.approx(117.0)
    long3.set_target(0, 110.0, 0.0, 0.0)
    assert long3.cash == pytest.approx(117.0) and long3.units == 0 and long3.margin == 0

    short2 = Account(cash=100.0)
    short2.set_target(1, 100.0, 0.0, 0.0, leverage=2, side=-1)
    assert short2.units == pytest.approx(-2.0)
    assert short2.equity(90.0) == pytest.approx(120.0)  # Kurs faellt: Short gewinnt
    assert short2.liquidated(150.0) and not short2.liquidated(140.0)
    short2.funding(100.0, 0.001)
    assert short2.margin == pytest.approx(99.8)

    fees = Account(cash=100.0)
    fees.set_target(1, 100.0, 0.001, 0.0, leverage=5, side=1)  # Gebuehr faellt auf die volle Position an
    assert fees.margin == pytest.approx(99.5) and fees.units == pytest.approx(4.975)


def test_short_strategy_mirrors_long_and_backtest_liquidates():
    up, down = trending(1500, seed=1, drift=0.002), trending(1500, seed=1, drift=-0.002)
    base = {"family": "ema_cross", "params": {"fast": 5, "slow": 30, "trend": 0}}
    long_up = backtest(strategies.normalize(base), up, 0.001, 0.0005)
    short_down = backtest(strategies.normalize({**base, "side": "short"}), down, 0.001, 0.0005)
    short_up = backtest(strategies.normalize({**base, "side": "short"}), up, 0.001, 0.0005)
    assert long_up["return"] > 0 and short_down["return"] > 0 and short_down["return"] > short_up["return"]

    lev3 = backtest(strategies.normalize({**base, "leverage": 3}), up, 0.001, 0.0005)
    assert lev3["max_drawdown"] > long_up["max_drawdown"]  # Hebel vergroessert auch die Einbrueche

    crash = frame(np.concatenate([np.linspace(100, 104, 200), [80.0, 80.0]]))  # Absturz in einer Kerze
    crash["low"] = crash["close"] * 0.99
    wiped = backtest(strategies.normalize({**base, "leverage": 10}), crash, 0.0, 0.0)
    assert wiped["return"] == pytest.approx(-1.0)  # liquidiert: Einsatz weg, Backtest-Filter lehnt ab


def test_leveraged_bot_is_liquidated_but_loses_only_its_own_money():
    cfg = FarmConfig(fee=0, slippage=0, epoch_bars=10**9, kill_drawdown=0.99, global_stop=0.01, max_leverage=5,
                     milestones=(10.0,), giveback=0.99, funding_per_hour=0.0)
    farm = Farm(cfg)
    farm.treasury = 50.0  # die Haelfte bleibt ausserhalb der Position
    genome = strategies.normalize({**ALWAYS_IN, "symbol": "BTC/USDT", "leverage": 5})
    farm._add_bot((genome, {"return": 0.0, "max_drawdown": 0.0}), 50.0, parent=None, probation=False)
    farm._signal = lambda bot, df: np.ones(len(df))
    run(farm, frame(np.full(30, 100.0)))
    bot = farm.bots[0]
    assert bot.account.units == pytest.approx(2.5)

    farm.tick(110.0, "t")
    assert farm.equity_of(bot) == pytest.approx(75.0)
    farm.tick(79.0, "t")  # -21 % Kurs bei 5x: Margin aufgebraucht
    assert bot.status == "dead" and "liquidiert" in bot.death_reason
    assert farm.total_wealth() == pytest.approx(farm.treasury + farm.vault)
    assert farm.treasury >= 50.0  # der Rest der Farm ist unberuehrt


@pytest.mark.parametrize("seed", [1, 3])
def test_multi_coin_farm_with_leverage_keeps_the_books_balanced(seed):
    symbols = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
    cfg = FarmConfig(seed=seed, symbols=symbols, max_leverage=3, allow_short=True, candidates=12)
    frames = data.synthetic_frames(symbols, 4000, seed=seed)
    farm = Farm.start(cfg, frames, 1399)
    for i in range(1400, 4000):
        farm.step(frames, i)
        if i % 500 == 0:
            farm.tick({s: float(frames[s]["close"].iloc[i]) * 1.001 for s in symbols}, "t")
    assert sum(farm.profit_of(b) for b in farm.bots) == pytest.approx(farm.total_wealth() - cfg.start_capital)
    assert farm.vault == pytest.approx(sum(b.secured for b in farm.bots))
    assert farm.treasury >= 0 and all(b.account.cash >= -1e-9 and b.account.margin >= 0 for b in farm.bots)
    assert all(1 <= b.leverage <= 3 and b.genome["symbol"] in symbols for b in farm.bots)
    resumed = Farm.from_dict(json.loads(json.dumps(farm.to_dict())))
    assert resumed.total_wealth() == pytest.approx(farm.total_wealth()) and resumed.prices == farm.prices


def test_state_from_single_coin_version_still_loads():
    cfg = FarmConfig(fee=0, slippage=0, epoch_bars=10**9)
    farm = run(one_bot_farm(cfg), frame(np.linspace(100, 120, 40)))
    old = farm.to_dict()
    old["last_price"] = old.pop("prices")["BTC/USDT"]
    for key in ("symbols", "max_leverage", "allow_short", "bar_minutes", "funding_per_hour"):
        old["config"].pop(key)
    for bot in old["bots"]:
        bot["account"] = {"cash": bot["account"]["cash"], "units": bot["account"]["units"]}
    loaded = Farm.from_dict(old)
    assert loaded.total_wealth() == pytest.approx(farm.total_wealth())
    assert loaded.bots[0].account.margin == pytest.approx(loaded.bots[0].account.units * 120.0)


def test_cli_simulates_several_coins_and_blocks_leverage_with_real_money(capsys):
    from botfarm import cli

    cli.main(["simulate", "--bars", "3000", "--symbols", "BTC/USDT,ETH/USDT", "--max-leverage", "3", "--short"])
    out = capsys.readouterr().out
    assert "Gesamtvermoegen" in out and "ETH/USDT:" in out
    with pytest.raises(SystemExit, match="Spielgeld"):
        cli.main(["live", "--exchange", "bitget", "--max-leverage", "3", "--once"])


# ---------------- Nachrichten und Marktauswertung
RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Bitcoin surges to record high as ETF inflows soar</title><link>https://example.com/a</link>
<description>&lt;p&gt;Strong rally&lt;/p&gt;</description><pubDate>Fri, 13 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Solana exchange hacked, funds stolen</title><link>https://example.com/b</link>
<description>Exploit drains wallets</description><pubDate>Fri, 13 Nov 2026 11:00:00 GMT</pubDate></item>
<item><title>Developer conference announced</title><link>javascript:alert(1)</link><description></description></item>
</channel></rss>"""


def test_news_are_parsed_rated_and_decay(tmp_path, monkeypatch):
    from botfarm import news

    items = news.parse_feed(RSS, "Test")
    assert [i["title"][:7] for i in items] == ["Bitcoin", "Solana ", "Develop"]
    assert items[0]["summary"] == "Strong rally" and items[2]["link"] == ""  # nur http-Links, kein HTML
    with pytest.raises(ValueError):
        news.parse_feed('<!DOCTYPE x [<!ENTITY a "b">]><rss/>', "Test")

    coins = ["BTC", "ETH", "SOL"]
    news.rate_keywords(items, coins)
    assert items[0]["coins"] == {"BTC": 1.0} and items[1]["coins"] == {"SOL": -1.0} and items[2]["coins"] == {}
    now = items[1]["published"]
    mood = news.scores(items, coins, now)
    assert mood["BTC"] > 0.3 and mood["SOL"] == -0.5 and mood["ETH"] == 0
    assert abs(news.scores(items, coins, now + 24 * 3600)["SOL"]) < 0.05  # nach einem Tag fast vergessen

    monkeypatch.setattr(news, "fetch_feeds", lambda: (news.parse_feed(RSS, "Test"), ["Decrypt: Timeout"]))
    watcher = news.Watcher(str(tmp_path / "news.json"), ["BTC/USDT", "SOL/USDT"])
    assert watcher.due()
    watcher.update()
    watcher.update()  # dieselben Nachrichten werden nicht doppelt gezaehlt
    saved = json.load(open(tmp_path / "news.json"))
    assert len(saved["items"]) == 3 and saved["errors"] == ["Decrypt: Timeout"] and saved["method"] == "Stichwörter"
    assert not watcher.due() and len(news.Watcher(str(tmp_path / "news.json"), ["BTC/USDT"]).items) == 3


def test_claude_news_ratings_are_validated_and_fall_back(tmp_path, monkeypatch):
    import anthropic

    from botfarm import news

    payload = {"ratings": [
        {"index": 0, "coins": [{"coin": "BTC", "sentiment": 9}, {"coin": "PEPE", "sentiment": 1}],
         "importance": 5, "reason": "ETF-Zuflüsse"},
        {"index": 99, "coins": [], "importance": 1, "reason": "gibt es nicht"},
    ]}
    seen = {}

    class FakeMessages:
        def create(self, **kwargs):
            seen.update(kwargs)
            return types.SimpleNamespace(stop_reason="end_turn",
                                         content=[types.SimpleNamespace(type="text", text=json.dumps(payload))])

    monkeypatch.setattr(anthropic, "Anthropic", lambda: types.SimpleNamespace(beta=types.SimpleNamespace(messages=FakeMessages())))
    items = news.parse_feed(RSS, "Test")
    news.rate_claude(items, ["BTC", "SOL"])
    assert items[0]["coins"] == {"BTC": 1.0} and items[0]["importance"] == 1.0  # begrenzt, fremde Coins verworfen
    assert items[1]["coins"] == {} and items[1]["reason"] == "nicht bewertet"
    assert "untrusted" in seen["messages"][0]["content"] and seen["model"] == "claude-opus-5-5"

    def boom():
        raise RuntimeError("kein Guthaben")

    monkeypatch.setattr(anthropic, "Anthropic", boom)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(news, "fetch_feeds", lambda: (news.parse_feed(RSS, "Test"), []))
    mood = news.Watcher(str(tmp_path / "n.json"), ["BTC/USDT", "SOL/USDT"]).update()
    saved = json.load(open(tmp_path / "n.json"))
    assert mood["SOL"] < 0 and saved["errors"] == ["Claude: RuntimeError"]  # ohne Claude: Stichwoerter


def test_bad_news_blocks_longs_and_good_news_blocks_shorts():
    cfg = FarmConfig(fee=0, slippage=0, epoch_bars=10**9, allow_short=True)
    farm = one_bot_farm(cfg)
    short = strategies.normalize({**ALWAYS_IN, "symbol": "BTC/USDT", "side": "short"})
    farm._add_bot((short, {"return": 0.0, "max_drawdown": 0.0}), 100.0, parent=None, probation=False)
    long_bot, short_bot = farm.bots
    df = frame(np.full(40, 100.0))

    farm.news = {"BTC": -0.8}  # sehr schlechte Nachrichten
    for i in range(0, 10):
        farm.step(df, i)
    assert long_bot.account.units == 0 and short_bot.account.units < 0

    farm.news = {"BTC": 0.2}  # schwache Stimmung sperrt niemanden
    for i in range(10, 20):
        farm.step(df, i)
    assert long_bot.account.units > 0 and short_bot.account.units < 0

    farm.news = {"BTC": 0.9}  # sehr gute Nachrichten: Short raus
    farm.step(df, 20)
    assert long_bot.account.units > 0 and short_bot.account.units == 0
    assert any(e["type"] == "news" for e in farm.events)
    assert Farm.from_dict(farm.to_dict()).news == {"BTC": 0.9}


def test_market_analysis_reads_trend():
    up = trending(600, seed=1, drift=0.003, vol=0.004)
    down = trending(600, seed=2, drift=-0.003, vol=0.004)
    view = data.analyse({"BTC/USDT": up, "ETH/USDT": down}, bar_minutes=60)
    assert view["BTC/USDT"]["trend"] == "aufwärts" and view["ETH/USDT"]["trend"] == "abwärts"
    assert view["BTC/USDT"]["change_24h"] > 0 > view["ETH/USDT"]["change_24h"]
    assert view["BTC/USDT"]["rsi"] > 50 > view["ETH/USDT"]["rsi"]
