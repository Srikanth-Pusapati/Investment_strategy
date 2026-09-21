import json, random, statistics, math
P="/Users/spusapati/Personal/Investment_stratergy/state/trades.jsonl"
EXCL_REASONS={"hedge_unwind","core_defense","regime_trim","defensive_rotate","correction"}
EXCL_SYM={"QQQ","PSQ","SGOV"}
rows=[json.loads(l) for l in open(P) if l.strip()]
win=[r for r in rows if r["ts"]>="2026-08-31"]
sat=[r for r in win if r["action"]=="sell" and r.get("realized_pl") is not None and (r.get("exit_reason") or "") not in EXCL_REASONS and r["symbol"] not in EXCL_SYM]
print("ledger rows total:",len(rows),"in-window (ts>=2026-08-31):",len(win))
print("sells in window:",sum(1 for r in win if r['action']=='sell'))
print("excluded sells (system/exit_reason):",[(r['symbol'],r['exit_reason'],round(r['realized_pl'],2)) for r in win if r['action']=='sell' and r.get('realized_pl') is not None and ((r.get('exit_reason') or '') in EXCL_REASONS or r['symbol'] in EXCL_SYM)])
print("sells with null realized_pl:",[(r['symbol'],r['exit_reason']) for r in win if r['action']=='sell' and r.get('realized_pl') is None])
pl=[r["realized_pl"] for r in sat]
N=len(pl); S=sum(pl); mean=S/N
W=[x for x in pl if x>0]; L=[x for x in pl if x<=0]
WR=len(W)/N
gross_w=sum(W); gross_l=-sum(L)
PF=gross_w/gross_l if gross_l>0 else float('inf')
srt=sorted(pl,reverse=True)
top3=srt[:3]; ex3=srt[3:]
mean_ex3=sum(ex3)/len(ex3)
top3_share=sum(top3)/S if S!=0 else float('nan')
sd=statistics.stdev(pl); t=mean/(sd/math.sqrt(N))
random.seed(0)
bs=[]
for _ in range(10000):
    smp=[random.choice(pl) for _ in range(N)]
    bs.append(sum(smp)/N)
bs.sort()
lo=bs[int(0.025*10000)]; hi=bs[int(0.975*10000)-1]
lo1=bs[int(0.05*10000)]
print(f"N={N} sum={S:.2f} mean={mean:.2f} WR={WR:.3f} ({len(W)}W/{len(L)}L)")
print(f"gross_win={gross_w:.2f} gross_loss={gross_l:.2f} PF={PF:.3f}")
print(f"top3={[round(x,2) for x in top3]} top3_sum={sum(top3):.2f} top3_share_of_sum={top3_share:.3f}")
print(f"mean_ex_top3={mean_ex3:.2f} sum_ex_top3={sum(ex3):.2f}")
print(f"sd={sd:.2f} t={t:.3f} (crit one-sided 95% df={N-1}: ~1.70)")
print(f"bootstrap95 two-sided [{lo:.2f}, {hi:.2f}]  one-sided 5th pct={lo1:.2f}")
print(f"P(mean<=0) bootstrap={sum(1 for b in bs if b<=0)/10000:.3f}")
print("median=",statistics.median(pl), "avg win=",gross_w/len(W), "avg loss=",-gross_l/len(L))
print("\n--- satellite rows (ts, sym, exit_reason, realized_pl, pct, fill_price) ---")
for r in sorted(sat,key=lambda r:r['ts']):
    print(r['ts'][:19], r['symbol'].ljust(6), (r.get('exit_reason') or '').ljust(14), f"{r['realized_pl']:>10.2f}", f"{(r.get('realized_pl_pct') or 0):>7.2f}%", r.get('fill_price'), r.get('instrument'))
print("\n--- by exit_reason ---")
from collections import defaultdict
d=defaultdict(list)
for r in sat: d[r.get('exit_reason') or ''].append(r['realized_pl'])
for k,v in sorted(d.items(), key=lambda kv: sum(kv[1])):
    print(k.ljust(16), "n=",len(v), "sum=",round(sum(v),2), "wins=",sum(1 for x in v if x>0))
print("\n--- by instrument ---")
d=defaultdict(list)
for r in sat: d[r.get('instrument') or ''].append(r['realized_pl'])
for k,v in d.items(): print(k, "n=",len(v),"sum=",round(sum(v),2))
# holding period: match to buys
buys=[r for r in win if r['action']=='buy']
print("\n--- holding period (sell ts minus most recent prior buy of same symbol) ---")
from datetime import datetime
def pt(s): return datetime.fromisoformat(s.replace('Z','+00:00'))
hp=[]
for r in sorted(sat,key=lambda r:r['ts']):
    cands=[b for b in buys if b['symbol']==r['symbol'] and b['ts']<r['ts']]
    if cands:
        b=max(cands,key=lambda b:b['ts'])
        days=(pt(r['ts'])-pt(b['ts'])).total_seconds()/86400
        hp.append((r['symbol'],round(days,2),round(r['realized_pl'],2),r['exit_reason'],b['ts'][:10],r['ts'][:10],b.get('composite_score'),b.get('conviction')))
for h in hp: print(h)
short=[h for h in hp if h[1]<=1.5]
print("trips held <=1.5d:",len(short),"sum=",round(sum(h[2] for h in short),2))
long_=[h for h in hp if h[1]>1.5]
print("trips held >1.5d:",len(long_),"sum=",round(sum(h[2] for h in long_),2))
# By entry date
d=defaultdict(list)
for h in hp: d[h[4]].append(h[2])
print("\n--- realized P&L by ENTRY date ---")
for k in sorted(d): print(k,"n=",len(d[k]),"sum=",round(sum(d[k]),2))
d=defaultdict(list)
for r in sat: d[r['ts'][:10]].append(r['realized_pl'])
print("\n--- realized P&L by EXIT date ---")
for k in sorted(d): print(k,"n=",len(d[k]),"sum=",round(sum(d[k]),2))
