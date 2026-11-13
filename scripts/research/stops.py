"""Mismo par de candidatas, simuladas trade a trade con stop de emergencia intradia."""
import os, sys, math
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import explore as E


def sim(sym, kind, stop, mk):
    d = pd.read_pickle(os.path.join(E.HERE, sym.replace("/", "-") + ".pkl"))
    o, h, l, c = (d[k].values for k in ("open", "high", "low", "close"))
    cs = d["close"]
    if kind == "trend":
        sma = cs.rolling(50).mean().values
        ent = c > sma; ex = c < sma
    else:
        r = E.rsi(cs, 2).values
        ent = (r < 15) & (c > cs.rolling(200).mean().values); ex = c > cs.rolling(5).mean().values
    cost = E.COST[mk]
    daily = np.zeros(len(c)); pos = False; e = sp = 0.0; trades = []; blocked = False
    for t in range(201, len(c)):
        # decision con el cierre de t-1, se ejecuta al open de t
        if pos and ex[t - 1]:
            daily[t] += 0  # sale al open: el retorno open->open previo ya se conto
            trades.append(o[t] / e - 1 - 2 * cost); pos = False; daily[t] -= cost
        if not pos and ent[t - 1] and not ex[t - 1]:
            # tras un stop, tendencia: no reentrar hasta que la senal se apague y vuelva
            if not (blocked and kind == "trend"):
                pos = True; e = o[t]; sp = e * (1 - stop) if stop else 0.0; daily[t] -= cost; mark = o[t]
        if kind == "trend" and not ent[t - 1]: blocked = False
        if pos:
            ref = mark
            if stop and l[t] <= sp:
                px = min(o[t], sp) if o[t] <= sp else sp
                daily[t] += px / ref - 1 - cost
                trades.append(px / e - 1 - 2 * cost); pos = False; blocked = True
            else:
                nxt = o[t + 1] if t + 1 < len(c) else c[t]
                daily[t] += nxt / ref - 1; mark = nxt
    return pd.Series(daily, index=d.index), trades, d.index


for mk, syms, kind, stops in (("cry", E.CRY, "trend", (None, 0.10, 0.15, 0.20, 0.25)),
                               ("sto", E.STO, "rsi2", (None, 0.05, 0.08, 0.10, 0.15))):
    print(f"\n## {kind} ({mk}) fuera de muestra 2024-2026")
    for st in stops:
        rs = []; tr = []
        for s in syms:
            dly, trades, idx = sim(s, kind, st, mk)
            rs.append(dly)
        P = pd.concat(rs, axis=1).fillna(0).mean(axis=1)
        P = P[P.index >= E.SPLIT]
        m = E.metrics(P, pd.Series(0.0, index=P.index), pd.Series(0.0, index=P.index), mk)
        PI = pd.concat(rs, axis=1).fillna(0).mean(axis=1); PI = PI[(PI.index < E.SPLIT) & (PI.index >= PI.index[250])]
        mi = E.metrics(PI, pd.Series(0.0, index=PI.index), pd.Series(0.0, index=PI.index), mk)
        print(f" stop={'ninguno' if not st else f'{st*100:.0f}%':8s} OOS CAGR={m['cagr']:+6.1f}% Sharpe={m['sharpe']:+.2f} maxDD={m['dd']:6.1f}%"
              f"   | hasta 2023: CAGR={mi['cagr']:+6.1f}% Sharpe={mi['sharpe']:+.2f} maxDD={mi['dd']:6.1f}%")
