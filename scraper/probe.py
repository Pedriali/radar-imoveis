import asyncio, os
URLS = {
 "zap": "https://www.zapimoveis.com.br/venda/apartamentos/pr+londrina/?areaMinima=100&areaMaxima=200",
 "vivareal": "https://www.vivareal.com.br/venda/parana/londrina/apartamento_residencial/?areaMinima=100&areaMaxima=200",
 "olx": "https://www.olx.com.br/imoveis/venda/apartamentos/estado-pr/norte-do-parana/londrina",
}
def cffi():
    from curl_cffi import requests
    for k,u in URLS.items():
        try:
            r = requests.get(u, impersonate="chrome", timeout=30)
            t = r.text.split('<title>')[1][:80] if '<title>' in r.text else ''
            print(f"[cffi] {k}: {r.status_code} len={len(r.text)} title={t}")
            open(f"out/cffi_{k}.html","w").write(r.text)
        except Exception as e: print(f"[cffi] {k}: ERR {e}")
async def pw():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = await b.new_context(locale="pt-BR", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36")
        for k,u in URLS.items():
            pg = await ctx.new_page()
            try:
                r = await pg.goto(u, timeout=60000); await pg.wait_for_timeout(8000)
                h = await pg.content()
                print(f"[pw] {k}: {r.status if r else None} len={len(h)} title={await pg.title()}")
                open(f"out/pw_{k}.html","w").write(h)
            except Exception as e: print(f"[pw] {k}: ERR {e}")
        await b.close()
os.makedirs("out", exist_ok=True)
cffi(); asyncio.run(pw())
