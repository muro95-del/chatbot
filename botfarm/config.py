"""Alle Stellschrauben der Farm an einem Ort."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class FarmConfig:
    # --- Kapital ---------------------------------------------------------
    start_capital: float = 100.0  # Startgeld des ersten Bots. Es kommt nie Geld von aussen dazu.

    # --- Handelskosten ---------------------------------------------------
    fee: float = 0.001  # 0,1 % Boersengebuehr pro Kauf/Verkauf
    slippage: float = 0.0005  # 0,05 % Kursabweichung pro Kauf/Verkauf

    # --- Gewinnsicherung -------------------------------------------------
    # Gewinn-Stufen als Vielfaches des Startkapitals eines Bots.
    # 3.0 heisst: bei 100 Start wird bei 300 Gewinn gesichert.
    milestones: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0)
    secure_min: float = 0.15  # Anteil des Gewinns bei guter Aussicht (weiterlaufen lassen)
    secure_max: float = 0.50  # Anteil des Gewinns bei schlechter Aussicht
    outlook_bars: int = 72  # Rueckblick fuer die Aussicht
    outlook_target: float = 0.05  # +5 % im Rueckblick gilt als sehr gute Aussicht
    # Faellt der Gewinn um diesen Anteil vom Hoechststand zurueck, wird sofort gesichert.
    giveback: float = 0.30
    giveback_min_profit: float = 0.10  # erst ab +10 % Gewinn aktiv

    # --- Leben und Sterben -----------------------------------------------
    epoch_bars: int = 168  # alle 168 Kerzen (1 Woche bei 1h) wird Bilanz gezogen
    kill_drawdown: float = 0.30  # -30 % vom eigenen Hoechststand: Bot stirbt
    max_unprofitable_epochs: int = 4  # so lange ohne Gewinn: Bot stirbt

    # --- Vermehrung ------------------------------------------------------
    max_bots: int = 8
    spawn_min_profit: float = 0.15  # Bot braucht +15 % Gewinn im Konto, um ein Kind zu bekommen
    spawn_fraction: float = 0.5  # Anteil des Gewinns, der an das Kind geht
    min_child_capital: float = 10.0
    candidates: int = 12  # so viele Strategie-Varianten werden pro Kind getestet
    backtest_bars: int = 1000  # Laenge des Backtests fuer Kandidaten
    backtest_max_drawdown: float = 0.25
    backtest_min_trades: int = 5
    probation_bars: int = 168  # Kind handelt erst so lange mit Spielgeld
    use_llm: bool = False  # Claude neue Strategien vorschlagen lassen

    # --- Sicherheit ------------------------------------------------------
    global_stop: float = 0.60  # Gesamtvermoegen < 60 % des Startgelds: alles anhalten

    seed: int = 7

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FarmConfig":
        data = dict(data)
        if "milestones" in data:
            data["milestones"] = tuple(data["milestones"])
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def milestone(self, index: int) -> float:
        """Stufe `index`; nach der letzten Stufe verdoppelt sich jede weitere."""
        if index < len(self.milestones):
            return self.milestones[index]
        return self.milestones[-1] * 2 ** (index - len(self.milestones) + 1)
