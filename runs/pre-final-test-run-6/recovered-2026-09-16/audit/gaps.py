import re, datetime as dt
# deadman gaps
rows=[]
for line in open('logs/deadman.log'):
    m=re.match(r'\[(2026-09-1[0-5]) (\d\d:\d\d:\d\d) ET\] (.*)', line)
    if m:
        rows.append((dt.datetime.fromisoformat(m.group(1)+'T'+m.group(2)), m.group(3).strip()))
print("deadman rows Sep10-15:", len(rows))
prev=None
for t,msg in rows:
    if prev and (t-prev[0]).total_seconds()>360:
        print("DEADMAN GAP", prev[0], "->", t, round((t-prev[0]).total_seconds()/60,1), "min | prev:", prev[1][:60], "| next:", msg[:60])
    prev=(t,msg)
# what kinds of messages in-hours Sep14/15
from collections import Counter
for day in ('2026-09-14','2026-09-15'):
    c=Counter(msg.split('(')[0].strip()[:40] for t,msg in rows if t.date().isoformat()==day)
    print(day, dict(c))
# decision cycle gaps
for f in ('logs/Sep_11_2026.log','logs/Sep_14_2026.log','logs/bot.log'):
    ts=[]
    for line in open(f, errors='replace'):
        if 'Market regime' in line:
            ts.append(dt.datetime.strptime(line[:19],'%Y-%m-%d %H:%M:%S'))
    print(f, [ (round((b-a).total_seconds()/60,2)) for a,b in zip(ts,ts[1:])])
