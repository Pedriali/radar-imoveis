"""Varredura de apartamentos à venda (Zap, Viva Real, OLX) e atualização de docs/data/listings.json."""
import html as htmllib
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "scraper" / "config.json").read_text())
DATA = ROOT / "docs" / "data" / "listings.json"
NOW = datetime.now(timezone.utc)
NOW_ISO = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

S = requests.Session(impersonate="chrome")


def log(*a):
    print(*a, flush=True)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower()).strip()


def bairro_ok(name):
    n = norm(name)
    return any(b in n for b in CFG["bairros"])


def area_ok(a):
    return a is not None and CFG["area_min"] <= a <= CFG["area_max"]


def fetch(url, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, timeout=45)
            if r.status_code == 200:
                return r.text
            log(f"  HTTP {r.status_code} {url}")
        except Exception as e:  # noqa: BLE001
            log(f"  erro {e} {url}")
        time.sleep(3 * (i + 1))
    return None


def rsc(page):
    """Junta o fluxo de dados do Next.js (self.__next_f.push) embutido na página."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,(".*?")\]\)</script>', page, re.S)
    return "".join(json.loads(c) for c in chunks)


def age_days(iso):
    if not iso:
        return None
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (NOW - dt).total_seconds() / 86400


# ---------- nome do prédio ----------
NAME_RE = re.compile(
    r"\b(?i:Ed\.|Edif\.|Edif[ií]cio|Residencial|Condom[ií]nio|Cond\.|Torre|Res\.)\s+"
    r"((?:[A-ZÀ-Ý0-9][\wÀ-ÿ'&\.\-]*)(?:\s+(?:d[aeo]s?\s+)?[A-ZÀ-Ý0-9][\wÀ-ÿ'&\.\-]*){0,4})"
)
STOP_PREFIX = ("apartamento", "apto", "vendo", "venda", "lindo", "excelente", "oportunidade", "otimo", "ótimo", "imovel", "imóvel")


def building_from_text(*texts):
    for t in texts:
        if not t:
            continue
        m = NAME_RE.search(t)
        if m:
            name = m.group(1).strip(" .-")
            if 2 < len(name) < 45 and norm(name) not in ("de alto padrao", "fechado", "clube"):
                return name
    return None


def building_from_olx_subject(subject):
    # Padrão comum de imobiliárias: "NOME DO PRÉDIO - R$ ... - Apartamento..."
    if " - " in subject:
        head = subject.split(" - ")[0].strip()
        if 1 <= len(head.split()) <= 4 and not norm(head).startswith(STOP_PREFIX) and "r$" not in norm(head):
            head = re.sub(r"(?i)^(ed\.|edif\.|edif[ií]cio|residencial|condom[ií]nio)\s+", "", head)
            return head.title() if head.isupper() else head
    return None


# ---------- Grupo Zap (Zap Imóveis + Viva Real) ----------
def zap_img(src):
    return (src or "").replace("{description}", "foto").replace("{action}", "crop").replace("{width}x{height}", "360x240")


def parse_grupo(page):
    s = rsc(page)
    dec = json.JSONDecoder()
    out = []
    for m in re.finditer(r'\{"id":"\d+","prices"', s):
        try:
            out.append(dec.raw_decode(s, m.start())[0])
        except ValueError:
            pass
    return out


def grupo_record(o):
    am = o.get("amenities") or {}
    areas = am.get("usableAreas") or []
    idx = next((i for i, a in enumerate(areas) if area_ok(a)), None)
    if idx is None:
        return None

    def pick(key):
        v = am.get(key) or []
        return v[idx] if idx < len(v) else (v[0] if v else None)

    sale = (o.get("prices") or {}).get("sale") or {}
    vals = sale.get("values") or []
    price = vals[idx] if idx < len(vals) else sale.get("value")
    addr = o.get("address") or {}
    imgs = ((o.get("medias") or {}).get("images")) or []
    predio = o.get("condominiumName") or None
    if not predio and o.get("contractType") == "PROPERTY_DEVELOPER":
        m = re.search(r"/lancamentos/(.+?)-id-\d+", o.get("href", ""))
        if m:
            predio = m.group(1).replace("-", " ").title()
    rua = ", ".join(x for x in [addr.get("street"), addr.get("streetNumber")] if x)
    return {
        "id": "g" + o["id"],
        "predio": predio,
        "endereco": rua,
        "bairro": addr.get("neighborhood"),
        "foto": zap_img(imgs[0]["dangerousSrc"]) if imgs else None,
        "area": areas[idx],
        "preco": price,
        "quartos": pick("bedrooms"),
        "banheiros": pick("bathrooms"),
        "vagas": pick("parkingSpaces"),
        "titulo": o.get("title"),
        "links": {},
    }


def grupo_detail(rec):
    """Página do anúncio: data de criação e descrição (para achar o nome do prédio)."""
    url = rec["links"].get("zap") or rec["links"].get("vivareal")
    page = fetch(url) if url else None
    if not page:
        return
    m = re.search(r'\\?"createdAt\\?":\\?"(\d{4}-\d\d-\d\dT[\d:.]+Z)', page)
    if m:
        rec["publicado"] = m.group(1)[:19] + "Z"
    if not rec.get("predio"):
        descs = re.findall(r'"description":"(.{20,2000}?)"[,}]', page)
        desc = htmllib.unescape(re.sub(r"<[^>]+>", " ", max(descs, key=len))) if descs else ""
        rec["predio"] = building_from_text(rec.get("titulo"), desc)


def scan_grupo(known, max_pages, found):
    status = {}
    for portal, base in (("zap", "https://www.zapimoveis.com.br/venda/apartamentos/{slug}/"),
                         ("vivareal", None)):
        ok = 0
        for slug in CFG["grupo_zap_searches"]:
            if portal == "zap":
                url0 = base.format(slug=slug)
            else:
                b = slug.split("++")[1]
                url0 = f"https://www.vivareal.com.br/venda/parana/londrina/bairros/{b}/apartamento_residencial/"
            for p in range(1, max_pages + 1):
                url = f"{url0}?areaMinima={CFG['area_min']}&areaMaxima={CFG['area_max']}&ordem=MOST_RECENT&pagina={p}"
                page = fetch(url)
                if page is None:
                    break
                items = parse_grupo(page)
                ok += 1
                if not items:
                    break
                fresh = 0
                for o in items:
                    if o.get("business") != "SALE" or o.get("unitType") not in ("APARTMENT", None):
                        continue
                    if not bairro_ok((o.get("address") or {}).get("neighborhood")):
                        continue
                    rec = grupo_record(o)
                    if not rec:
                        continue
                    cur = found.setdefault(rec["id"], rec)
                    cur["links"][portal] = o.get("href")
                    if rec["id"] not in known:
                        fresh += 1
                log(f"  {portal} {slug} p{p}: {len(items)} anúncios, {fresh} desconhecidos")
                if known and fresh == 0:
                    break
                time.sleep(1.5)
        status[portal] = "ok" if ok else "falhou"
    return status


# ---------- OLX ----------
def olx_num(v):
    d = re.sub(r"\D", "", v or "")
    return int(d) if d else None


def scan_olx(known, max_pages, found):
    pages_ok = 0
    for p in range(1, max_pages * 2 + 1):
        url = f"{CFG['olx_search']}?ss={CFG['area_min']}&se={CFG['area_max']}&sf=1&o={p}"
        page = fetch(url)
        if page is None:
            break
        s = rsc(page)
        i = s.find('{"ads":[')
        if i < 0:
            break
        ads = json.JSONDecoder().raw_decode(s, i)[0].get("ads", [])
        pages_ok += 1
        fresh = 0
        for a in ads:
            if not a.get("listId"):
                continue
            loc = a.get("locationDetails") or {}
            if norm(loc.get("municipality")) != "londrina" or not bairro_ok(loc.get("neighbourhood")):
                continue
            props = {x["name"]: x.get("value") for x in a.get("properties", [])}
            area = olx_num(props.get("size"))
            if not area_ok(area):
                continue
            if "venda" not in norm(props.get("real_estate_type", "venda")):
                continue
            imgs = a.get("images") or []
            rid = f"o{a['listId']}"
            rec = {
                "id": rid,
                "predio": building_from_olx_subject(a.get("subject", "")) or building_from_text(a.get("subject")),
                "endereco": None,
                "bairro": loc.get("neighbourhood"),
                "foto": imgs[0]["original"] if imgs else None,
                "area": area,
                "preco": olx_num(a.get("price") or a.get("priceValue")),
                "quartos": olx_num(props.get("rooms")),
                "banheiros": olx_num(props.get("bathrooms")),
                "vagas": olx_num(props.get("garage_spaces")),
                "titulo": a.get("subject"),
                "publicado": datetime.fromtimestamp(a["date"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if a.get("date") else None,
                "links": {"olx": a.get("url")},
            }
            found.setdefault(rid, rec)
            if rid not in known:
                fresh += 1
        log(f"  olx p{p}: {len(ads)} anúncios, {fresh} desconhecidos")
        if not ads or (known and fresh == 0):
            break
        time.sleep(1.5)
    return {"olx": "ok" if pages_ok else "falhou"}


def olx_detail(rec):
    page = fetch(rec["links"]["olx"])
    if not page:
        return
    t = htmllib.unescape(page)
    m = re.search(r'"body":"(.{0,1500})', t)
    rec["predio"] = building_from_text(rec.get("titulo"), m.group(1) if m else "")


# ---------- principal ----------
def same_property(a, b):
    """Mesmo imóvel anunciado na OLX e no Grupo Zap: mesma área, mesmo preço e mesmo bairro."""
    return (a["area"] == b["area"] and a["preco"] and a["preco"] == b["preco"]
            and norm(a.get("bairro")) == norm(b.get("bairro")))


def main():
    state = json.loads(DATA.read_text()) if DATA.exists() else {"runs": [], "listings": {}}
    L = state["listings"]
    initial = not L
    max_pages = CFG["max_pages_initial"] if initial else CFG["max_pages"]
    log(f"Varredura {NOW_ISO} ({'inicial' if initial else 'incremental'})")

    found = {}
    status = {}
    status.update(scan_grupo(L, max_pages, found))
    status.update(scan_olx(L, max_pages, found))

    new_ids = [k for k in found if k not in L]
    log(f"{len(found)} anúncios no filtro, {len(new_ids)} novos")

    # Atualiza os já conhecidos (preço, links, última vez visto)
    for k, rec in found.items():
        if k in L:
            cur = L[k]
            cur["visto"] = NOW_ISO
            if not cur.get("visivel"):
                continue
            cur["links"].update(rec["links"])
            if rec.get("preco") and rec["preco"] != cur.get("preco"):
                cur.setdefault("preco_anterior", cur.get("preco"))
                cur["preco"] = rec["preco"]
            cur["visto"] = NOW_ISO

    limite = CFG["initial_recent_days"] if initial else CFG["max_age_days_new"]
    # Grupo Zap: IDs crescem com o tempo, então os maiores são os mais novos. Ao encontrar
    # vários anúncios seguidos mais velhos que o limite, os restantes não precisam ser abertos.
    grupo_new = sorted((k for k in new_ids if k.startswith("g")), key=lambda k: -int(k[1:]))
    olx_new = [k for k in new_ids if k.startswith("o")]
    added = old_streak = 0
    for k in grupo_new + olx_new:
        rec = found[k]
        if k.startswith("g"):
            if old_streak < 6:
                grupo_detail(rec)
                time.sleep(1)
                idade = age_days(rec.get("publicado"))
                old_streak = old_streak + 1 if idade is not None and idade > limite else 0
            recente = old_streak < 6 and (age_days(rec.get("publicado")) or 0) <= limite
        else:
            recente = (age_days(rec.get("publicado")) or 0) <= limite
            if recente and not rec.get("predio"):
                olx_detail(rec)
                time.sleep(1)
        # Mesmo imóvel já listado por outro portal? Junta os links.
        twin = next((x for x in L.values() if x.get("visivel") and same_property(x, rec)), None)
        if twin:
            twin["links"].update(rec["links"])
            rec["visivel"] = False
            rec["duplicado_de"] = twin["id"]
        else:
            rec["visivel"] = recente
            added += recente
        if not rec["visivel"]:
            rec = {"id": k, "visivel": False, "area": rec["area"], "preco": rec["preco"], "bairro": rec["bairro"]}
        rec["detectado"] = NOW_ISO
        rec["visto"] = NOW_ISO
        L[k] = rec

    state["runs"].append({"em": NOW_ISO, "novos": added, "portais": status, "inicial": initial})
    state["runs"] = state["runs"][-60:]
    state["atualizado"] = NOW_ISO
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    log(f"Status: {status}. Novos visíveis: {added}. Total armazenado: {len(L)}")
    if all(v == "falhou" for v in status.values()):
        sys.exit("Todos os portais falharam")


if __name__ == "__main__":
    main()
