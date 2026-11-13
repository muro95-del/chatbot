"""Echte Orders: setzt die Summe aller Bot-Positionen auf der Boerse um.

Die Farm fuehrt weiter Buch ueber jeden Bot. Dieser Teil vergleicht nach jedem Schritt, wie viel
die Bots zusammen halten sollen, mit dem, was tatsaechlich gekauft wurde, und gleicht die
Differenz mit einer Market-Order aus. Er gibt nie mehr aus als das festgelegte Budget.

Schluessel kommen aus den Umgebungsvariablen BOTFARM_API_KEY und BOTFARM_API_SECRET.
Boersen mit API-Passphrase (z. B. Bitget) brauchen zusaetzlich BOTFARM_API_PASSPHRASE.
"""

from __future__ import annotations

import json
import os
import time

KEY_ENV, SECRET_ENV, PASSPHRASE_ENV = "BOTFARM_API_KEY", "BOTFARM_API_SECRET", "BOTFARM_API_PASSPHRASE"
BUY_RESERVE = 0.003  # etwas Luft fuer Gebuehren und Kursbewegung beim Kauf


def connect(exchange: str, uta: bool = False):
    """Mit dem Konto verbinden. `uta` schaltet bei Bitget auf das Unified Trading Account um."""
    import ccxt

    key, secret = os.environ.get(KEY_ENV), os.environ.get(SECRET_ENV)
    if not key or not secret:
        raise SystemExit(f"Bitte {KEY_ENV} und {SECRET_ENV} als Umgebungsvariablen setzen.")
    ex = getattr(ccxt, exchange)({"apiKey": key, "secret": secret, "enableRateLimit": True})
    if ex.requiredCredentials.get("password"):
        ex.password = os.environ.get(PASSPHRASE_ENV)
        if not ex.password:
            raise SystemExit(f"{exchange} braucht zusaetzlich die API-Passphrase in {PASSPHRASE_ENV}.")
    if uta:
        ex.options["uta"] = True
    ex.load_markets()
    return ex


class LiveExecutor:
    """Spiegelt je Coin die Summe der Bot-Positionen (nur Spot, ohne Hebel) auf der Boerse."""

    def __init__(self, exchange, symbols, budget: float, path: str, real_orders: bool):
        self.ex, self.path, self.real_orders = exchange, path, real_orders
        self.symbols = [symbols] if isinstance(symbols, str) else list(symbols)
        self.markets = {s: exchange.market(s) for s in self.symbols}
        self.quote = self.markets[self.symbols[0]]["quote"]
        if os.path.exists(path):
            with open(path) as fh:
                self.state = json.load(fh)
            if not isinstance(self.state["units"], dict):  # Stand aus der Version mit nur einem Coin
                self.state["units"] = {self.state.pop("symbol"): self.state["units"]}
            unknown = [s for s, u in self.state["units"].items() if u and s not in self.symbols]
            if unknown:
                raise SystemExit(f"Der gespeicherte Stand haelt noch {', '.join(unknown)}. "
                                 "Diese Coins muessen in der Liste bleiben, bis sie verkauft sind.")
        else:
            self.state = {"budget": budget, "cash": budget, "units": {}, "fees": 0.0, "orders": [],
                          "real_orders": bool(real_orders)}

    # ------------------------------------------------------------------ Pruefungen
    def check_balance(self) -> None:
        """Vor dem ersten echten Kauf: liegt das Budget wirklich auf dem Konto?"""
        free = float(self.ex.fetch_balance().get("free", {}).get(self.quote) or 0.0)
        if free + 1e-9 < self.state["cash"]:
            raise SystemExit(
                f"Auf dem Konto sind nur {free:.2f} {self.quote} frei, der Bot braucht {self.state['cash']:.2f}. "
                "Zahle mehr ein oder starte mit kleinerem --capital."
            )

    def refresh_balance(self) -> dict:
        """Echten Kontostand von der Boerse lesen (nur lesen, aendert nichts)."""
        balance = self.ex.fetch_balance()

        def amount(kind: str, coin: str) -> float:
            return float((balance.get(kind) or {}).get(coin) or 0.0)

        self.state["account"] = {
            "quote": self.quote,
            "quote_free": amount("free", self.quote),
            "quote_total": amount("total", self.quote) or amount("free", self.quote),
            "coins": {m["base"]: amount("total", m["base"]) or amount("free", m["base"])
                      for m in self.markets.values()},
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return self.state["account"]

    def value(self, prices: dict) -> float:
        """Echter Wert des Bot-Anteils: uebriges Budget plus gekaufte Coins zum aktuellen Kurs."""
        return self.state["cash"] + sum(u * prices.get(s, 0.0) for s, u in self.state["units"].items())

    # ------------------------------------------------------------------ Abgleich
    def sync(self, farm, prices: dict) -> list[dict]:
        """Echte Positionen an die Buchfuehrung der Farm angleichen. Verkaeufe zuerst, damit Geld frei wird."""
        desired = {s: 0.0 for s in self.symbols}
        if not farm.halted:
            for bot in farm.alive:
                symbol = bot.genome.get("symbol") or self.symbols[0]
                if symbol in desired and bot.account.units > 0:
                    desired[symbol] += bot.account.units
        deltas = {s: desired[s] - self.state["units"].get(s, 0.0) for s in self.symbols}
        orders = []
        for symbol in sorted(self.symbols, key=lambda s: deltas[s]):
            price = prices.get(symbol)
            if price:
                order = self._sync_one(symbol, deltas[symbol], price, farm.last_ts)
                if order:
                    orders.append(order)
        return orders

    def _sync_one(self, symbol: str, delta: float, price: float, ts) -> dict | None:
        held = self.state["units"].get(symbol, 0.0)
        side = "buy" if delta > 0 else "sell"
        amount = abs(delta)
        if side == "buy":
            amount = min(amount, self.state["cash"] * (1 - BUY_RESERVE) / price)
        else:
            amount = min(amount, held)
        amount = float(self.ex.amount_to_precision(symbol, amount)) if amount > 0 else 0.0

        limits = self.markets[symbol].get("limits", {})
        min_amount = (limits.get("amount") or {}).get("min") or 0.0
        min_cost = (limits.get("cost") or {}).get("min") or 0.0
        if amount <= 0 or amount < min_amount or amount * price < min_cost:
            return None  # zu klein fuer die Boerse: beim naechsten Mal erneut pruefen

        order = {"ts": ts, "symbol": symbol, "side": side, "amount": amount, "price": price,
                 "cost": amount * price, "fee": 0.0, "real": self.real_orders}
        if self.real_orders:
            order.update(self._place(symbol, side, amount, price))
        if side == "buy":
            self.state["cash"] -= order["cost"] + order["fee"]
            held += order["amount"]
        else:
            self.state["cash"] += order["cost"] - order["fee"]
            held -= order["amount"]
        self.state["units"][symbol] = max(held, 0.0)
        self.state["fees"] += order["fee"]
        self.state["orders"] = (self.state["orders"] + [order])[-200:]
        self.save()
        return order

    def _place(self, symbol: str, side: str, amount: float, price: float) -> dict:
        """Market-Order abschicken und die tatsaechliche Ausfuehrung zuruecklesen."""
        if side == "buy" and self.ex.has.get("createMarketBuyOrderWithCost"):
            cost = float(self.ex.cost_to_precision(symbol, amount * price))
            placed = self.ex.create_market_buy_order_with_cost(symbol, cost)
        else:
            placed = self.ex.create_order(symbol, "market", side, amount)
        result = placed
        for _ in range(5):  # manche Boersen liefern die Ausfuehrung erst kurz danach
            if result.get("filled") and result.get("cost"):
                break
            time.sleep(1)
            try:
                result = self.ex.fetch_order(placed["id"], symbol)
            except Exception:
                continue
        filled = float(result.get("filled") or 0.0)
        cost = float(result.get("cost") or 0.0)
        if not filled or not cost:
            raise RuntimeError(
                f"Order {placed.get('id')} abgeschickt, aber die Ausfuehrung ist unklar. "
                "Bitte auf der Boerse pruefen. Der Bot haelt an, damit nichts doppelt gekauft wird."
            )
        fee_quote, fee_base = 0.0, 0.0
        fees = result.get("fees") or ([result["fee"]] if result.get("fee") else [])
        for fee in fees:
            if not fee or not fee.get("cost"):
                continue
            if fee.get("currency") == self.markets[symbol]["base"]:
                fee_base += float(fee["cost"])
            else:
                fee_quote += float(fee["cost"])
        return {"id": placed.get("id"), "amount": filled - (fee_base if side == "buy" else 0.0), "cost": cost,
                "price": cost / filled, "fee": fee_quote}

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.state, fh, indent=1)
        os.replace(tmp, self.path)
