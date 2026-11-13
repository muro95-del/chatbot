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


def farm_holding(units, halted=""):
    bots = [types.SimpleNamespace(account=types.SimpleNamespace(units=u)) for u in units]
    return types.SimpleNamespace(alive=bots, halted=halted, last_ts="2024-01-01")


def test_live_executor_follows_net_position_within_budget(tmp_path):
    from botfarm.live import LiveExecutor

    ex = FakeExchange(price=100.0)
    live = LiveExecutor(ex, "BTC/USDT", 50.0, str(tmp_path / "live.json"), real_orders=True)
    live.check_balance()

    order = live.sync(farm_holding([0.2, 0.1]), 100.0)  # Bots halten zusammen 0,3 = 30 USDT
    assert order["side"] == "buy" and live.state["units"] == pytest.approx(0.3)
    assert live.state["cash"] == pytest.approx(50 - 30 - 0.03)
    assert live.sync(farm_holding([0.2, 0.1]), 100.0) is None  # nichts zu tun: keine zweite Order

    live.sync(farm_holding([5.0]), 100.0)  # Buch will 500 USDT, Budget erlaubt nur den Rest
    assert live.state["cash"] >= 0 and live.state["units"] < 0.5
    spent = sum(c for side, c in ex.sent if side == "buy")
    assert spent <= 50.0

    live.sync(farm_holding([0.3], halted="Not-Aus"), 100.0)  # Halt: alles verkaufen
    assert live.state["units"] == pytest.approx(0.0, abs=1e-6) and ex.sent[-1][0] == "sell"
    assert live.value(100.0) == pytest.approx(50 - live.state["fees"], abs=1e-3)

    resumed = LiveExecutor(ex, "BTC/USDT", 999.0, str(tmp_path / "live.json"), real_orders=True)
    assert resumed.state["budget"] == 50.0 and resumed.state["cash"] == pytest.approx(live.state["cash"])
    with pytest.raises(SystemExit):
        LiveExecutor(ex, "ETH/USDT", 50.0, str(tmp_path / "live.json"), real_orders=True)


def test_live_executor_dry_run_and_safety_checks(tmp_path, monkeypatch):
    from botfarm.live import LiveExecutor

    ex = FakeExchange(price=100.0, free=20.0)
    dry = LiveExecutor(ex, "BTC/USDT", 50.0, str(tmp_path / "dry.json"), real_orders=False)
    order = dry.sync(farm_holding([0.2]), 100.0)
    assert order["real"] is False and ex.sent == []  # Probelauf schickt nichts ab
    assert dry.sync(farm_holding([0.200001]), 100.0) is None  # unter der Mindestgroesse

    real = LiveExecutor(ex, "BTC/USDT", 50.0, str(tmp_path / "real.json"), real_orders=True)
    with pytest.raises(SystemExit):
        real.check_balance()  # nur 20 frei, Bot braeuchte 50

    ex.create_market_buy_order_with_cost = lambda symbol, cost: {"id": "x"}
    ex.fetch_order = lambda oid, symbol: {"id": "x"}
    import botfarm.live as live_module
    monkeypatch.setattr(live_module.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError):
        real.sync(farm_holding([0.2]), 100.0)  # unklare Ausfuehrung: anhalten statt raten
    assert real.state["units"] == 0.0 and real.state["cash"] == 50.0


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
    assert real["units"] == pytest.approx(booked, rel=0.01)
    assert real["cash"] >= 0 and sum(c for s, c in ex.sent if s == "buy") <= 100.0
    assert real["cash"] + real["units"] * farm.last_price == pytest.approx(farm.total_wealth(), rel=0.01)
    cli.main(["status"])
    assert "Echtes Konto" in capsys.readouterr().out
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
    assert real["real_orders"] is True and real["cash"] + real["units"] * ex.price == pytest.approx(100, abs=1)
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
    monkeypatch.setattr(cli.data, "last_price", lambda *a: next(prices))
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
    assert real["units"] == pytest.approx(0, abs=1e-6) and ex.sent[-1][0] == "sell"
    assert clock["t"] == 20  # reagiert beim zweiten Tick, nicht erst bei der naechsten Kerze
