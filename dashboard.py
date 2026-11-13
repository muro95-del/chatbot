"""Dashboard fuer die Bot-Farm.  Start:  streamlit run dashboard.py"""

from __future__ import annotations

import json
import os

import altair as alt
import pandas as pd
import streamlit as st

from botfarm import Farm, FarmConfig, data, strategies
from botfarm.cli import STATE_DIR
from botfarm.farm import WARMUP

STATE_FILE = os.path.join(STATE_DIR, "state.json")
LIVE_FILE = os.path.join(STATE_DIR, "live.json")
BLUE, ORANGE = "#2a78d6", "#eb6834"
STATUS = {"active": "🟢 aktiv", "probation": "🟡 Probezeit", "dead": "⚫ gestorben"}
EVENT = {"birth": "🐣 Geburt", "activate": "✅ Start", "secure": "🔒 Sicherung", "death": "💀 Tod",
         "halt": "⛔ Halt", "wait": "⏳ Warten", "llm_error": "⚠️ Claude"}

st.set_page_config(page_title="Bot-Farm", page_icon="🤖", layout="wide")


@st.cache_data(show_spinner="Simulation läuft …")
def demo_state(seed: int, capital: float) -> dict:
    df = data.synthetic(8000, seed=seed)
    first = WARMUP + 1000
    farm = Farm.start(FarmConfig(start_capital=capital, seed=seed), df, first - 1)
    for i in range(first, len(df)):
        farm.step(df, i)
    state = farm.to_dict()
    state["events"] = farm.events
    return state


def render(farm: Farm, demo: bool, real: dict | None = None) -> None:
    price, start = farm.last_price, farm.config.start_capital
    total = farm.total_wealth()
    in_bots = sum(b.account.equity(price) for b in farm.alive)

    if demo:
        st.info("Demo mit künstlichen Zufallskursen. Sie zeigt nur die Mechanik und sagt nichts über echte Gewinne aus.")
    if farm.halted:
        st.error(f"Farm angehalten: {farm.halted}")
    if not demo:
        st.caption(f"Kurs: **{price:,.2f}** · zuletzt aktualisiert: {farm.tick_ts or farm.last_ts}")
    st.caption(f"Letzte Kerze: {farm.last_ts} · {farm.bar_count} Kerzen verarbeitet"
               + ("" if real else " · nur Spielgeld"))

    if real and real.get("account"):
        acct = real["account"]
        worth = acct["quote_total"] + acct["base_total"] * price
        st.subheader("Dein Bitget-Konto")
        a = st.columns(4)
        a[0].metric(f"Gesamtwert ({acct['quote']})", f"{worth:,.2f}")
        a[1].metric(f"{acct['quote']} gesamt", f"{acct['quote_total']:,.2f}")
        a[2].metric(f"{acct['quote']} frei", f"{acct['quote_free']:,.2f}")
        a[3].metric(f"{acct['base']} im Konto", f"{acct['base_total']:.6f}")
        st.caption(f"Direkt von Bitget gelesen um {acct['ts']} (Handelskonto, {acct['base']} und {acct['quote']}). "
                   "Der Bot nutzt davon nur sein Budget.")
    elif real:
        st.warning("Der echte Kontostand konnte noch nicht von Bitget gelesen werden. Schau ins Bot-Fenster.")
    if real:
        value = real["cash"] + real["units"] * price
        armed = bool(real["orders"]) and real["orders"][-1]["real"]
        st.success(
            f"**Anteil des Bots ({real['symbol']})** · Wert {value:,.2f} bei Budget {real['budget']:,.2f} "
            f"({value / real['budget'] - 1:+.1%}) · Gebühren {real['fees']:,.2f} · {len(real['orders'])} Orders"
            + ("" if armed or not real["orders"] else " · Probelauf, es wurde nichts abgeschickt")
        )
        st.caption("Die Zahlen darunter sind die Buchführung der Farm. Sie weicht leicht vom echten Konto ab, "
                   "weil echte Ausführungen nie genau zum Schlusskurs passieren.")

    cols = st.columns(5)
    cols[0].metric("Gesamtvermögen", f"{total:,.2f}", f"{total / start - 1:+.1%} seit Start")
    cols[1].metric("Gesichert (Tresor)", f"{farm.vault:,.2f}")
    cols[2].metric("In Bots", f"{in_bots:,.2f}")
    cols[3].metric("Freie Reserve", f"{farm.treasury:,.2f}")
    cols[4].metric("Bots am Leben", len(farm.alive), f"{len(farm.bots) - len(farm.alive)} gestorben", delta_color="off", delta_arrow="off")

    st.subheader("Vermögen im Zeitverlauf")
    log = farm.wealth_log + [{"ts": farm.last_ts, "total": total, "vault": farm.vault}]
    history = pd.DataFrame(log).drop_duplicates("ts", keep="last")
    if len(history) < 2:
        st.write("Noch zu wenig Verlauf. Der erste Punkt kommt nach einer Woche Laufzeit.")
    else:
        history["Zeit"] = pd.to_datetime(history["ts"])
        long = history.rename(columns={"total": "Gesamtvermögen", "vault": "Gesichert"}).melt(
            "Zeit", ["Gesamtvermögen", "Gesichert"], "Reihe", "Betrag")
        chart = (
            alt.Chart(long)
            .mark_line(strokeWidth=2)
            .encode(
                x=alt.X("Zeit:T", title=None),
                y=alt.Y("Betrag:Q", title=None),
                color=alt.Color("Reihe:N", title=None,
                                scale=alt.Scale(domain=["Gesamtvermögen", "Gesichert"], range=[BLUE, ORANGE]),
                                legend=alt.Legend(orient="top")),
                tooltip=[alt.Tooltip("Zeit:T", format="%d.%m.%Y %H:%M"), "Reihe:N", alt.Tooltip("Betrag:Q", format=",.2f")],
            )
            .properties(height=320)
        )
        st.altair_chart(chart, width="stretch")

    st.subheader("Bots")
    if not farm.bots:
        st.write("Noch kein Bot. Bisher hat keine Strategie den Backtest bestanden.")
    else:
        only_alive = st.toggle("Nur lebende Bots zeigen", value=False)
        rows = [
            {
                "Bot": b.id,
                "Eltern": str(b.parent_id) if b.parent_id else "–",
                "Generation": b.generation,
                "Status": STATUS[b.status],
                "Position": "–" if not b.alive else ("📈 investiert" if (b.shadow or b.account).units > 0 else "💤 wartet"),
                "Startgeld": b.start_capital,
                "Konto": b.account.equity(price),
                "Gesichert": b.secured,
                "An Kinder": b.given,
                "Gewinn": b.total_profit(price),
                "Strategie": strategies.describe(b.genome),
                "Todesursache": b.death_reason,
            }
            for b in farm.bots if b.alive or not only_alive
        ]
        money = st.column_config.NumberColumn(format="%.2f")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={k: money for k in ("Startgeld", "Konto", "Gesichert", "An Kinder", "Gewinn")})

    st.subheader("Letzte Ereignisse")
    events = [
        {"Zeit": e["ts"], "Art": EVENT.get(e["type"], e["type"]), "Bot": str(e["bot"]) if e["bot"] else "–", "Was ist passiert": e["message"]}
        for e in reversed(farm.events[-40:])
    ]
    if events:
        st.dataframe(pd.DataFrame(events), hide_index=True, width="stretch")
    else:
        st.write("Noch keine Ereignisse.")


st.title("🤖 Bot-Farm")
has_state = os.path.exists(STATE_FILE)
with st.sidebar:
    st.header("Datenquelle")
    source = st.radio("Was anzeigen?", ["Laufende Farm", "Demo-Simulation"], index=0 if has_state else 1)
    if source == "Demo-Simulation":
        seed = st.number_input("Zufalls-Startwert", min_value=1, max_value=999, value=2)
        capital = st.number_input("Startgeld", min_value=10.0, value=100.0, step=10.0)
    else:
        auto = st.toggle("Live aktualisieren (alle 5 Sekunden)", value=True)
        st.button("Jetzt neu laden")

if source == "Demo-Simulation":
    render(Farm.from_dict(demo_state(int(seed), float(capital))), demo=True)
elif not has_state:
    st.warning("Es läuft noch keine Farm. Starte sie in einem Terminal und lade dann neu:")
    st.code("python -m botfarm paper --exchange bitget --symbol BTC/USDT --capital 100", language="bash")
else:
    @st.fragment(run_every=5 if auto else None)
    def live() -> None:
        real = None
        if os.path.exists(LIVE_FILE):
            with open(LIVE_FILE) as fh:
                real = json.load(fh)
        render(Farm.load(STATE_FILE), demo=False, real=real)

    live()
