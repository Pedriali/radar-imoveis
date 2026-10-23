"""Varredura de imóveis à venda em Londrina e atualização de docs/data/<perfil>.json.

Cada perfil de config.json ("perfis") é uma busca independente que vira uma aba da página:
    python scraper/radar.py              # todos os perfis
    python scraper/radar.py casas        # só um
"""
import difflib
import functools
import html as htmllib
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "scraper" / "config.json").read_text())
CFG = {}  # perfil em uso: CONFIG["comum"] + CONFIG["perfis"][nome] (ver usar_perfil)
DATA_DIR = ROOT / "docs" / "data"
NOW = datetime.now(timezone.utc)
NOW_ISO = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

S = requests.Session(impersonate="chrome")


# Anúncios já guardados do perfil em uso (preenchido em main): as páginas de anúncio que só servem para
# completar dados (prédio, endereço, área, quartos) não são abertas de novo; o resto vem da busca.
CONHECIDOS = {}
CAMPOS_DETALHE = ("tipo", "predio", "endereco", "bairro", "foto", "area", "quartos", "banheiros", "vagas", "titulo",
                  "publicado", "lat", "lon", "condominio")


def conhecido(rid, links, **novos):
    """Registro de um anúncio já guardado, sem abrir a página dele: os campos guardados + o que a busca
    trouxe agora (preço, por exemplo). None se o anúncio é desconhecido."""
    k = CONHECIDOS.get(rid)
    if not k:
        return None
    rec = {f: k.get(f) for f in CAMPOS_DETALHE if k.get(f) is not None}
    rec.update({f: v for f, v in novos.items() if v is not None}, id=rid, links=links)
    return rec


def preco_ok(p):
    """Perfil com "preco_min" (sem preço = "sob consulta", passa)."""
    return not (CFG.get("preco_min") and p and p < CFG["preco_min"])


def usar_perfil(nome):
    CFG.clear()
    CFG.update(CONFIG["comum"])
    CFG.update(CONFIG["perfis"][nome])
    CFG["perfil"] = nome
    ALVOS.clear()
    cond_citado.cache_clear()
    if CFG.get("por_condominio"):
        for a in CONFIG["condominios_alvo"]:
            chaves = sorted((norm(c) for c in a["chaves"]), key=len, reverse=True)
            # "nao": trechos que não contam para este condomínio (o bairro Vale dos Tucanos × Condomínio Tucanos)
            nao = re.compile("|".join(re.escape(norm(x)) for x in a["nao"])) if a.get("nao") else None
            ALVOS.append((a["nome"], re.compile(r"\b(?:" + "|".join(map(re.escape, chaves)) + r")\b"), True, nao))
        # outros condomínios conhecidos: não entram, mas separam um condomínio do outro (lote de 360 m² do
        # Royal Forest × do Ville Campestre) e barram o anúncio de outro condomínio que só cita um alvo
        for c in CONFIG.get("condominios_outros", []):
            ALVOS.append((c.title(), re.compile(r"\b" + re.escape(norm(c)) + r"\b"), False, None))


# Perfis "por_condominio" (casas e terrenos): só entram imóveis dos condomínios de "condominios_alvo"
# (config.json), reconhecidos pelas palavras-chave de cada um no nome do condomínio, título ou bairro
# do anúncio (Zap, OLX e Chaves na Mão também na descrição, ver alvo_detalhe). A primeira palavra-chave
# é o termo da busca por condomínio na OLX.
ALVOS = []  # [(nome, regex das palavras-chave, é alvo?, regex do que não conta)] do perfil em uso


def apto():
    return CFG["tipo"] == "apartamento"


def terrenos():
    """Perfil só de terrenos em condomínio (busca ampla: também lotes sem a marca de condomínio,
    aceitos quando o anúncio prova que é condomínio — ver condominio_ok)."""
    return CFG["tipo"] == "terreno_condominio"


def log(*a):
    print(*a, flush=True)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower()).strip()


def bairro_ok(name):
    n = norm(name)
    if CFG.get("bairro_exato"):  # Centro: "Centro" e só ele (não "Centro Cívico", "Jardim do Centro"...)
        return re.sub(r"\s*[-,/(].*$", "", n) in CFG["bairros"]
    return any(b in n for b in CFG["bairros"])


FORA = {}  # bairros descartados pela zona nesta varredura (para revisar a lista de bairros)


def fora(motivo):
    FORA[motivo or "?"] = FORA.get(motivo or "?", 0) + 1


@functools.lru_cache(maxsize=None)  # chamado para cada par de anúncios no agrupamento
def cond_citado(*textos):
    """(nome, é alvo?) do condomínio (de "condominios_alvo" ou "condominios_outros") citado nos textos, ou
    None. Os textos vão em ordem de confiança (nome do condomínio, título... bairro por último: o Artesano
    fica no bairro Vivendas do Arvoredo): vale o primeiro que cita algum. "Ao lado do condomínio X" não
    conta; citando mais de um, fica a palavra-chave mais longa ("alphaville 2" ganha de "alphaville")."""
    for x in textos:
        t = COND_PERTO.sub(" ", norm(x))
        achados = [(len(m.group()), nome, ok) for nome, rx, ok, nao in ALVOS
                   for m in [rx.search(nao.sub(" ", t) if nao else t)] if m]
        if achados:
            return max(achados)[1:]
    return None


def alvo(*textos):
    """Nome do condomínio-alvo citado nos textos, ou None (também se o citado é um dos "outros")."""
    c = cond_citado(*textos)
    return c[0] if c and c[1] else None


def excluido(*textos):
    """Anúncio que se declara de outra zona (zonas_fora) ou cita um nome de "excluir"."""
    t = norm(" ".join(x for x in textos if x))
    zonas = "|".join(CFG.get("zonas_fora", ["norte", "leste"]))
    return bool(re.search(rf"\b(?:zona|regiao) (?:{zonas})\b", t)) or any(x in t for x in CFG.get("excluir", []))


def local_ok(bairro, *textos, pendente=False):
    """Apartamentos: bairro na lista. Casas/terrenos: nome de um condomínio-alvo no bairro/textos (ver
    alvo); com pendente=True, sem o nome mas com o bairro na lista da zona também passa — o nome é
    procurado depois na página do anúncio e, sem ele, o imóvel não vira card (group_imoveis)."""
    if apto():
        return bairro_ok(bairro)
    if excluido(bairro, *textos):
        return False
    if alvo(*textos, bairro) or (pendente and bairro_ok(bairro)):
        return True
    fora(bairro)
    return False


def na_caixa(lat, lon):
    cx = CFG.get("caixa")
    return not cx or (cx.get("lat_min", -90) <= lat <= cx["lat_max"] and cx["lon_min"] <= lon <= cx["lon_max"])


def coord_ok(addr):
    """Zap/Viva Real trazem latitude/longitude: com localização exata fora da caixa da zona sul do
    perfil ("caixa"), o anúncio sai mesmo que o bairro esteja na lista (há bairro cadastrado errado)."""
    cx = CFG.get("caixa")
    c = (addr or {}).get("coordinates")
    if not cx or not isinstance(c, dict) or addr.get("isApproximateLocation") or not c.get("latitude"):
        return True
    if na_caixa(c["latitude"], c.get("longitude") or 0):
        return True
    fora(f"{addr.get('neighborhood')} (coordenadas fora da zona sul)")
    return False


# Prova de condomínio para lotes de buscas sem essa marca: nome de condomínio conhecido ou o texto
# do anúncio falando em condomínio/loteamento fechado ("ao lado do condomínio" não conta).
COND_TXT = re.compile(r"\bcondominio\b|\bcond\b|loteamento fechado|\bfechado\b")
COND_PERTO = re.compile(r"\b(?:ao lado|proximo|perto|vizinho|em frente|atras)(?:\s+(?:a|ao|do|de|da|dos)\b)?"
                        r"(?:\s+(?:condominio|cond)\b\.?)?(?:\s+[a-z0-9]+){0,3}")


def condominio_ok(*textos):
    if CFG.get("por_condominio"):  # o nome de um condomínio-alvo é a prova
        return bool(alvo(*textos))
    t = COND_PERTO.sub(" ", norm(" ".join(x for x in textos if x)))
    return bool(COND_TXT.search(t))


def alvo_detalhe(rec, *textos):
    """Página do anúncio aberta: guarda o condomínio citado nela, alvo ou outro ("" = página lida, nenhum).
    O do título/bairro vale mais que o da descrição (que às vezes cita vizinhos)."""
    if CFG.get("por_condominio"):
        c = cond_citado(rec.get("predio"), rec.get("titulo"), rec.get("bairro")) or cond_citado(*textos)
        rec["condominio"] = c[0] if c else ""


def cond_rec(x):
    """Condomínio (alvo ou outro) do anúncio guardado."""
    c = cond_citado(x.get("predio"), x.get("titulo"), x.get("bairro"))
    return x.get("condominio") or (c[0] if c else None)


def area_ok(a):
    return a is not None and CFG["area_min"] <= a <= CFG["area_max"]


# Área útil citada no texto do anúncio. Muitos anunciantes põem a área total (com garagem e áreas comuns)
# no campo "área útil" do portal, mas a descrição diz "181 m² privativos", "área útil de 114 m²"...
_NUM = r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
_M2 = r"(?:m²|m2|mts?2?|metros?(?: quadrados)?)"
_UTIL = r"(?:privativ[ao]s?|ute?is|util|interna|construid[ao]s?|de constru[cç]ao)"
AREA_UTIL_TXT = [
    re.compile(_NUM + r"\s*" + _M2 + r"\s*(?:de\s+)?(?:area\s+)?" + _UTIL),
    re.compile(r"area\s+" + _UTIL + r"\s*(?:total\s*)?(?:de|:|-|=|com|e)?\s*(?:aprox\w*\.?\s*)?" + _NUM + r"\s*" + _M2),
    re.compile(r"(?:parte|area) interna (?:sao|e|tem|com|de) " + _NUM + r"\s*" + _M2),
]


def _num_area(s):
    s = s.replace(".", "") if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", s) else s
    try:
        return round(float(s.replace(",", ".")))
    except ValueError:
        return None


def area_util_texto(*textos):
    """Menor área útil/privativa/construída citada nos textos (None se nenhuma). A menor, porque o
    texto às vezes cita também a do terraço, a total etc. — e área útil abaixo de 25 m² é ruído."""
    t = norm(" ".join(x for x in textos if x)).replace("\\n", " ")
    vals = [_num_area(m.group(1)) for rx in AREA_UTIL_TXT for m in rx.finditer(t)]
    vals = [v for v in vals if v and v >= 25]
    return min(vals) if vals else None


def corrige_area(rec, *textos):
    """Troca a área do anúncio pela área útil citada no texto, se esta for menor (o campo do portal
    trazia a total). Terreno fica com a área do lote. Guarda a original em area_anunciada."""
    if rec.get("tipo") == "terreno" or not rec.get("area"):
        return
    u = area_util_texto(rec.get("titulo"), *textos)
    if u and u < rec["area"] - 1 and u >= rec["area"] * 0.4:
        rec.setdefault("area_anunciada", rec["area"])
        rec["area"] = u


def fetch(url, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, timeout=45)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                log(f"  HTTP 404 {url}")
                return None
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


# Caixa de Londrina (área urbana e condomínios da zona sul): coordenada fora dela é erro de cadastro.
LONDRINA = {"lat": (-23.45, -23.20), "lon": (-51.32, -51.05)}


def em_londrina(lat, lon):
    return LONDRINA["lat"][0] <= lat <= LONDRINA["lat"][1] and LONDRINA["lon"][0] <= lon <= LONDRINA["lon"][1]


def coords(lat, lon, aproximada=False):
    """{"lat", "lon"} quando o portal informa a localização exata (dentro de Londrina); senão {}."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return {}
    if aproximada or not em_londrina(lat, lon):
        return {}
    return {"lat": round(lat, 6), "lon": round(lon, 6)}


# ---------- nome do prédio ----------
NAME_RE = re.compile(
    r"\b(?i:Ed\.|Edif\.|Edif[ií]cio|Residencial|Condom[ií]nio|Cond\.|Torre|Res\.)\s+"
    r"((?:[A-ZÀ-Ý0-9][\wÀ-ÿ'&\.\-]*)(?:\s+(?:d[aeo]s?\s+)?[A-ZÀ-Ý0-9][\wÀ-ÿ'&\.\-]*){0,4})"
)
STOP_PREFIX = ("apartamento", "apto", "vendo", "venda", "lindo", "excelente", "oportunidade", "otimo", "ótimo", "imovel", "imóvel",
               "casa", "sobrado", "terreno", "lote")


def building_from_text(*texts):
    for t in texts:
        if not t:
            continue
        m = NAME_RE.search(t)
        if m:
            name = re.split(r"\.\s", m.group(1))[0].strip(" .-")  # "Royal Tennis. Com 4 suítes"
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
    rua = ", ".join(x for x in [addr.get("street"), addr.get("streetNumber")] if x and "undefined" not in x)
    return {
        "id": "g" + o["id"],
        "tipo": GRUPO_TIPOS.get(o.get("unitType")) if not apto() else None,
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
        **coords((addr.get("coordinates") or {}).get("latitude"), (addr.get("coordinates") or {}).get("longitude"),
                 aproximada=addr.get("isApproximateLocation")),
    }


def grupo_detail(rec):
    """Página do anúncio: data de criação e descrição (nome do prédio, área útil citada no texto)."""
    url = rec["links"].get("zap") or rec["links"].get("vivareal")
    page = fetch(url, tries=1) if url else None
    if not page:
        return False
    rec.pop("detalhe_pendente", None)
    m = re.search(r'\\?"createdAt\\?":\\?"(\d{4}-\d\d-\d\dT[\d:.]+Z)', page)
    if m:
        rec["publicado"] = m.group(1)[:19] + "Z"
    descs = re.findall(r'"description":"(.{20,4000}?)"[,}]', rsc(page))
    desc = htmllib.unescape(re.sub(r"<[^>]+>", " ", max(descs, key=len))) if descs else ""
    corrige_area(rec, desc)
    if not rec.get("predio"):
        rec["predio"] = building_from_text(rec.get("titulo"), desc)
    alvo_detalhe(rec, desc)
    return True


DETALHES_POR_VARREDURA = 40  # anúncios do Zap/Viva Real cuja página ficou para depois (bloqueio 429)


class Detalhes:
    """Abre páginas de anúncio do Zap/Viva Real. O Zap bloqueia (HTTP 429) depois de muitas páginas
    seguidas: com 3 falhas seguidas, para de abrir nesta varredura e marca o anúncio como pendente
    (as próximas varreduras abrem até DETALHES_POR_VARREDURA pendentes)."""

    def __init__(self, pausa):
        self.pausa, self.falhas = pausa, 0

    def abrir(self, rec):
        if self.falhas >= 3:
            rec["detalhe_pendente"] = True
            return False
        ok = grupo_detail(rec)
        self.falhas = 0 if ok else self.falhas + 1
        if not ok:
            rec["detalhe_pendente"] = True
            if self.falhas == 3:
                log("  zap: páginas de anúncio bloqueadas; o resto fica para as próximas varreduras")
        time.sleep(self.pausa)
        return ok


# Casas: unitType CONDOMINIUM = casa de condomínio; RESIDENTIAL_ALLOTMENT_LAND = lote. A busca de lotes
# "em condomínio" do Zap traz todos os lotes residenciais, por isso vai com amenities=Condomínio fechado.
# Para lotes, usableAreas é a área do terreno. condominiumName vem vazio: o nome sai da descrição.
GRUPO_TIPOS = {"CONDOMINIUM": "casa", "RESIDENTIAL_ALLOTMENT_LAND": "terreno", "ALLOTMENT_LAND": "terreno"}
GRUPO_FECHADO = "amenities=Condom%C3%ADnio%20fechado&"


def grupo_buscas(portal):
    """[(url com '?' e parâmetros, unitTypes aceitos, exige prova de condomínio)] da busca do perfil no portal."""
    if apto():
        out = []
        for slug in CFG["grupo_zap_searches"]:
            if portal == "zap":
                url0 = f"https://www.zapimoveis.com.br/venda/apartamentos/{slug}/"
            else:
                url0 = f"https://www.vivareal.com.br/venda/parana/londrina/bairros/{slug.split('++')[1]}/apartamento_residencial/"
            out.append((f"{url0}?areaMinima={CFG['area_min']}&areaMaxima={CFG['area_max']}&", ("APARTMENT", None), False))
        return out
    lotes = ("https://www.zapimoveis.com.br/venda/terrenos-lotes-condominios/pr+londrina/?" if portal == "zap" else
             "https://www.vivareal.com.br/venda/parana/londrina/lote-terreno_residencial/?")
    tipos_lote = ("RESIDENTIAL_ALLOTMENT_LAND", "ALLOTMENT_LAND")
    if terrenos():  # lotes marcados como condomínio fechado + todos os lotes (com prova de condomínio)
        return [(lotes + GRUPO_FECHADO, tipos_lote, False), (lotes, tipos_lote, True)]
    casas = ("https://www.zapimoveis.com.br/venda/casas-de-condominio/pr+londrina/?" if portal == "zap" else
             "https://www.vivareal.com.br/venda/parana/londrina/condominio_residencial/?")
    return [(casas, ("CONDOMINIUM",), False)]


def parar(known, sem_novos):
    """Busca incremental: para depois de N páginas seguidas sem anúncio desconhecido no filtro
    (perfil "paginas_sem_novos"; buscas amplas têm muitas páginas sem nada do filtro)."""
    return bool(known) and sem_novos >= CFG.get("paginas_sem_novos", 1)


def scan_grupo(known, max_pages, found):
    status = {}
    for portal in ("zap", "vivareal"):
        ok = 0
        for url0, tipos, prova in grupo_buscas(portal):
            slug = url0.split("/venda/")[1].split("?")[0].strip("/") + (" (ampla)" if prova else "")
            sem_novos = 0
            for p in range(1, max_pages + 1):
                url = f"{url0}ordem=MOST_RECENT&pagina={p}"
                page = fetch(url)
                if page is None:
                    break
                items = parse_grupo(page)
                ok += 1
                if not items:
                    break
                fresh = 0
                for o in items:
                    if o.get("business") != "SALE" or o.get("unitType") not in tipos:
                        continue
                    addr = o.get("address") or {}
                    if prova and not condominio_ok(o.get("title"), addr.get("neighborhood")):
                        continue
                    # o nome do condomínio quase nunca está no título do Zap: sai da descrição (grupo_detail)
                    if not local_ok(addr.get("neighborhood"), o.get("title"), o.get("condominiumName"),
                                    pendente=not prova) or not coord_ok(addr):
                        continue
                    rec = grupo_record(o)
                    if not rec:
                        continue
                    cur = found.setdefault(rec["id"], rec)
                    cur["links"][portal] = o.get("href")
                    if rec["id"] not in known:
                        fresh += 1
                log(f"  {portal} {slug} p{p}: {len(items)} anúncios, {fresh} desconhecidos")
                sem_novos = 0 if fresh else sem_novos + 1
                if parar(known, sem_novos):
                    break
                time.sleep(1.5)
        status[portal] = "ok" if ok else "falhou"
    return status


# ---------- OLX ----------
def olx_num(v):
    d = re.sub(r"\D", "", v or "")
    return int(d) if d else None


# Casas: categoria "casas de condomínio" (real_estate_type "Venda - casa em condominio fechado").
# Terrenos: a OLX não marca lote de condomínio; busca "condominio" nos lotes à venda (sst=s) e aceita
# os que têm taxa de condomínio ou citam condomínio/residencial no título.
OLX_LONDRINA = "estado-pr/regiao-de-londrina/londrina"


def olx_buscas():
    if apto():
        return [(f"{CFG['olx_search']}?ss={CFG['area_min']}&se={CFG['area_max']}&", None)]
    # Uma busca por condomínio-alvo (a primeira palavra-chave) em todos os imóveis à venda; o tipo
    # (casa/terreno) sai da categoria de cada anúncio (olx_tipo)
    cat = "terrenos/lotes" if terrenos() else "venda"
    return [(f"https://www.olx.com.br/imoveis/{cat}/{OLX_LONDRINA}?sst=s&q={urllib.parse.quote(a['chaves'][0])}&", "auto")
            for a in CONFIG["condominios_alvo"]]


def olx_tipo(a, props):
    """"casa", "terreno" ou None (outra categoria), para as buscas por condomínio."""
    c = norm(a.get("category") or props.get("category"))
    if c.startswith("casa"):
        return None if terrenos() else "casa"
    if (c.startswith("terreno") or c.startswith("lote")) and terrenos():
        return "terreno"
    return None


def olx_tipo_ok(tipo, props, subject):
    if tipo is None:
        return "venda" in norm(props.get("real_estate_type", "venda"))
    if "venda" not in norm(props.get("real_estate_type", "venda")):
        return False
    # casa em condomínio fechado (ou com taxa de condomínio: há casa de condomínio cadastrada "em rua pública")
    if tipo == "casa":
        return "condominio fechado" in norm(props.get("real_estate_type")) or bool(olx_num(props.get("condominio")))
    return bool(olx_num(props.get("condominio"))) or condominio_ok(subject)


def scan_olx(known, max_pages, found):
    pages_ok = 0
    for url0, tipo in olx_buscas():
        pages_ok += _scan_olx(url0, tipo, known, max_pages, found)
    return {"olx": "ok" if pages_ok else "falhou"}


def _scan_olx(url0, tipo0, known, max_pages, found):
    pages_ok = sem_novos = 0
    # busca de um condomínio: poucas páginas (a busca por palavra traz depois o que só se parece com ela)
    paginas = CFG.get("max_pages_condominio", 2) if tipo0 == "auto" else max_pages * 2
    for p in range(1, paginas + 1):
        url = f"{url0}sf=1&o={p}"
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
            # sem o nome do condomínio no título, ele é procurado no texto do anúncio (olx_detail)
            if norm(loc.get("municipality")) != "londrina" or not local_ok(loc.get("neighbourhood"), a.get("subject"),
                                                                             pendente=tipo0 == "auto"):
                continue
            props = {x["name"]: x.get("value") for x in a.get("properties", [])}
            tipo = olx_tipo(a, props) if tipo0 == "auto" else tipo0
            if tipo0 == "auto" and not tipo:
                continue
            area = olx_num(props.get("size"))
            if not area_ok(area):
                continue
            if not olx_tipo_ok(tipo, props, a.get("subject")):
                continue
            imgs = a.get("images") or []
            rid = f"o{a['listId']}"
            rec = {
                "id": rid,
                "tipo": tipo,
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
            if rid not in found and rid not in known:
                fresh += 1
            found.setdefault(rid, rec)
        q = re.search(r"[?&]q=([^&]+)", url0)
        log(f"  olx {urllib.parse.unquote(q.group(1)) if q else tipo0 or ''} p{p}: {len(ads)} anúncios, {fresh} desconhecidos")
        sem_novos = 0 if fresh else sem_novos + 1
        if not ads or parar(known, sem_novos):
            break
        time.sleep(1.5)
    return pages_ok


def olx_detail(rec):
    page = fetch(rec["links"]["olx"])
    if not page:
        return
    t = htmllib.unescape(page)
    m = re.search(r'"body":"(.{0,4000}?)","', t)
    body = m.group(1) if m else ""
    corrige_area(rec, body)
    if not rec.get("predio"):
        rec["predio"] = building_from_text(rec.get("titulo"), body)
    alvo_detalhe(rec, body)


# ---------- imobiliárias locais ----------
# ---------- Raul Fulgêncio (raulfulgencio.com.br, plataforma Moldsystems / msys-imob) ----------
# O site (Next.js) consulta um Solr interno via GET /api/solr/search/<json url-encoded>, que devolve
# {"response": {"numFound", "docs": [...]}}. Parâmetros aceitos: type ("S" = venda, inclui "SL" venda+locação),
# idtCityList, idtDistrictList, idtsCategories (1 = Apartamentos), start/numRows (sem limite prático),
# fieldList (campos a devolver) e idtsPropertys (busca por ids). Os filtros de área (minArea/maxArea) são
# ignorados nessa rota e "usefulArea" quebra a consulta, por isso o bairro e a área são filtrados aqui.
# Varredura = 2 requisições: (1) todos os apartamentos à venda em Londrina com campos enxutos (~550 docs,
# ~250 KB); (2) os que passaram no filtro, por id, só com as fotos (campo mais pesado; sem foto própria,
# usa a do condomínio, como o site).
# Características: prop_char_95 = Área Útil, prop_char_2 = Área total, prop_char_5 = Dormitórios,
# prop_char_176 = Total de banheiros, prop_char_7 = Banheiros, prop_char_12 = Garagens.
RF_BASE = "https://raulfulgencio.com.br"
RF_LONDRINA = 564
RF_FIELDS = ["idtProperty", "indType", "indStatus", "namCity", "namDistrict", "namStreet", "numNumber",
             "namCondominium", "namCategory", "desTitleSite", "prop_char_95", "prop_char_5", "prop_char_7",
             "prop_char_176", "prop_char_12", "valSales", "flgHideValSaleSite", "dtaRegister",
             "dtaRegisterCaptivatorSales", "idtCategory", "idtSubCategory", "prop_char_1", "prop_char_2",
             "latitude", "longitude", "flgShowMapSite"]
# Casas em condomínio: categoria 2 (Casas) subcategorias 10 (Condomínio) e 40 (Sobrado Condomínio);
# terrenos: categoria 5 subcategoria 22 (Condomínio). A API ignora filtro por subcategoria. Há casas e
# terrenos de condomínio cadastrados em outras subcategorias, mas com namCondominium preenchido.
# Área: prop_char_1 = construída (casas), prop_char_2 = total (o lote, nos terrenos).
RF_COND_SUBS = {10, 40, 22}
RF_PREDIO_PREFIX = re.compile(
    r"^(?:ed\.?|edif\.?|edif[ií]cio|cond\.?|condom[ií]nio|res\.?|residencial)\s+", re.I)


def _rf_search(q):
    url = RF_BASE + "/api/solr/search/" + urllib.parse.quote(json.dumps(q, separators=(",", ":")))
    txt = fetch(url)
    if not txt:
        return None
    try:
        d = json.loads(txt)
        return d["response"] if isinstance(d, dict) else None  # erro do Solr vem como string JSON
    except (ValueError, KeyError, TypeError):
        return None


def _rf_int(v):
    try:
        return int(round(float(v))) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _rf_publicado(o):
    """dtaRegister = cadastro do imóvel no sistema (com hora). Em imóveis "venda e locação" o cadastro pode
    ser antigo (entrou para aluguel); se a captação para venda for posterior e não futura, usa-a."""
    reg = o.get("dtaRegister")
    cap = o.get("dtaRegisterCaptivatorSales")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if o.get("indType") == "SL" and cap and reg and reg < cap <= now:
        return cap[:19] + "Z"
    return reg[:19] + "Z" if reg else None


def scan_raulfulgencio(found):
    """Adiciona em found[id] os anúncios no filtro. Retorna "ok" ou "falhou"."""
    q = {"type": "S", "idtCityList": [RF_LONDRINA], "idtsCategories": [1] if apto() else [5] if terrenos() else [2],
         "start": 0, "numRows": 3000,
         "getAccess": True, "fieldList": RF_FIELDS}
    resp = _rf_search(q)
    if resp is None or not isinstance(resp.get("docs"), list):
        log("  raulfulgencio: busca falhou")
        return "falhou"
    docs, total = resp["docs"], resp.get("numFound") or 0
    recs = {}
    for o in docs:
        if o.get("indType") not in ("S", "SL") or norm(o.get("namCity")) != "londrina":
            continue
        bairro = o.get("namDistrict") or ""
        predio = RF_PREDIO_PREFIX.sub("", (o.get("namCondominium") or "").strip()).strip(" .-")
        predio = predio.split("|")[0].strip()  # "SUN LAKE | TERRENO"
        if apto():
            if norm(o.get("namCategory")) != "apartamentos":
                continue
            tipo, area = None, _rf_int(o.get("prop_char_95"))
        else:
            if o.get("idtCategory") != (5 if terrenos() else 2):
                continue
            if o.get("idtSubCategory") not in RF_COND_SUBS and not predio and not (
                    terrenos() and condominio_ok(o.get("desTitleSite"))):
                continue
            tipo = "casa" if o["idtCategory"] == 2 else "terreno"
            area = _rf_int(o.get("prop_char_1" if tipo == "casa" else "prop_char_2"))
        if not local_ok(bairro, predio, o.get("desTitleSite")) or not area_ok(area):
            continue
        pid = o["idtProperty"]
        rua = (o.get("namStreet") or "").strip()
        num = (o.get("numNumber") or "").strip()
        preco = None if o.get("flgHideValSaleSite") else (_rf_int(o.get("valSales")) or None)
        recs[pid] = {
            "id": f"rf{pid}",
            "tipo": tipo,
            "predio": predio or building_from_text(o.get("desTitleSite")),
            "endereco": (f"{rua}, {num}" if num and num != "0" else rua) or None,
            "bairro": bairro,
            "foto": None,
            "area": area,
            "preco": preco,
            "quartos": _rf_int(o.get("prop_char_5")),
            "banheiros": _rf_int(o.get("prop_char_176")) or _rf_int(o.get("prop_char_7")),
            "vagas": _rf_int(o.get("prop_char_12")),
            "titulo": (o.get("desTitleSite") or "").strip(" |") or None,
            "publicado": _rf_publicado(o),
            "links": {"raulfulgencio": f"{RF_BASE}/imovel/{pid}"},  # redireciona para a URL canônica
            **coords(o.get("latitude"), o.get("longitude"), aproximada=not o.get("flgShowMapSite")),
        }
    # Fotos (campo pesado) só para os que passaram no filtro.
    if recs:
        time.sleep(1)
        ph = _rf_search({"idtsPropertys": list(recs), "start": 0, "numRows": len(recs) + 10,
                         "fieldList": ["idtProperty", "jsonPhotos", "jsonPhotosCondominium"]})
        for o in (ph or {}).get("docs") or []:
            r = recs.get(o.get("idtProperty"))
            try:
                fotos = json.loads(o.get("jsonPhotos") or "[]")
                if not fotos:  # sem foto própria: o site mostra as do condomínio
                    fotos = json.loads(o.get("jsonPhotosCondominium") or "{}").get("photosCondominium") or []
            except (ValueError, AttributeError):
                fotos = []
            fotos = [f["urlPhoto"] for f in fotos
                     if isinstance(f, dict) and f.get("urlPhoto") and not f.get("flgNotShowSite")]
            if r is not None and fotos:
                r["foto"] = fotos[0]
    for r in recs.values():
        found[r["id"]] = r
    log(f"  raulfulgencio: {len(recs)} anúncios no filtro ({total} à venda em Londrina na categoria)")
    return "ok"


# ---------- Gleba Imóveis (www.glebaimoveis.com, plataforma Kenlo) ----------
# A página de listagem (HTML renderizado no servidor) traz os anúncios em JSON embutido em
# window.markoVars['listings-xxxx'] = {...}. Filtros pela URL: finalidade/tipo/cidade no caminho,
# área (útil) em ?area=MIN~MAX, paginação em ?pagina=N (12 por página).
GLEBA_BASE = "https://www.glebaimoveis.com"
GLEBA_MARKO = re.compile(r"window\.markoVars\['listings-[a-z0-9]+'\]\s*=\s*")


def _gleba_first(v):
    if isinstance(v, list):
        v = v[0] if v else None
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


# Casas e terrenos: não há URL para "em condomínio"; busca casa e terreno e fica com os que têm
# condo_name ou a comodidade GATED_COMMUNITY. "area" é a construída (casa) ou a do lote (terreno).
GLEBA_TIPOS = {"apartamento": "APARTMENT", "casa": "HOUSE", "terreno": "LAND"}


def scan_gleba(found):
    """Adiciona em found[id] os anúncios no filtro. Retorna "ok" ou "falhou"."""
    if apto():
        return _scan_gleba(f"{GLEBA_BASE}/imoveis/a-venda/apartamento/londrina"
                           f"?area={CFG['area_min']}~{CFG['area_max']}&", "apartamento", found)
    tipos = ("terreno",) if terrenos() else ("casa",)
    st = [_scan_gleba(f"{GLEBA_BASE}/imoveis/a-venda/{t}/londrina?", t, found) for t in tipos]
    return "ok" if "ok" in st else "falhou"


def _scan_gleba(url0, tipo, found):
    page, total, n = 1, None, 0
    while page <= 15:
        html = fetch(url0 + f"pagina={page}")
        if not html:
            return "falhou" if page == 1 else "ok"
        m = GLEBA_MARKO.search(html)
        if not m:
            log("  gleba: JSON de listagem não encontrado")
            return "falhou" if page == 1 else "ok"
        try:
            listings = json.JSONDecoder().raw_decode(html, m.end())[0]["settings"]["listings"]
        except (ValueError, KeyError, TypeError):
            log("  gleba: JSON de listagem inválido")
            return "falhou" if page == 1 else "ok"
        data = listings.get("data") or []
        total = listings.get("count") or 0
        for o in data:
            if o.get("property_type") != GLEBA_TIPOS[tipo] or "SALE" not in (o.get("property_purposes") or ""):
                continue
            if norm(o.get("city")) != "londrina":
                continue
            if tipo != "apartamento" and not (o.get("condo_name") or "GATED_COMMUNITY" in (o.get("amenities") or [])
                                              or (terrenos() and condominio_ok(o.get("website_title")))):
                continue
            bairro = o.get("neighborhood_display") or o.get("neighborhood") or ""
            area = _gleba_first(o.get("area"))
            if not local_ok(bairro, o.get("condo_name"), o.get("website_title")) or not area_ok(area):
                continue
            ref = o.get("property_full_reference") or o.get("property_reference")
            if not ref:
                continue
            # full_address na listagem: "Rua X - Bairro - Cidade/UF" (rua sem número)
            fa = (o.get("full_address") or "").split(" - ")[0].strip()
            endereco = fa if fa and norm(fa) != norm(bairro) else None
            predio = (o.get("condo_name") or "").strip() or building_from_text(
                o.get("website_title"), o.get("listing_description"))
            preco = _gleba_first(o.get("sale_price"))
            found["gi" + ref] = {
                "id": "gi" + ref,
                "tipo": None if tipo == "apartamento" else tipo,
                "predio": predio or None,
                "endereco": endereco,
                "bairro": bairro,
                "foto": o.get("picture_full") or o.get("picture_thumb") or None,
                "area": area,
                "preco": preco or None,
                "quartos": _gleba_first(o.get("bedrooms")),
                "banheiros": _gleba_first(o.get("bathrooms")),
                "vagas": _gleba_first(o.get("garages")),
                "titulo": o.get("heading1") or o.get("website_title"),
                "publicado": None,  # o site só expõe updated_at
                "links": {"gleba": GLEBA_BASE + o["url"] if o.get("url") else None},
            }
            corrige_area(found["gi" + ref], o.get("listing_description"))
            n += 1
        if not data or page * 12 >= total:
            break
        page += 1
        time.sleep(1)
    log(f"  gleba {tipo}: {n} anúncios no filtro ({total} na busca, {page} páginas)")
    return "ok"


# ---------- Imobiliária Santamérica (Londrina) — plataforma Kurole, HTML ----------
SA_BASE = "https://www.santamerica.com.br"
SA_TIPOS = ("1", "3", "8")  # Apartamento: Cobertura, Duplex, Padrão
SA_TIPOS_COND = {"10": "casa", "28": "terreno"}  # Casa - Condomínio, Terreno - Condomínio
# ids de bairro (Londrina) usados se a lista dinâmica do formulário falhar
SA_BAIRROS_FALLBACK = ("310", "3812", "3723", "534", "2673", "3887", "3629", "3261")


def _sa_txt(s):
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def _sa_int(s):
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else None


def _sa_bairro_ids():
    page = fetch(SA_BASE + "/busca-imobiliaria2-refinar.php?locacao_venda=V")
    time.sleep(1)
    if page:
        m = re.search(r'<select[^>]*name="id_bairro\[\]"[^>]*>(.*?)</select>', page, re.S)
        g = m and re.search(r'<optgroup label="Londrina"[^>]*>(.*?)</optgroup>', m.group(1), re.S)
        if g:
            ids = [v for v, n in re.findall(r'value="(\d+)"[^>]*>([^<]*)', g.group(1)) if bairro_ok(htmllib.unescape(n))]
            if ids:
                return ids
    log("  santamerica: usando ids de bairro fixos")
    return list(SA_BAIRROS_FALLBACK)


def _sa_cards(page):
    out = []
    for blk in page.split('<div class="muda_card1')[1:]:
        m = re.search(r'href="(/comprar/[^"]+/(\d+))"', blk)
        if not m:
            continue
        foto = re.search(r'data-flickity-lazyload-src="([^"]+)"', blk)
        preco = re.search(r'card-valores">\s*<div>\s*R\$\s*([\d.]+)', blk)
        bairro = re.search(r'card-bairro-cidade-texto">(.*?)</div>', blk, re.S)
        titulo = re.search(r'<h2[^>]*card-titulo[^>]*>(.*?)</h2>', blk, re.S)
        desc = re.search(r'card-texto">(.*?)</div>', blk, re.S)
        b = _sa_txt(bairro.group(1)) if bairro else ""
        out.append({
            "cod": m.group(2), "url": SA_BASE + m.group(1),
            "foto": foto.group(1) if foto else None,
            "preco": _sa_int(preco.group(1)) if preco else None,
            "bairro": b.split(" - ")[0].strip(),
            "cidade": b,
            "titulo": _sa_txt(titulo.group(1)) if titulo else None,
            "desc": _sa_txt(desc.group(1)) if desc else "",
        })
    return out


def _sa_num(page, cls):
    m = re.search(r'class="[^"]*\b' + cls + r'\b[^"]*"[^>]*>.*?<div class="fw-bold[^"]*">(?:<span></span>)?\s*(?:<strong[^>]*>)?\s*([\d.,]+)', page, re.S)
    return m.group(1) if m else None


def scan_santamerica(found):
    """Adiciona em found[id] os anúncios no filtro. Retorna "ok" ou "falhou"."""
    try:
        if apto():
            bairros = _sa_bairro_ids()
            q = "locacao_venda=V&id_cidade[]=2&" + "&".join(f"id_tipo_imovel[]={t}" for t in SA_TIPOS)
            q += "&" + "&".join(f"id_bairro[]={b}" for b in bairros)
            q += f"&area_tipo=area-util&a_min={CFG['area_min']}&a_max={CFG['area_max']}"
            q += f"&a_util_min={CFG['area_min']}&a_util_max={CFG['area_max']}"
        else:  # a zona é filtrada aqui, pelo bairro/nome do condomínio
            q = "locacao_venda=V&id_cidade[]=2&" + "&".join(
                f"id_tipo_imovel[]={t}" for t, tp in SA_TIPOS_COND.items() if (tp == "terreno") == terrenos())
        cards, seen, ok_pages = [], set(), 0
        for pag in range(1, 12):
            page = fetch(f"{SA_BASE}/pesquisa-de-imoveis/?{q}" + (f"&pag={pag}" if pag > 1 else ""))
            time.sleep(1)
            if page is None:
                break
            ok_pages += 1
            novos = [c for c in _sa_cards(page) if c["cod"] not in seen]
            for c in novos:
                seen.add(c["cod"])
                cards.append(c)
            if not novos or f"pag={pag + 1}" not in page:
                break
        if not ok_pages:
            return "falhou"
        log(f"  santamerica: {len(cards)} cards na busca")
        n = 0
        for c in cards:
            if "londrina" not in norm(c["cidade"]) or not local_ok(c["bairro"], c["titulo"]):
                continue
            rec = conhecido("sa" + c["cod"], {"santamerica": c["url"]}, preco=c["preco"])
            if rec:
                found[rec["id"]] = rec
                n += 1
                continue
            page = fetch(c["url"])
            time.sleep(1)
            if not page:
                continue
            h1 = re.search(r"<h1[^>]*titulo-imovel[^>]*>(.*?)</h1>", page, re.S)
            titulo = _sa_txt(h1.group(1)) if h1 else c["titulo"]
            tipo = None
            if apto():
                a = _sa_num(page, "a-util-ico-imo")
            elif "/terreno/" in c["url"].lower():
                tipo, a = "terreno", _sa_num(page, "a-terr-ico-imo") or _sa_num(page, "a-total-ico-imo")
            else:
                # Casas: nem toda página traz área útil; cai para a construída ou a citada no texto
                tipo, a = "casa", _sa_num(page, "a-util-ico-imo") or _sa_num(page, "a-cons[a-z]*-ico-imo")
                if not a:
                    m = re.search(r"(\d{2,4}(?:[.,]\d+)?)\s*m(?:²|2)\s*(?:de\s+[áa]rea\s+)?(?:privativ|constru|[úu]til)",
                                  titulo + " " + c["desc"], re.I)
                    a = m.group(1) if m else None
            area = round(float(a.replace(".", "").replace(",", ".") if "," in a else a)) if a else None
            if not area_ok(area):
                continue
            if not apto() and not local_ok(c["bairro"], c["titulo"], titulo):
                continue
            preco = c["preco"]
            pm = re.search(r'"price":"([\d.]+)"', page)
            if pm and float(pm.group(1)) > 0:
                preco = int(float(pm.group(1)))
            ed = re.search(r'Itens do (?:Edif|Condom)[^<]*<a[^>]*>(.*?)</a>', page, re.S)
            predio = None
            if ed:
                predio = re.sub(r"(?i)^(?:(?:ed\.|edif\.|edif[ií]cio|residencial|condom[ií]nio)\s+)+", "", _sa_txt(ed.group(1))) or None
            if not predio:
                ld = re.search(r'"description":"(.*?)","', page)
                desc = json.loads('"' + ld.group(1) + '"') if ld else c["desc"]
                predio = building_from_text(titulo, c["titulo"], desc)
            if not predio:
                pm = re.search(r"(?i)\b(?:edif[ií]cio|condom[ií]nio|residencial)\s+(.{2,40}?)(?:,|\.|\s+(?:n[ao]s?|em|localizado|com|-)\s|$)", titulo or "")
                predio = pm.group(1).strip() if pm else None
            foto = c["foto"]
            if not foto:
                og = re.search(r'property="og:image" content="([^"]+)"', page)
                foto = og.group(1) if og else None
            rid = "sa" + c["cod"]
            found[rid] = {
                "id": rid,
                "tipo": tipo,
                "predio": predio,
                "endereco": None,
                "bairro": c["bairro"],
                "foto": foto,
                "area": area,
                "preco": preco,
                "quartos": _sa_int(_sa_num(page, "dorm-ico-imo")),
                "banheiros": _sa_int(_sa_num(page, "banh-ico-imo")),
                "vagas": _sa_int(_sa_num(page, "gar-ico-imo")),
                "titulo": titulo,
                "publicado": None,
                "links": {"santamerica": c["url"]},
            }
            n += 1
        log(f"  santamerica: {n} anúncios no filtro")
        return "ok"
    except Exception as e:  # noqa: BLE001
        log(f"  santamerica: erro {e}")
        return "falhou"


# ---------- Imobiliária Perez (Londrina) ----------
# site Next.js da plataforma Keyspot (tenant KS1274).
# Usa a API JSON pública que o próprio site chama: site-api.keyspot.com.br/api/ks/properties/search
# (filtros: venda, apartamento, Londrina, área privativa) + /api/ks/property/<código> só para os
# anúncios que passam no filtro (para obter rua e número).

PZ_API = "https://site-api.keyspot.com.br/api/ks"
PZ_TENANT = "KS1274"
PZ_SITE = "https://www.imobiliariaperez.com.br"
# createdAt dos imóveis importados na migração para a Keyspot (lote de 2026-01-07) não é data real de publicação.
PZ_MIGRACAO_ATE = "2026-01-08"


def _pz_get(path, params):
    params = dict(params, code=PZ_TENANT)
    for i in range(3):
        try:
            r = S.get(f"{PZ_API}{path}", params=params, timeout=45,
                      headers={"x-tenant-code": PZ_TENANT, "Accept": "application/json",
                               "Origin": PZ_SITE, "Referer": PZ_SITE + "/"})
            if r.status_code == 200:
                return r.json()
            log(f"  HTTP {r.status_code} perez {path}")
            if r.status_code == 404:
                return None
        except Exception as e:  # noqa: BLE001
            log(f"  erro {e} perez {path}")
        time.sleep(3 * (i + 1))
    return None


def _pz_slug(s):
    return re.sub(r"[^a-z0-9]+", "-", norm(s)).strip("-")


def _pz_link(o):
    n = []
    for v, um, varios in ((o.get("bedrooms"), "quarto", "quartos"), (o.get("suites"), "suite", "suites"),
                          (o.get("bathrooms"), "banheiro", "banheiros"), (o.get("parkingSpaces"), "vaga", "vagas")):
        if v and v > 0:
            n.append(f"{v}-{um if v == 1 else varios}")
    parts = [_pz_slug(o.get("propertyTypeName") or "imovel"), "a-venda", *n,
             _pz_slug(o.get("neighborhoodName") or "bairro"), _pz_slug(o.get("cityName") or "londrina"),
             _pz_slug(o.get("stateUf") or "pr"), f"cod-{o['code']}"]
    return f"{PZ_SITE}/imovel/{'-'.join(p for p in parts if p)}"


# Casas: propertyType "Casa em Condomínio" (builtArea = construída; sem ela, totalArea). Não existe
# "Terreno em Condomínio": os lotes de condomínio vêm misturados em "Terrenos" (área em totalArea;
# minArea/maxArea filtram por builtArea, que é 0 nos terrenos) e são reconhecidos pela taxa de
# condomínio ou pelo texto do anúncio. A API limita a 30 por página.
PZ_TIPOS = {"Casa em Condomínio": "casa", "Terrenos": "terreno"}


def _pz_busca(base):
    itens, page, pages = [], 1, 1
    while page <= pages and page <= 25:
        d = _pz_get("/properties/search", dict(base, page=page))
        if d is None or "items" not in d:
            return None
        itens += d["items"]
        pages = d.get("pages") or 1
        page += 1
        time.sleep(1)
    return itens


def scan_perez(found):
    """Adiciona em found[id] os anúncios no filtro. Retorna "ok" ou "falhou"."""
    base = {"limit": 30, "operationType": "SALE", "city": "Londrina"}
    if apto():
        buscas = {"Apartamento": dict(base, propertyType="Apartamento", minArea=max(0, CFG["area_min"] - 1),
                                      maxArea=CFG["area_max"] + 1)}
    else:
        buscas = {t: dict(base, propertyType=t) for t, tp in PZ_TIPOS.items() if (tp == "terreno") == terrenos()}
    itens = []
    for t, q in buscas.items():
        r = _pz_busca(q)
        if r is None:
            log(f"  perez: falha na busca {t}")
            if apto():
                return "falhou"
            continue
        for o in r:
            o["_tipo"] = PZ_TIPOS.get(t)
        itens += r
        log(f"  perez: {len(r)} {t} em Londrina")
    if not itens:
        return "falhou"

    n = 0
    for o in itens:
        if o.get("operationType") != "SALE" or norm(o.get("cityName")) != "londrina":
            continue
        bairro = o.get("neighborhoodName") or ""
        tipo = o["_tipo"]
        if tipo == "terreno":
            area = round(o["totalArea"]) if o.get("totalArea") else None
        else:  # builtArea = área privativa/construída
            area = round(o.get("builtArea") or (o.get("totalArea") if tipo else 0) or 0) or None
        if not local_ok(bairro, o.get("title")) or not area_ok(area):
            continue
        fotos = o.get("photos") or []
        rec = conhecido(f"pz{o['code']}", {"perez": _pz_link(o)}, preco=int(o["price"]) if o.get("price") else None,
                        foto=fotos[0]["url"] if fotos else None)
        if rec:
            found[rec["id"]] = rec
            n += 1
            continue
        endereco = None
        det = _pz_get(f"/property/{o['code']}", {})
        time.sleep(1)
        if det:
            rua, num = (det.get("street") or "").strip(), (det.get("number") or "").strip()
            if rua:
                if not re.match(r"(?i)(rua|r\.|av|avenida|alameda|al\.|travessa|rodovia|estrada|pra[cç]a)\b", rua):
                    rua = "Rua " + rua
                endereco = f"{rua}, {num}" if num and num not in ("0", "S/N", "s/n") else rua
        if tipo == "terreno" and not ((det or {}).get("condominiumFee") or re.search(
                r"(?i)condom[ií]nio|alphaville", f"{o.get('title')} {(det or {}).get('description')}")):
            continue  # lote de rua
        cond = (det or {}).get("condominium")
        predio = (cond.get("name") if isinstance(cond, dict) else None) or building_from_text(
            o.get("title"), (det or {}).get("description"))
        criado = o.get("createdAt")
        publicado = None
        if criado and criado[:10] >= PZ_MIGRACAO_ATE:
            publicado = criado[:19] + "Z"
        fotos = o.get("photos") or []
        found[f"pz{o['code']}"] = {
            "id": f"pz{o['code']}",
            "tipo": tipo,
            "predio": predio,
            "endereco": endereco,
            "bairro": bairro,
            "foto": fotos[0]["url"] if fotos else None,
            "area": area,
            "preco": int(o["price"]) if o.get("price") else None,
            "quartos": o.get("bedrooms") or None,
            "banheiros": o.get("bathrooms") or None,
            "vagas": o.get("parkingSpaces") if o.get("parkingSpaces") is not None else None,
            "titulo": o.get("title"),
            "publicado": publicado,
            "links": {"perez": _pz_link(o)},
            **coords(o.get("latitude"), o.get("longitude")),
        }
        n += 1
    log(f"  perez: {n} no filtro")
    return "ok"


# ---------- Catuaí Imóveis (Londrina) ----------
# https://www.catuaiimoveis.com.br  (plataforma Imobase, HTML)


def scan_catuai(found):
    """Adiciona em found[id] os anúncios no filtro. Retorna "ok" ou "falhou"."""
    base = "https://www.catuaiimoveis.com.br"
    amin, amax = CFG["area_min"], CFG["area_max"]
    # Busca do próprio site: tipo + venda + cidade + área privativa. O bairro é filtrado aqui
    # (uma busca só por cidade cobre todos os bairros-alvo em ~4 páginas de 15).
    # Casas/terrenos em condomínio têm categoria própria; sem filtro de área (13 dos terrenos não
    # mostram área no card, só "m² TOTAL" na página do anúncio).
    if apto():
        searches = [f"{base}/imoveis/apartamento/a-venda/londrina-pr?mt_minima={amin}&mt_maxima={amax}",
                    f"{base}/imoveis/apartamento-na-planta/a-venda/londrina-pr?mt_minima={amin}&mt_maxima={amax}"]
    elif terrenos():  # categoria de condomínio + todos os terrenos (estes com prova de condomínio no título)
        searches = [f"{base}/imoveis/terreno-em-condominio/a-venda/londrina-pr?x=1",
                    f"{base}/imoveis/terreno/a-venda/londrina-pr?x=1"]
    else:
        searches = [f"{base}/imoveis/casa-em-condominio/a-venda/londrina-pr?x=1"]

    def num(s):
        d = re.sub(r"\D", "", (s or "").split(",")[0])
        return int(d) if d else None

    def clean(s):
        return re.sub(r"\s+", " ", htmllib.unescape(s or "")).strip()

    def predio_from_title(t):
        # "Apartamento à venda - Ed. X - Bairro, Londrina", "Casa em condomínio à venda - Condomínio X - Y, Londrina",
        # "Terreno à venda em condomínio - Condomínio X, Londrina"
        m = re.match(r"(?i)^(?:apartamento|casa(?:\s+em\s+condom[ií]nio)?|terreno(?:\s+em\s+condom[ií]nio)?)\s+"
                     r"(?:[àáa]\s+venda|para\s+venda)(?:\s+e\s+loca[çc][ãa]o)?(?:\s+em\s+condom[ií]nio)?\s*-?\s*(.+?)\s*"
                     r"(?:-\s*[^-]*,\s*londrina|,?\s*londrina)\s*$", t or "")
        if not m:
            return None
        name = re.sub(r"(?i)^(ed\.|edif[ií]cio|residencial|condom[ií]nio)\s+", "", m.group(1)).strip(" -")
        if not name or (apto() and bairro_ok(name)) or re.search(r"(?i)\d+\s*m2|m²|quartos|su[ií]tes", name) or len(name) > 45:
            return None
        return name

    ok = 0
    recs = {}
    for url0 in searches:
        last = 1
        p = 1
        while p <= last and p <= 10:
            page = fetch(url0 + (f"&page={p}" if p > 1 else ""))
            if page is None:
                break
            ok += 1
            pages = [int(x) for x in re.findall(r'class="page-link"[^>]*href="[^"]*[?&](?:amp;)?page=(\d+)"', page)]
            last = max(pages) if pages else 1
            chunks = page.split('class="carrossel-item"')[1:]
            n = 0
            for ch in chunks:
                m = re.search(r'class="property-title">\s*<h3>([^<]*)</h3>', ch)
                if not m:
                    continue
                n += 1
                titulo = clean(m.group(1))
                slug = re.search(r'href="/imovel/([^"]+)"', ch)
                addr = re.search(r"<address>([^<]*)</address>", ch)
                if not slug or not addr:
                    continue
                bairro, _, cidade = clean(addr.group(1)).rpartition(" - ")
                if norm(cidade) != "londrina" or not local_ok(bairro, titulo):
                    continue
                if "/terreno/" in url0 and not condominio_ok(titulo):
                    continue
                am = re.search(r"<b>\s*([\d.,]+)\s*m²\s*</b>\s*<span>\s*privativ", ch)
                area = num(am.group(1)) if am else None
                if apto() and not area_ok(area):
                    continue
                ref = re.search(r"Refer[êe]ncia:\.?\s*([A-Za-z0-9]+)", ch)
                code = ref.group(1) if ref else slug.group(1)
                pm = re.search(r"<strong>\s*R\$\s*([\d.,]+)", ch)
                foto = re.search(r'<img[^>]+src="(https://[^"]+)"', ch)
                link = f"{base}/imovel/{slug.group(1)}"
                recs["ct" + code] = {
                    "id": "ct" + code, "predio": None,
                    "tipo": None if apto() else ("terreno" if "/terreno" in url0 else "casa"), "endereco": None, "bairro": bairro,
                    "foto": foto.group(1) if foto else None, "area": area,
                    "preco": num(pm.group(1)) if pm else None,
                    "quartos": None, "banheiros": None, "vagas": None, "titulo": titulo,
                    "publicado": None, "links": {"catuai": link},
                }
            log(f"  catuai {url0.split('/imoveis/')[1].split('?')[0]} p{p}/{last}: {n} cards, {len(recs)} no filtro")
            if n == 0:
                break
            p += 1
            time.sleep(1)

    # Página do anúncio: quartos, banheiros, vagas, rua e descrição (nome do prédio). O site não expõe data de cadastro.
    for rid, rec in list(recs.items()):
        k = conhecido(rid, rec["links"], preco=rec["preco"], foto=rec["foto"])
        if k:
            recs[rid] = rec = k
            if area_ok(rec.get("area")):
                found[rid] = rec
            continue
        time.sleep(1)
        page = fetch(rec["links"]["catuai"])
        if page:
            ok += 1
            det = page.split('class="property-details"', 1)[-1][:6000]
            for key, pat in (("quartos", r"(\d+)\s*Quartos?"), ("banheiros", r"(\d+)\s*BWCs?"),
                             ("vagas", r"(\d+)\s*Vagas?")):
                m = re.search(pat, det)
                if m:
                    rec[key] = int(m.group(1))
            m = re.search(r'<h1 class="type-city">[^<]*?\bEM\s+([^<]+?),\s*[^,<]+-\s*[A-Z]{2}\s*</h1>', page)
            if m:
                rua = clean(m.group(1))
                if not bairro_ok(rua) and norm(rua) != norm(rec["bairro"]):
                    rec["endereco"] = rua
            if not rec["area"]:
                m = re.search(r"([\d.,]+)\s*m²\s*(?:</[^>]+>\s*<[^>]+>\s*)?TOTAL", page)
                rec["area"] = num(m.group(1)) if m else None
            m = re.search(r"DESCRI[ÇC][ÃA]O DO IM[ÓO]VEL.*?<article>(.*?)</article>", page, re.S)
            desc = clean(re.sub(r"<[^>]+>", " ", m.group(1))) if m else ""
            rec["predio"] = predio_from_title(rec["titulo"]) or building_from_text(rec["titulo"], desc)
        else:
            rec["predio"] = predio_from_title(rec["titulo"]) or building_from_text(rec["titulo"])
        if area_ok(rec["area"]):
            found[rec["id"]] = rec
    return "ok" if ok else "falhou"


# ---------- Imobiliária Inglaterra (Londrina) — site Kenlo ----------
# API JSON interna do site (a mesma que o widget de listagem usa):
#   /api/listings/a-venda/apartamento/londrina?area=100~200&size=48&pagina=N
# devolve {"data": [...], "count": N}. Filtra bairro aqui (bairro_ok), pois a API só aceita
# um bairro por slug e os nomes variam. Para os anúncios no filtro sem nome de prédio,
# consulta /api/listing/<ref> (traz "building" e endereço com número), com limite.
INGLATERRA_BASE = "https://www.imobiliariainglaterra.com.br"
INGLATERRA_DETAIL_MAX = 25
KENLO_PAGINAS = 40  # de 48 anúncios (casas e terrenos de Londrina inteira: ~900)


def _in_first(v):
    if isinstance(v, list):
        v = v[0] if v else None
    return v


def _in_int(v):
    v = _in_first(v)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return int(round(v)) if v > 0 else None


def _in_street(full_address):
    """'Doutor Dimas de Barros - Gleba Palhano - Londrina/PR' -> 'Doutor Dimas de Barros'."""
    if not full_address:
        return None
    parts = [p.strip() for p in full_address.split(" - ")]
    return parts[0] if len(parts) >= 3 and parts[0] else None


def _in_title_ok(t):
    return bool(t) and len(t) > 20 and "ilustrativa" not in norm(t)


def scan_inglaterra(found):
    """Adiciona em found[id] os anúncios no filtro. Retorna "ok" ou "falhou"."""
    if apto():
        urls = [(f"{INGLATERRA_BASE}/api/listings/a-venda/apartamento/londrina"
                 f"?area={CFG['area_min']}~{CFG['area_max']}&size=48", None)]
    elif terrenos():  # todos os terrenos; os sem a etiqueta de condomínio precisam de prova (_scan_inglaterra)
        urls = [(f"{INGLATERRA_BASE}/api/listings/a-venda/terreno/londrina?size=48", "terreno")]
    else:  # não há tipo "em condomínio": casas, com a etiqueta de condomínio fechado
        urls = [(f"{INGLATERRA_BASE}/api/listings/a-venda/casa/londrina?size=48&tags=GATED_COMMUNITY", "casa")]
    items, total = [], 0
    for url, tipo in urls:
        got = _in_lista(url)
        if got is None:
            if apto():
                return "falhou"
            continue
        for x in got[0]:
            x["_tipo"] = tipo
        items += got[0]
        total += got[1]
    if not items and total == 0:
        return "falhou"
    return _scan_inglaterra(items, total, found)


def _in_lista(url):
    items, total, page = [], None, 1
    while page <= KENLO_PAGINAS:
        try:
            r = S.get(url + f"&pagina={page}", timeout=45, headers={"Accept": "application/json"})
            j = r.json() if r.status_code == 200 else None
        except Exception as e:  # noqa: BLE001
            log(f"  inglaterra erro {e}")
            j = None
        if not j or not isinstance(j.get("data"), list):
            log(f"  inglaterra: falha na página {page}")
            return None if page == 1 else (items, total)
        total = j.get("count") or 0
        items += j["data"]
        if not j["data"] or len(items) >= total:
            break
        page += 1
        time.sleep(1)
    return items, total


def _scan_inglaterra(items, total, found):
    recs = []
    for x in items:
        if x.get("type") != "imob_property":  # "imob_condo" = página de empreendimento
            continue
        if "FOR_SALE" not in str(x.get("property_purposes")):
            continue
        bairro = x.get("neighborhood_display") or x.get("neighborhood") or ""
        if norm(x.get("city")) != "londrina" or not local_ok(bairro, x.get("condo_name"), x.get("picture_title"),
                                                              x.get("website_title")):
            continue
        # tags=GATED_COMMUNITY deixou de filtrar (2026-10-23: a API devolve todas as casas): a marca é
        # conferida em cada anúncio
        if not apto() and not (x.get("condo_name") or "GATED_COMMUNITY" in (x.get("amenities") or [])
                               or condominio_ok(x.get("picture_title"), x.get("website_title"))):
            continue
        area = _in_int(x.get("area"))
        if not area_ok(area):
            continue
        ref = x.get("property_full_reference")
        if not ref:
            continue
        ptitle = x.get("picture_title")
        titulo = ptitle if _in_title_ok(ptitle) else (x.get("website_title") or x.get("heading1"))
        condo = (x.get("condo_name") or "").split("|")[0].strip() or None
        predio = condo or building_from_text(ptitle, x.get("listing_description"))
        rec = {
            "id": "in" + ref,
            "tipo": x["_tipo"],
            "predio": predio,
            "endereco": _in_street(x.get("full_address")),
            "bairro": bairro,
            "foto": x.get("picture_full") or x.get("picture_thumb"),
            "area": area,
            "preco": _in_int(x.get("sale_price")),
            "quartos": _in_int(x.get("bedrooms")),
            "banheiros": _in_int(x.get("bathrooms")),
            "vagas": _in_int(x.get("garages")),
            "titulo": titulo,
            "publicado": None,  # o site só expõe updated_at (data de atualização)
            "links": {"inglaterra": INGLATERRA_BASE + x["url"] if x.get("url") else INGLATERRA_BASE},
        }
        corrige_area(rec, x.get("listing_description"))
        recs.append((ref, rec))

    # detalhe (prédio + endereço com número) para quem ficou sem nome de prédio
    n = 0
    for ref, rec in recs:
        if rec["predio"] or n >= INGLATERRA_DETAIL_MAX or rec["id"] in CONHECIDOS or not preco_ok(rec["preco"]):
            continue
        n += 1
        time.sleep(1)
        try:
            r = S.get(f"{INGLATERRA_BASE}/api/listing/{ref}", timeout=45, headers={"Accept": "application/json"})
            d = r.json() if r.status_code == 200 else None
        except Exception:  # noqa: BLE001
            d = None
        if not isinstance(d, dict):
            continue
        b = (d.get("building") or "").split("|")[0].strip()
        rec["predio"] = b or building_from_text(d.get("title"), rec["titulo"])
        fa = d.get("full_address") or ""
        street = fa.split(" - ")[0].strip()
        if street and "," in street:
            rec["endereco"] = street
        ua = _in_int(d.get("usable_floor_area")) or _in_int(d.get("private_floor_area"))
        if ua and area_ok(ua) and rec["tipo"] != "terreno":
            rec["area"] = ua

    for ref, rec in recs:
        found[rec["id"]] = rec
    log(f"  inglaterra: {len(recs)} no filtro ({len(items)} de {total} lidos, {n} detalhes)")
    return "ok"


# ---------- Chaves na Mão (chavesnamao.com.br) ----------
# Portal grande (Next.js, app router). A página de busca traz os anúncios no fluxo RSC (rsc(): objetos
# "dataBySegment": {...}, 15 por página) e um JSON-LD (RealEstateListing) com foto, endereço e coordenadas
# de cada um. Filtros pela URL: /<tipo>-a-venda/pr-londrina/[<bairro>/]?filtro=amin:100,amax:200,pmin:N,or:6&pg=N
# (or:6 = mais recentes). Nos cards as áreas vêm como "2.783" (ponto = milhar); o primeiro anúncio da
# página vem completo (com createdAt) e aí a área tem decimal ("40.470" = 40,47 m²).
# Data de publicação (createdAt, horário de Brasília sem fuso) só na página do anúncio: é aberta para os
# anúncios desconhecidos no filtro, até CM_DETALHES_MAX por varredura (traz também a descrição).
# Coordenadas: as do JSON-LD só quando o endereço é público e tem número (sem número é o centro do bairro).
CM_BASE = "https://www.chavesnamao.com.br"
CM_DETALHES_MAX = 40
CM_LDJSON = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def cm_buscas():
    """[(caminho com '?filtro=...', tipo, exige prova de condomínio)] da busca do perfil."""
    if apto():
        f = [f"amin:{CFG['area_min']}", f"amax:{CFG['area_max']}"]
        if CFG.get("preco_min"):
            f.append(f"pmin:{CFG['preco_min']}")
        # Perfil de um bairro só (Centro): busca no bairro; senão a cidade toda (os nomes de bairro variam:
        # "Gleba Palhano", "Gleba Fazenda Palhano", "Palhano 1"...) e o bairro é filtrado aqui.
        b = f"{re.sub(r'[^a-z0-9]+', '-', CFG['bairros'][0])}/" if len(CFG["bairros"]) == 1 else ""
        return [(f"/apartamentos-a-venda/pr-londrina/{b}?filtro={','.join(f)},or:6", None, False)]
    lotes = "/terrenos-em-condominio-a-venda/pr-londrina/?filtro=or:6"
    if terrenos():  # lotes de condomínio + todos os lotes (com prova de condomínio)
        return [(lotes, "terreno", False), ("/terrenos-a-venda/pr-londrina/?filtro=or:6", "terreno", True)]
    return [("/casas-em-condominio-a-venda/pr-londrina/?filtro=or:6", "casa", False)]


def cm_area(v, decimal):
    v = str(v or "").strip()
    if not re.fullmatch(r"[\d.,]+", v):
        return None
    try:
        a = float(v.replace(",", ".")) if decimal else float(v.replace(".", "").replace(",", "."))
    except ValueError:
        return None
    return round(a) or None


def cm_ld(page):
    """{url do anúncio: itemOffered do JSON-LD} da página de busca."""
    out = {}
    for b in CM_LDJSON.findall(page):
        try:
            d = json.loads(b)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "RealEstateListing":
            for e in (d.get("offers") or {}).get("itemListElement") or []:
                if isinstance(e, dict) and e.get("url"):
                    out[e["url"]] = e.get("itemOffered") or {}
    return out


def cm_itens(s):
    dec = json.JSONDecoder()
    out, vistos = [], set()
    for m in re.finditer(r'"dataBySegment":(?=\{"id":\d+)', s):
        try:
            o = dec.raw_decode(s, m.end())[0]
        except ValueError:
            continue
        if o.get("id") not in vistos:
            vistos.add(o.get("id"))
            out.append(o)
    return out


def cm_publicado(v):
    """createdAt "2026-10-20T19:35:39.000" (Brasília, UTC-3) -> ISO UTC."""
    try:
        dt = datetime.fromisoformat(str(v)[:19]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return datetime.fromtimestamp(dt.timestamp() + 3 * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cm_record(o, ld, tipo):
    loc = o.get("location") or {}
    area = o.get("area") or {}
    dec = "createdAt" in o  # anúncio completo: área com decimal
    if tipo == "terreno":
        a = cm_area(area.get("total"), dec) or cm_area(area.get("useful"), dec)
    else:
        a = cm_area(area.get("useful"), dec) or cm_area(area.get("total"), dec)
    if not area_ok(a):
        return None
    rua = ((loc.get("street") or {}).get("name") or "").strip()
    num = ((loc.get("street") or {}).get("addressNumber") or "").strip()
    if not loc.get("publicAddress") or "dispon" in norm(rua):
        rua = num = ""
    num = num if re.fullmatch(r"\d+", num) and num != "0" else ""
    url = CM_BASE + o["url"]
    item = ld.get(url) or {}
    geo = item.get("geo") or {}
    pr = o.get("prices") or {}
    img = item.get("image")
    rec = {
        "id": f"cm{o['id']}",
        "tipo": tipo,
        "predio": building_from_text(o.get("title")),
        "endereco": (f"{rua}, {num}" if num else rua) or None,
        "bairro": (loc.get("neighborhood") or {}).get("name"),
        "foto": (img[0] if isinstance(img, list) and img else img) or None,
        "area": a,
        "preco": pr.get("rawPrice") if isinstance(pr.get("rawPrice"), int) and pr["rawPrice"] > 0 else None,
        "quartos": (o.get("bedrooms") or {}).get("count") if isinstance(o.get("bedrooms"), dict) else None,
        "banheiros": (o.get("bathrooms") or {}).get("count") if isinstance(o.get("bathrooms"), dict) else None,
        "vagas": (o.get("garages") or {}).get("count") if isinstance(o.get("garages"), dict) else None,
        "titulo": o.get("title"),
        "publicado": cm_publicado(o["createdAt"]) if o.get("createdAt") else None,
        "links": {"chavesnamao": url},
    }
    rec.update(coords(geo.get("latitude"), geo.get("longitude"), aproximada=not num))
    return rec


def cm_detalhe(rec):
    """Página do anúncio: data de publicação e descrição (nome do prédio)."""
    page = fetch(rec["links"]["chavesnamao"], tries=2)
    if not page:
        return
    s = rsc(page)
    m = re.search(r'"data":(?=\{"id":' + rec["id"][2:] + r",)", s)
    try:
        d = json.JSONDecoder().raw_decode(s, m.end())[0] if m else {}
    except ValueError:
        d = {}
    if d.get("createdAt"):
        rec["publicado"] = cm_publicado(d["createdAt"])
    corrige_area(rec, d.get("descriptionRaw"))
    if not rec.get("predio"):
        rec["predio"] = building_from_text(rec.get("titulo"), d.get("descriptionRaw"))
    alvo_detalhe(rec, d.get("descriptionRaw"))


def scan_chavesnamao(known, max_pages, found):
    pages_ok, detalhes = 0, 0
    for path, tipo, prova in cm_buscas():
        sem_novos = 0
        for p in range(1, max_pages + 1):
            page = fetch(CM_BASE + path + (f"&pg={p}" if p > 1 else ""))
            if page is None:
                break
            itens = cm_itens(rsc(page))
            ld = cm_ld(page)
            pages_ok += 1
            fresh = 0
            for o in itens:
                if o.get("transaction") != "SELL" or not o.get("url") or not o.get("id"):
                    continue
                loc = o.get("location") or {}
                if norm((loc.get("city") or {}).get("name")) != "londrina":
                    continue
                bairro = (loc.get("neighborhood") or {}).get("name")
                if prova and not condominio_ok(o.get("title"), bairro):
                    continue
                if not local_ok(bairro, o.get("title"), pendente=not prova):  # nome na descrição: cm_detalhe
                    continue
                rec = cm_record(o, ld, tipo)
                if not rec:
                    continue
                if rec.get("lat") and not coord_ok({"coordinates": {"latitude": rec["lat"], "longitude": rec["lon"]},
                                                     "neighborhood": bairro}):
                    continue
                if rec["id"] in found:
                    continue
                if rec["id"] not in known:
                    fresh += 1
                    if detalhes < CM_DETALHES_MAX:
                        detalhes += 1
                        time.sleep(1)
                        cm_detalhe(rec)
                found[rec["id"]] = rec
            log(f"  chavesnamao {path.split('/')[1]}{' (ampla)' if prova else ''} p{p}: "
                f"{len(itens)} anúncios, {fresh} desconhecidos")
            sem_novos = 0 if fresh else sem_novos + 1
            if len(itens) < 15 or parar(known, sem_novos):
                break
            time.sleep(1)
    return {"chavesnamao": "ok" if pages_ok else "falhou"}


# ---------- Imobiliárias Aurora Imobi, Casttelo e Monaco (Londrina) — sites Kenlo ----------
# Mesmo motor do site da Inglaterra (ver acima): API JSON interna
#   /api/listings/a-venda/<apartamento|casa|terreno>/londrina[/<bairro>]?area=MIN~MAX&size=48&pagina=N
# devolve {"data": [...], "count": N}; tags=GATED_COMMUNITY = só condomínio fechado. Não há filtro de preço
# nem data de cadastro (só updated_at). /api/listing/<ref> traz o prédio ("building"), o endereço com número
# e latitude/longitude: aberto para os anúncios no filtro, até KENLO_DETALHES_MAX por site e varredura
# (coordenadas só quando o endereço tem número; sem ele a posição é aproximada).
KENLO_SITES = {  # chave do portal: (site, prefixo do id)
    "aurora": ("https://www.auroraimobi.com.br", "au"),
    "casttelo": ("https://www.imobiliariacasttelo.com.br", "cs"),
    "monaco": ("https://www.imobiliariamonaco.com.br", "mn"),
}
KENLO_DETALHES_MAX = 40


def _kenlo_json(url):
    for i in range(2):
        try:
            r = S.get(url, timeout=45, headers={"Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
            log(f"  HTTP {r.status_code} {url}")
            if r.status_code == 404:
                return None
        except Exception as e:  # noqa: BLE001
            log(f"  erro {e} {url}")
        time.sleep(3 * (i + 1))
    return None


def _kenlo_lista(url):
    items, total, page = [], 0, 1
    while page <= KENLO_PAGINAS:
        j = _kenlo_json(url + f"&pagina={page}")
        if not isinstance(j, dict) or not isinstance(j.get("data"), list):
            return None if page == 1 else (items, total)
        total = j.get("count") or 0
        items += j["data"]
        if not j["data"] or len(items) >= total:
            break
        page += 1
        time.sleep(1)
    return items, total


def _kenlo_buscas(base):
    api = f"{base}/api/listings/a-venda"
    if apto():  # perfil de um bairro só (Centro): busca no bairro; senão a cidade toda
        b = f"/{re.sub(r'[^a-z0-9]+', '-', CFG['bairros'][0])}" if len(CFG["bairros"]) == 1 else ""
        return [(f"{api}/apartamento/londrina{b}?area={CFG['area_min']}~{CFG['area_max']}&size=48", None)]
    if terrenos():  # todos os terrenos; os sem marca de condomínio precisam de prova
        return [(f"{api}/terreno/londrina?size=48", "terreno")]
    return [(f"{api}/casa/londrina?size=48&tags=GATED_COMMUNITY", "casa")]


def _kenlo_detalhe(base, ref, rec):
    d = _kenlo_json(f"{base}/api/listing/{ref}")
    if not isinstance(d, dict):
        return
    if not rec["predio"]:
        rec["predio"] = (d.get("building") or "").split("|")[0].strip() or building_from_text(
            d.get("title"), d.get("listing_description"))
    rua = (d.get("full_address") or "").split(" - ")[0].strip()
    if re.search(r",\s*\d+", rua):
        rec["endereco"] = rua
        rec.update(coords(d.get("latitude"), d.get("longitude")))
    ua = _in_int(d.get("usable_floor_area")) or _in_int(d.get("private_floor_area"))
    if ua and area_ok(ua) and rec["tipo"] is None:
        rec["area"] = ua


def _scan_kenlo(chave, found):
    base, pref = KENLO_SITES[chave]
    items, total, ok = [], 0, False
    for url, tipo in _kenlo_buscas(base):
        got = _kenlo_lista(url)
        time.sleep(1)
        if got is None:
            log(f"  {chave}: falha na busca {url}")
            continue
        ok = True
        for x in got[0]:
            x["_tipo"] = tipo
        items += got[0]
        total += got[1]
    if not ok:
        return "falhou"
    recs = {}
    for x in items:
        if x.get("type") != "imob_property" or "FOR_SALE" not in str(x.get("property_purposes")):
            continue
        bairro = x.get("neighborhood_display") or x.get("neighborhood") or ""
        if norm(x.get("city")) != "londrina" or not local_ok(bairro, x.get("condo_name"), x.get("picture_title"),
                                                              x.get("website_title")):
            continue
        # tags=GATED_COMMUNITY deixou de filtrar (2026-10-23: a API devolve todas as casas): a marca é
        # conferida em cada anúncio
        if not apto() and not (x.get("condo_name") or "GATED_COMMUNITY" in (x.get("amenities") or [])
                               or condominio_ok(x.get("picture_title"), x.get("website_title"))):
            continue
        area = _in_int(x.get("area"))
        ref = x.get("property_full_reference")
        if not area_ok(area) or not ref:
            continue
        ptitle = x.get("picture_title")
        condo = (x.get("condo_name") or "").split("|")[0].strip() or None
        recs[ref] = {
            "id": pref + ref,
            "tipo": x["_tipo"],
            "predio": condo or building_from_text(ptitle, x.get("listing_description")),
            "endereco": _in_street(x.get("full_address")),
            "bairro": bairro,
            "foto": x.get("picture_full") or x.get("picture_thumb"),
            "area": area,
            "preco": _in_int(x.get("sale_price")),
            "quartos": _in_int(x.get("bedrooms")),
            "banheiros": _in_int(x.get("bathrooms")),
            "vagas": _in_int(x.get("garages")),
            "titulo": ptitle if _in_title_ok(ptitle) else (x.get("website_title") or x.get("heading1")),
            "publicado": None,  # o site só expõe updated_at (data de atualização)
            "links": {chave: base + x["url"] if x.get("url") else base},
        }
        corrige_area(recs[ref], x.get("listing_description"))
    # detalhe: primeiro os que estão sem nome de prédio
    for ref, rec in recs.items():  # já guardado: o detalhe foi aberto antes
        k = conhecido(rec["id"], rec["links"], preco=rec["preco"], foto=rec["foto"])
        if k:
            recs[ref] = k
    fila = sorted((r for r in recs if recs[r]["id"] not in CONHECIDOS and preco_ok(recs[r]["preco"])),
                  key=lambda r: bool(recs[r]["predio"]))[:KENLO_DETALHES_MAX]
    for ref in fila:
        time.sleep(1)
        _kenlo_detalhe(base, ref, recs[ref])
    for rec in recs.values():
        found[rec["id"]] = rec
    log(f"  {chave}: {len(recs)} no filtro ({len(items)} de {total} lidos, {len(fila)} detalhes)")
    return "ok"


def scan_aurora(found):
    """Adiciona em found[id] os anúncios no filtro. Retorna "ok" ou "falhou"."""
    return _scan_kenlo("aurora", found)


def scan_casttelo(found):
    """Adiciona em found[id] os anúncios no filtro. Retorna "ok" ou "falhou"."""
    return _scan_kenlo("casttelo", found)


def scan_monaco(found):
    """Adiciona em found[id] os anúncios no filtro. Retorna "ok" ou "falhou"."""
    return _scan_kenlo("monaco", found)


# ---------- Cube Imóveis (cubeimoveis.com.br) — CRM Vista, site Next.js (Loft/gtmcapital) ----------
# A busca /busca?finalidade=Venda&tipo=<Categoria>&cidade=Londrina[&bairro=X][&areaPrivativaMin=&areaPrivativaMax=]&page=N
# é renderizada no servidor: o fluxo RSC (rsc()) traz "properties": [...] com os campos do Vista (Codigo,
# Categoria, Bairro, AreaPrivativa/AreaTotal, ValorVenda, Dormitorios, Vagas, Empreendimento, Endereco sem
# número, Latitude/Longitude, DataCadastro só com o dia) e "total"/"quantity" (12 por página).
# Coordenadas às vezes vêm zeradas ou fora da cidade: coords() só fica com as que caem em Londrina.
CUBE_BASE = "https://cubeimoveis.com.br"
CUBE_TIPOS = {"Apartamento": None, "Casa em Condomínio": "casa", "Terreno": "terreno"}


def _cube_buscas():
    """[(parâmetros da busca, tipo, exige prova de condomínio)]."""
    q = "finalidade=Venda&cidade=Londrina&tipo="
    if apto():
        b = f"&bairro={urllib.parse.quote(CFG['bairros'][0].title())}" if len(CFG["bairros"]) == 1 else ""
        return [(f"{q}Apartamento{b}&areaPrivativaMin={CFG['area_min']}&areaPrivativaMax={CFG['area_max']}", None, False)]
    t = urllib.parse.quote("Casa em Condomínio")
    # não há "Terreno em Condomínio": lotes de condomínio vêm em "Terreno" (com prova de condomínio)
    return [(q + "Terreno", "terreno", True)] if terrenos() else [(q + t, "casa", False)]


def _cube_link(o):
    a = o.get("AreaPrivativa") or 0
    a = int(a) if float(a).is_integer() else a
    parts = [_pz_slug(o.get("Categoria")), _pz_slug(o.get("Bairro")), _pz_slug(o.get("Cidade")), _pz_slug(o.get("UF")),
             f"{o['Dormitorios']}-quartos" if (o.get("Dormitorios") or 0) > 0 else "", f"{a}m2" if a else ""]
    return f"{CUBE_BASE}/imovel/{'-'.join(p for p in parts if p)}-{o['Codigo']}"


def scan_cube(found):
    """Adiciona em found[id] os anúncios no filtro. Retorna "ok" ou "falhou"."""
    ok, n, lidos = False, 0, 0
    dec = json.JSONDecoder()
    for q, tipo, prova in _cube_buscas():
        page, total = 1, 0
        while page <= 20:
            html = fetch(f"{CUBE_BASE}/busca?{q}&page={page}")
            if not html:
                break
            s = rsc(html)
            i = s.find('"properties":[')
            if i < 0:
                log("  cube: lista de imóveis não encontrada")
                break
            try:
                props = dec.raw_decode(s, i + len('"properties":'))[0]
            except ValueError:
                break
            ok = True
            m = re.search(r'"total":(\d+),"page":\d+,"quantity":(\d+)', s)
            total, qtd = (int(m.group(1)), int(m.group(2))) if m else (0, 12)
            lidos += len(props)
            for o in props:
                if not o.get("Codigo") or not (o.get("FinalidadeStatus") or {}).get("VENDA"):
                    continue
                if norm(o.get("Cidade")) != "londrina" or CUBE_TIPOS.get(o.get("Categoria"), 0) != tipo:
                    continue
                titulo = (o.get("TituloSite") or "").strip() or None
                bairro = o.get("Bairro") or ""
                emp = (o.get("Empreendimento") or "").strip()
                desc = o.get("DescricaoWebResumo") or ""
                if prova and not condominio_ok(titulo, emp, desc):
                    continue
                if not local_ok(bairro, emp, titulo):
                    continue
                if tipo == "terreno":
                    area = round(o.get("AreaTotal") or o.get("AreaPrivativa") or 0) or None
                else:
                    area = round(o.get("AreaPrivativa") or o.get("AreaTotal") or 0) or None
                if not area_ok(area):
                    continue
                rua = " ".join(x for x in (o.get("TipoEndereco"), o.get("Endereco")) if x).strip()
                predio = re.sub(r"(?i)^(?:ed\.|edif\.|edif[ií]cio|residencial|condom[ií]nio)\s+", "", emp.split("|")[0]).strip()
                if re.match(r"(?i)(?:terreno|lote|casa)\b", predio):  # "Terreno - Rua X"
                    predio = ""
                dc = str(o.get("DataCadastro") or "")
                rec = {
                    "id": "cb" + o["Codigo"],
                    "tipo": tipo,
                    "predio": predio or building_from_text(titulo, desc),
                    "endereco": rua if o.get("Endereco") else None,
                    "bairro": bairro,
                    "foto": o.get("FotoDestaque") or o.get("FotoDestaquePequena") or None,
                    "area": area,
                    "preco": int(o["ValorVenda"]) if o.get("ValorVenda") else None,
                    "quartos": o.get("Dormitorios") or None,
                    # TotalBanheiros = BanheiroSocialQtd + Suites, mas o "social" já costuma contar os das suítes
                    "banheiros": o.get("BanheiroSocialQtd") or o.get("TotalBanheiros") or None,
                    "vagas": o.get("Vagas") if o.get("Vagas") or tipo != "terreno" else None,
                    "titulo": titulo,
                    "publicado": dc[:10] + "T00:00:00Z" if re.match(r"\d{4}-\d\d-\d\d", dc) else None,
                    "links": {"cube": _cube_link(o)},
                }
                rec.update(coords(o.get("Latitude"), o.get("Longitude")))
                found[rec["id"]] = rec
                n += 1
            if not props or page * qtd >= total:
                break
            page += 1
            time.sleep(1)
        time.sleep(1)
    log(f"  cube: {n} no filtro ({lidos} lidos)")
    return "ok" if ok else "falhou"


# ---------- desduplicação ----------
# O mesmo apartamento costuma aparecer em vários portais (e às vezes 2× no mesmo portal, por
# imobiliárias diferentes). Cada anúncio fica guardado separado em "listings"; a cada varredura
# eles são agrupados de novo em "imoveis", que é o que a página mostra.
def origem(rec):
    return rec.get("origem") or {"g": "grupo", "o": "olx"}.get(rec["id"][0])


NAME_PREFIX = re.compile(r"^(?:ed|edif|edificio|residencial|res|condominio|cond|torre|l|le|la)\b\.?\s*")
STREET_PREFIX = re.compile(r"^(?:rua|r|avenida|av|alameda|al|travessa|tv)\b\.?\s*")


def bairro_key(b):
    n = norm(b)
    return next((k for k in CFG["bairros"] if k in n), n)


def name_key(p):
    n = re.sub(r"[^a-z0-9 ]", " ", norm(p))
    n = re.sub(r"\s+", " ", n).strip()
    while True:
        m = NAME_PREFIX.sub("", n)
        if m == n:
            return n.replace(" ", "")
        n = m


def same_name(a, b):
    x, y = name_key(a), name_key(b)
    return bool(x and y) and (x == y or (min(len(x), len(y)) >= 5 and (x in y or y in x)))


def street(e):
    """("joao wyclif", "420") a partir de "Rua João Wyclif, 420"."""
    if not e:
        return None, None
    parts = [p.strip() for p in e.split(",")]
    nome = STREET_PREFIX.sub("", norm(parts[0]))
    num = re.sub(r"\D", "", parts[1]) if len(parts) > 1 else ""
    return nome or None, num or None


def mesma_rua(a, b):
    # Tolera grafias diferentes entre portais ("Zuimglio" × "Zuínglio")
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def texto(x):
    """Título e URLs do anúncio, só letras e números (as URLs costumam trazer o nome do prédio)."""
    t = " ".join([x.get("titulo") or ""] + list((x.get("links") or {}).values()))
    return re.sub(r"[^a-z0-9]", "", norm(t))


def conflito(a, b):
    """a e b certamente NÃO são o mesmo apartamento. Os prédios da Gleba têm muitas unidades
    iguais (mesma planta), então as tolerâncias são apertadas: o que separa unidades é o preço."""
    if a.get("tipo") != b.get("tipo"):
        return True
    if terrenos():
        # Lotes: o bairro varia entre portais (a OLX erra muito) e o Zap inventa número de rua;
        # o que separa é o condomínio (ver chave_cond), a área e o preço.
        ca, cb = chave_cond([a]), chave_cond([b])
        return bool(ca and cb and ca != cb) or abs(a["area"] - b["area"]) > 3 or preco_conflita(a, b)
    if bairro_key(a.get("bairro")) != bairro_key(b.get("bairro")):
        return True
    for f in ("quartos", "vagas"):
        if a.get(f) and b.get(f) and a[f] != b[f]:
            return True
    if abs(a["area"] - b["area"]) > 3:
        return True
    if a.get("predio") and b.get("predio") and not same_name(a["predio"], b["predio"]):
        return True
    (ra, na), (rb, nb) = street(a.get("endereco")), street(b.get("endereco"))
    if ra and rb and (not mesma_rua(ra, rb) or (na and nb and na != nb)):
        return True
    return preco_conflita(a, b)


def preco_conflita(a, b):
    pa, pb = a.get("preco"), b.get("preco")
    if pa and pb:
        dif = abs(pa - pb) / max(pa, pb)
        # Mesma imobiliária/portal com preços diferentes = unidades diferentes
        return dif > 0.03 or (origem(a) == origem(b) and dif > 0.005)
    return False


def mesmo_predio(a, b):
    if terrenos():
        return bool(chave_cond([a])) and chave_cond([a]) == chave_cond([b])
    if a.get("predio") and b.get("predio"):
        return True  # sem conflito, então são o mesmo nome
    ra, rb = street(a.get("endereco"))[0], street(b.get("endereco"))[0]
    if ra and rb:
        return True
    for x, y in ((a, b), (b, a)):
        k = name_key(x.get("predio"))
        if len(k) >= 5 and k in texto(y):
            return True
    return False


def pair_ok(a, b):
    """a e b são o mesmo apartamento? Mesmo prédio: tolera pequenas diferenças de preço/área entre
    anúncios (sem preço, só com área quase igual). Sem saber o prédio: área e preço quase iguais."""
    if conflito(a, b):
        return False
    pa, pb = a.get("preco"), b.get("preco")
    if mesmo_predio(a, b):
        return bool(pa and pb) or abs(a["area"] - b["area"]) <= 1
    return bool(pa and pb) and abs(pa - pb) / max(pa, pb) <= 0.01 and abs(a["area"] - b["area"]) <= 2


def dif_preco(a, b):
    pa, pb = a.get("preco"), b.get("preco")
    return abs(pa - pb) / max(pa, pb) if pa and pb else 1


def tipo_ok(x):
    """Casas e terrenos são abas excludentes: casa não entra em Terrenos, terreno não entra em Casas."""
    return apto() or x.get("tipo") == ("terreno" if terrenos() else "casa")


def group_imoveis(L):
    ads = [x for x in L.values() if x.get("links") and x.get("area") and tipo_ok(x)]
    ads.sort(key=lambda x: (not x.get("visivel"), x.get("detectado") or "", x["id"]))
    grupos = []
    for a in ads:
        # Entra no grupo se casar com algum anúncio dele e não conflitar com nenhum; havendo mais de
        # um grupo possível (unidades parecidas no mesmo prédio), fica no de preço mais próximo.
        aptos = [g for g in grupos if any(pair_ok(a, b) for b in g) and not any(conflito(a, b) for b in g)]
        g = min(aptos, key=lambda g: min(dif_preco(a, b) for b in g)) if aptos else None
        if g is not None:
            g.append(a)
        else:
            grupos.append([a])
    # A ordem de chegada pode deixar separados grupos que se casam (A~C só se nota depois de B entrar)
    juntou = True
    while juntou:
        juntou = False
        for i in range(len(grupos)):
            for j in range(i + 1, len(grupos)):
                gi, gj = grupos[i], grupos[j]
                if any(pair_ok(a, b) for a in gi for b in gj) and not any(conflito(a, b) for a in gi for b in gj):
                    gi.extend(gj)
                    del grupos[j]
                    juntou = True
                    break
            if juntou:
                break

    medias = medias_m2(grupos) if CFG.get("media") else {}
    out = []
    for g in grupos:
        vis = [x for x in g if x.get("visivel")]
        if not vis:
            continue
        # Só condomínio-alvo (o nome às vezes só aparece depois de abrir o anúncio: "condominio")
        cond = chave_cond(g) if CFG.get("por_condominio") else None
        if CFG.get("por_condominio") and (not alvo(cond) or any(excluido(x.get("bairro"), x.get("predio"), x.get("titulo"))
                                                                for x in g)):
            continue
        # Principal: o anúncio visível mais completo
        main_ = max(vis, key=lambda x: sum(bool(x.get(k)) for k in ("predio", "endereco", "foto", "quartos", "vagas", "publicado")))
        im = {k: main_.get(k) for k in ("id", "tipo", "predio", "endereco", "bairro", "foto", "area", "preco",
                                        "preco_anterior", "quartos", "banheiros", "vagas", "titulo")}
        # Nome do prédio: o mais citado entre os anúncios (há imobiliária que cadastra errado)
        nomes = [x["predio"] for x in g if x.get("predio")]
        if nomes:
            chaves = [name_key(n) for n in nomes]
            im["predio"] = nomes[max(range(len(nomes)), key=lambda i: (chaves.count(chaves[i]), -i))]
        geo = next((x for x in g if x.get("lat")), None)
        if geo:
            im.update(lat=geo["lat"], lon=geo["lon"], geo="exato")
        for k in ("endereco", "foto", "quartos", "banheiros", "vagas"):
            if not im[k]:
                im[k] = next((x[k] for x in g if x.get(k)), None)
        com_num = [x["endereco"] for x in g if street(x.get("endereco"))[1]]
        if com_num and not street(im["endereco"])[1]:
            im["endereco"] = com_num[0]
        datas = [x["publicado"] for x in g if x.get("publicado")]
        im["publicado"] = min(datas) if datas else None
        im["detectado"] = min(x["detectado"] for x in vis)
        fontes, vistos = [], set()
        for x in vis + [x for x in g if not x.get("visivel")]:
            for portal, url in x["links"].items():
                if url and url not in vistos:
                    vistos.add(url)
                    fontes.append({"portal": portal, "url": url, "preco": x.get("preco"), "area": x.get("area"),
                                   "area_anunciada": x.get("area_anunciada")})
        im["fontes"] = fontes
        if cond and not im["predio"]:  # condomínio citado só no título/bairro/descrição
            im["predio"] = cond
        if medias:
            im.update(abaixo_da_media(g, medias))
        local = chave_cond(g) if terrenos() else (chave_predio(g) or [None])[0]
        if local and apto():  # mesmo nome de prédio em bairros diferentes
            local = f"{bairro_key(im.get('bairro'))}|{local}"
        im["_local"] = local
        out.append(im)
    localizar(out)
    # endereço localizado com número fora da caixa do perfil: não é do setor
    return [im for im in out if im.get("geo") != "exato" or na_caixa(im["lat"], im["lon"])]


# ---------- preço abaixo da média (perfil com "media") ----------
# Referência: mediana do R$/m² dos imóveis (grupos, já sem anúncios repetidos) do mesmo condomínio
# vistos nos últimos dias; com poucos no condomínio, a do bairro. Inclui o estoque de referência
# (anúncios que não viraram card), para a mediana ter mais base.
def chave_cond(g):
    """Condomínio do grupo (alvo ou outro): o mais citado nos anúncios (ver cond_rec)."""
    achados = [c for c in map(cond_rec, g) if c]
    return max(set(achados), key=achados.count) if achados else None


def m2_grupo(g):
    """R$/m² do grupo (anúncio com preço e área mais recente); None se faltar dado ou a área for
    pequena demais para ser lote (erro de cadastro comum: "10 m²")."""
    ok = [x for x in g if x.get("preco") and x.get("area") and x["area"] >= CFG["media"]["area_min"]]
    if not ok:
        return None
    x = max(ok, key=lambda x: x.get("visto") or "")
    return x["preco"] / x["area"]


def chave_predio(g):
    """Prédio do grupo (apartamentos): o nome mais citado; sem nome, rua + número."""
    nomes = [x["predio"] for x in g if x.get("predio") and name_key(x["predio"])]
    if nomes:
        chaves = [name_key(n) for n in nomes]
        i = max(range(len(nomes)), key=lambda i: (chaves.count(chaves[i]), -i))
        return chaves[i], nomes[i].title()
    for x in g:
        rua, num = street(x.get("endereco"))
        if rua and num:
            return f"{rua}|{num}", x["endereco"].split(",")[0].strip().title() + ", " + num
    return None


def base_media(g):
    """[(tipo, chave, rótulo)] em ordem de preferência: condomínio/prédio, depois bairro."""
    b = bairro_key(next((x.get("bairro") for x in g if x.get("bairro")), ""))
    if apto():
        p = chave_predio(g)
        primeiro = [("predio", p[0], p[1])] if p else []
    else:
        c = chave_cond(g)
        primeiro = [("cond", c, c)] if c else []
    bases = primeiro + [("bairro", b, f"bairro {b.title()}")] * bool(b)
    # "bases" restringe a referência (Centro: só o prédio; o bairro mistura prédios novos e antigos)
    return [x for x in bases if x[0] in CFG["media"].get("bases", ("predio", "cond", "bairro"))]


def medias_m2(grupos):
    """{(base, nome): [(área, R$/m²), ...]} dos grupos vistos nos últimos dias."""
    lim = CFG["media"]
    recentes = [g for g in grupos if (age_days(max(x.get("visto") or x.get("detectado") or "" for x in g) or None) or 0)
                <= lim["dias"]]
    vals = {}
    for g in recentes:
        v = m2_grupo(g)
        if v:
            for k in base_media(g):
                vals.setdefault(k[:2], []).append((area_grupo(g), v))
    return vals


def area_grupo(g):
    ok = [x for x in g if x.get("preco") and x.get("area") and x["area"] >= CFG["media"]["area_min"]]
    return max(ok, key=lambda x: x.get("visto") or "")["area"]


def mediana(v):
    v, n = sorted(v), len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def abaixo_da_media(g, medias):
    """Compara com imóveis de tamanho parecido (por padrão, metade ao dobro da área): lote grande
    tem R$/m² menor. O próprio grupo fica fora da sua referência."""
    v = m2_grupo(g)
    if not v:
        return {}
    a = area_grupo(g)
    f = CFG["media"].get("faixa_area", 2)
    for tipo, chave, nome in base_media(g):
        ref = [m for ar, m in medias.get((tipo, chave), []) if a / f <= ar <= a * f]
        if v in ref:
            ref.remove(v)
        minimo = CFG["media"].get("min_amostra_" + tipo, CFG["media"]["min_amostra"])
        if len(ref) < minimo:
            continue
        ref = {"m2": mediana(ref), "n": len(ref)}
        pct = round((v / ref["m2"] - 1) * 100)
        media = {"ref_m2": round(ref["m2"]), "base": nome, "n": ref["n"], "pct": pct}
        if pct <= -CFG["media"]["pct_alerta"]:
            # muito abaixo costuma ser erro de cadastro (área ou preço), não pechincha
            media["suspeito"] = pct <= -CFG["media"]["pct_suspeito"]
            return {"media": media, "abaixo": True}
        return {"media": media}
    return {}


# ---------- localização no mapa ----------
# Coordenada exata (Zap/Viva Real sem "localização aproximada", Raul Fulgêncio, Perez) vale para o
# prédio todo: os outros imóveis do mesmo prédio/condomínio herdam a mediana dela. Sem isso, busca no
# OpenStreetMap (Nominatim, gratuito, no máximo 1 consulta/s) a rua com número, depois o nome do prédio
# e, por fim, o bairro. "geo" diz a precisão: exato, predio, rua ou bairro. As respostas (inclusive as
# vazias) ficam em docs/data/geocache.json, então cada endereço só é consultado uma vez.
GEOCACHE = DATA_DIR / "geocache.json"
GEO_MAX_CONSULTAS = 80  # por perfil e varredura; o resto fica para as próximas
NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Resultado do Nominatim aceito como "o prédio/condomínio" (evita casar nome com loja ou rua)
GEO_CLASSES = {"building", "landuse", "place", "leisure", "amenity"}


class Geo:
    def __init__(self):
        try:
            self.cache = json.loads(GEOCACHE.read_text())
        except (OSError, ValueError):
            self.cache = {}
        self.consultas = 0

    def salvar(self):
        GEOCACHE.write_text(json.dumps(self.cache, ensure_ascii=False, indent=0, sort_keys=True))

    def buscar(self, chave, params, aceita=lambda r: True):
        if chave in self.cache:
            return self.cache[chave]
        if self.consultas >= GEO_MAX_CONSULTAS:
            return None
        self.consultas += 1
        q = dict(params, format="jsonv2", limit=5, countrycodes="br", viewbox="-51.32,-23.20,-51.05,-23.45", bounded=1)
        url = NOMINATIM + "?" + urllib.parse.urlencode(q)
        res = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "radar-imoveis/1.0 (github.com/Pedriali/radar-imoveis)",
                                                       "Accept-Language": "pt-BR"})
            with urllib.request.urlopen(req, timeout=30) as r:
                for o in json.load(r):
                    lat, lon = float(o["lat"]), float(o["lon"])
                    if em_londrina(lat, lon) and aceita(o):
                        res = [round(lat, 6), round(lon, 6), o.get("category")]
                        break
        except Exception as e:  # noqa: BLE001 — sem rede/limite: tenta de novo na próxima varredura
            log(f"  geo: erro {e}")
            time.sleep(1.1)
            return None
        time.sleep(1.1)
        self.cache[chave] = res
        return res


# Último recurso para o bairro, quando nenhum imóvel dele tem coordenada e o OpenStreetMap não acha
BAIRROS_CENTRO = {"palhano": [-23.3325, -51.1835], "centro": [-23.3105, -51.1625]}


def _meio(pts):
    return [round(mediana([a for a, _ in pts]), 6), round(mediana([b for _, b in pts]), 6)]


def localizar(imoveis):
    geo = Geo()
    # 1) prédio/condomínio com coordenada exata em algum anúncio (desta varredura ou de antes)
    exatos = {}
    for im in imoveis:
        if im.get("lat") and im.get("_local"):
            exatos.setdefault(im["_local"], []).append((im["lat"], im["lon"]))
    for k, v in exatos.items():
        geo.cache["local:" + k] = _meio(v)
    pend = []
    for im in imoveis:
        k = im.get("_local")
        if im.get("lat"):
            continue
        achado = None
        if k and geo.cache.get("local:" + k):
            achado = geo.cache["local:" + k], "predio"
        rua, num = street(im.get("endereco"))
        if not achado and rua and num and not terrenos():  # o Zap inventa número em lote de condomínio
            nome = im["endereco"].split(",")[0].strip()
            r = geo.buscar(f"rua:{norm(nome)}|{num}", {"street": f"{num} {nome}", "city": "Londrina", "state": "Paraná"})
            # sem o número no OpenStreetMap, ele devolve a rua inteira: posição aproximada
            achado = r and (r[:2], "rua" if r[2] == "highway" else "exato")
        if not achado and im.get("predio"):
            p = re.sub(r"(?i)^(ed\.?|edif[ií]cio|residencial|condom[ií]nio)\s+", "", im["predio"])
            pref = "Condomínio" if CFG["tipo"] != "apartamento" else "Edifício"
            r = geo.buscar(f"predio:{norm(p)}", {"q": f"{pref} {p}, Londrina"},
                           lambda o: o.get("category") in GEO_CLASSES and o.get("type") not in ("city", "town", "administrative"))
            achado = r and (r[:2], "predio")
        if achado:
            (im["lat"], im["lon"]), im["geo"] = achado
        else:
            pend.append(im)
    # 2) outros imóveis do mesmo prédio já localizados (pela rua); 3) o bairro: mediana dos imóveis
    # localizados dele, senão o OpenStreetMap, senão BAIRROS_CENTRO
    por_local, por_bairro = {}, {}
    for im in imoveis:
        if im.get("lat"):
            if im.get("_local"):
                por_local.setdefault(im["_local"], []).append((im["lat"], im["lon"]))
            if im.get("geo") in ("exato", "rua", "predio"):
                por_bairro.setdefault(bairro_key(im.get("bairro")), []).append((im["lat"], im["lon"]))
    # Resposta do OpenStreetMap longe (mais de ~3 km) dos imóveis exatos do bairro é outro lugar com o mesmo nome
    for im in imoveis:
        b = por_bairro.get(bairro_key(im.get("bairro")), [])
        if im.get("geo") in ("rua", "predio") and len(b) >= 5:
            c = _meio(b)
            if abs(im["lat"] - c[0]) + abs(im["lon"] - c[1]) > 0.03:
                im.pop("lat"), im.pop("lon"), im.pop("geo")
                pend.append(im)
    sem = 0
    for im in pend:
        k, b = im.get("_local"), bairro_key(im.get("bairro"))
        if k in por_local:
            im["lat"], im["lon"] = _meio(por_local[k])
            im["geo"] = "predio"
            continue
        r = None
        if len(por_bairro.get(b, [])) >= 3:
            r = _meio(por_bairro[b])
        elif im.get("bairro"):
            r = (geo.buscar(f"bairro:{norm(im['bairro'])}", {"q": f"{im['bairro']}, Londrina, Paraná"}) or [None])[:2]
            r = r if r[0] else BAIRROS_CENTRO.get(b)
        if r:
            im["lat"], im["lon"] = r
            im["geo"] = "bairro"
        else:
            sem += 1
    for im in imoveis:
        im.pop("_local", None)
    geo.salvar()
    log(f"  mapa: {len(imoveis) - sem} de {len(imoveis)} imóveis localizados ({geo.consultas} consultas ao OpenStreetMap)")


# ---------- arquivos da página ----------
# O estado completo (todos os anúncios, histórico de varreduras) fica em docs/data/<dados>; a página só baixa
# docs/data/pagina/<dados> (cards e última varredura, sem campos vazios) e resumo.json (alvos por aba).
PAGINA_DIR = DATA_DIR / "pagina"


def enxuto(v):
    if isinstance(v, dict):
        return {k: enxuto(x) for k, x in v.items() if x not in (None, "", [], {})}
    if isinstance(v, list):
        return [enxuto(x) for x in v]
    return v


def pagina(state):
    PAGINA_DIR.mkdir(exist_ok=True)
    p = {"atualizado": state["atualizado"], "runs": state["runs"][-1:], "imoveis": enxuto(state["imoveis"])}
    (PAGINA_DIR / CFG["dados"]).write_text(json.dumps(p, ensure_ascii=False, separators=(",", ":")))
    f = PAGINA_DIR / "resumo.json"
    try:
        resumo = json.loads(f.read_text())
    except (OSError, ValueError):
        resumo = {}
    resumo[CFG["dados"]] = len(state["imoveis"])
    f.write_text(json.dumps(resumo, ensure_ascii=False, indent=0, sort_keys=True))


# ---------- principal ----------
SCANNERS = [
    # (chave, função, recebe (known, max_pages, found)?)
    ("grupo", scan_grupo, True),
    ("olx", scan_olx, True),
    ("raulfulgencio", scan_raulfulgencio, False),
    ("gleba", scan_gleba, False),
    ("santamerica", scan_santamerica, False),
    ("perez", scan_perez, False),
    ("catuai", scan_catuai, False),
    ("inglaterra", scan_inglaterra, False),
    ("chavesnamao", scan_chavesnamao, True),
    ("aurora", scan_aurora, False),
    ("casttelo", scan_casttelo, False),
    ("monaco", scan_monaco, False),
    ("cube", scan_cube, False),
]


def main(perfil):
    usar_perfil(perfil)
    FORA.clear()
    DATA = DATA_DIR / CFG["dados"]
    state = json.loads(DATA.read_text()) if DATA.exists() else {"runs": [], "listings": {}}
    L = state["listings"]
    initial = not L
    max_pages = CFG["max_pages_initial"] if initial else CFG["max_pages"]
    log(f"Varredura {perfil} {NOW_ISO} ({'inicial' if initial else 'incremental'})")
    conhecidos = {origem(x) for x in L.values()}
    CONHECIDOS.clear()
    CONHECIDOS.update(L)

    found = {}
    status = {}
    for chave, fn, paginado in SCANNERS:
        antes = set(found)
        try:
            st = fn(L, max_pages, found) if paginado else {chave: fn(found)}
        except Exception as e:  # noqa: BLE001 — um portal quebrado não derruba os outros
            log(f"  {chave}: erro {e!r}")
            st = {chave: "falhou"}
        status.update(st)
        for k in set(found) - antes:
            found[k]["origem"] = chave

    # Perfil com "preco_min": fica de fora o que tem preço abaixo dele (sem preço = "sob consulta", entra).
    # Localização exata fora da "caixa" do perfil também sai, venha de qualquer fonte (bairro cadastrado errado).
    for k in [k for k, x in found.items() if not preco_ok(x.get("preco")) or not tipo_ok(x)
              or (x.get("lat") and not na_caixa(x["lat"], x["lon"]))]:
        del found[k]
    for k in [k for k, x in L.items() if x.get("lat") and not na_caixa(x["lat"], x["lon"])]:
        del L[k]  # guardados antes da caixa existir
    for k in [k for k, x in L.items() if not tipo_ok(x)]:
        del L[k]  # terrenos guardados quando a aba de casas também tinha terrenos

    new_ids = [k for k in found if k not in L]
    log(f"{len(found)} anúncios no filtro, {len(new_ids)} novos")

    # Atualiza os já conhecidos (preço, links, última vez visto)
    for k, rec in found.items():
        if k in L:
            cur = L[k]
            cur["visto"] = NOW_ISO
            cur.setdefault("links", {}).update(rec["links"])
            for f in ("predio", "endereco", "bairro", "quartos", "banheiros", "vagas", "lat", "lon"):
                if cur.get(f) is None and rec.get(f) is not None:
                    cur[f] = rec[f]
            if rec.get("preco") and rec["preco"] != cur.get("preco"):
                cur.setdefault("preco_anterior", cur.get("preco"))
                cur["preco"] = rec["preco"]

    # Grupo Zap: IDs crescem com o tempo, então os maiores são os mais novos. Ao encontrar
    # vários anúncios seguidos mais velhos que o limite, os restantes não precisam ser abertos.
    grupo_new = sorted((k for k in new_ids if found[k]["origem"] == "grupo"), key=lambda k: -int(k[1:]))
    outros_new = [k for k in new_ids if found[k]["origem"] != "grupo"]
    # Perfil com "inicial_mostra_tudo": na estreia todo o estoque vira card (mercado, não só lançamentos).
    tudo = initial and CFG.get("inicial_mostra_tudo")
    det = Detalhes(3 if tudo else 1)
    old_streak = 0
    for k in grupo_new + outros_new:
        rec = found[k]
        estreante = rec["origem"] not in conhecidos  # primeira varredura deste portal
        limite = CFG["initial_recent_days"] if initial or estreante else CFG["max_age_days_new"]
        if rec["origem"] == "grupo":
            if tudo or old_streak < 6:
                det.abrir(rec)
                idade = age_days(rec.get("publicado"))
                old_streak = old_streak + 1 if idade is not None and idade > limite else 0
            recente = tudo or (old_streak < 6 and (age_days(rec.get("publicado")) or 0) <= limite)
        else:
            idade = age_days(rec.get("publicado"))
            # Sem data de publicação: na estreia do portal não dá para saber se é novo, então o
            # estoque atual entra só como referência (links extras em imóveis já mostrados).
            recente = tudo or (idade <= limite if idade is not None else not estreante)
            if recente and rec["origem"] == "olx":
                olx_detail(rec)
                time.sleep(1)
        corrige_area(rec)
        if not area_ok(rec.get("area")):  # a área útil do texto ficou fora da faixa do perfil
            recente = False
        rec["visivel"] = recente
        if not recente:
            rec.pop("foto", None)
            rec.pop("titulo", None)
        rec["detectado"] = NOW_ISO
        rec["visto"] = NOW_ISO
        L[k] = rec

    # Páginas de anúncio que ficaram para depois (bloqueio do Zap), só dos que estão na página
    pend = [x for x in L.values() if x.get("detalhe_pendente") and x.get("visivel") and x["id"] not in new_ids]
    for rec in pend[:DETALHES_POR_VARREDURA]:
        if not det.abrir(rec) and det.falhas >= 3:
            break

    # Por condomínio: anúncios guardados sem o nome de um condomínio-alvo no título/bairro, cuja página
    # ainda não foi lida procurando por ele (de antes da busca por condomínio ou que ficaram para depois)
    if CFG.get("por_condominio"):
        abrir = {"grupo": det.abrir, "olx": olx_detail, "chavesnamao": cm_detalhe}
        falta = [x for x in L.values() if x.get("visivel") and "condominio" not in x and origem(x) in abrir
                 and not cond_rec(x) and x.get("links")]
        # Zap/Viva Real bloqueiam depois de muitas páginas (Detalhes); OLX e Chaves na Mão aguentam mais
        cota = {"grupo": DETALHES_POR_VARREDURA, "olx": 120, "chavesnamao": 120}
        lidos = 0
        for rec in falta:
            o = origem(rec)
            if cota[o] <= 0 or (o == "grupo" and det.falhas >= 3):
                continue
            cota[o] -= 1
            lidos += 1
            abrir[o](rec)
            if o != "grupo":
                time.sleep(1)
        if falta:
            log(f"  condomínio procurado na página de {lidos} de {len(falta)} anúncios")

    state["imoveis"] = group_imoveis(L)
    novos = sum(1 for im in state["imoveis"] if im["detectado"] == NOW_ISO)
    run = {"em": NOW_ISO, "novos": novos, "portais": status, "inicial": initial}
    if FORA:  # bairros descartados pela zona, para revisar a lista em config.json
        run["fora_da_zona"] = dict(sorted(FORA.items(), key=lambda kv: -kv[1])[:40])
    state["runs"].append(run)
    state["runs"] = state["runs"][-60:]
    state["atualizado"] = NOW_ISO
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    pagina(state)
    log(f"Status: {status}. Imóveis novos: {novos}. Imóveis na página: {len(state['imoveis'])}. Anúncios armazenados: {len(L)}")
    return not all(v == "falhou" for v in status.values())


if __name__ == "__main__":
    perfis = sys.argv[1:] or list(CONFIG["perfis"])
    falhos = [p for p in perfis if not main(p)]
    if falhos:
        sys.exit(f"Todos os portais falharam: {', '.join(falhos)}")
