import os, sys
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import explore as E

def oos(pr, ex, tu, mk):
    m = pr.index >= E.SPLIT
    return E.metrics(pr[m], ex[m], tu[m], mk)

for mk, syms, name, fn in (("cry", E.CRY, "SMA 50", lambda O, C: E.f_sma(O, C, 50)),
                           ("cry", E.CRY, "Donchian 20", lambda O, C: E.f_don(O, C, 20)),
                           ("sto", E.STO, "RSI2 dip 15", lambda O, C: E.f_rsi2(O, C, 15)),
                           ("sto", E.STO, "RSI2 dip 10", lambda O, C: E.f_rsi2(O, C, 10))):
    O, C = E.load(syms)
    print(f"\n## {name} ({mk}) fuera de muestra 2024-2026")
    base = E.COST[mk]
    for mult in (1, 2, 4):
        E.COST[mk] = base * mult
        print(E.line(f" costos x{mult} ({E.COST[mk]*100:.2f}%/lado)", oos(*E.run(O, C, fn(O, C), mk), mk)))
    E.COST[mk] = base
    # un dia extra de retraso en la ejecucion
    W = fn(O, C).shift(1).fillna(0)
    print(E.line(" +1 dia de retraso", oos(*E.run(O, C, W, mk), mk)))
    for s in syms:
        O1, C1 = O[[s]], C[[s]]
        m = oos(*E.run(O1, C1, fn(O1, C1), mk), mk)
        b = oos(*E.run(O1, C1, E.f_bh(O1, C1), mk), mk)
        print(f"   {s:9s} estrategia CAGR={m['cagr']:+6.1f}% DD={m['dd']:6.1f}% Sharpe={m['sharpe']:+.2f} | "
              f"buy&hold CAGR={b['cagr']:+6.1f}% DD={b['dd']:6.1f}% Sharpe={b['sharpe']:+.2f}")
    # operaciones individuales (RSI2): estadistica por trade en OOS
    if "RSI2" in name:
        th = int(name.split()[-1])
        r = C.apply(lambda c: E.rsi(c, 2))
        st = E.state((r < th) & (C > C.rolling(200).mean()), C > C.rolling(5).mean())
        rets = []
        for s in syms:
            h = st[s].shift(1).fillna(0).values; o = O[s].values; idx = C.index
            i = 0
            while i < len(h) - 1:
                if h[i] == 1 and (i == 0 or h[i - 1] == 0):
                    j = i
                    while j < len(h) - 1 and h[j] == 1: j += 1
                    if idx[i] >= E.SPLIT: rets.append((o[j] / o[i] - 1 - 2 * base) * 100)
                    i = j
                else: i += 1
        rs = pd.Series(rets)
        print(f"   trades OOS: n={len(rs)} aciertos={(rs > 0).mean()*100:.0f}% media={rs.mean():+.2f}% "
              f"mediana={rs.median():+.2f}% peor={rs.min():+.1f}% PF={rs[rs > 0].sum() / -rs[rs <= 0].sum():.2f} "
              f"t={rs.mean() / (rs.std() / len(rs) ** 0.5):+.2f}")
