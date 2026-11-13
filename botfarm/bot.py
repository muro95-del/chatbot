"""Ein einzelner Bot: Konto, Strategie und Kennzahlen."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

MAINTENANCE = 0.005  # Mindest-Sicherheit der Boerse: darunter wird eine Hebel-Position liquidiert


@dataclass
class Account:
    """Konto eines Bots. Eine Position ist isoliert: verloren gehen kann hoechstens ihre Margin."""

    cash: float
    units: float = 0.0  # gehaltene Menge; negativ = Short
    entry: float = 0.0  # Einstiegskurs
    margin: float = 0.0  # in der Position gebundenes Geld

    def equity(self, price: float) -> float:
        if self.units == 0:
            return self.cash
        return self.cash + max(self.margin + self.units * (price - self.entry), 0.0)

    def set_target(self, target: float, price: float, fee: float, slippage: float,
                   leverage: int = 1, side: int = 1) -> None:
        """Position mit allem Geld eroeffnen (target=1) oder ganz schliessen (target=0)."""
        if target >= 0.5 and self.units == 0 and self.cash > 0:
            exec_price = price * (1 + side * slippage)
            self.margin = self.cash * (1 - leverage * fee)
            self.units = side * self.margin * leverage / exec_price
            self.entry = exec_price
            self.cash = 0.0
        elif target < 0.5 and self.units != 0:
            self.cash += self._close(1.0, price, fee, slippage)

    def _close_value(self, price: float, fee: float, slippage: float) -> float:
        exec_price = price * (1 - (1 if self.units > 0 else -1) * slippage)
        return max(self.margin + self.units * (exec_price - self.entry) - abs(self.units) * exec_price * fee, 0.0)

    def _close(self, fraction: float, price: float, fee: float, slippage: float) -> float:
        value = self._close_value(price, fee, slippage) * fraction
        self.units -= self.units * fraction
        self.margin -= self.margin * fraction
        if fraction >= 1.0 or abs(self.units) * price < 1e-9:
            self.units, self.margin, self.entry = 0.0, 0.0, 0.0
        return value

    def withdraw(self, amount: float, price: float, fee: float, slippage: float) -> float:
        """Betrag entnehmen; falls noetig wird dafuer ein Teil der Position geschlossen."""
        if amount <= 0:
            return 0.0
        if self.cash < amount and self.units != 0:
            full = self._close_value(price, fee, slippage)
            if full > 0:
                self.cash += self._close(min(1.0, (amount - self.cash) / full), price, fee, slippage)
        out = min(amount, self.cash)
        self.cash -= out
        return out

    def liquidated(self, price: float) -> bool:
        """Wuerde die Boerse die Position bei diesem Kurs zwangsweise schliessen?"""
        if self.units == 0:
            return False
        return self.margin + self.units * (price - self.entry) <= abs(self.units) * price * MAINTENANCE

    def liquidate(self) -> None:
        self.units, self.margin, self.entry = 0.0, 0.0, 0.0

    def funding(self, price: float, rate: float) -> None:
        """Laufende Finanzierungskosten einer Hebel-Position."""
        if self.units:
            self.margin = max(self.margin - abs(self.units) * price * rate, 0.0)

    @classmethod
    def from_dict(cls, data: dict, price: float = 0.0) -> "Account":
        acct = cls(**data)
        if acct.units and not acct.margin:  # Stand aus einer aelteren Version
            acct.entry = acct.entry or price
            acct.margin = abs(acct.units) * acct.entry
        return acct


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

    @property
    def leverage(self) -> int:
        return int(self.genome.get("leverage", 1))

    @property
    def side(self) -> int:
        return -1 if self.genome.get("side") == "short" else 1

    @property
    def risky(self) -> bool:
        """Hebel oder Short: kann liquidiert werden und zahlt Finanzierungskosten."""
        return self.leverage > 1 or self.side < 0

    def total_profit(self, price: float) -> float:
        """Gesamter Gewinn: Konto + bereits Gesichertes + an Kinder Gegebenes - Startkapital."""
        if self.final_profit is not None:
            return self.final_profit
        return self.account.equity(price) + self.secured + self.given - self.start_capital

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict, price: float = 0.0) -> "Bot":
        data = dict(data)
        data["account"] = Account.from_dict(data["account"], price)
        if data.get("shadow"):
            data["shadow"] = Account.from_dict(data["shadow"], price)
        return cls(**data)
