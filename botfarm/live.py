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
    def __init__(self, exchange, symbol: str, budget: float, path: str, real_orders: bool):
        self.ex, self.symbol, self.path, self.real_orders = exchange, symbol, path, real_orders
        self.market = exchange.market(symbol)
        if os.path.exists(path):
            with open(path) as fh:
                self.state = json.load(fh)
            if self.state["symbol"] != symbol:
                raise SystemExit(f"Der gespeicherte Stand gehoert zu {self.state['symbol']}, nicht zu {symbol}.")
        else:
            self.state = {"symbol": symbol, "budget": budget, "cash": budget, "units": 0.0, "fees": 0.0, "orders": [],
                          "real_orders": bool(real_orders)}

    # ------------------------------------------------------------------ Pruefungen
    def check_balance(self) -> None:
        """Vor dem ersten echten Kauf: liegt das Budget wirklich auf dem Konto?"""
        quote = self.market["quote"]
        free = float(self.ex.fetch_balance().get("free", {}).get(quote) or 0.0)
        if free + 1e-9 < self.state["cash"]:
            raise SystemExit(
                f"Auf dem Konto sind nur {free:.2f} {quote} frei, der Bot braucht {self.state['cash']:.2f}. "
                "Zahle mehr ein oder starte mit kleinerem --capital."
            )

    def refresh_balance(self) -> dict:
        """Echten Kontostand von der Boerse lesen (nur lesen, aendert nichts)."""
        balance = self.ex.fetch_balance()
        base, quote = self.market["base"], self.market["quote"]

        def amount(kind: str, coin: str) -> float:
            return float((balance.get(kind) or {}).get(coin) or 0.0)

        self.state["account"] = {
            "quote": quote,
            "base": base,
            "quote_free": amount("free", quote),
            "quote_total": amount("total", quote) or amount("free", quote),
            "base_total": amount("total", base) or amount("free", base),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return self.state["account"]

    def value(self, price: float) -> float:
        """Echter Wert: uebriges Budget plus gekaufte Menge zum aktuellen Kurs."""
        return self.state["cash"] + self.state["units"] * price

    # ------------------------------------------------------------------ Abgleich
    def sync(self, farm, price: float) -> dict | None:
        """Echte Position an die Buchfuehrung der Farm angleichen. Gibt die Order zurueck."""
        desired = 0.0 if farm.halted else sum(b.account.units for b in farm.alive)
        delta = desired - self.state["units"]
        side = "buy" if delta > 0 else "sell"
        amount = abs(delta)
        if side == "buy":
            amount = min(amount, self.state["cash"] * (1 - BUY_RESERVE) / price)
        else:
            amount = min(amount, self.state["units"])
        amount = float(self.ex.amount_to_precision(self.symbol, amount)) if amount > 0 else 0.0

        limits = self.market.get("limits", {})
        min_amount = (limits.get("amount") or {}).get("min") or 0.0
        min_cost = (limits.get("cost") or {}).get("min") or 0.0
        if amount <= 0 or amount < min_amount or amount * price < min_cost:
            return None  # zu klein fuer die Boerse: beim naechsten Mal erneut pruefen

        order = {"ts": farm.last_ts, "side": side, "amount": amount, "price": price, "cost": amount * price,
                 "fee": 0.0, "real": self.real_orders}
        if self.real_orders:
            order.update(self._place(side, amount, price))
        if side == "buy":
            self.state["cash"] -= order["cost"] + order["fee"]
            self.state["units"] += order["amount"]
        else:
            self.state["cash"] += order["cost"] - order["fee"]
            self.state["units"] -= order["amount"]
        self.state["units"] = max(self.state["units"], 0.0)
        self.state["fees"] += order["fee"]
        self.state["orders"] = (self.state["orders"] + [order])[-200:]
        self.save()
        return order

    def _place(self, side: str, amount: float, price: float) -> dict:
        """Market-Order abschicken und die tatsaechliche Ausfuehrung zuruecklesen."""
        if side == "buy" and self.ex.has.get("createMarketBuyOrderWithCost"):
            cost = float(self.ex.cost_to_precision(self.symbol, amount * price))
            placed = self.ex.create_market_buy_order_with_cost(self.symbol, cost)
        else:
            placed = self.ex.create_order(self.symbol, "market", side, amount)
        result = placed
        for _ in range(5):  # manche Boersen liefern die Ausfuehrung erst kurz danach
            if result.get("filled") and result.get("cost"):
                break
            time.sleep(1)
            try:
                result = self.ex.fetch_order(placed["id"], self.symbol)
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
            if fee.get("currency") == self.market["base"]:
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
