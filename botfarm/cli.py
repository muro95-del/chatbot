"""Kommandozeile: simulate (Test auf alten Kursen), paper (laufend mit Spielgeld), status."""

from __future__ import annotations

import argparse
import os
import time

from . import data, strategies
from .config import FarmConfig
from .farm import WARMUP, Farm

STATE_DIR = "botfarm_state"


def _print_events(farm: Farm, start: int) -> int:
    for event in farm.events[start:]:
        who = f"Bot {event['bot']}" if event["bot"] else "Farm"
        print(f"  {event['ts']}  {who}: {event['message']}")
    return len(farm.events)


def report(farm: Farm) -> str:
    price = farm.last_price
    start = farm.config.start_capital
    total = farm.total_wealth(price)
    lines = [
        f"Stand: {farm.last_ts}  ({farm.bar_count} Kerzen verarbeitet)",
        f"Gesamtvermoegen: {total:.2f}  (Start {start:.2f}, {total / start - 1:+.1%})",
        f"  davon gesichert: {farm.vault:.2f}",
        f"  in Bots:         {sum(b.account.equity(price) for b in farm.alive):.2f}",
        f"  freie Reserve:   {farm.treasury:.2f}",
        f"Bots: {len(farm.alive)} am Leben, {len(farm.bots) - len(farm.alive)} gestorben",
    ]
    if farm.halted:
        lines.append(f"ANGEHALTEN: {farm.halted}")
    lines.append("")
    lines.append(f"{'Bot':>4} {'Eltern':>6} {'Status':<10} {'Start':>8} {'Konto':>8} {'Gesichert':>9} {'Gewinn':>8}  Strategie")
    for b in farm.bots:
        status = {"active": "aktiv", "probation": "Probezeit", "dead": "tot"}[b.status]
        lines.append(
            f"{b.id:>4} {b.parent_id or '-':>6} {status:<10} {b.start_capital:>8.2f} {b.account.equity(price):>8.2f} "
            f"{b.secured:>9.2f} {b.total_profit(price):>+8.2f}  {strategies.describe(b.genome)}"
        )
    return "\n".join(lines)


def _config(args) -> FarmConfig:
    config = FarmConfig(start_capital=args.capital, use_llm=args.llm, seed=args.seed)
    if args.milestones:
        config.milestones = tuple(sorted(float(x) for x in args.milestones.split(",")))
    return config


def _load_data(args, bars: int):
    if args.csv:
        return data.from_csv(args.csv)
    if args.exchange:
        return data.from_exchange(args.exchange, args.symbol, args.timeframe, bars)
    return data.synthetic(bars, seed=args.seed)


def simulate(args) -> Farm:
    df = _load_data(args, args.bars)
    first = min(WARMUP + 1000, len(df) // 2)
    farm = Farm.start(_config(args), df, first - 1)
    shown = _print_events(farm, 0) if args.verbose else 0
    for i in range(first, len(df)):
        farm.step(df, i)
        if args.verbose:
            shown = _print_events(farm, shown)
    print(report(farm))
    hold = float(df["close"].iloc[-1] / df["close"].iloc[first - 1] - 1)
    print(f"\nZum Vergleich: einfach kaufen und halten haette {hold:+.1%} gebracht.")
    return farm


def paper(args) -> None:
    if not args.exchange:
        raise SystemExit("paper braucht --exchange (z. B. --exchange kraken --symbol BTC/EUR)")
    os.makedirs(STATE_DIR, exist_ok=True)
    state = os.path.join(STATE_DIR, "state.json")
    stop_file = os.path.join(STATE_DIR, "STOP")
    farm = Farm.load(state) if os.path.exists(state) else None
    shown = len(farm.events) if farm else 0
    while True:
        try:
            df = data.from_exchange(args.exchange, args.symbol, args.timeframe, 1500)
        except Exception as exc:  # Netzwerkfehler: spaeter erneut versuchen
            print(f"Kursdaten nicht abrufbar ({type(exc).__name__}: {exc}).")
            if args.once:
                raise SystemExit(1)
            time.sleep(args.interval)
            continue
        if farm is None:
            farm = Farm.start(_config(args), df, len(df) - 1)
        else:
            for i in range(len(df)):
                if str(df["timestamp"].iloc[i]) > farm.last_ts:
                    farm.step(df, i)
        if os.path.exists(stop_file) and not farm.halted:
            farm.halt("Not-Aus per STOP-Datei")
        farm.save(state)
        shown = _print_events(farm, shown)
        total = farm.total_wealth()
        print(f"{farm.last_ts}  Vermoegen {total:.2f}  gesichert {farm.vault:.2f}  Bots {len(farm.alive)}")
        if farm.halted or args.once:
            return
        time.sleep(args.interval)


def status(_args) -> None:
    state = os.path.join(STATE_DIR, "state.json")
    if not os.path.exists(state):
        raise SystemExit("Noch kein gespeicherter Stand. Starte zuerst 'paper'.")
    farm = Farm.load(state)
    print(report(farm))
    print("\nLetzte Ereignisse:")
    _print_events(farm, max(0, len(farm.events) - 15))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="botfarm", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("simulate", simulate), ("paper", paper), ("status", status)):
        p = sub.add_parser(name)
        p.set_defaults(func=func)
        if name == "status":
            continue
        p.add_argument("--capital", type=float, default=100.0, help="Startgeld des ersten Bots")
        p.add_argument("--exchange", help="Boerse fuer echte Kursdaten, z. B. kraken oder binance")
        p.add_argument("--symbol", default="BTC/EUR")
        p.add_argument("--timeframe", default="1h")
        p.add_argument("--csv", help="Kursdaten aus CSV (timestamp,open,high,low,close,volume)")
        p.add_argument("--llm", action="store_true", help="Claude schlaegt zusaetzliche Strategien vor")
        p.add_argument("--seed", type=int, default=7)
        p.add_argument("--milestones", help="Gewinn-Stufen als Vielfache des Startgelds, z. B. 3,10")
        if name == "simulate":
            p.add_argument("--bars", type=int, default=8000)
            p.add_argument("--verbose", "-v", action="store_true", help="alle Ereignisse zeigen")
        else:
            p.add_argument("--interval", type=int, default=300, help="Sekunden zwischen zwei Abfragen")
            p.add_argument("--once", action="store_true", help="nur einen Durchlauf")
    args = parser.parse_args(argv)
    args.func(args)
