"""Range-forecast sizing A/B on the Apex Master v2 trade series. Spec: PREREG.md (hashed before this ran)."""
import json, sys
import numpy as np
import pandas as pd
import yfinance as yf

END = pd.Timestamp("2026-11-05")
SEED = 20261106
TICK = ["SPY", "QQQ", "IWM"]
MINTR, STEP = 1500, 21


def dl(t, start):
    d = yf.download(t, start=start, progress=False, auto_adjust=False)
    d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
    return d[d.index <= END].dropna(subset=["Open", "High", "Low", "Close"])


def forecast(px, vix):
    """Walk-forward HAR-X. Returns DataFrame indexed by the day being forecast (t+1) with fit, sd, actual range."""
    rng = (px["High"] - px["Low"]) / px["Close"].shift(1)
    d = pd.DataFrame({"rng": rng, "vix": vix}).dropna()
    d = d[d["rng"] > 0]
    d["x1"] = np.log(d["rng"]); d["x5"] = np.log(d["rng"].rolling(5).mean()); d["x22"] = np.log(d["rng"].rolling(22).mean())
    d["xv"] = np.log(d["vix"]); d["y"] = d["x1"].shift(-1); d["next_rng"] = d["rng"].shift(-1)
    d["target_date"] = pd.Series(d.index, index=d.index).shift(-1)
    z = d.dropna().copy()
    X = np.column_stack([np.ones(len(z)), z["x1"], z["x5"], z["x22"], z["xv"]]); y = z["y"].values
    fit = np.full(len(z), np.nan); sd = np.full(len(z), np.nan)
    i = MINTR
    while i < len(z):
        b, *_ = np.linalg.lstsq(X[:i], y[:i], rcond=None)
        s = (y[:i] - X[:i] @ b).std(ddof=5)
        j = min(i + STEP, len(z)); fit[i:j] = X[i:j] @ b; sd[i:j] = s; i = j
    z["fit"] = fit; z["sd"] = sd
    z = z.dropna(subset=["fit"])
    out = pd.DataFrame({"F": np.exp(z["fit"]), "sd": z["sd"], "actual": z["next_rng"], "naive": z["rng"]}, index=pd.DatetimeIndex(z["target_date"]))
    return out


def main():
    res = {}
    vix = dl("^VIX", "1990-01-01")["Close"]
    px = {t: dl(t, "1993-01-01") for t in TICK}
    # ---- trade series (reproduction)
    vprev = vix.shift(1)
    cand = {}
    for t in TICK:
        p = px[t]; ibs = (p["Close"] - p["Low"]) / (p["High"] - p["Low"])
        c = pd.DataFrame({"ibs_prev": ibs.shift(1), "vix_prev": vprev.reindex(p.index), "ret": (p["Close"] / p["Open"] - 1) * 1e4 - 2.0})
        cand[t] = c.dropna()
    days = sorted(set().union(*[set(c.index) for c in cand.values()]))
    rows = []
    for d in days:
        for t in TICK:
            c = cand[t]
            if d in c.index and c.at[d, "vix_prev"] >= 20 and c.at[d, "ibs_prev"] < 0.10:
                rows.append({"date": d, "ticker": t, "r": c.at[d, "ret"], "vix_prev": c.at[d, "vix_prev"]}); break
    tr = pd.DataFrame(rows).set_index("date")
    res["reproduction"] = {"n": int(len(tr)), "mean_bp": round(float(tr["r"].mean()), 2), "claimed_n": 734, "claimed_mean_bp": 18.19,
                           "n_ok": bool(abs(len(tr) / 734 - 1) <= 0.05), "mean_ok": bool(abs(tr["r"].mean() - 18.19) <= 3.0),
                           "first": str(tr.index.min().date()), "last": str(tr.index.max().date())}
    if not (res["reproduction"]["n_ok"] and res["reproduction"]["mean_ok"]):
        res["stopped"] = "reproduction gate failed"; json.dump(res, open("results.json", "w"), indent=1); print(json.dumps(res, indent=1)); return
    # ---- forecasts, accuracy gate
    F = {t: forecast(px[t], vix.reindex(px[t].index).ffill()) for t in TICK}
    acc = {}
    for t in TICK:
        o = F[t][F[t].index >= "2012-01-01"]
        mm = float((o["F"] - o["actual"]).abs().mean()); mn = float((o["naive"] - o["actual"]).abs().mean())
        lo = o["F"] * np.exp(-1.2816 * o["sd"]); hi = o["F"] * np.exp(1.2816 * o["sd"])
        cov = float(((o["actual"] >= lo) & (o["actual"] <= hi)).mean())
        acc[t] = {"n": int(len(o)), "improvement_pct": round(100 * (1 - mm / mn), 2), "coverage_pct": round(100 * cov, 2),
                  "pass": bool(100 * (1 - mm / mn) >= 15 and 76 <= 100 * cov <= 86)}
    res["accuracy_gate"] = acc
    # ---- weights
    medF = {t: F[t]["F"].rolling(252, min_periods=126).median().shift(1) for t in TICK}  # forecasts known before D (strictly earlier days)
    medV = vix.rolling(252, min_periods=126).median().shift(1)
    wA, wC, keep = [], [], []
    for d, row in tr.iterrows():
        t = row["ticker"]
        if d in F[t].index and not np.isnan(medF[t].get(d, np.nan)) and d in medV.index and not np.isnan(medV.get(d, np.nan)):
            wA.append(float(np.clip(medF[t][d] / F[t].at[d, "F"], 0.5, 2.0)))
            wC.append(float(np.clip(medV[d] / row["vix_prev"], 0.5, 2.0))); keep.append(d)
    t2 = tr.loc[keep].copy(); t2["wA"] = wA; t2["wC"] = wC
    r = t2["r"].values; WA = t2["wA"].values; WC = t2["wC"].values

    def ir(x): return x.mean() / x.std(ddof=1)
    def maxdd(x, w):
        c = np.cumsum(x); return float((c - np.maximum.accumulate(np.concatenate([[0], c]))[1:]).min() / w.mean())
    base = ir(r); dA = ir(r * WA) - base; dC = ir(r * WC) - base
    rng = np.random.default_rng(SEED); n = len(r); bA, bC, bD = [], [], []
    for _ in range(10000):
        i = rng.integers(0, n, n); rr = r[i]; b = ir(rr)
        a = ir(rr * WA[i]) - b; c = ir(rr * WC[i]) - b; bA.append(a); bC.append(c); bD.append(a - c)
    q = lambda v: [round(float(np.percentile(v, 2.5)), 4), round(float(np.percentile(v, 97.5)), 4)]
    res["ab"] = {"n_trades": int(n), "first": str(t2.index.min().date()), "baseline_IR": round(float(base), 4), "baseline_mean_bp": round(float(r.mean()), 2),
                 "dIR_A": round(float(dA), 4), "dIR_A_ci": q(bA), "dIR_C": round(float(dC), 4), "dIR_C_ci": q(bC),
                 "dIR_A_minus_C": round(float(dA - dC), 4), "dIR_A_minus_C_ci": q(bD),
                 "mean_w_A": round(float(WA.mean()), 3), "mean_w_C": round(float(WC.mean()), 3),
                 "mean_bp_per_unit_weight": {"base": round(float(r.mean()), 2), "A": round(float((r * WA).sum() / WA.sum()), 2), "C": round(float((r * WC).sum() / WC.sum()), 2)},
                 "maxdd_bp_per_mean_weight": {"base": round(maxdd(r, np.ones(n)), 0), "A": round(maxdd(r * WA, WA), 0), "C": round(maxdd(r * WC, WC), 0)}}
    yr = t2.index.year
    by = {}
    for y_ in sorted(set(yr)):
        m = yr == y_; by[int(y_)] = {"n": int(m.sum()), "base_bp": round(float(r[m].sum()), 0), "A_bp": round(float((r * WA)[m].sum() / WA.mean()), 0)}
    res["by_year_total_bp"] = by
    m15 = t2.index >= "2015-01-01"
    res["subset_2015plus"] = {"n": int(m15.sum()), "dIR_A": round(float(ir(r[m15] * WA[m15]) - ir(r[m15])), 4), "dIR_C": round(float(ir(r[m15] * WC[m15]) - ir(r[m15])), 4)}
    gate = all(a["pass"] for a in acc.values())
    res["decision"] = {"accuracy_gate_all": gate, "dIR_A_ci_lower_gt0": bool(q(bA)[0] > 0), "A_beats_C_ci_lower_gt0": bool(q(bD)[0] > 0),
                       "PASS": bool(gate and q(bA)[0] > 0 and q(bD)[0] > 0)}
    json.dump(res, open("results.json", "w"), indent=1); print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
