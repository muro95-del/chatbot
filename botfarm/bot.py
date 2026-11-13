"""Ein einzelner Bot: Konto, Strategie und Kennzahlen."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Account:
    cash: float
    units: float = 0.0  # gehaltene Menge der Kryptowaehrung

    def equity(self, price: float) -> float:
        return self.cash + self.units * price

    def set_target(self, target: float, price: float, fee: float, slippage: float) -> None:
        """Alles kaufen (target=1) oder alles verkaufen (target=0)."""
        if target >= 0.5 and self.units == 0 and self.cash > 0:
            self.units = self.cash * (1 - fee) / (price * (1 + slippage))
            self.cash = 0.0
        elif target < 0.5 and self.units > 0:
            self.cash += self.units * price * (1 - slippage) * (1 - fee)
            self.units = 0.0

    def withdraw(self, amount: float, price: float, fee: float, slippage: float) -> float:
        """Betrag entnehmen; falls noetig wird dafuer ein Teil der Position verkauft."""
        if amount <= 0:
            return 0.0
        if self.cash < amount and self.units > 0:
            net_price = price * (1 - slippage) * (1 - fee)
            sell = min(self.units, (amount - self.cash) / net_price)
            self.units -= sell
            self.cash += sell * net_price
        out = min(amount, self.cash)
        self.cash -= out
        return out


@dataclass
class Bot:
    id: int
    genome: dict
    start_capital: float
    account: Account
    born_bar: int
    parent_id: int | None = None
    generation: int = 0
    status: str = "active"  # probation | active | dead
    shadow: Account | None = None  # Spielgeld-Konto waehrend der Probezeit
    secured: float = 0.0  # von diesem Bot gesicherter Gewinn
    given: float = 0.0  # an Kinder weitergegebener Gewinn
    peak_equity: float = 0.0
    peak_profit: float = 0.0
    milestone_idx: int = 0
    giveback_armed: bool = False
    prev_equity: float = 0.0
    perf: float = 1.0  # Wertentwicklung ohne Ein-/Auszahlungen
    perf_hist: list[float] = field(default_factory=list)
    epoch_start_perf: float = 1.0
    unprofitable_epochs: int = 0
    children: int = 0
    death_reason: str = ""
    died_bar: int | None = None
    final_profit: float | None = None

    def __post_init__(self) -> None:
        if not self.peak_equity:
            self.peak_equity = self.account.cash
        if not self.prev_equity:
            self.prev_equity = self.account.cash

    @property
    def alive(self) -> bool:
        return self.status != "dead"

    def total_profit(self, price: float) -> float:
        """Gesamter Gewinn: Konto + bereits Gesichertes + an Kinder Gegebenes - Startkapital."""
        if self.final_profit is not None:
            return self.final_profit
        return self.account.equity(price) + self.secured + self.given - self.start_capital

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Bot":
        data = dict(data)
        data["account"] = Account(**data["account"])
        if data.get("shadow"):
            data["shadow"] = Account(**data["shadow"])
        return cls(**data)
