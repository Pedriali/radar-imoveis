import re, json, os
from curl_cffi import requests
S = requests.Session(impersonate="chrome")
os.makedirs("out", exist_ok=True)
def rsc(h):
    ch = re.findall(r'self\.__next_f\.push\(\[1,(".*?")\]\)</script>', h, re.S)
    return "".join(json.loads(c) for c in ch)
def grp(h):
    s = rsc(h); d = json.JSONDecoder(); out = []
    for m in re.finditer(r'\{"id":"\d+","prices"', s):
        try: out.append(d.raw_decode(s, m.start())[0])
        except Exception: pass
    return out
def olx(h):
    s = rsc(h); i = s.find('{"ads":[')
    return json.JSONDecoder().raw_decode(s, i)[0] if i >= 0 else {}
def get(name, u):
    r = S.get(u, timeout=40)
    open(f"out/{name}.html", "w").write(r.text)
    return r
# Grupo (zap/vivareal)
for name, u in {
 "zap_bairro": "https://www.zapimoveis.com.br/venda/apartamentos/pr+londrina++gleba-palhano/?areaMinima=100&areaMaxima=200",
 "zap_bairro_p2": "https://www.zapimoveis.com.br/venda/apartamentos/pr+londrina++gleba-palhano/?areaMinima=100&areaMaxima=200&pagina=2",
 "zap_recent": "https://www.zapimoveis.com.br/venda/apartamentos/pr+londrina++gleba-palhano/?areaMinima=100&areaMaxima=200&ordem=MOST_RECENT",
 "zap_city": "https://www.zapimoveis.com.br/venda/apartamentos/pr+londrina/?areaMinima=100&areaMaxima=200",
 "vr_bairro": "https://www.vivareal.com.br/venda/parana/londrina/bairros/gleba-palhano/apartamento_residencial/?areaMinima=100&areaMaxima=200",
 "vr_recent": "https://www.vivareal.com.br/venda/parana/londrina/bairros/gleba-palhano/apartamento_residencial/?areaMinima=100&areaMaxima=200&ordem=MOST_RECENT",
}.items():
    try:
        r = get(name, u); L = grp(r.text)
        tot = re.search(r'"totalCount":(\d+)', rsc(r.text))
        print(f"[{name}] {r.status_code} final={r.url[:120]} n={len(L)} total={tot.group(1) if tot else '?'}")
        print("   ids:", [x['id'] for x in L[:6]])
        print("   bairros:", sorted({x['address'].get('neighborhood') for x in L}))
        print("   areas:", [x['amenities'].get('usableAreas') for x in L[:10]])
    except Exception as e: print(f"[{name}] ERR {e}")
# detalhe
try:
    L = grp(open("out/zap_bairro.html").read())
    r = get("zap_detail", L[0]['href']); t = r.text
    print("[detail]", r.status_code, len(t))
    for k in ["createdAt","updatedAt","publishedAt","creationDate","publicationDate","\"createdDate\""]:
        for m in list(re.finditer(re.escape(k), t))[:3]: print("   ", t[m.start():m.start()+70].replace("\n"," "))
except Exception as e: print("[detail] ERR", e)
# OLX
for name, u in {
 "olx_city": "https://www.olx.com.br/imoveis/venda/apartamentos/estado-pr/regiao-de-londrina/londrina",
 "olx_city_size": "https://www.olx.com.br/imoveis/venda/apartamentos/estado-pr/regiao-de-londrina/londrina?ss=100&se=200",
 "olx_bairro": "https://www.olx.com.br/imoveis/venda/apartamentos/estado-pr/regiao-de-londrina/londrina/gleba-palhano?ss=100&se=200",
 "olx_recent": "https://www.olx.com.br/imoveis/venda/apartamentos/estado-pr/regiao-de-londrina/londrina?ss=100&se=200&sf=1",
}.items():
    try:
        r = get(name, u); o = olx(r.text); A = o.get('ads', [])
        print(f"[{name}] {r.status_code} final={r.url[:120]} n={len(A)} total={o.get('totalOfAds')}")
        print("   locs:", [a.get('location') for a in A[:8]])
        print("   sizes:", [next((p['value'] for p in a.get('properties',[]) if p['name']=='size'), None) for a in A[:10]])
        print("   dates:", [a.get('date') for a in A[:8]])
    except Exception as e: print(f"[{name}] ERR {e}")
# OLX detalhe
try:
    A = olx(open("out/olx_city_size.html").read())['ads']
    r = get("olx_detail", A[0]['url']); t = r.text
    print("[olx_detail]", r.status_code, len(t))
    for k in ["listTime","origListTime","\"date\"","publishedAt","Publicado"]:
        for m in list(re.finditer(re.escape(k), t))[:3]: print("   ", t[m.start():m.start()+80].replace("\n"," "))
except Exception as e: print("[olx_detail] ERR", e)
