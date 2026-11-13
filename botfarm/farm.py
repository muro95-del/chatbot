"""Die Farm: laesst Bots handeln, sichert Gewinne, gruendet Kinder, beendet Verlierer."""

from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from . import strategies
from .backtest import backtest
from .bot import Account, Bot
from .config import FarmConfig

WARMUP = 400  # laengster Indikator; so viele Kerzen Vorlauf braucht ein Backtest


class Farm:
    def __init__(self, config: FarmConfig | None = None):
        self.config = config or FarmConfig()
        self.bots: list[Bot] = []
        self.vault = 0.0  # gesicherter Gewinn; wird nie wieder gehandelt
        self.treasury = self.config.start_capital  # freies Geld ohne Bot
        self.bar_count = 0
        self.last_ts: str | None = None
        self.prices: dict[str, float] = {}  # letzter Kurs je Coin
        self.tick_ts: str | None = None
        self.news: dict[str, float] = {}  # Nachrichten-Stimmung je Coin (BTC, ETH, ...)
        self.market: dict[str, dict] = {}  # Marktauswertung je Handelspaar
        self.halted = ""
        self.next_id = 1
        self.events: list[dict] = []
        self.wealth_log: list[dict] = []
        self._signals: dict[int, tuple[tuple, np.ndarray]] = {}
        self._llm_failed = False

    # ------------------------------------------------------------------ Uebersicht
    @property
    def alive(self) -> list[Bot]:
        return [b for b in self.bots if b.alive]

    @property
    def last_price(self) -> float:
        """Kurs des ersten Coins (bei nur einem Coin: der Kurs)."""
        return self.prices.get(self.config.symbols[0], 0.0)

    def symbol_of(self, bot: Bot) -> str:
        return bot.genome.get("symbol") or self.config.symbols[0]

    def price_of(self, bot: Bot) -> float:
        return self.prices.get(self.symbol_of(bot), 0.0)

    def equity_of(self, bot: Bot) -> float:
        return bot.account.equity(self.price_of(bot))

    def profit_of(self, bot: Bot) -> float:
        return bot.total_profit(self.price_of(bot))

    def total_wealth(self, price: float | None = None) -> float:
        return self.vault + self.treasury + sum(
            b.account.equity(self.price_of(b) if price is None else price) for b in self.alive)

    def _frames(self, frames) -> dict:
        return frames if isinstance(frames, dict) else {self.config.symbols[0]: frames}

    def _event(self, kind: str, message: str, bot: Bot | None = None, **extra) -> None:
        self.events.append(
            {"bar": self.bar_count, "ts": self.last_ts, "type": kind, "bot": bot.id if bot else None,
             "message": message, **extra}
        )

    # ------------------------------------------------------------------ ein Schritt
    def step(self, frames, i: int) -> None:
        """Die abgeschlossene Kerze `i` verarbeiten. `frames`: Kursdaten je Coin (gleiche Zeitachse)."""
        if self.halted:
            return
        cfg = self.config
        frames = self._frames(frames)
        first = next(iter(frames.values()))
        for symbol, df in frames.items():
            self.prices[symbol] = float(df["close"].iloc[i])
        self.last_ts = str(first["timestamp"].iloc[i])
        self.bar_count += 1

        for bot in self.alive:
            df = frames.get(self.symbol_of(bot))
            if df is None:
                continue
            price = self.price_of(bot)
            acct = bot.shadow if bot.status == "probation" else bot.account
            if bot.risky and acct.units:
                worst = float(df["low"].iloc[i] if bot.side > 0 else df["high"].iloc[i])
                if acct.liquidated(worst):
                    acct.liquidate()
                    self._kill(bot, price, "Position liquidiert (Hebel)")
                    continue
                acct.funding(price, cfg.funding_per_bar)
            target = self._signal(bot, df)[i]
            mood = self.news.get(self.symbol_of(bot).split("/")[0], 0.0)
            if bot.side * mood <= -cfg.news_block:  # Nachrichten sprechen klar gegen diese Richtung
                if target >= 0.5 and acct.units and bot.status == "active":
                    self._event("news", f"steigt wegen Nachrichten aus (Stimmung {mood:+.2f})", bot)
                target = 0.0
            acct.set_target(target, price, cfg.fee, cfg.slippage, bot.leverage, bot.side)
            equity = acct.equity(price)
            if bot.prev_equity > 0:
                bot.perf *= equity / bot.prev_equity
            bot.prev_equity = equity
            bot.perf_hist = (bot.perf_hist + [bot.perf])[-(cfg.outlook_bars + 1):]

            if bot.status == "probation":
                if self.bar_count - bot.born_bar >= cfg.probation_bars:
                    self._end_probation(bot, price)
                continue

            bot.peak_equity = max(bot.peak_equity, equity)
            if equity < bot.peak_equity * (1 - cfg.kill_drawdown):
                self._kill(bot, price, f"{cfg.kill_drawdown:.0%} Verlust vom Hoechststand")
                continue
            self._secure_profit(bot, price)

        if self.bar_count % cfg.epoch_bars == 0:
            self._epoch(frames, i)

        if self.total_wealth() < cfg.global_stop * cfg.start_capital:
            self.halt(f"Gesamtvermoegen unter {cfg.global_stop:.0%} des Startgelds")

    def tick(self, prices, ts: str | None = None) -> None:
        """Zwischen zwei Kerzen: aktuelle Kurse. Schutz und Gewinnsicherung greifen sofort.

        Gehandelt nach Strategie wird weiter nur am Kerzenschluss; hier werden nur Verluste
        begrenzt, Gewinne gesichert, Liquidationen erkannt und der globale Stopp geprueft.
        `prices`: Kurs je Coin, oder eine einzelne Zahl bei nur einem Coin.
        """
        if self.halted:
            return
        cfg = self.config
        if not isinstance(prices, dict):
            prices = {cfg.symbols[0]: prices}
        self.prices.update({symbol: float(p) for symbol, p in prices.items()})
        self.tick_ts = ts
        for bot in self.alive:
            if self.symbol_of(bot) not in prices:
                continue
            price = self.price_of(bot)
            if bot.status != "active":
                if bot.risky and bot.shadow and bot.shadow.liquidated(price):
                    bot.shadow.liquidate()
                    self._kill(bot, price, "in der Probezeit liquidiert (Hebel)")
                continue
            if bot.risky and bot.account.liquidated(price):
                bot.account.liquidate()
                self._kill(bot, price, "Position liquidiert (Hebel)")
                continue
            equity = bot.account.equity(price)
            if bot.prev_equity > 0:
                bot.perf *= equity / bot.prev_equity
            bot.prev_equity = equity
            bot.peak_equity = max(bot.peak_equity, equity)
            if equity < bot.peak_equity * (1 - cfg.kill_drawdown):
                self._kill(bot, price, f"{cfg.kill_drawdown:.0%} Verlust vom Hoechststand")
                continue
            self._secure_profit(bot, price)
        if self.total_wealth() < cfg.global_stop * cfg.start_capital:
            self.halt(f"Gesamtvermoegen unter {cfg.global_stop:.0%} des Startgelds")

    def _signal(self, bot: Bot, df: pd.DataFrame) -> np.ndarray:
        key = (len(df), str(df["timestamp"].iloc[-1]))
        cached = self._signals.get(bot.id)
        if cached is None or cached[0] != key:
            cached = (key, strategies.signal(bot.genome, df))
            self._signals[bot.id] = cached
        return cached[1]

    # ------------------------------------------------------------------ Gewinnsicherung
    def outlook(self, bot: Bot, price: float) -> float:
        """0 = schlechte Aussicht, 1 = sehr gute Aussicht (laeuft gut, kaum Rueckschlag)."""
        cfg = self.config
        recent = bot.perf / bot.perf_hist[0] - 1 if bot.perf_hist else 0.0
        momentum = min(max(recent / cfg.outlook_target, 0.0), 1.0)
        drawdown = 1 - bot.account.equity(price) / bot.peak_equity if bot.peak_equity > 0 else 0.0
        calm = min(max(1 - drawdown / cfg.kill_drawdown, 0.0), 1.0)
        return momentum * calm

    def _secure_profit(self, bot: Bot, price: float) -> None:
        cfg = self.config
        base = bot.start_capital
        profit = bot.total_profit(price)
        if profit > bot.peak_profit:
            bot.peak_profit = profit
            bot.giveback_armed = True

        fraction, reason = 0.0, ""
        hit = False
        while profit >= cfg.milestone(bot.milestone_idx) * base:
            bot.milestone_idx += 1
            hit = True
        if hit:
            outlook = self.outlook(bot, price)
            fraction = cfg.secure_max - (cfg.secure_max - cfg.secure_min) * outlook
            level = cfg.milestone(bot.milestone_idx - 1) * base
            reason = f"Gewinn-Stufe {level:.2f} erreicht, Aussicht {outlook:.0%}"
        elif (
            bot.giveback_armed
            and bot.peak_profit >= cfg.giveback_min_profit * base
            and profit <= bot.peak_profit * (1 - cfg.giveback)
        ):
            fraction = cfg.secure_max
            reason = f"Gewinn faellt vom Hoechststand {bot.peak_profit:.2f} zurueck"
            bot.giveback_armed = False

        available = bot.account.equity(price) - base
        if fraction <= 0 or available <= 0:
            return
        out = bot.account.withdraw(fraction * available, price, cfg.fee, cfg.slippage)
        if out <= 0:
            return
        bot.secured += out
        self.vault += out
        self._after_flow(bot, price, -out)
        self._event("secure", f"{out:.2f} gesichert ({reason})", bot, amount=out)

    @staticmethod
    def _after_flow(bot: Bot, price: float, amount: float) -> None:
        """Hoechststand und Vergleichswert nach einer Ein-/Auszahlung anpassen."""
        equity = bot.account.equity(price)
        bot.peak_equity = max(bot.peak_equity + amount, equity)
        bot.prev_equity = equity

    # ------------------------------------------------------------------ Leben und Sterben
    def _end_probation(self, bot: Bot, price: float) -> None:
        result = bot.shadow.equity(price) / bot.start_capital - 1
        if result > 0:
            bot.status = "active"
            bot.shadow = None
            bot.prev_equity = bot.peak_equity = bot.account.equity(price)
            bot.perf, bot.perf_hist, bot.epoch_start_perf = 1.0, [], 1.0
            self._event("activate", f"Probezeit bestanden ({result:+.1%}), handelt jetzt mit {bot.start_capital:.2f}", bot)
        else:
            self._kill(bot, price, f"Probezeit nicht bestanden ({result:+.1%})")

    def _kill(self, bot: Bot, price: float, reason: str) -> None:
        cfg = self.config
        bot.account.set_target(0, price, cfg.fee, cfg.slippage)
        rest = bot.account.cash
        bot.final_profit = rest + bot.secured + bot.given - bot.start_capital
        bot.account.cash = 0.0
        bot.status, bot.shadow, bot.death_reason, bot.died_bar = "dead", None, reason, self.bar_count
        self._signals.pop(bot.id, None)
        parent = next((b for b in self.bots if b.id == bot.parent_id and b.status == "active"), None)
        if parent:
            parent.account.cash += rest
            parent.given -= rest
            self._after_flow(parent, self.price_of(parent), rest)
            target = f"Bot {parent.id}"
        else:
            self.treasury += rest
            target = "die Reserve"
        self._event("death", f"gestorben: {reason}. {rest:.2f} zurueck an {target}", bot, amount=rest)

    def halt(self, reason: str, price: float | None = None) -> None:
        """Not-Aus: alle Positionen schliessen, nichts mehr handeln."""
        for bot in self.alive:
            bot.account.set_target(0, self.price_of(bot) if price is None else price,
                                   self.config.fee, self.config.slippage)
        self.halted = reason
        self._event("halt", f"Farm angehalten: {reason}")

    def _epoch(self, frames: dict, i: int) -> None:
        cfg = self.config
        active = [b for b in self.alive if b.status == "active"]
        profitable = set()
        for bot in active:
            if bot.perf > bot.epoch_start_perf:
                bot.unprofitable_epochs = 0
                profitable.add(bot.id)
            else:
                bot.unprofitable_epochs += 1
            bot.epoch_start_perf = bot.perf
            if bot.unprofitable_epochs >= cfg.max_unprofitable_epochs:
                self._kill(bot, self.price_of(bot), f"{bot.unprofitable_epochs} Wochen ohne Gewinn")

        for bot in sorted(active, key=lambda b: -self.profit_of(b)):
            if not bot.alive or bot.id not in profitable or len(self.alive) >= cfg.max_bots:
                continue
            price = self.price_of(bot)
            in_bot_profit = bot.account.equity(price) - bot.start_capital
            capital = cfg.spawn_fraction * in_bot_profit
            if in_bot_profit < cfg.spawn_min_profit * bot.start_capital or capital < cfg.min_child_capital:
                continue
            bred = self._breed(bot, frames, i)
            if not bred:
                continue
            out = bot.account.withdraw(capital, price, cfg.fee, cfg.slippage)
            bot.given += out
            bot.children += 1
            self._after_flow(bot, price, -out)
            self._add_bot(bred, out, parent=bot)

        if not self.alive and self.treasury >= cfg.min_child_capital:
            bred = self._breed(None, frames, i)
            if bred:
                capital, self.treasury = self.treasury, 0.0
                self._add_bot(bred, capital, parent=None)

        self.wealth_log.append(
            {"ts": self.last_ts, "total": round(self.total_wealth(), 4), "vault": round(self.vault, 4),
             "bots": len(self.alive)}
        )

    # ------------------------------------------------------------------ Vermehrung
    def _add_bot(self, bred: tuple[dict, dict], capital: float, parent: Bot | None, probation: bool = True) -> Bot:
        genome, stats = bred
        bot = Bot(
            id=self.next_id,
            genome=genome,
            start_capital=capital,
            account=Account(cash=capital),
            born_bar=self.bar_count,
            parent_id=parent.id if parent else None,
            generation=parent.generation + 1 if parent else 0,
            status="probation" if probation else "active",
            shadow=Account(cash=capital) if probation else None,
        )
        self.next_id += 1
        self.bots.append(bot)
        origin = f"Kind von Bot {parent.id}" if parent else "aus der Reserve"
        self._event(
            "birth",
            f"neuer Bot ({origin}) mit {capital:.2f}: {strategies.describe(genome)}, "
            f"Backtest {stats['return']:+.1%} bei max. {stats['max_drawdown']:.1%} Einbruch",
            bot,
        )
        return bot

    def _breed(self, parent: Bot | None, frames, i: int) -> tuple[dict, dict] | None:
        """Kandidaten auf allen Coins erzeugen, auf vergangenen Kursen testen, den besten zurueckgeben."""
        cfg = self.config
        frames = self._frames(frames)
        symbols = list(frames)
        rng = np.random.default_rng([cfg.seed, self.bar_count, self.next_id])
        candidates = []
        for k in range(cfg.candidates):
            if parent and k < cfg.candidates * 0.6:
                genome = strategies.mutate(parent.genome, rng)
                symbol = self.symbol_of(parent) if rng.random() < 0.7 else str(rng.choice(symbols))
                leverage = parent.leverage + int(rng.integers(-1, 2))
                side = parent.genome.get("side", "long")
            else:
                genome = strategies.random_genome(rng)
                symbol = str(rng.choice(symbols))
                leverage = int(rng.integers(1, cfg.max_leverage + 1))
                side = "short" if cfg.allow_short and rng.random() < 0.3 else "long"
            if symbol not in frames:
                symbol = symbols[0]
            if side == "short" and not cfg.allow_short:
                side = "long"
            candidates.append(strategies.normalize(
                {**genome, "symbol": symbol, "leverage": min(max(leverage, 1), cfg.max_leverage), "side": side}))

        def window(symbol: str):
            df = frames[symbol].iloc[max(0, i + 1 - cfg.backtest_bars - WARMUP): i + 1]
            return df, min(WARMUP, len(df) // 3)

        if cfg.use_llm and not self._llm_failed:
            symbol = self.symbol_of(parent) if parent and self.symbol_of(parent) in frames else symbols[0]
            candidates += self._ask_llm(parent, symbol, *window(symbol))

        taken = {json.dumps(b.genome, sort_keys=True) for b in self.alive}
        best = None
        for genome in candidates:
            if json.dumps(genome, sort_keys=True) in taken:
                continue
            df, warmup = window(genome["symbol"])
            stats = backtest(genome, df, cfg.fee, cfg.slippage, warmup, cfg.funding_per_bar)
            if (
                stats["return"] <= 0
                or stats["max_drawdown"] > cfg.backtest_max_drawdown
                or stats["trades"] < cfg.backtest_min_trades
            ):
                continue
            if best is None or stats["score"] > best[1]["score"]:
                best = (genome, stats)
        return best

    def _ask_llm(self, parent: Bot | None, symbol: str, window: pd.DataFrame, warmup: int) -> list[dict]:
        from . import llm

        cfg = self.config
        try:
            parent_stats = backtest(parent.genome, window, cfg.fee, cfg.slippage, warmup) if parent else None
            proposals = llm.propose(parent.genome if parent else None, parent_stats, window, count=4)
            return [strategies.normalize({**g, "symbol": symbol, "leverage": 1, "side": "long"}) for g in proposals]
        except Exception as exc:  # Ohne Claude geht es mit Zufalls-Varianten weiter
            self._llm_failed = True
            self._event("llm_error", f"Claude nicht erreichbar, nutze nur Zufalls-Varianten: {exc}")
            return []

    # ------------------------------------------------------------------ Start, Speichern, Laden
    @classmethod
    def start(cls, config: FarmConfig, frames, i: int) -> "Farm":
        """Farm gruenden: bester Kandidat aus dem Backtest bis Kerze `i` wird der erste Bot."""
        farm = cls(config)
        frames = farm._frames(frames)
        farm.last_ts = str(next(iter(frames.values()))["timestamp"].iloc[i])
        farm.prices = {symbol: float(df["close"].iloc[i]) for symbol, df in frames.items()}
        bred = farm._breed(None, frames, i)
        if bred is None:  # kein Kandidat besteht den Test: Geld bleibt in der Reserve
            farm._event("wait", "Keine Strategie besteht den Backtest. Neuer Versuch nach einer Woche.")
            return farm
        capital, farm.treasury = farm.treasury, 0.0
        farm._add_bot(bred, capital, parent=None, probation=False)
        return farm

    def to_dict(self) -> dict:
        return {
            "config": self.config.to_dict(),
            "bots": [b.to_dict() for b in self.bots],
            "vault": self.vault,
            "treasury": self.treasury,
            "bar_count": self.bar_count,
            "last_ts": self.last_ts,
            "prices": self.prices,
            "tick_ts": self.tick_ts,
            "news": self.news,
            "market": self.market,
            "halted": self.halted,
            "next_id": self.next_id,
            "events": self.events[-500:],
            "wealth_log": self.wealth_log[-2000:],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Farm":
        farm = cls(FarmConfig.from_dict(data["config"]))
        # Staende aus aelteren Versionen kannten nur einen Coin und einen Kurs.
        farm.prices = data.get("prices") or {farm.config.symbols[0]: data.get("last_price", 0.0)}
        farm.bots = [Bot.from_dict(b, farm.prices.get(b["genome"].get("symbol") or farm.config.symbols[0], 0.0))
                     for b in data["bots"]]
        for key in ("vault", "treasury", "bar_count", "last_ts", "halted", "next_id", "events", "wealth_log"):
            setattr(farm, key, data[key])
        farm.tick_ts = data.get("tick_ts")
        farm.news, farm.market = data.get("news") or {}, data.get("market") or {}
        return farm

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.to_dict(), fh, indent=1)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "Farm":
        with open(path) as fh:
            return cls.from_dict(json.load(fh))
