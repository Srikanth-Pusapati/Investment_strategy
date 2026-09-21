import json, random, statistics, math
from datetime import datetime
from collections import defaultdict
P="/Users/spusapati/Personal/Investment_stratergy/state/trades.jsonl"
SYS_REASONS={"hedge_unwind","core_defense","regime_trim","defensive_rotate","correction"}
SYS_SYM={"QQQ","PSQ"}   # v3 --system-symbols
rows=[json.loads(l) for l in open(P) if l.strip()]
win=[r for r in rows if r["ts"]>="2026-08-31"]
sells=[r for r in win if r["action"]=="sell"]
print("in-window rows",len(win),"sells",len(sells))
print("exit_reason counts:",{k:sum(1 for r in sells if (r.get('exit_reason') or '')==k) for k in set((r.get('exit_reason') or '') for r in sells)})
print("null realized_pl sells:",[(r['ts'][:16],r['symbol'],r.get('exit_reason')) for r in sells if r.get('realized_pl') is None])
sat=[r for r in sells if r.get("realized_pl") is not None and (r.get("exit_reason") or "") not in SYS_REASONS and r["symbol"] not in SYS_SYM]
# duplicate/phantom drop per v3 rule 1
def pt(s): return datetime.fromisoformat(s.replace('Z','+00:00'))
kept=[]; dropped=[]
for r in sorted(sat,key=lambda r:r['ts']):
    if (r.get('qty') or 0)<=0: dropped.append(('qty<=0',r['symbol'],r['ts'][:16])); continue
    dup=False
    for k in kept:
        if k['symbol']==r['symbol'] and k.get('qty')==r.get('qty') and k.get('realized_pl')==r.get('realized_pl') and (k.get('exit_reason') or '')==(r.get('exit_reason') or '') and abs((pt(r['ts'])-pt(k['ts'])).total_seconds())<=4*3600:
            dup=True; break
    if dup: dropped.append(('dup',r['symbol'],r['ts'][:16]))
    else: kept.append(r)
print("v3 phantom/dup dropped:",dropped)
sat=kept
pl=[r["realized_pl"] for r in sat]
N=len(pl); S=sum(pl); mean=S/N
W=[x for x in pl if x>0]; L=[x for x in pl if x<=0]
PF=sum(W)/(-sum(L))
srt=sorted(pl,reverse=True); top3=srt[:3]; ex3=srt[3:]
sd=statistics.stdev(pl); t=mean/(sd/math.sqrt(N))
random.seed(0); bs=sorted(sum(random.choice(pl) for _ in range(N))/N for _ in range(10000))
print(f"N={N} sum={S:.2f} mean={mean:.2f} WR={len(W)/N:.3f} ({len(W)}W/{len(L)}L) PF={PF:.3f} top3={[round(x,2) for x in top3]} mean_ex_top3={sum(ex3)/len(ex3):.2f} t={t:.3f} boot95=[{bs[250]:.2f},{bs[9749]:.2f}]")
# with SGOV also excluded? (analyst excluded SGOV by symbol)
print("SGOV sells in sat:",[(r['ts'][:16],r['exit_reason'],r['realized_pl']) for r in sat if r['symbol']=='SGOV'])
# option rows in sat
print("instrument counts in sat:",{k:sum(1 for r in sat if (r.get('instrument') or 'equity')==k) for k in set((r.get('instrument') or 'equity') for r in sat)})
# what does v2 count? show system rows excluded
print("excluded system rows:",[(r['ts'][:16],r['symbol'],r.get('exit_reason'),round(r['realized_pl'],2)) for r in sells if r.get('realized_pl') is not None and ((r.get('exit_reason') or '') in SYS_REASONS or r['symbol'] in SYS_SYM)])
# after NVDA trail 2026-09-09T14:03Z
nv=[r for r in sat if r['symbol']=='NVDA']
print("NVDA sat rows:",[(r['ts'],r['exit_reason'],round(r['realized_pl'],2)) for r in nv])
after=[r for r in sat if r['ts']>"2026-09-09T14:03"]
print(f"after 09-09T14:03Z: n={len(after)} wins={sum(1 for r in after if r['realized_pl']>0)} sum={sum(r['realized_pl'] for r in after):.2f}")
for r in after: print("  ",r['ts'][:19],r['symbol'],r['exit_reason'],round(r['realized_pl'],2))
# cohorts by entry date (most recent prior buy)
buys=[r for r in win if r['action']=='buy']
hp=[]
for r in sorted(sat,key=lambda r:r['ts']):
    c=[b for b in buys if b['symbol']==r['symbol'] and b['ts']<r['ts']]
    if c:
        b=max(c,key=lambda b:b['ts'])
        hp.append(dict(sym=r['symbol'],entry=b['ts'][:10],exit=r['ts'][:10],pl=r['realized_pl'],comp=b.get('composite_score'),conv=b.get('conviction'),reason=r['exit_reason'],hold=(pt(r['ts'])-pt(b['ts'])).total_seconds()/86400))
    else:
        hp.append(dict(sym=r['symbol'],entry=None,exit=r['ts'][:10],pl=r['realized_pl'],comp=None,conv=None,reason=r['exit_reason'],hold=None))
print("unmatched sells (no prior in-window buy):",[(h['sym'],h['exit'],round(h['pl'],2)) for h in hp if h['entry'] is None])
e13=[h for h in hp if h['entry'] and '2026-09-01'<=h['entry']<='2026-09-03']
e8=[h for h in hp if h['entry'] and h['entry']>='2026-09-08']
print(f"entries Sep1-3 closed: n={len(e13)} sum={sum(h['pl'] for h in e13):.2f} wins={sum(1 for h in e13 if h['pl']>0)}")
print(f"entries Sep8+ closed: n={len(e8)} sum={sum(h['pl'] for h in e8):.2f} wins={sum(1 for h in e8 if h['pl']>0)}")
for h in e8: print("  ",h['sym'],h['entry'],'->',h['exit'],h['reason'],round(h['pl'],2),'comp',h['comp'],'conv',h['conv'])
oth=[h for h in hp if h['entry'] and h['entry'] not in [x['entry'] for x in e13] and h['entry']<'2026-09-08']
print("entries Sep 4-7 closed:",[(h['sym'],h['entry'],round(h['pl'],2)) for h in oth])
# spearman
def spearman(x,y):
    def rk(v):
        s=sorted(range(len(v)),key=lambda i:v[i]); r=[0]*len(v)
        i=0
        while i<len(s):
            j=i
            while j+1<len(s) and v[s[j+1]]==v[s[i]]: j+=1
            for k in range(i,j+1): r[s[k]]=(i+j)/2+1
            i=j+1
        return r
    rx,ry=rk(x),rk(y); n=len(x); mx=sum(rx)/n; my=sum(ry)/n
    num=sum((a-mx)*(b-my) for a,b in zip(rx,ry)); den=math.sqrt(sum((a-mx)**2 for a in rx)*sum((b-my)**2 for b in ry))
    return num/den
pc=[h for h in hp if h['comp'] is not None]
pv=[h for h in hp if h['conv'] is not None]
print(f"spearman comp vs pl (n={len(pc)}): {spearman([h['comp'] for h in pc],[h['pl'] for h in pc]):.3f}")
print(f"spearman conv vs pl (n={len(pv)}): {spearman([h['conv'] for h in pv],[h['pl'] for h in pv]):.3f}")
lw=[h['comp'] for h in pc if h['pl']>0]; ll=[h['comp'] for h in pc if h['pl']<=0]
print(f"winners mean comp {sum(lw)/len(lw):.2f} (n={len(lw)}) losers mean comp {sum(ll)/len(ll):.2f} (n={len(ll)})")
# re-entries
print("--- per-symbol buy/sell sequence for INTC NU RIG ---")
for s in ['INTC','NU','RIG']:
    for r in sorted([r for r in win if r['symbol']==s],key=lambda r:r['ts']):
        print("  ",s,r['ts'][:16],r['action'],r.get('exit_reason') or '',round(r.get('realized_pl') or 0,2),'qty',r.get('qty'),'cost',r.get('cost_usd'))
