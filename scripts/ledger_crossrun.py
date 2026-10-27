"""Cross-run ledger statistics for the trial windows (runs 4-7), read-only.

Usage (from the repo root):
    python3 scripts/ledger_crossrun.py runs/LEDGER_CROSSRUN_<date>.md /tmp/trips.jsonl

Reads the archived trades.jsonl / equity_history.jsonl of runs 4-6 under runs/ and the live
state/ files for run-7; writes one markdown report (argv[1]) and one reconstructed trip per line
(argv[2]). Trip = sell row with non-null realized_pl (the checker definition); satellite = exit_reason
outside the system set. Entry attributes for runs 4-6 are FIFO-reconstructed (see the report's
caveats). Written 2026-10-27 for docs/MODEL_DIRECTION.md section 2b. Stdlib only.
"""
import json, math, collections, statistics, sys, os
from datetime import datetime, timedelta, timezone

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..") + "/"
RUNS = [
 ("run4", "runs/pre-final-test-run-4/state/trades.jsonl", "runs/pre-final-test-run-4/state/equity_history.jsonl"),
 ("run5", "runs/pre-final-test-run-5/state/trades.jsonl", "runs/pre-final-test-run-5/state/equity_history.jsonl"),
 ("run6", "runs/pre-final-test-run-6/state-post-window/trades.jsonl", "runs/pre-final-test-run-6/state-post-window/equity_history.jsonl"),
 ("run7", "state/trades.jsonl", "state/equity_history.jsonl"),
]
SYSTEM = {"hedge_unwind","core_defense","regime_trim","defensive_rotate","correction","beta_trim"}
SYSTEM_SYMBOLS = {"QQQ","PSQ"}
BEAR_STRATS = {"long_put","bear_put_spread"}
INVERSE = {"PSQ","SH","SQQQ","SDS","RWM"}
ET = timezone(timedelta(hours=-4))  # EDT for the whole Jul 28 - Oct 26 2026 span

def pts(s):
    if not s: return None
    s = s.replace("Z","+00:00")
    return datetime.fromisoformat(s)

# ---- t distribution (no scipy) ----
def betacf(a,b,x):
    MAXIT=300; EPS=3e-14; FPMIN=1e-300
    qab=a+b; qap=a+1; qam=a-1; c=1.0; d=1-qab*x/qap
    if abs(d)<FPMIN: d=FPMIN
    d=1/d; h=d
    for m in range(1,MAXIT+1):
        m2=2*m
        aa=m*(b-m)*x/((qam+m2)*(a+m2))
        d=1+aa*d; d=FPMIN if abs(d)<FPMIN else d
        c=1+aa/c; c=FPMIN if abs(c)<FPMIN else c
        d=1/d; h*=d*c
        aa=-(a+m)*(qab+m)*x/((a+m2)*(qap+m2))
        d=1+aa*d; d=FPMIN if abs(d)<FPMIN else d
        c=1+aa/c; c=FPMIN if abs(c)<FPMIN else c
        d=1/d; de=d*c; h*=de
        if abs(de-1)<EPS: break
    return h
def betai(a,b,x):
    if x<=0: return 0.0
    if x>=1: return 1.0
    bt=math.exp(math.lgamma(a+b)-math.lgamma(a)-math.lgamma(b)+a*math.log(x)+b*math.log(1-x))
    if x<(a+1)/(a+b+2): return bt*betacf(a,b,x)/a
    return 1-bt*betacf(b,a,1-x)/b
def t_cdf(t,df):
    x=df/(df+t*t); p=0.5*betai(df/2,0.5,x)
    return 1-p if t>0 else p
def t_ppf(q,df):
    lo,hi=0.0,200.0
    for _ in range(200):
        mid=(lo+hi)/2
        if t_cdf(mid,df)<q: lo=mid
        else: hi=mid
    return (lo+hi)/2
assert abs(t_ppf(0.95,10)-1.8125)<0.002 and abs(t_ppf(0.95,30)-1.6973)<0.002, (t_ppf(0.95,10),t_ppf(0.95,30))

# ---- signal family mapping for cited (key_signals) text ----
FAM_KEYS = [
 ("congress", ["congress","senator","representative","capitol"]),
 ("insider", ["insider"]),
 ("lobbying", ["lobby"]),
 ("govcontracts", ["govcontract","gov contract","government contract"]),
 ("options_flow", ["options flow","options_flow","call flow","put flow","c/p","call/put","flow +","flow -","call imbalance","put imbalance"]),
 ("options_chain", ["options chain","options_chain","p/c oi","put/call oi","chain +","chain -"]),
 ("technical", ["technical","uptrend","downtrend","macd","rsi","200dma","200d","50d","sma","breakdown","breakout","momentum","px>","px<"]),
 ("fundamentals", ["fundamental","margin","rev +","revenue","ebitda","eps","guidance","earnings"]),
 ("news", ["news","fda","catalyst","headline","announc","election","tariff"]),
 ("discovery", ["discovery"]),
 ("offexchange", ["offexchange","off-exchange","off exchange","short vol","short int","dark pool"]),
 ("composite", ["composite"]),
]
def fam_of(text):
    t=text.lower()
    for fam,keys in FAM_KEYS:
        if any(k in t for k in keys): return fam
    return "other"
def cited_fams(ks):
    out=set()
    for s in ks or []:
        out.add(fam_of(s))
    return out

# ---- build trips ----
def load(path):
    return [json.loads(l) for l in open(REPO+path) if l.strip()]

def build(run, rows):
    rows = sorted(rows, key=lambda r: r["ts"])
    lots = collections.defaultdict(list)   # (symbol, instrument) -> [lot]
    trips=[]; excluded=[]; unmatched=0; neg_qty=0
    for r in rows:
        key=(r["symbol"], r["instrument"])
        if r["action"]=="buy":
            lots[key].append({"row":r, "rem":float(r["qty"] or 0)})
            continue
        if r["action"]!="sell": continue
        if (r.get("qty") or 0) < 0: neg_qty+=1
        if r.get("realized_pl") is None:
            excluded.append(("pl_null",r)); continue
        reason=r.get("exit_reason") or ""
        d=r["ts"][:10]
        if run=="run7" and (d=="2026-10-27" or reason=="flatten"):
            excluded.append(("flatten/oct27",r)); continue
        if reason=="external" and float(r["realized_pl"])==0.0:
            excluded.append(("external_zero",r)); continue
        open_lots=[l for l in lots[key] if l["rem"]>1e-9]
        n_open=len(open_lots)
        first=open_lots[0]["row"] if open_lots else None
        if first is None: unmatched+=1
        q=float(r["qty"] or 0)
        for l in open_lots:
            take=min(l["rem"],q); l["rem"]-=take; q-=take
            if q<=1e-9: break
        pl=float(r["realized_pl"])
        strat=(first or {}).get("option_strategy")
        sym=r["symbol"]
        bearish = (strat in BEAR_STRATS) or (sym in INVERSE) or (r["instrument"]=="option" and len(sym)>6 and sym[-9]=="P" and sym[:-15].isalpha())
        exit_dt=pts(r["ts"]); entry_dt=pts(first["ts"]) if first else None
        # run-7 cross-check
        ledger_entry = pts(r.get("entry_ts")) if r.get("entry_ts") else None
        trips.append({
          "run":run,"symbol":sym,"instrument":r["instrument"],"strategy":strat,
          "reason":reason,"system":reason in SYSTEM,"system_sym":sym in SYSTEM_SYMBOLS,
          "pl":pl,"pct":r.get("realized_pl_pct"),"qty":float(r["qty"] or 0),
          "exit_dt":exit_dt,"entry_dt":entry_dt,"ledger_entry_dt":ledger_entry,
          "conv":(first or {}).get("conviction"),"ledger_conv":r.get("entry_conviction"),
          "comp":(first or {}).get("composite_score"),"ledger_comp":r.get("entry_composite"),
          "entry_signals":(first or {}).get("entry_signals") or [],
          "key_signals":(first or {}).get("key_signals") or [],
          "n_open":n_open,"ledger_lots_n":r.get("lots_n"),
          "bearish":bearish,"rationale":r.get("rationale") or "",
          "cost":(first or {}).get("cost_usd"),
        })
    return trips, excluded, unmatched, neg_qty

ALL={}; EXCL={}; META={}
for run,tp,ep in RUNS:
    rows=load(tp)
    trips,excl,unm,neg=build(run,rows)
    ALL[run]=trips; EXCL[run]=excl
    META[run]={"rows":len(rows),"buys":sum(1 for r in rows if r["action"]=="buy"),"sells":sum(1 for r in rows if r["action"]=="sell"),
               "first":min(r["ts"] for r in rows)[:10],"last":max(r["ts"] for r in rows)[:10],"unmatched":unm,"neg_qty":neg}
POOL=[t for r in ("run5","run6","run7") for t in ALL[r]]
R4WIN=[t for t in ALL["run4"] if "2026-08-03"<=t["exit_dt"].strftime("%Y-%m-%d")<="2026-08-14"]
COLS=[("run4",ALL["run4"]),("run5",ALL["run5"]),("run6",ALL["run6"]),("run7",ALL["run7"]),("pool5-7",POOL)]

def money(x):
    if x is None: return "—"
    s=f"{abs(x):,.0f}"
    return ("−$" if x<0 else "$")+s
def stats(ts):
    pls=[t["pl"] for t in ts]; n=len(pls)
    if n==0: return {"n":0}
    mean=sum(pls)/n; med=statistics.median(pls); tot=sum(pls)
    sd=statistics.stdev(pls) if n>1 else None
    lb=(mean - t_ppf(0.95,n-1)*sd/math.sqrt(n)) if n>1 else None
    wr=sum(1 for p in pls if p>0)/n
    return {"n":n,"wr":wr,"mean":mean,"med":med,"tot":tot,"sd":sd,"lb":lb}
def cell(ts):
    s=stats(ts)
    if s["n"]==0: return "—"
    return f"N={s['n']} WR={s['wr']*100:.0f}% μ={money(s['mean'])} Σ={money(s['tot'])}"
def table(title, groups, filt=lambda t: True, note=None):
    """groups: list of (label, predicate)"""
    out=[f"\n**{title}**\n"]
    if note: out.append(note+"\n")
    out.append("| bucket | "+" | ".join(c for c,_ in COLS)+" |")
    out.append("|---|"+"---|"*len(COLS))
    for label,pred in groups:
        out.append(f"| {label} | "+" | ".join(cell([t for t in ts if filt(t) and pred(t)]) for _,ts in COLS)+" |")
    return "\n".join(out)

L=[]
L.append("# Cross-run ledger analysis (runs 4–7) — generated 2026-10-27, read-only\n")
L.append("## (a) Schema notes\n")
L.append("- All four ledgers are **per-fill** rows (`action` = buy|sell), one JSON object per line. A **trip** = one `sell` row with non-null `realized_pl` (the project's own checker, `scripts/eval_contract_check.py`, uses the same definition). Sell rows carry `realized_pl`, `realized_pl_pct`, `exit_reason`, `exit_price`, `qty`, `rationale`; they carry **no** entry attributes (`entry_price`=0, `conviction`=0) in runs 4–6, so each sell was FIFO-matched to the open buy lot(s) of the same `(symbol, instrument)` to recover conviction / composite / signals / entry time / lot count.")
L.append("- Common keys (all runs): ts, symbol, action, instrument (equity|option), qty, entry_price, cost_usd, conviction, take_profit_pct, stop_loss_pct, take_profit_price, stop_loss_price, exit_price, rationale, key_signals (cited, free text), entry_signals (families fed), realized_pl_pct, realized_pl, verdict, risk_note, exit_reason, option_strategy, order_id, composite_score.")
L.append("- Drift: run-4 has `underlying`/`occ_symbols`/`repair_note` on only 2 rows and keys options by **underlying** symbol (MSFT, AMZN, PLTR, HL; no OCC symbols on buys); run-5 adds `underlying`, `occ_symbols`, `repair_note` on every row and keys single-leg options by OCC symbol, spreads by underlying; run-6 adds `occ_sides`, `fill_price`, `fill_qty`, `fill_ts`; run-7 adds `sell_events`, `quote_exit_price`, `quote_realized_pl(_pct)`, `spy_intraday_ret_at_decision`, `regime_label`, `falling_names`, `would_haircut_usd`, `stop_pct_if_floor_6`, `vol_stop_raw_pct`, `floor6_*`, and on **sell rows** `entry_ts`, `entry_fill_price`, `entry_fill_source`, `entry_composite`, `entry_conviction`, `entry_stop_pct`, `entry_key_signals`, `lots_n`, `quote_cost_usd`.")
L.append("- `entry_signals` lists the signal **families available/fed** at decision (news/technical/fundamentals appear on ~95% of buys, so it is weakly discriminating); `key_signals` is the model's **cited** list — used here for 'primary/first-cited' (first element, keyword-mapped to a family) and 'cited-any'.")
L.append("- System core/hedge fills (QQQ, PSQ, SGOV) are buy rows with conviction 0.0, composite null, `entry_signals` = [core_fill|auto_hedge|defensive_fill].")
L.append("- Equity history: one row per session {date, ts, equity, cash, unrealized_pl, realized_pl, total_return, day_pl}; run-6+ add `basis`, `book_beta_spy`; run-7 adds `broker_day_pl`, `day_pl_basis`.")
L.append("- Actual ledger date spans: " + "; ".join(f"{r}: {META[r]['first']} → {META[r]['last']} ({META[r]['rows']} rows = {META[r]['buys']} buys + {META[r]['sells']} sells)" for r,_,_ in RUNS) + ". Run-4's ledger starts Jul 28 (the Jul 27 fresh-cycle reset), not Jul 7. run-6 `state/` (52 rows, to Sep 11) is a strict prefix of `state-post-window/` (93 rows, to Sep 21); the latter was used.")
L.append("- Exclusions: run-4 2 rows `exit_reason=external` with realized_pl 0.0 (Aug 17 AMZN option legs that vanished broker-side; the 2-contract AMZN call shows exit_price 22.0 vs entry 46.75 but pl recorded 0.0 — unrecorded loss). run-7: **0** rows dated 2026-10-27 and **0** rows with exit_reason 'flatten' — the Oct 27 flatten is not in this ledger. "
         + "; ".join(f"{r}: sells unmatched to any buy={META[r]['unmatched']}, negative-qty sells={META[r]['neg_qty']}" for r,_,_ in RUNS) + ".")
L.append("\nExample rows (one per file, truncated):\n")
for run,tp,_ in RUNS:
    rows=load(tp)
    ex=[r for r in rows if r["action"]=="sell"][0]
    s=json.dumps(ex)
    L.append(f"- {run} sell: `{s[:420]}…`")
rows=load("state/trades.jsonl"); ex=[r for r in rows if r["action"]=="buy" and r["instrument"]=="equity" and r["conviction"]>0][0]
L.append(f"- run7 buy: `{json.dumps(ex)[:420]}…`")
rows=load("runs/pre-final-test-run-4/state/trades.jsonl"); ex=[r for r in rows if r["action"]=="buy" and r["instrument"]=="option"][0]
L.append(f"- run4 option buy: `{json.dumps(ex)[:300]}…`")

# ---- 1. headline ----
L.append("\n## (b) Tables\n\nCell format: N / win rate / mean P&L per trip / total P&L. Pooled = runs 5+6+7. Run-4 is kept separate (several change-sets inside it).\n")
L.append("### 1. Headline — all trips, satellite (model) trips, system trips\n")
L.append("| scope | run | N | WR | mean | median | total | sd | 95% one-sided LB of mean |")
L.append("|---|---|---|---|---|---|---|---|---|")
for scope,pred in [("all",lambda t:True),("satellite (exit_reason ∉ system set)",lambda t: not t["system"]),("system exits",lambda t: t["system"])]:
    for c,ts in COLS+[("run4 Aug3–14 window",R4WIN)]:
        s=stats([t for t in ts if pred(t)])
        if s["n"]==0: L.append(f"| {scope} | {c} | 0 | — | — | — | — | — | — |"); continue
        L.append(f"| {scope} | {c} | {s['n']} | {s['wr']*100:.0f}% | {money(s['mean'])} | {money(s['med'])} | {money(s['tot'])} | {money(s['sd']) if s['sd'] is not None else '—'} | {money(s['lb']) if s['lb'] is not None else '—'} |")

# satellite with the checker's symbol filter too
L.append("\nSatellite with the project checker's extra symbol filter (QQQ/PSQ rows dropped regardless of exit_reason):\n")
L.append("| run | N | WR | mean | total | 95% LB |"); L.append("|---|---|---|---|---|---|")
for c,ts in COLS:
    s=stats([t for t in ts if not t["system"] and not t["system_sym"]])
    L.append(f"| {c} | {s['n']} | {s['wr']*100:.0f}% | {money(s['mean'])} | {money(s['tot'])} | {money(s['lb']) if s.get('lb') is not None else '—'} |")

# ---- 3. concentration ----
L.append("\n### 3. Concentration of satellite P&L (top-3 winners; ex-top-3 mean = kill-criterion input)\n")
L.append("| run | N sat | net total sat | top-3 trips | top-3 sum | top-3 ÷ net total | top-3 share of gross wins | mean ex-top-3 | WR ex-top-3 | 95% LB ex-top-3 |")
L.append("|---|---|---|---|---|---|---|---|---|---|")
for c,ts in COLS+[("run4 Aug3–14 window",R4WIN)]:
    sat=[t for t in ts if not t["system"]]
    if not sat: continue
    srt=sorted(sat,key=lambda t:-t["pl"]); top=srt[:3]; rest=srt[3:]
    ts_=sum(t["pl"] for t in sat); tsum=sum(t["pl"] for t in top)
    share = (f"{tsum/ts_:.2f}×" if ts_>0 else "net total ≤ 0")
    gross=sum(t["pl"] for t in sat if t["pl"]>0)
    gshare = f"{tsum/gross*100:.0f}% of {money(gross)}" if gross else "—"
    rs=stats(rest)
    L.append(f"| {c} | {len(sat)} | {money(ts_)} | "+", ".join(f"{t['symbol']} {money(t['pl'])} ({t['reason']})" for t in top)+f" | {money(tsum)} | {share} | {gshare} | {money(rs['mean']) if rs['n'] else '—'} | {rs['wr']*100:.0f}% | {money(rs['lb']) if rs.get('lb') is not None else '—'} |")
L.append("\nSame, satellite with QQQ/PSQ symbol filter (checker definition): " + "; ".join(
    (lambda sat: f"{c}: N={len(sat)}, ex-top-3 mean={money(stats(sorted(sat,key=lambda t:-t['pl'])[3:])['mean']) if len(sat)>3 else '—'}")([t for t in ts if not t['system'] and not t['system_sym']]) for c,ts in COLS))
# losers concentration too
L.append("\nMirror — bottom-3 losers share of satellite losses: " + "; ".join(
    (lambda sat: (lambda losses: f"{c}: bottom-3 {money(sum(sorted(losses)[:3]))} = {sum(sorted(losses)[:3])/sum(losses)*100:.0f}% of all losses {money(sum(losses))}" if losses else f"{c}: no losses")([t['pl'] for t in sat if t['pl']<0]))([t for t in ts if not t['system']]) for c,ts in COLS))

# ---- 4. exit reason ----
reasons=sorted({t["reason"] for ts in ALL.values() for t in ts}, key=lambda r:(r in SYSTEM, r))
L.append("\n### 4. By exit reason\n")
L.append(table("exit_reason (system set marked †)", [((r+" †") if r in SYSTEM else r, (lambda r: lambda t: t["reason"]==r)(r)) for r in reasons]))
dec=lambda t: t["reason"]=="decision"
L.append(table("'decision' exits split by rationale source", [
 ("watchdog decision (rationale starts 'watchdog decision')", lambda t: dec(t) and t["rationale"].startswith("watchdog decision")),
 ("model/other decision text", lambda t: dec(t) and not t["rationale"].startswith("watchdog decision")),
 ("exchange-side backfill rows (any reason)", lambda t: t["rationale"].startswith("exchange-side exit backfill")),
]))
L.append(table("Exit mechanism groups", [
 ("stops (bracket_stop+stop)", lambda t: t["reason"] in ("bracket_stop","stop")),
 ("takes (bracket_take+take)", lambda t: t["reason"] in ("bracket_take","take")),
 ("trail", lambda t: t["reason"]=="trail"),
 ("decision", dec),
 ("time", lambda t: t["reason"]=="time"),
 ("system †", lambda t: t["system"]),
]))

# ---- 5. asset class / options / direction ----
L.append("\n### 5. Equity vs options; bullish vs bearish\n")
L.append(table("Asset class (all trips)", [
 ("equity", lambda t: t["instrument"]=="equity"),
 ("option (any)", lambda t: t["instrument"]=="option"),
 ("option: long_call", lambda t: t["strategy"]=="long_call"),
 ("option: bull_call_spread", lambda t: t["strategy"]=="bull_call_spread"),
 ("option: long_put", lambda t: t["strategy"]=="long_put"),
 ("option: bear_put_spread", lambda t: t["strategy"]=="bear_put_spread"),
 ("option: strategy unknown (unmatched buy)", lambda t: t["instrument"]=="option" and t["strategy"] is None),
]))
L.append(table("Direction (all trips)", [
 ("bullish (equity long + calls)", lambda t: not t["bearish"]),
 ("bearish (puts + inverse-ETF hedge PSQ)", lambda t: t["bearish"]),
 ("bearish, satellite only (puts)", lambda t: t["bearish"] and not t["system"]),
 ("bullish, satellite only", lambda t: (not t["bearish"]) and not t["system"]),
 ("bullish equity, satellite only", lambda t: (not t["bearish"]) and not t["system"] and t["instrument"]=="equity"),
]))

# ---- 6. signals ----
L.append("\n### 6. By signal family (satellite trips only; first-lot attributes)\n")
fams=["congress","insider","lobbying","govcontracts","options_flow","options_chain","technical","fundamentals","news","discovery","offexchange","composite","other"]
sat=lambda t: not t["system"]
L.append(table("Primary = family of the FIRST cited key_signal (keyword-mapped)", [(f, (lambda f: lambda t: (fam_of(t["key_signals"][0]) if t["key_signals"] else "none")==f)(f)) for f in fams]+[("none cited / no match", lambda t: not t["key_signals"])], filt=sat))
L.append(table("Cited-any = family appears anywhere in key_signals", [(f, (lambda f: lambda t: f in cited_fams(t["key_signals"]))(f)) for f in fams if f not in ("other",)], filt=sat))
efams=["congress","insider","lobbying","govcontracts","options_flow","options_chain","technical","fundamentals","news","discovery","offexchange"]
L.append(table("Fed-any = family present in entry_signals (available at decision)", [(f, (lambda f: lambda t: f in t["entry_signals"])(f)) for f in efams]+[("congress NOT fed", lambda t: "congress" not in t["entry_signals"] and t["entry_signals"]),("insider NOT fed", lambda t: "insider" not in t["entry_signals"] and t["entry_signals"])], filt=sat))

# ---- 7. conviction / composite ----
L.append("\n### 7. By conviction and composite (satellite trips; first-lot values)\n")
def cb(t):
    c=t["conv"]
    if c is None: return "unknown"
    if c==0: return "0 (system fill)"
    if c<0.6: return "<0.6"
    if c<0.75: return "0.6–0.75"
    if c<0.9: return "0.75–0.9"
    return "≥0.9"
L.append(table("Conviction bucket (requested)", [(b,(lambda b: lambda t: cb(t)==b)(b)) for b in ["<0.6","0.6–0.75","0.75–0.9","≥0.9","0 (system fill)","unknown"]], filt=sat))
L.append(table("Conviction, finer (nearly all mass is 0.60–0.75)", [
 ("=0.60 exactly", lambda t: t["conv"] is not None and abs(t["conv"]-0.60)<1e-9),
 ("0.61–0.63", lambda t: t["conv"] is not None and 0.605<t["conv"]<0.635),
 ("0.64–0.69", lambda t: t["conv"] is not None and 0.635<=t["conv"]<0.695),
 ("≥0.70", lambda t: t["conv"] is not None and t["conv"]>=0.695),
], filt=sat))
def pb(t):
    c=t["comp"]
    if c is None: return "null (options/system)"
    if c<1.0: return "<1.0"
    if c<1.5: return "1.0–1.5"
    if c<2.0: return "1.5–2.0"
    return "≥2.0"
L.append(table("Composite score bucket", [(b,(lambda b: lambda t: pb(t)==b)(b)) for b in ["<1.0","1.0–1.5","1.5–2.0","≥2.0","null (options/system)"]], filt=sat))

# ---- 8. entry time ----
L.append("\n### 8. By entry time of day (ET, first lot's buy timestamp; satellite trips)\n")
def tb(t):
    if not t["entry_dt"]: return "unknown"
    e=t["entry_dt"].astimezone(ET); m=e.hour*60+e.minute
    if m<9*60+45: return "09:30–09:45"
    if m<10*60+30: return "09:45–10:30"
    if m<12*60: return "10:30–12:00"
    if m<14*60: return "12:00–14:00"
    return "14:00–16:00"
L.append(table("Entry ET bucket", [(b,(lambda b: lambda t: tb(t)==b)(b)) for b in ["09:30–09:45","09:45–10:30","10:30–12:00","12:00–14:00","14:00–16:00","unknown"]], filt=sat))
L.append(table("Entry ET bucket — all trips incl. system", [(b,(lambda b: lambda t: tb(t)==b)(b)) for b in ["09:30–09:45","09:45–10:30","10:30–12:00","12:00–14:00","14:00–16:00","unknown"]]))

# ---- 9. holding ----
L.append("\n### 9. By holding period (exit ts − first-lot buy ts; satellite trips)\n")
def hb(t):
    if not t["entry_dt"]: return "unknown"
    d=(t["exit_dt"]-t["entry_dt"]).total_seconds()/86400
    if d<1: return "<1 day"
    if d<3: return "1–3 d"
    if d<7: return "3–7 d"
    return ">7 d"
L.append(table("Holding bucket", [(b,(lambda b: lambda t: hb(t)==b)(b)) for b in ["<1 day","1–3 d","3–7 d",">7 d","unknown"]], filt=sat))
# mean hold of winners vs losers
L.append("\nMean holding days, satellite winners vs losers: " + "; ".join(
    (lambda w,l: f"{c}: winners {statistics.mean(w):.1f} d (n={len(w)}), losers {statistics.mean(l):.1f} d (n={len(l)})" if w and l else f"{c}: —")(
      [(t['exit_dt']-t['entry_dt']).total_seconds()/86400 for t in ts if sat(t) and t['entry_dt'] and t['pl']>0],
      [(t['exit_dt']-t['entry_dt']).total_seconds()/86400 for t in ts if sat(t) and t['entry_dt'] and t['pl']<=0]) for c,ts in COLS))

# ---- 10. lots ----
L.append("\n### 10. By lot sequence (open buy lots for the symbol at exit time, FIFO-derived; satellite trips)\n")
L.append(table("Lots open at exit", [
 ("single lot (no top-up)", lambda t: t["n_open"]==1),
 ("topped-up (≥2 lots open)", lambda t: t["n_open"]>=2),
 ("unmatched (0 lots)", lambda t: t["n_open"]==0),
], filt=sat))
r7=ALL["run7"]
mm=sum(1 for t in r7 if t["ledger_lots_n"] is not None and t["ledger_lots_n"]!=t["n_open"])
L.append(f"\nrun-7 ledger `lots_n` vs FIFO-derived open-lot count: {mm}/{len(r7)} sells differ (ledger counts lots per bracket leg for bracket exits; e.g. INTC bracket_stop lots_n=1 while 3 lots were open). run-7 ledger lots_n distribution: {dict(collections.Counter(t['ledger_lots_n'] for t in r7))}.")
cc=sum(1 for t in r7 if t["ledger_conv"] is not None and t["conv"] is not None and abs(t["ledger_conv"]-t["conv"])>1e-9)
ce=sum(1 for t in r7 if t["ledger_entry_dt"] and t["entry_dt"] and abs((t["ledger_entry_dt"]-t["entry_dt"]).total_seconds())>120)
L.append(f"run-7 cross-check of FIFO matching against the ledger's own `entry_conviction`/`entry_ts`: conviction mismatches {cc}/{len(r7)}, entry_ts mismatches (>2 min) {ce}/{len(r7)}.")

# ---- 11. equity ----
L.append("\n### 11. Equity curve (equity_history.jsonl, one row per session)\n")
L.append("| run | first row (date, equity) | last row (date, equity) | return first→last | return vs $1,000,000 nominal | peak equity (date) | max drawdown (peak date → trough date) | sessions | realized_pl at last row (equity file) |")
L.append("|---|---|---|---|---|---|---|---|---|")
for run,_,ep in RUNS:
    eq=load(ep); eq=sorted(eq,key=lambda r:r["ts"])
    s,e=eq[0],eq[-1]
    peak=-1; pd=None; mdd=0; tdate=None; pkd=None
    for r in eq:
        if r["equity"]>peak: peak=r["equity"]; pd=r["date"]
        dd=(r["equity"]-peak)/peak
        if dd<mdd: mdd=dd; tdate=r["date"]; pkd=pd
    pk=max(eq,key=lambda r:r["equity"])
    L.append(f"| {run} | {s['date']} {money(s['equity'])} | {e['date']} {money(e['equity'])} | {(e['equity']/s['equity']-1)*100:+.2f}% | {(e['equity']/1e6-1)*100:+.2f}% | {money(pk['equity'])} ({pk['date']}) | {mdd*100:.2f}% ({pkd} → {tdate}) | {len({r['date'] for r in eq})} | {money(e.get('realized_pl'))} |")
L.append("\nLedger realized P&L (sum of trips, all exits) for comparison: " + "; ".join(f"{c}: {money(sum(t['pl'] for t in ts))} (satellite {money(sum(t['pl'] for t in ts if not t['system']))}, system {money(sum(t['pl'] for t in ts if t['system']))})" for c,ts in COLS))

# dump trips for my own review
with open(sys.argv[1],"w") as f: f.write("\n".join(L)+"\n")
with open(sys.argv[2],"w") as f:
    for ts in ALL.values():
        for t in ts:
            d=dict(t); d["exit_dt"]=str(d["exit_dt"]); d["entry_dt"]=str(d["entry_dt"]); d["ledger_entry_dt"]=str(d["ledger_entry_dt"]); d["key_signals"]=d["key_signals"][:2]; d["rationale"]=d["rationale"][:60]
            f.write(json.dumps(d, default=str)+"\n")
print("ok", {r:len(v) for r,v in ALL.items()}, "excluded:", {r:[(k,x['symbol'],x['realized_pl']) for k,x in v] for r,v in EXCL.items()})
