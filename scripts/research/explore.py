"""Explora familias de estrategia en velas diarias. Senal al cierre del dia t,
ejecucion al open de t+1. Solo largos, sin apalancamiento, efectivo al 0%."""
import os, sys, math
import numpy as np, pandas as pd

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily")
CRY = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
STO = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "SPY"]
SPLIT = pd.Timestamp("2024-01-01", tz="UTC")
COST = {"cry": 0.0015, "sto": 0.0005}   # por lado
ANN = {"cry": 365, "sto": 252}


def load(syms):
    d = {s: pd.read_pickle(os.path.join(HERE, s.replace("/", "-") + ".pkl")) for s in syms}
    idx = sorted(set().union(*[x.index for x in d.values()]))
    O = pd.DataFrame({s: d[s]["open"] for s in syms}).reindex(idx)
    C = pd.DataFrame({s: d[s]["close"] for s in syms}).reindex(idx)
    return O, C


def rsi(c, n):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def state(entry, exit_):
    """1 desde que entry es True hasta que exit_ es True (por columna)."""
    out = pd.DataFrame(0.0, index=entry.index, columns=entry.columns)
    for col in entry.columns:
        e, x = entry[col].values, exit_[col].values
        s = np.zeros(len(e)); on = False
        for i in range(len(e)):
            if on and x[i]: on = False
            elif (not on) and e[i]: on = True
            s[i] = on
        out[col] = s
    return out


# ── familias: devuelven pesos objetivo por activo (fraccion del capital) al cierre de t ──
def w_equal(sig, C):
    n = C.notna().sum(axis=1).clip(lower=1)
    return sig.where(C.notna(), 0).div(n, axis=0)


def f_bh(O, C):          return w_equal(C.notna().astype(float), C)
def f_sma(O, C, n):      return w_equal((C > C.rolling(n).mean()).astype(float), C)
def f_don(O, C, n):
    hi = C.rolling(n).max(); lo = C.rolling(max(n // 2, 5)).min()
    return w_equal(state(C >= hi, C <= lo), C)
def f_rsi2(O, C, th):
    r = C.apply(lambda c: rsi(c, 2))
    return w_equal(state((r < th) & (C > C.rolling(200).mean()), C > C.rolling(5).mean()), C)
def f_bb(O, C, k):
    m = C.rolling(20).mean(); sd = C.rolling(20).std()
    return w_equal(state((C < m - k * sd) & (C > C.rolling(200).mean()), C >= m), C)
def f_rot(O, C, L, k=2, every=5):
    mom = C / C.shift(L) - 1
    W = pd.DataFrame(0.0, index=C.index, columns=C.columns)
    cur = pd.Series(0.0, index=C.columns)
    for i, t in enumerate(C.index):
        if i % every == 0:
            m = mom.loc[t].dropna(); m = m[m > 0].nlargest(k)
            cur = pd.Series(0.0, index=C.columns); cur[m.index] = 1.0 / k
        W.loc[t] = cur
    return W
def f_reg(O, C, n, ref):   # tendencia propia + regimen del activo de referencia
    ok = (C[ref] > C[ref].rolling(n).mean())
    return w_equal(((C > C.rolling(n).mean()).mul(ok, axis=0)).astype(float), C)


def run(O, C, W, mk):
    """W: pesos al cierre de t -> se tienen desde open t+1 hasta open t+2."""
    r_oo = O.shift(-1) / O - 1                 # retorno open t -> open t+1
    held = W.shift(1).fillna(0)                # peso durante [open t, open t+1]
    turn = (held - held.shift(1).fillna(0)).abs()
    pr = (held * r_oo.fillna(0)).sum(axis=1) - turn.sum(axis=1) * COST[mk]
    return pr.iloc[:-1], held.sum(axis=1).iloc[:-1], turn.sum(axis=1).iloc[:-1]


def metrics(pr, expo, turn, mk):
    if len(pr) < 30: return None
    eq = (1 + pr).cumprod(); yrs = len(pr) / ANN[mk]
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    vol = pr.std() * math.sqrt(ANN[mk])
    dd = (eq / eq.cummax() - 1).min()
    return dict(cagr=cagr * 100, vol=vol * 100, sharpe=(pr.mean() * ANN[mk]) / vol if vol > 0 else 0,
                dd=dd * 100, expo=expo.mean() * 100, turn=turn.sum() / yrs)


def line(name, m):
    if m is None: return f"{name:22s} (sin datos)"
    return (f"{name:22s} CAGR={m['cagr']:+7.1f}% vol={m['vol']:5.1f}% Sharpe={m['sharpe']:+5.2f} "
            f"maxDD={m['dd']:6.1f}% expuesto={m['expo']:4.0f}% rotacion={m['turn']:5.1f}x/a")


def main():
    for mk, syms, ref in (("cry", CRY, "BTC/USDT"), ("sto", STO, "SPY")):
        O, C = load(syms)
        fam = {"buy&hold": [("", lambda: f_bh(O, C))],
               "SMA": [(n, lambda n=n: f_sma(O, C, n)) for n in (50, 100, 200)],
               "Donchian": [(n, lambda n=n: f_don(O, C, n)) for n in (20, 55, 100)],
               "Regimen+SMA": [(n, lambda n=n: f_reg(O, C, n, ref)) for n in (50, 100, 200)],
               "Rotacion top2": [(L, lambda L=L: f_rot(O, C, L, every=7 if mk == "cry" else 5)) for L in (30, 90, 180)],
               "RSI2 dip": [(t, lambda t=t: f_rsi2(O, C, t)) for t in (5, 10, 15)],
               "Bollinger dip": [(k, lambda k=k: f_bb(O, C, k)) for k in (1.5, 2.0, 2.5)]}
        print(f"\n{'=' * 22} {'CRYPTO' if mk == 'cry' else 'STOCKS'} {'=' * 22}")
        print(f"datos {str(C.index[0])[:10]} -> {str(C.index[-1])[:10]} | corte {str(SPLIT)[:10]}")
        chosen = []
        for fname, opts in fam.items():
            best = None
            for p, fn in opts:
                pr, ex, tu = run(O, C, fn(), mk)
                warm = pr.index[250]        # mismo arranque para todas (SMA200)
                mi = metrics(pr[(pr.index >= warm) & (pr.index < SPLIT)], ex[(ex.index >= warm) & (ex.index < SPLIT)],
                             tu[(tu.index >= warm) & (tu.index < SPLIT)], mk)
                mo = metrics(pr[pr.index >= SPLIT], ex[ex.index >= SPLIT], tu[tu.index >= SPLIT], mk)
                nm = f"{fname} {p}".strip()
                print(" IS  " + line(nm, mi)); print(" OOS " + line(nm, mo))
                if best is None or mi["sharpe"] > best[1]["sharpe"]: best = (nm, mi, mo, pr)
            chosen.append(best)
        print(f"\n--- {'CRYPTO' if mk == 'cry' else 'STOCKS'}: elegida por Sharpe hasta 2023 -> resultado 2024-2026 (fuera de muestra)")
        for nm, mi, mo, pr in chosen:
            print(line(nm, mo) + f"   [IS Sharpe {mi['sharpe']:+.2f}]")
        print("    por anio (retorno %):")
        yrs = sorted(set(chosen[0][3].index.year))[-6:]
        print("    " + " " * 22 + " ".join(f"{y:>7d}" for y in yrs))
        for nm, mi, mo, pr in chosen:
            print("    " + f"{nm:22s}" + " ".join(f"{((1 + pr[pr.index.year == y]).prod() - 1) * 100:+7.1f}" for y in yrs))
        if mk == "sto":      # efecto nocturno: comprar al cierre, vender al open
            ov = (O.shift(-1) / C - 1).mean(axis=1).iloc[:-1]
            for c_, lab in ((0.0, "nocturno sin costos"), (2 * COST[mk], "nocturno con costos")):
                p2 = ov - c_
                print(" OOS " + line(lab, metrics(p2[p2.index >= SPLIT], pd.Series(1.0, index=p2.index)[p2.index >= SPLIT],
                                                   pd.Series(2.0, index=p2.index)[p2.index >= SPLIT], mk)))


if __name__ == "__main__":
    main()
