import sys,json,os; sys.path.insert(0,"/private/tmp/claude-501/-Users-mainfolder/42e9f951-0852-4ac4-b34f-2a160144053a/scratchpad")
from rpclib import rpc
from keccak import topic
SP="/private/tmp/claude-501/-Users-mainfolder/42e9f951-0852-4ac4-b34f-2a160144053a/scratchpad"
L=open(SP+"/wfeat.log","w",buffering=1)
BUY=topic('CurveBuy(address,address,uint256,uint256,uint256,uint256)')
SELL=topic('CurveSell(address,address,uint256,uint256,uint256,uint256)')
EX=topic('SnipeTaxExempted(address)')
CH=topic('SnipeTaxCharged(address,uint256)')
P=json.load(open(SP+"/pons_launches.json"))['events']
G=json.load(open(SP+"/pons_graduations.json"))['events']
T=json.load(open(SP+"/grad_tokens.json"))
H=json.load(open(SP+"/grad_ohlcv2.json"))
c2l={v['curve'].lower():v for v in P.values() if v.get('curve')}
work=[]
for c,tok in T.items():
    tok=tok.lower(); hv=H.get(tok)
    if not hv or 'peak_high' not in hv: continue
    op=float(hv.get('first_open') or 0)
    if op<=0: continue
    l=c2l.get(c); g=G.get(c)
    if not l or not g: continue
    work.append({'curve':c,'token':tok,'lb':l['block'],'gb':g['block'],
                 'mult':float(hv['peak_high'])/op,'launcher':l.get('launcher')})
L.write(f"curves with launch+grad+outcome: {len(work)}\n")
out=json.load(open(SP+"/wfeat.json")) if os.path.exists(SP+"/wfeat.json") else {}
def getlogs(addr,f,t):
    res=[]; b=f
    while b<=t:
        to=min(b+40000,t)
        r=rpc('eth_getLogs',[{'fromBlock':hex(b),'toBlock':hex(to),'address':addr}])
        if 'result' in r: res.extend(r['result'])
        b=to+1
    return res
for i,w in enumerate(work,1):
    if w['curve'] in out: continue
    logs=getlogs(w['curve'],w['lb'],w['gb'])
    buys={}; sells={}; exempt=set(); sniped=set(); order=[]
    for lg in logs:
        t0=lg['topics'][0]
        if t0==BUY:
            a='0x'+lg['topics'][1][-40:]; d=lg['data'][2:]
            q=int(d[0:64],16); tk=int(d[64:128],16)
            buys[a]=buys.get(a,0)+tk; order.append((int(lg['blockNumber'],16),a,'B'))
        elif t0==SELL:
            a='0x'+lg['topics'][1][-40:]; d=lg['data'][2:]
            tk=int(d[0:64],16)
            sells[a]=sells.get(a,0)+tk; order.append((int(lg['blockNumber'],16),a,'S'))
        elif t0==EX: exempt.add('0x'+lg['topics'][1][-40:])
        elif t0==CH: sniped.add('0x'+lg['topics'][1][-40:])
    tot=sum(buys.values()) or 1
    top=sorted(buys.values(),reverse=True)
    net={a:buys.get(a,0)-sells.get(a,0) for a in set(buys)|set(sells)}
    ex_sold=sum(1 for a in exempt if sells.get(a,0)>0)
    out[w['curve']]={'token':w['token'],'mult':w['mult'],'launcher':w['launcher'],
      'n_buyers':len(buys),'n_sellers':len(sells),'n_exempt':len(exempt),'n_sniped':len(sniped),
      'top1_share':top[0]/tot if top else 0,
      'top5_share':sum(top[:5])/tot if top else 0,
      'exempt_sold_frac':ex_sold/len(exempt) if exempt else 0,
      'sell_to_buy_ratio':sum(sells.values())/tot,
      'n_logs':len(logs)}
    if i%10==0:
        L.write(f"{i}/{len(work)}\n"); json.dump(out,open(SP+"/wfeat.json","w"))
json.dump(out,open(SP+"/wfeat.json","w"))
L.write(f"DONE {len(out)}\n")
