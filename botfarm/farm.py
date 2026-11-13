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
        self.last_price = 0.0
        self.tick_ts: str | None = None
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

    def total_wealth(self, price: float | None = None) -> float:
        price = self.last_price if price is None else price
        return self.vault + self.treasury + sum(b.account.equity(price) for b in self.alive)

    def _event(self, kind: str, message: str, bot: Bot | None = None, **extra) -> None:
        self.events.append(
            {"bar": self.bar_count, "ts": self.last_ts, "type": kind, "bot": bot.id if bot else None,
             "message": message, **extra}
        )

    # ------------------------------------------------------------------ ein Schritt
    def step(self, df: pd.DataFrame, i: int) -> None:
        """Die abgeschlossene Kerze `i` von `df` verarbeiten."""
        if self.halted:
            return
        cfg = self.config
        price = float(df["close"].iloc[i])
        self.last_price = price
        self.last_ts = str(df["timestamp"].iloc[i])
        self.bar_count += 1

        for bot in self.alive:
            target = self._signal(bot, df)[i]
            acct = bot.shadow if bot.status == "probation" else bot.account
            acct.set_target(target, price, cfg.fee, cfg.slippage)
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
            self._epoch(df, i, price)

        if self.total_wealth(price) < cfg.global_stop * cfg.start_capital:
            self.halt(f"Gesamtvermoegen unter {cfg.global_stop:.0%} des Startgelds", price)

    def tick(self, price: float, ts: str | None = None) -> None:
        """Zwischen zwei Kerzen: aktueller Kurs. Schutz und Gewinnsicherung greifen sofort.

        Gehandelt nach Strategie wird weiter nur am Kerzenschluss; hier werden nur Verluste
        begrenzt, Gewinne gesichert und der globale Stopp geprueft.
        """
        if self.halted:
            return
        cfg = self.config
        self.last_price = float(price)
        self.tick_ts = ts
        for bot in self.alive:
            if bot.status != "active":
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
        if self.total_wealth(price) < cfg.global_stop * cfg.start_capital:
            self.halt(f"Gesamtvermoegen unter {cfg.global_stop:.0%} des Startgelds", price)

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
            self._after_flow(parent, price, rest)
            target = f"Bot {parent.id}"
        else:
            self.treasury += rest
            target = "die Reserve"
        self._event("death", f"gestorben: {reason}. {rest:.2f} zurueck an {target}", bot, amount=rest)

    def halt(self, reason: str, price: float | None = None) -> None:
        """Not-Aus: alle Positionen verkaufen, nichts mehr handeln."""
        price = self.last_price if price is None else price
        for bot in self.alive:
            bot.account.set_target(0, price, self.config.fee, self.config.slippage)
        self.halted = reason
        self._event("halt", f"Farm angehalten: {reason}")

    def _epoch(self, df: pd.DataFrame, i: int, price: float) -> None:
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
                self._kill(bot, price, f"{bot.unprofitable_epochs} Wochen ohne Gewinn")

        for bot in sorted(active, key=lambda b: -b.total_profit(price)):
            if not bot.alive or bot.id not in profitable or len(self.alive) >= cfg.max_bots:
                continue
            in_bot_profit = bot.account.equity(price) - bot.start_capital
            capital = cfg.spawn_fraction * in_bot_profit
            if in_bot_profit < cfg.spawn_min_profit * bot.start_capital or capital < cfg.min_child_capital:
                continue
            bred = self._breed(bot, df, i)
            if not bred:
                continue
            out = bot.account.withdraw(capital, price, cfg.fee, cfg.slippage)
            bot.given += out
            bot.children += 1
            self._after_flow(bot, price, -out)
            self._add_bot(bred, out, parent=bot)

        if not self.alive and self.treasury >= cfg.min_child_capital:
            bred = self._breed(None, df, i)
            if bred:
                capital, self.treasury = self.treasury, 0.0
                self._add_bot(bred, capital, parent=None)

        self.wealth_log.append(
            {"ts": self.last_ts, "total": round(self.total_wealth(price), 4), "vault": round(self.vault, 4),
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

    def _breed(self, parent: Bot | None, df: pd.DataFrame, i: int) -> tuple[dict, dict] | None:
        """Kandidaten erzeugen, auf vergangenen Kursen testen, den besten zurueckgeben."""
        cfg = self.config
        rng = np.random.default_rng([cfg.seed, self.bar_count, self.next_id])
        candidates = []
        for k in range(cfg.candidates):
            if parent and k < cfg.candidates * 0.6:
                candidates.append(strategies.mutate(parent.genome, rng))
            else:
                candidates.append(strategies.random_genome(rng))

        window = df.iloc[max(0, i + 1 - cfg.backtest_bars - WARMUP): i + 1]
        warmup = min(WARMUP, len(window) // 3)
        if cfg.use_llm and not self._llm_failed:
            candidates += self._ask_llm(parent, window, warmup)

        taken = {json.dumps(b.genome, sort_keys=True) for b in self.alive}
        best = None
        for genome in candidates:
            if json.dumps(genome, sort_keys=True) in taken:
                continue
            stats = backtest(genome, window, cfg.fee, cfg.slippage, warmup)
            if (
                stats["return"] <= 0
                or stats["max_drawdown"] > cfg.backtest_max_drawdown
                or stats["trades"] < cfg.backtest_min_trades
            ):
                continue
            if best is None or stats["score"] > best[1]["score"]:
                best = (genome, stats)
        return best

    def _ask_llm(self, parent: Bot | None, window: pd.DataFrame, warmup: int) -> list[dict]:
        from . import llm

        cfg = self.config
        try:
            parent_stats = backtest(parent.genome, window, cfg.fee, cfg.slippage, warmup) if parent else None
            return llm.propose(parent.genome if parent else None, parent_stats, window, count=4)
        except Exception as exc:  # Ohne Claude geht es mit Zufalls-Varianten weiter
            self._llm_failed = True
            self._event("llm_error", f"Claude nicht erreichbar, nutze nur Zufalls-Varianten: {exc}")
            return []

    # ------------------------------------------------------------------ Start, Speichern, Laden
    @classmethod
    def start(cls, config: FarmConfig, df: pd.DataFrame, i: int) -> "Farm":
        """Farm gruenden: bester Kandidat aus dem Backtest bis Kerze `i` wird der erste Bot."""
        farm = cls(config)
        farm.last_ts = str(df["timestamp"].iloc[i])
        farm.last_price = float(df["close"].iloc[i])
        bred = farm._breed(None, df, i)
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
            "last_price": self.last_price,
            "tick_ts": self.tick_ts,
            "halted": self.halted,
            "next_id": self.next_id,
            "events": self.events[-500:],
            "wealth_log": self.wealth_log[-2000:],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Farm":
        farm = cls(FarmConfig.from_dict(data["config"]))
        farm.bots = [Bot.from_dict(b) for b in data["bots"]]
        for key in ("vault", "treasury", "bar_count", "last_ts", "last_price", "halted", "next_id", "events",
                    "wealth_log"):
            setattr(farm, key, data[key])
        farm.tick_ts = data.get("tick_ts")
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
