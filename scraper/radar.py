"""Varredura de imóveis à venda em Londrina e atualização de docs/data/<perfil>.json.

Cada perfil de config.json ("perfis") é uma busca independente que vira uma aba da página:
    python scraper/radar.py              # todos os perfis
    python scraper/radar.py casas        # só um
"""
import bisect
import difflib
import functools
import html as htmllib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from curl_cffi import requests

import predios

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "scraper" / "config.json").read_text())
CFG = {}  # perfil em uso: CONFIG["comum"] + CONFIG["perfis"][nome] (ver usar_perfil)
DATA_DIR = ROOT / "docs" / "data"
NOW = datetime.now(timezone.utc)
NOW_ISO = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

class _Sessao:
    """Uma sessão curl_cffi por thread: as fontes rodam em paralelo (ver main) e a sessão não é thread-safe.
    Uso igual ao de uma sessão: S.get(...), S.post(...)."""

    def __init__(self):
        self._local = threading.local()
        self._geracao = 0

    def __getattr__(self, nome):
        loc = self._local
        if getattr(loc, "geracao", None) != self._geracao:
            loc.sessao, loc.geracao = requests.Session(impersonate="chrome"), self._geracao
        return getattr(loc.sessao, nome)


S = _Sessao()


def nova_sessao():
    """Sessões novas (cookies zerados), para a segunda tentativa depois de um bloqueio."""
    S._geracao += 1


# Anúncios já guardados do perfil em uso (preenchido em main): as páginas de anúncio que só servem para
# completar dados (prédio, endereço, área, quartos) não são abertas de novo; o resto vem da busca.
CONHECIDOS = {}
CAMPOS_DETALHE = ("tipo", "predio", "endereco", "bairro", "foto", "area", "quartos", "banheiros", "vagas", "titulo",
                  "publicado", "lat", "lon", "condominio", "contexto", "det")


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
    bairro_key.cache_clear()
    CTX.clear()
    if CFG.get("contexto"):
        CTX.append(re.compile(r"\b(?:" + "|".join(re.escape(norm(c)) for c in CFG["contexto"]) + r")\b"))
    if CFG.get("por_condominio"):
        # "alvos": a lista de config.json com os condomínios/prédios do perfil (Nova Prochet: "predios_alvo")
        for a in CONFIG[CFG.get("alvos", "condominios_alvo")]:
            chaves = sorted((norm(c) for c in a["chaves"]), key=len, reverse=True)
            # "nao": trechos que não contam para este condomínio (o bairro Vale dos Tucanos × Condomínio Tucanos)
            nao = re.compile("|".join(re.escape(norm(x)) for x in a["nao"])) if a.get("nao") else None
            ALVOS.append((a["nome"], re.compile(r"\b(?:" + "|".join(map(re.escape, chaves)) + r")\b"), True, nao))
        # outros condomínios conhecidos: não entram, mas separam um condomínio do outro (lote de 360 m² do
        # Royal Forest × do Ville Campestre) e barram o anúncio de outro condomínio que só cita um alvo
        for c in (CONFIG.get("condominios_outros", []) if not apto() else []):
            ALVOS.append((c.title(), re.compile(r"\b" + re.escape(norm(c)) + r"\b"), False, None))


# Perfis "por_condominio" (casas, terrenos e Nova Prochet): só entram imóveis da lista do perfil ("alvos", padrão "condominios_alvo")
# (config.json), reconhecidos pelas palavras-chave de cada um no nome do condomínio, título ou bairro
# do anúncio (Zap, OLX e Chaves na Mão também na descrição, ver alvo_detalhe). A primeira palavra-chave
# é o termo da busca por condomínio na OLX.
ALVOS = []  # [(nome, regex das palavras-chave, é alvo?, regex do que não conta)] do perfil em uso
# Perfil de apartamentos "por_condominio" (Nova Prochet / Bela Suíça): só os prédios de "predios_alvo".
# Os nomes são curtos (Hera, Lina, Oscar, Wave...), então o anúncio também precisa estar num dos
# "bairros" ou citar uma palavra de "contexto" (prochet, vectra, adhemar...) — ver contexto_ok.
CTX = []


def contexto_ok(g):
    """Grupo de anúncios com um dos bairros do perfil ou palavra de "contexto" (também na descrição)."""
    if not CTX:
        return True
    return any(x.get("contexto") or bairro_ok(x.get("bairro")) or
               CTX[0].search(norm(" ".join(str(x.get(k) or "") for k in ("predio", "titulo", "endereco", "bairro"))))
               for x in g)


def apto():
    return CFG["tipo"] == "apartamento"


def por_predio():
    """Aba de apartamentos por prédio da lista (Nova Prochet): o nome pode estar só na página do anúncio."""
    return apto() and bool(CFG.get("por_condominio"))


def terrenos():
    """Perfil só de terrenos em condomínio (busca ampla: também lotes sem a marca de condomínio,
    aceitos quando o anúncio prova que é condomínio — ver condominio_ok)."""
    return CFG["tipo"] == "terreno_condominio"


def log(*a):
    print(*a, flush=True)


@functools.lru_cache(maxsize=None)
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
    if apto() and not CFG.get("por_condominio"):
        return bairro_ok(bairro)
    if excluido(bairro, *textos):
        return False
    if alvo(*textos, bairro) or (pendente and bairro_ok(bairro)):
        return True
    if not apto():
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
        if CTX and CTX[0].search(norm(" ".join(t for t in textos if t))):
            rec["contexto"] = True


def cond_rec(x, peso=False):
    """Condomínio (alvo ou outro) do anúncio guardado; com peso=True, (nome, peso): o citado só no bairro
    vale menos que o do nome/título/descrição (Sunset Dream fica no bairro Recanto do Salto)."""
    c = cond_citado(x.get("predio"), x.get("titulo"))
    nome, p = (x.get("condominio") or (c[0] if c else None)), 2
    if not nome:
        c, p = cond_citado(x.get("bairro")), 1
        nome = c[0] if c else None
    return (nome, p) if peso else nome


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


AREA_TOTAL_TXT = [
    re.compile(_NUM + r"\s*" + _M2 + r"\s*(?:de\s+)?(?:area\s+)?total"),
    re.compile(r"(?:area|metragem)\s+total\s*(?:de|:|-|=|com|e)?\s*(?:aprox\w*\.?\s*)?" + _NUM + r"\s*" + _M2),
]
# Pares área total -> privativa por prédio, aprendidos das descrições que citam as duas ("104 m² privativos,
# 192 m² de área total"): anúncio do mesmo prédio que traz só a total no campo da área é corrigido
# (aplica_areas_predio). Guardados no estado do perfil ("areas_predio").
AREAS_PREDIO = {}


def area_total_texto(*textos):
    t = norm(" ".join(x for x in textos if x)).replace("\\n", " ")
    vals = [_num_area(m.group(1)) for rx in AREA_TOTAL_TXT for m in rx.finditer(t)]
    vals = [v for v in vals if v and v >= 25]
    return max(vals) if vals else None


def chave_area(rec):
    return name_key(limpa_predio(rec.get("condominio") or rec.get("predio")) or "") if (
        rec.get("condominio") or rec.get("predio")) else ""


def aprende_area(rec, util, total):
    if not apto():  # casa: "total" é o lote e a chave é o condomínio inteiro, não um prédio
        return
    k = chave_area(rec)
    if k and len(k) >= 3 and util and total and 1.1 <= total / util <= 2.5:
        AREAS_PREDIO.setdefault(k, {})[str(total)] = util


def aprende_por_preco(L):
    """Mesmo prédio, mesmo preço (±2%) e mesmos quartos, uma área 15% a 130% maior que a outra: é a mesma
    unidade, anunciada com a total num portal e a privativa noutro (Gaia: 198 m² e 111 m² por R$ 2,2 mi)."""
    if not apto():  # casa: "total" é o lote e a chave é o condomínio inteiro, não um prédio
        return
    por = {}
    for x in L.values():
        if x.get("visivel") and x.get("area") and x.get("preco") and x.get("tipo") != "terreno":
            k = chave_area(x)
            if k and len(k) >= 3:
                por.setdefault(k, []).append(x)
    for k, g in por.items():
        privs = {u for u in (AREAS_PREDIO.get(k) or {}).values()}
        for a in g:
            for b in g:
                if not (1.15 <= b["area"] / a["area"] <= 2.3) or abs(a["preco"] - b["preco"]) > 0.02 * b["preco"]:
                    continue
                if a.get("quartos") and b.get("quartos") and a["quartos"] != b["quartos"]:
                    continue
                if any(abs(b["area"] - u) <= 2 for u in privs):  # a maior é planta de verdade do prédio
                    continue
                AREAS_PREDIO.setdefault(k, {}).setdefault(str(b["area"]), a["area"])


def aplica_areas_predio(L):
    """Anúncio cuja área é a total de uma planta conhecida do prédio passa a mostrar a privativa dela.
    Não mexe quando a mesma metragem também é a privativa de outra planta do prédio."""
    if not apto():  # casa: "total" é o lote e a chave é o condomínio inteiro, não um prédio
        return
    n = 0
    for x in L.values():
        if not x.get("visivel") or not x.get("area") or x.get("tipo") == "terreno":
            continue
        m = AREAS_PREDIO.get(chave_area(x))
        if not m:
            continue
        privs = set(m.values())
        if any(abs(x["area"] - u) <= 2 for u in privs):
            continue
        for t, u in m.items():
            if abs(x["area"] - int(t)) <= 2 and u < x["area"]:
                x.setdefault("area_anunciada", x["area"])
                x["area"] = u
                n += 1
                break
    if n:
        log(f"  área privativa do prédio aplicada em {n} anúncios que traziam a total")


def corrige_area(rec, *textos):
    """Troca a área do anúncio pela área útil citada no texto, se esta for menor (o campo do portal
    trazia a total). Terreno fica com a área do lote. Guarda a original em area_anunciada."""
    if rec.get("tipo") == "terreno" or not rec.get("area"):
        return
    u = area_util_texto(rec.get("titulo"), *textos)
    if u:
        aprende_area(rec, u, area_total_texto(rec.get("titulo"), *textos))
    if u and u < rec["area"] - 1 and u >= rec["area"] * 0.4:
        rec.setdefault("area_anunciada", rec["area"])
        rec["area"] = u


FETCH_404 = set()  # URLs que responderam 404 nesta varredura (anúncio retirado)


# Fontes fora do ar nesta execução (todas as abas): chave -> motivo. Não são consultadas de novo nem
# entram na segunda tentativa — antes, Santamerica (desafio do Cloudflare) e Arbo (API em 503) gastavam
# uns 10 minutos por execução em tentativas que não tinham como dar certo.
FORA_DO_AR = {}
DESAFIO = set()  # hosts que responderam com o desafio do Cloudflare ("Just a moment...") nesta execução
# Fontes que bloqueiam robôs de propósito (desafio do Cloudflare): status "bloqueado", não "falhou" — a página
# mostra em cinza, à parte das falhas de verdade (não se tenta passar pelo desafio)
BLOQUEADO = set()


def desafio_cloudflare(r):
    return r.status_code in (403, 503) and (r.headers.get("cf-mitigated") == "challenge"
                                            or "<title>Just a moment...</title>" in r.text[:3000])


def fetch(url, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, timeout=45)
            if r.status_code == 200:
                return r.text
            if r.status_code in (404, 410):
                log(f"  HTTP {r.status_code} {url}")
                FETCH_404.add(url)
                return None
            log(f"  HTTP {r.status_code} {url}")
            if desafio_cloudflare(r):  # exige navegador de verdade: tentar de novo agora não adianta
                DESAFIO.add(urllib.parse.urlsplit(url).netloc)
                return None
            # 403/429 = bloqueio de robô: insistir em segundos não adianta. Uma tentativa a mais e a busca
            # fica para a segunda rodada, no fim da varredura (ver REPETIR em main).
            if r.status_code in (403, 429) and i >= 1:
                return None
        except Exception as e:  # noqa: BLE001
            log(f"  erro {e} {url}")
        if i < tries - 1:
            time.sleep(3 * (i + 1))
    return None


def retirado(rec, url):
    """Página do anúncio respondeu 404: saiu do portal. Não vira card nem é aberto de novo."""
    if url not in FETCH_404:
        return False
    rec.pop("detalhe_pendente", None)
    rec.setdefault("condominio", "")
    rec["visivel"] = False
    rec["retirado"] = NOW_ISO
    return True


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


# Nome do prédio como vem dos portais: "Fernando Pessoa\r\n\r\nOPORTUNIDADE", "BOTÂNICO 122M",
# "Lumini Gleba Palhano", "À Venda Condomínio Sun Lake", "Pronto para morar"... limpa_predio deixa só o nome.
_PRED_ANTES = re.compile(r"(?i)^(?:(?:[àa]\s+venda|vendo|venda|exclusividade|fechado|lindo|linda|excelente|"
                         r"oportunidade|apartamento|apto|cobertura)\b[\s:,-]*)+")
_PRED_PREFIXO = re.compile(r"(?i)^(?:(?:condom[ií]n?io|condomnio|cond|edif[ií]cios?|edif|ed|sociedade(?:\s+civil)?)\b\.?\s*)+")
_PRED_DEPOIS = re.compile(
    r"(?i)[\s,.-]+(?:(?:centro|regi[aã]o central)\s+de\s+londrina\w*|londrina\w*|gleba(?:\s+fazenda)?\s+palhano|"
    r"jardim\s+ipiranga\w*|zona\s+sul|\d+\s*(?:m²|m2|m|mts?)\b|\d{3}|\d+\s+(?:su[ií]tes?|quartos?|dorm\w*)|"
    r"i\s+\d+.*|apartamento|apto|venda|vista|excelente\w*(?:\s+localiza\w+)?|lindo\w*|conforto|"
    r"oportunidade|exclusividade|andar\s+alto(?:\s+com)?|alto\s+padr[aã]o|88\s+im[oó]veis|com|edif[ií]cio|"
    r"mais|more|completo|apresentamos|sobrado|\d+\s*su[ií]tes)\s*$")
_PRED_RUIM = re.compile(r"(?i)^(?:completo|excelente|gleba(?: fazenda)? palhano|centro|sta|portaria \d+|torre \d+|"
                        r"bloco \d+|central|pronto para morar|com lazer.*|casa no .*|apartamento.*|"
                        r"\d+|lindo.*|oportunidade.*|venda.*)$")


def limpa_predio(nome):
    if not nome:
        return nome
    n = re.split(r"[\r\n|]|\s{2,}|\.(?=\s*(?:na|no|em|com)\b)", str(nome).strip())[0]
    n = re.sub(r"([a-zà-ÿ])([A-ZÀ-Ý][a-zà-ÿ])", r"\1 \2", n)          # "IpirangaMais"
    n = re.sub(r"([A-ZÀ-Ý]{3,})([A-ZÀ-Ý][a-zà-ÿ])", r"\1 \2", n)      # "LONDRINAApartamento"
    n = n.strip(" .,-:")
    for _ in range(4):
        m = _PRED_DEPOIS.sub("", _PRED_PREFIXO.sub("", _PRED_ANTES.sub("", n))).strip(" .,-:")
        if not m or m == n:
            break
        n = m
    if not n or len(n) < 2 or _PRED_RUIM.match(n):
        return None
    if (n.isupper() and len(n) > 3) or n.islower():
        n = " ".join(w if w in ("NYC", "II", "III", "IV", "VI", "XV", "XVI") else w.capitalize() for w in n.split())
    return n


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
        return retirado(rec, url)
    rec.pop("detalhe_pendente", None)
    m = re.search(r'\\?"createdAt\\?":\\?"(\d{4}-\d\d-\d\dT[\d:.]+Z)', page)
    if m:
        rec["publicado"] = m.group(1)[:19] + "Z"
        rec.pop("data_estimada", None)
    descs = re.findall(r'"description":"(.{20,4000}?)"[,}]', rsc(page))
    desc = htmllib.unescape(re.sub(r"<[^>]+>", " ", max(descs, key=len))) if descs else ""
    corrige_area(rec, desc)
    if not rec.get("predio"):
        rec["predio"] = building_from_text(rec.get("titulo"), desc)
    alvo_detalhe(rec, desc)
    return True


def pontos_id_data(L):
    """[(id numérico, publicado)] dos anúncios do Zap/Viva Real com data lida na página, em ordem de id."""
    return sorted((int(x["id"][1:]), x["publicado"]) for x in L.values()
                  if origem(x) == "grupo" and x.get("publicado") and not x.get("data_estimada")
                  and x["id"][1:].isdigit())


def data_por_id(rid, pontos):
    """Data de publicação estimada pelo id (no Zap/Viva Real os ids crescem com o tempo: em 1.726
    anúncios guardados só 2 fogem da ordem). Serve quando a página do anúncio está bloqueada e a data
    não pode ser lida. Interpola entre os vizinhos; id além do maior conhecido fica com a data dele."""
    if len(pontos) < 50 or not rid[1:].isdigit():
        return None
    n = int(rid[1:])
    i = bisect.bisect_left(pontos, (n, ""))
    if i == 0:
        return pontos[0][1]
    if i >= len(pontos):
        return pontos[-1][1]
    (a, da), (b, db) = pontos[i - 1], pontos[i]
    ta, tb = (datetime.fromisoformat(d.replace("Z", "+00:00")) for d in (da, db))
    t = ta + (tb - ta) * ((n - a) / (b - a) if b > a else 0)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


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
        # cada item: "pr+londrina++bairro" (mesmo nome nos dois portais) ou {"zap": ..., "vivareal": ...}
        # quando o nome difere (o Zap abrevia: sta-rosa, jd-s-jorge; o Viva Real usa santa-rosa, jardim-sao-jorge)
        for item in CFG["grupo_zap_searches"]:
            if isinstance(item, dict):
                zap_slug, vr_slug = item["zap"], item["vivareal"]
            else:
                zap_slug, vr_slug = item, item.split("++")[1]
            if portal == "zap":
                url0 = f"https://www.zapimoveis.com.br/venda/apartamentos/{zap_slug}/"
            else:
                url0 = f"https://www.vivareal.com.br/venda/parana/londrina/bairros/{vr_slug}/apartamento_residencial/"
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


# Portais paginados (buscas com muitas páginas). Cada um faz uma varredura completa por dia (data de
# Brasília): as buscas vão até o fim, para "visto" valer para todo o estoque. A completa só conta como
# feita quando o portal respondeu a todas as buscas, e são no máximo 2 tentativas por dia, para um portal
# bloqueado não fazer toda varredura percorrer 30 páginas (mais acessos = mais bloqueio).
PORTAIS_PAGINADOS = ("zap", "vivareal", "olx", "chavesnamao")
COMPLETA = set()  # portais que fazem a completa nesta rodada (ver main)
PAGINAS = {"completa": 30, "normal": 6}  # ajustado em main pelo perfil
BUSCAS = {}  # portal -> [buscas feitas, buscas que responderam]
REPETIR = []  # (portal, função) das buscas que não responderam: segunda tentativa no fim (ver main)


def limite(portal):
    return PAGINAS["completa"] if portal in COMPLETA else PAGINAS["normal"]


def parar(portal, known, sem_novos):
    """Busca incremental: para depois de N páginas seguidas sem anúncio desconhecido no filtro
    (perfil "paginas_sem_novos"; buscas amplas têm muitas páginas sem nada do filtro).
    Na varredura completa do dia não para: é ela que confirma quais anúncios continuam no ar."""
    return portal not in COMPLETA and bool(known) and sem_novos >= CFG.get("paginas_sem_novos", 1)


def conta_busca(portal, paginas_ok, refazer):
    """Registra uma busca do portal; a que não respondeu (0 páginas) vai para a segunda tentativa."""
    b = BUSCAS.setdefault(portal, [0, 0])
    b[0] += 1
    if paginas_ok:
        b[1] += 1
    else:
        REPETIR.append((portal, refazer))


def status_buscas(portal):
    """ok = todas as buscas responderam; parcial = só algumas; falhou = nenhuma."""
    total, ok = BUSCAS.get(portal, (0, 0))
    return "falhou" if not ok else "ok" if ok == total else "parcial"


def scan_grupo(known, max_pages, found):
    for portal in ("zap", "vivareal"):
        for url0, tipos, prova in grupo_buscas(portal):
            n = _busca_grupo(portal, url0, tipos, prova, known, found)
            conta_busca(portal, n, functools.partial(_busca_grupo, portal, url0, tipos, prova, known, found))
    return {p: status_buscas(p) for p in ("zap", "vivareal")}


def _busca_grupo(portal, url0, tipos, prova, known, found):
    """Uma busca do Zap/Viva Real. Retorna quantas páginas responderam."""
    ok = 0
    slug = url0.split("/venda/")[1].split("?")[0].strip("/") + (" (ampla)" if prova else "")
    sem_novos = 0
    for p in range(1, limite(portal) + 1):
        url = f"{url0}ordem=MOST_RECENT&pagina={p}"
        page = fetch(url)
        if page is None:
            if p == 1 and url in FETCH_404:
                log(f"  {portal} {slug}: a busca não existe (404) — revisar grupo_zap_searches em config.json")
            break
        items = parse_grupo(page)
        if not items:
            break
        ok += 1
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
        if parar(portal, known, sem_novos):
            break
        time.sleep(1.5)
    return ok


# ---------- OLX ----------
def olx_num(v):
    d = re.sub(r"\D", "", v or "")
    return int(d) if d else None


# Casas: categoria "casas de condomínio" (real_estate_type "Venda - casa em condominio fechado").
# Terrenos: a OLX não marca lote de condomínio; busca "condominio" nos lotes à venda (sst=s) e aceita
# os que têm taxa de condomínio ou citam condomínio/residencial no título.
OLX_LONDRINA = "estado-pr/regiao-de-londrina/londrina"


def olx_buscas():
    if apto() and not CFG.get("por_condominio"):
        return [(f"{CFG['olx_search']}?ss={CFG['area_min']}&se={CFG['area_max']}&", None)]
    # Uma busca por condomínio-alvo (a primeira palavra-chave) em todos os imóveis à venda; o tipo
    # (casa/terreno) sai da categoria de cada anúncio (olx_tipo)
    cat = "terrenos/lotes" if terrenos() else "venda/apartamentos" if apto() else "venda"
    return [(f"https://www.olx.com.br/imoveis/{cat}/{OLX_LONDRINA}?sst=s&q={urllib.parse.quote(a.get('busca') or a['chaves'][0])}&",
             "auto") for a in CONFIG[CFG.get("alvos", "condominios_alvo")]] + [
        # Nova Prochet: também a busca pelo bairro (o prédio sai da descrição, olx_detail)
        (f"https://www.olx.com.br/imoveis/{cat}/{OLX_LONDRINA}?sst=s&q={urllib.parse.quote(q)}&", "auto")
        for q in CFG.get("olx_extra", [])]


def olx_tipo(a, props):
    """"casa", "terreno" ou None (outra categoria), para as buscas por condomínio."""
    c = norm(a.get("category") or props.get("category"))
    if apto():
        return "apartamento" if c.startswith("apartamento") else None
    if c.startswith("casa"):
        return None if terrenos() else "casa"
    if (c.startswith("terreno") or c.startswith("lote")) and terrenos():
        return "terreno"
    return None


def olx_tipo_ok(tipo, props, subject):
    if tipo in (None, "apartamento"):
        return "venda" in norm(props.get("real_estate_type", "venda"))
    if "venda" not in norm(props.get("real_estate_type", "venda")):
        return False
    # casa em condomínio fechado (ou com taxa de condomínio: há casa de condomínio cadastrada "em rua pública")
    if tipo == "casa":
        return "condominio fechado" in norm(props.get("real_estate_type")) or bool(olx_num(props.get("condominio")))
    return bool(olx_num(props.get("condominio"))) or condominio_ok(subject)


def scan_olx(known, max_pages, found):
    for url0, tipo in olx_buscas():
        n = _scan_olx(url0, tipo, known, found)
        conta_busca("olx", n, functools.partial(_scan_olx, url0, tipo, known, found))
    return {"olx": status_buscas("olx")}


def _scan_olx(url0, tipo0, known, found):
    pages_ok = sem_novos = 0
    # busca de um condomínio: poucas páginas (a busca por palavra traz depois o que só se parece com ela)
    paginas = ((CFG.get("max_pages_condominio", 2) * (3 if "olx" in COMPLETA else 1)) if tipo0 == "auto"
               else limite("olx") * 2)
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
                "tipo": None if tipo == "apartamento" else tipo,
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
        if not ads or parar("olx", known, sem_novos):
            break
        time.sleep(1.5)
    return pages_ok


def olx_detail(rec):
    page = fetch(rec["links"]["olx"])
    if not page:
        retirado(rec, rec["links"]["olx"])
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
    novos = [p for p in recs if f"rf{p}" not in CONHECIDOS]
    if novos:
        time.sleep(1)
        ph = _rf_search({"idtsPropertys": novos, "start": 0, "numRows": len(recs) + 10,
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
# Desde 2026-11-18 o JSON vem dentro de uma função que troca o prefixo das fotos:
# (function(o,p,f){...return o})({...}, "https://img.kenlo.io", "https://imgs.kenlo.io")
GLEBA_FOTOS = ("picture_thumb", "picture_full", "picture_banner")


def _gleba_json(html, pos):
    """Objeto de window.markoVars['listings-…'] a partir de pos (literal ou embrulhado na função das fotos)."""
    dec = json.JSONDecoder()
    if html.startswith("(function", pos):
        i = html.find("})(", pos)
        if i < 0:
            raise ValueError("embrulho sem chamada")
        o, end = dec.raw_decode(html, i + 3)
        args = re.match(r'\s*,\s*("(?:[^"\\]|\\.)*")\s*,\s*("(?:[^"\\]|\\.)*")', html[end:end + 300])
        if args:
            de, para = json.loads(args.group(1)), json.loads(args.group(2))
            for x in ((o.get("settings") or {}).get("listings") or {}).get("data") or []:
                for f in GLEBA_FOTOS:
                    v = x.get(f)
                    if isinstance(v, str) and v.startswith(de):
                        x[f] = para + v[len(de):]
                    elif isinstance(v, list):
                        x[f] = [para + s[len(de):] if isinstance(s, str) and s.startswith(de) else s for s in v]
        return o
    return dec.raw_decode(html, pos)[0]


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
    while page <= 60:
        html = fetch(url0 + f"pagina={page}")
        if not html:
            return "falhou" if page == 1 else "ok"
        m = GLEBA_MARKO.search(html)
        if not m:
            log("  gleba: JSON de listagem não encontrado")
            return "falhou" if page == 1 else "ok"
        try:
            listings = _gleba_json(html, m.end())["settings"]["listings"]
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
            desc = [o.get("listing_description")] if por_predio() else []
            if not local_ok(bairro, o.get("condo_name"), o.get("website_title"), *desc) or not area_ok(area):
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
            alvo_detalhe(found["gi" + ref], o.get("listing_description"))
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
            if urllib.parse.urlsplit(SA_BASE).netloc in DESAFIO:
                FORA_DO_AR["santamerica"] = "desafio do Cloudflare nas páginas de busca"
                BLOQUEADO.add("santamerica")
                return "bloqueado"
            return "falhou"
        log(f"  santamerica: {len(cards)} cards na busca")
        n = 0
        for c in cards:
            if "londrina" not in norm(c["cidade"]) or not local_ok(c["bairro"], c["titulo"], pendente=por_predio()):
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
            area = _num_area(a) if a else None
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
            ld = re.search(r'"description":"(.*?)","', page)
            try:
                desc = json.loads('"' + ld.group(1) + '"') if ld else c["desc"]
            except ValueError:
                desc = c["desc"]
            if not predio:
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
            alvo_detalhe(found[rid], desc)
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
        if not local_ok(bairro, o.get("title"), pendente=por_predio()) or not area_ok(area):
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
            rua, num = str(det.get("street") or "").strip(), str(det.get("number") or "").strip()
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
            "vagas": o.get("parkingSpaces"),
            "titulo": o.get("title"),
            "publicado": publicado,
            "links": {"perez": _pz_link(o)},
            **coords(o.get("latitude"), o.get("longitude")),
        }
        alvo_detalhe(found[f"pz{o['code']}"], (det or {}).get("description"))
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
        while p <= last and p <= 40:
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
                if norm(cidade) != "londrina" or not local_ok(bairro, titulo, pendente=por_predio()):
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
            alvo_detalhe(rec, desc)
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
    rec["det"] = True


def scan_chavesnamao(known, max_pages, found):
    cota = {"detalhes": 0}  # páginas de anúncio lidas nesta varredura (todas as buscas)
    for path, tipo, prova in cm_buscas():
        n = _busca_cm(path, tipo, prova, known, found, cota)
        conta_busca("chavesnamao", n, functools.partial(_busca_cm, path, tipo, prova, known, found, cota))
    return {"chavesnamao": status_buscas("chavesnamao")}


def _busca_cm(path, tipo, prova, known, found, cota):
    """Uma busca do Chaves na Mão. Retorna quantas páginas responderam."""
    pages_ok = 0
    sem_novos = 0
    for p in range(1, limite("chavesnamao") + 1):
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
            k = known.get(rec["id"])  # guardado: a página é lida uma vez (o guardado é atualizado no lugar)
            if cota["detalhes"] < CM_DETALHES_MAX and not (k or rec).get("det") and not (k or {}).get("publicado"):
                cota["detalhes"] += 1
                time.sleep(1)
                cm_detalhe(k or rec)
            found[rec["id"]] = rec
        log(f"  chavesnamao {path.split('/')[1]}{' (ampla)' if prova else ''} p{p}: "
            f"{len(itens)} anúncios, {fresh} desconhecidos")
        sem_novos = 0 if fresh else sem_novos + 1
        if len(itens) < 15 or parar("chavesnamao", known, sem_novos):
            break
        time.sleep(1)
    return pages_ok


# ---------- Imobiliárias Aurora Imobi, Casttelo e Monaco (Londrina) — sites Kenlo ----------
# Mesmo motor do site da Inglaterra (ver acima): API JSON interna
#   /api/listings/a-venda/<apartamento|casa|terreno>/londrina[/<bairro>]?area=MIN~MAX&size=48&pagina=N
# devolve {"data": [...], "count": N}; tags=GATED_COMMUNITY = só condomínio fechado. Não há filtro de preço
# nem data de cadastro (só updated_at). /api/listing/<ref> traz o prédio ("building"), o endereço com número
# e latitude/longitude: aberto para os anúncios no filtro, até KENLO_DETALHES_MAX por site e varredura
# (coordenadas só quando o endereço tem número; sem ele a posição é aproximada).
KENLO_SITES = {  # chave do portal: (site, prefixo do id)
    "humaita": ("https://www.imobiliariahumaita.com.br", "hu"),
    "cruciol": ("https://www.cruciol.com.br", "cr"),
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
    if not rec.get("predio"):
        rec["predio"] = (d.get("building") or "").split("|")[0].split("\n")[0].strip() or building_from_text(
            d.get("title"), d.get("listing_description"))
    rua = (d.get("full_address") or "").split(" - ")[0].strip()
    if re.search(r",\s*\d+", rua):
        rec["endereco"] = rua
        rec.update(coords(d.get("latitude"), d.get("longitude")))
    ua = _in_int(d.get("usable_floor_area")) or _in_int(d.get("private_floor_area"))
    if ua and area_ok(ua) and rec.get("tipo") != "terreno":
        rec["area"] = ua
    alvo_detalhe(rec, d.get("listing_description"), d.get("title"))
    rec["det"] = True


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
        desc = [x.get("listing_description")] if por_predio() else []
        if norm(x.get("city")) != "londrina" or not local_ok(bairro, x.get("condo_name"), x.get("picture_title"),
                                                              x.get("website_title"), *desc):
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
        alvo_detalhe(recs[ref], x.get("listing_description"))
    # detalhe: primeiro os que estão sem nome de prédio
    for ref, rec in recs.items():  # já guardado: o detalhe foi aberto antes
        k = conhecido(rec["id"], rec["links"], preco=rec["preco"], foto=rec["foto"])
        if k:
            recs[ref] = k
    # detalhe ainda não lido (novos e os que passaram da cota antes); o guardado é atualizado no lugar
    def falta(r):
        k = CONHECIDOS.get(recs[r]["id"])
        return not (k or recs[r]).get("det") and not (k or {}).get("lat") and preco_ok(recs[r].get("preco") or 0)
    fila = sorted((r for r in recs if falta(r)), key=lambda r: (recs[r]["id"] in CONHECIDOS, bool(recs[r].get("predio"))))
    fila = fila[:KENLO_DETALHES_MAX]
    for ref in fila:
        time.sleep(1)
        _kenlo_detalhe(base, ref, CONHECIDOS.get(recs[ref]["id"]) or recs[ref])
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
        while page <= 60:
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
                if not local_ok(bairro, emp, titulo, desc, pendente=por_predio()):
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
                alvo_detalhe(rec, desc)
                found[rec["id"]] = rec
                n += 1
            if not props or page * qtd >= total:
                break
            page += 1
            time.sleep(1)
        time.sleep(1)
    log(f"  cube: {n} no filtro ({lidos} lidos)")
    return "ok" if ok else "falhou"


# ---------- Fontes novas (2026-11-17): Arbo, Loft, Inglaterra (site novo), Galmo ----------
def _rec_novo(chave, rid, tipo, *, predio, endereco, bairro, foto, area, preco, quartos=None, banheiros=None,
              vagas=None, titulo=None, publicado=None, link, lat=None, lon=None, desc=None, exato=True):
    """Registro comum das fontes novas, já com os filtros do perfil (local, área, preço, condomínio).
    None se o anúncio não entra. O nome do prédio/condomínio também é procurado na descrição."""
    if not area_ok(area) or not preco_ok(preco):
        return None
    extra = [desc] if por_predio() else []
    if not local_ok(bairro or "", predio, titulo, *extra, pendente=por_predio()):
        return None
    if terrenos() and not condominio_ok(predio, titulo, bairro, desc):
        return None
    rec = {"id": rid, "tipo": tipo, "predio": predio or building_from_text(titulo, desc), "endereco": endereco,
           "bairro": bairro, "foto": foto, "area": area, "preco": preco, "quartos": quartos, "banheiros": banheiros,
           "vagas": vagas, "titulo": titulo, "publicado": publicado, "links": {chave: link}}
    try:
        rec.update(coords(float(lat), float(lon), aproximada=not exato) if lat and lon else {})
    except (TypeError, ValueError):
        pass
    corrige_area(rec, desc)
    alvo_detalhe(rec, desc)
    return rec


def _tipo_perfil():
    return None if apto() else "terreno" if terrenos() else "casa"


# Arbo (arboimoveis.com.br, CRM Superlógica): portal de ~18 imobiliárias de Londrina (Human, GEUM, Mitral,
# CRV...). A API pública do site devolve a carteira de todas quando o Origin é o portal, ou só a de uma
# imobiliária quando é o site dela (88 Imóveis usa o mesmo sistema e não está no portal). Duas etapas:
# find-by-polygon-new (ids da busca, sem paginação, até 3000) e get-imoveis-new (100 registros por vez).
ARBO_API = "https://api-site.arboimoveis.com.br/api/imoveis-replica"
ARBO_SITES = {"arbo": "https://www.arboimoveis.com.br", "88imoveis": "https://www.88imoveis.com.br"}


def _arbo_params(d):
    out = []
    for k, v in d.items():
        if isinstance(v, list):
            out += [(k + "[]", json.dumps(x) if isinstance(x, (dict, list)) else str(x)) for x in v]
        elif isinstance(v, dict):
            out.append((k, json.dumps(v, ensure_ascii=False)))
        elif v is not None:
            out.append((k, str(v).lower() if isinstance(v, bool) else str(v)))
    return out


def _arbo_get(site, path, params):
    if "arbo" in FORA_DO_AR:
        return None
    for i in range(2):
        try:
            r = S.get(f"{ARBO_API}/{path}", params=params, timeout=60, headers={"Origin": site, "Referer": site + "/"})
            if r.status_code == 200:
                return r.json()
            log(f"  arbo HTTP {r.status_code} {path}")
            if r.status_code in (502, 503, 504) and i >= 1:  # API fora do ar (serve a Arbo e a 88 Imóveis)
                motivo = f"API da Arbo respondendo {r.status_code}"
                FORA_DO_AR.update(arbo=motivo, **{"88imoveis": motivo})
                return None
        except Exception as e:  # noqa: BLE001
            log(f"  arbo erro {e}")
        time.sleep(3)
    return None


def _arbo_ids(site, tipos, pmin, pmax, condominio):
    """ids da busca; com 3000 (limite) divide a faixa de preço."""
    f = {"tipovar": "a-venda", "order": "mais_relevantes", "mobilia": "talvez", "tipoimovel": tipos,
         "muncep_id": 5207, "localidade": "Londrina, PR", "search_name": "Londrina", "cidade": "londrina-pr",
         "precovenda": [pmin, pmax], "hasTypeSearch": True, "limit_imv": 3000,
         "center": {"lat": -14.235004, "lng": -51.92528}, "north_east": {"lat": -33.751279, "lng": -73.982661},
         "south_west": {"lat": 5.2716701, "lng": -32.388698}}
    if apto():
        f["area"] = [CFG["area_min"], CFG["area_max"]]
    if condominio:
        f["condominio"] = "sim"
    j = _arbo_get(site, "find-by-polygon-new", _arbo_params(f))
    time.sleep(1)
    if not isinstance(j, dict) or not isinstance(j.get("data"), list):
        return None
    if len(j["data"]) >= 3000 and pmax - pmin > 50000:
        meio = (pmin + pmax) // 2
        a, b = _arbo_ids(site, tipos, pmin, meio, condominio), _arbo_ids(site, tipos, meio + 1, pmax, condominio)
        return None if a is None or b is None else a + b
    return [x["id"] for x in j["data"] if x.get("id")]


def _scan_arbo(chave, found):
    site = ARBO_SITES[chave]
    tipos = ({"apartamento": True} if apto() else {"terreno": True} if terrenos() else {"casa": True, "sobrado": True})
    ids = _arbo_ids(site, tipos, CFG.get("preco_min_busca") or CFG.get("preco_min") or 0, 100_000_000, not apto())
    if ids is None:
        return "falhou"
    n = 0
    for i in range(0, len(ids), 100):
        where = json.dumps({"id": {"inq": ids[i:i + 100]}})
        j = _arbo_get(site, "get-imoveis-new", [("where", where), ("tipovar", "a-venda"), ("order", "mais_relevantes")])
        time.sleep(1)
        lote = j if isinstance(j, list) else (j or {}).get("data") if isinstance(j, dict) else None
        for o in lote or []:
            if norm(o.get("imv_cidade") or "londrina") != "londrina" or not o.get("imv_cod_gaia"):
                continue
            loc = o.get("imv_location") or {}
            rua, num = (o.get("imv_endereco") or "").strip(), o.get("imv_end_numero")
            fotos = o.get("fotos") or []
            area = o.get("imv_area_total") if terrenos() else (o.get("imv_area_util") or o.get("imv_area_total"))
            rec = _rec_novo(
                chave, "ar" + o["imv_cod_gaia"], _tipo_perfil(),
                predio=(o.get("imv_nome_cond") or "").strip() or None,
                endereco=(f"{rua}, {num}" if num else rua) or None, bairro=o.get("imv_bairro"),
                foto=fotos[0].get("imvft_url") if fotos and isinstance(fotos[0], dict) else None,
                area=round(area) if area else None, preco=round(o["imv_preco_venda"]) if o.get("imv_preco_venda") else None,
                quartos=o.get("imv_qtd_dorm") or None, banheiros=o.get("imv_qtd_banheiros") or None,
                vagas=o.get("imv_qtd_vagas") if o.get("imv_qtd_vagas") is not None else None,
                titulo=o.get("imv_titulo"), publicado=(o.get("createdAt") or "")[:19] + "Z" if o.get("createdAt") else None,
                link=o.get("url_amiga") or site, lat=loc.get("lat"), lon=loc.get("lng"), desc=o.get("imv_obs"),
                exato=bool(num))
            if rec:
                found[rec["id"]] = rec
                n += 1
    log(f"  {chave}: {n} no filtro ({len(ids)} na busca)")
    return "ok"


def scan_arbo(found):
    return _scan_arbo("arbo", found)


def scan_88imoveis(found):
    return _scan_arbo("88imoveis", found)


# Loft (loft.com.br): marketplace das imobiliárias do CRM Vista (~25 em Londrina: FR, C M Souza, Gomes e
# Borini, Nantes, Senador...). API landscape-api /listing/v3/search, 100 por página. Sem número da rua.
LOFT_API = "https://landscape-api.loft.com.br/listing/v3/search"


def scan_loft(found):
    if terrenos():
        return "ok"  # a Loft não separa lote de condomínio
    q = [("orderBy[]", "rankA"), ("cities[]", "londrina, pr"), ("transactionType[]", "for_sale"),
         ("realState[]", "apartment_default" if apto() else "house_condominium"), ("hitsPerPage", "100")]
    if apto():
        q += [("areaMin", str(CFG["area_min"])), ("areaMax", str(CFG["area_max"]))]
        if CFG.get("bairro_exato") and len(CFG["bairros"]) == 1:
            q.append(("neighborhood[]", f"{CFG['bairros'][0].title()}, Londrina, PR"))
    pmin = CFG.get("preco_min_busca") or CFG.get("preco_min")
    if pmin:
        q.append(("priceMin", str(pmin)))
    n, page, total_pages, ok = 0, 1, 1, False
    while page <= min(total_pages, 60):
        try:
            r = S.get(LOFT_API, params=q + [("page", str(page))], timeout=45,
                      headers={"Origin": "https://loft.com.br", "Referer": "https://loft.com.br/"})
            j = r.json() if r.status_code == 200 else None
        except Exception as e:  # noqa: BLE001
            log(f"  loft erro {e}")
            j = None
        if not isinstance(j, dict) or not isinstance(j.get("listings"), list):
            break
        ok = True
        total_pages = (j.get("pagination") or {}).get("totalPages") or 1
        for o in j["listings"]:
            if o.get("status") not in (None, "FOR_SALE") or not o.get("id"):
                continue
            a = o.get("address") or {}
            if norm(a.get("city") or "londrina") != "londrina":
                continue
            fotos = o.get("photos") or []
            rec = _rec_novo(
                "loft", "lf" + o["id"], _tipo_perfil(), predio=(a.get("complexName") or "").strip() or None,
                endereco=a.get("streetName"), bairro=a.get("neighborhood"),
                foto=f"https://content.loft.com.br/homes/{o['id']}/{fotos[0]}" if fotos else None,
                area=round(o["area"]) if o.get("area") else None, preco=o.get("price") or None,
                quartos=o.get("bedrooms") or None, banheiros=o.get("restrooms") or None, vagas=o.get("parkingSpots"),
                titulo=o.get("title") or None, publicado=(o.get("createdAt") or "")[:19] + "Z" if o.get("createdAt") else None,
                link=f"https://loft.com.br/home/{o['id']}", lat=a.get("lat"), lon=a.get("lng"),
                desc=o.get("description"), exato=False)
            if rec:
                found[rec["id"]] = rec
                n += 1
        page += 1
        time.sleep(1)
    log(f"  loft: {n} no filtro ({page - 1} de {total_pages} páginas)")
    return "ok" if ok else "falhou"


# Inglaterra: o site novo (Next.js, 2026-11) tem POST /api/imoveis/search (48 por página). A página do
# anúncio traz a descrição e a rua com número no JSON-LD: lida para os novos, até 40 por varredura.
INGLATERRA_BASE = "https://www.imobiliariainglaterra.com.br"


def scan_inglaterra(found):
    tipo = "Apartamento" if apto() else "Terreno" if terrenos() else "Casa"
    body = {"bairro": None, "condominio": None, "tipo": tipo, "negocio": "Comprar", "valorMinimo": None,
            "valorMaximo": None, "suitesMinimas": None, "quartosMinimos": None, "vagasMinimas": None,
            "areaMinima": CFG["area_min"] if apto() else None, "areaMaxima": CFG["area_max"] if apto() else None,
            "mobiliado": None, "order": "relevancia", "perPage": 48}
    itens, page = [], 1
    while page <= 60:
        try:
            r = S.post(INGLATERRA_BASE + "/api/imoveis/search", json=dict(body, page=page), timeout=45,
                       headers={"Origin": INGLATERRA_BASE, "Referer": INGLATERRA_BASE + "/imoveis"})
            j = r.json() if r.status_code == 200 else None
        except Exception as e:  # noqa: BLE001
            log(f"  inglaterra erro {e}")
            j = None
        if not isinstance(j, dict) or not isinstance(j.get("imoveis"), list):
            if page == 1:
                return "falhou"
            break
        itens += j["imoveis"]
        if not j.get("hasMore") or not j["imoveis"]:
            break
        page += 1
        time.sleep(1)
    n = det = 0
    for o in itens:
        if not o.get("codigo") or norm(o.get("cidade") or "londrina") != "londrina" or not o.get("precoVenda"):
            continue
        rid = "in" + o["codigo"] + "-INIC"  # mesmo id do site antigo (Kenlo)
        link = INGLATERRA_BASE + "/imoveis/" + o["slug"]
        k = CONHECIDOS.get(rid)
        desc = endereco = None
        if not k and det < 40 and o.get("slug"):  # novo: a página traz descrição e rua com número
            det += 1
            time.sleep(1)
            page_ = fetch(link, tries=1) or ""
            for m in CM_LDJSON.finditer(page_):
                try:
                    ld = json.loads(m.group(1))
                except ValueError:
                    continue
                if isinstance(ld, dict) and "RealEstateListing" in str(ld.get("@type")):
                    desc = ld.get("description")
                    endereco = (((ld.get("about") or ld.get("itemOffered") or {}).get("address") or {})
                                .get("streetAddress")) or None
        rec = _rec_novo(
            "inglaterra", rid, _tipo_perfil(), predio=o.get("condominio") or None, endereco=endereco,
            bairro=o.get("bairro"), foto=o.get("image"), area=o.get("area"), preco=o.get("precoVenda"),
            quartos=o.get("dormitorios"), vagas=o.get("vagas"), titulo=o.get("titulo"), link=link, desc=desc)
        if rec:
            found[rid] = rec
            n += 1
    log(f"  inglaterra: {n} no filtro ({len(itens)} lidos, {det} páginas de anúncio)")
    return "ok"


# Galmo (galmo.com.br): construtora com imobiliária própria; vende unidades dos prédios dela (Torre Verona,
# Latorre, TorreBlanca, Madri, Valverde...). Só apartamentos; HTML do site (WordPress + Vista), 9 por página.
GALMO_BASE = "https://galmo.com.br"


def scan_galmo(found):
    if not apto():
        return "ok"
    n, lidos = 0, 0
    for p in range(1, 20):
        page = fetch(f"{GALMO_BASE}/busca/?finalidade=Venda&listagem=grid&pagina={p}")
        if page is None:
            return "falhou" if p == 1 else "ok"
        cards = page.split("pgl-property animation")[1:]
        if not cards:
            break
        for c in cards:
            def g(rx):
                m = re.search(rx, c, re.S)
                return m.group(1) if m else None
            lidos += 1
            if norm(g(r'data-categoria="([^"]*)"')) not in ("apartamento", "cobertura", "apartamento duplex"):
                continue
            cod = g(r'data-codigo="(\d+)"')
            mod = re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", g(r'class="modalidade">(.*?)</span>') or ""))).strip()
            predio, _, bairro = mod.rpartition(" - ")
            venda = g(r"Venda:\s*</strong>(?:\s*</span>\s*<span[^>]*>)?\s*R\$\s*([\d.,]+)")
            preco = _num_area(venda) if venda else None
            area = _num_area(g(r"<small>([\d.,]+)\s*m<sup>") or "")
            dorm = g(r"(\d+)\s*(?:<[^>]+>\s*)*(?:Dorm|Quarto)")
            vag = g(r"(\d+)\s*(?:<[^>]+>\s*)*Vaga")
            link = g(r'href="(https://galmo\.com\.br/imovel/[^"]+)"') or f"{GALMO_BASE}/imovel/{cod}/x"
            if not cod:
                continue
            rec = _rec_novo("galmo", "gm" + cod, None, predio=predio or None, endereco=None, bairro=bairro or None,
                            foto=g(r"url\((https://cdn[^)]+)\)"), area=area, preco=preco,
                            quartos=int(dorm) if dorm else None, vagas=int(vag) if vag else None,
                            titulo=f"Apartamento {predio} - Galmo" if predio else None, link=link.split("?")[0],
                            desc="Galmo")
            if rec:
                found[rec["id"]] = rec
                n += 1
        time.sleep(1)
    log(f"  galmo: {n} no filtro ({lidos} lidos)")
    return "ok"


# X5 Imóveis (x5imoveis.com.br, Tecimob/Next.js): a lista vem pronta no __NEXT_DATA__ das páginas
# /comprar/<tipo>/londrina-pr?offset=N (21 por página). O robots.txt bloqueia /api/ e os filtros da
# busca (bairro, preço, área), não a paginação. A faixa colorida do card (stripe_text) é o prédio.
X5_BASE = "https://x5imoveis.com.br"


def _x5_area(areas, tipo):
    a = areas or {}
    ordem = ("total_area",) if tipo == "terreno" else ("private_area", "primary_area", "total_area")
    for k in ordem:
        v = _num_area(str((a.get(k) or {}).get("value") or ""))
        if v:
            return v
    return None


def scan_x5(found):
    tipo = _tipo_perfil()
    slug = {"casa": "casa", "terreno": "terreno"}.get(tipo, "apartamento")
    n = lidos = 0
    for off in range(1, 40):
        page = fetch(f"{X5_BASE}/comprar/{slug}/londrina-pr?offset={off}")
        if page is None:
            return "falhou" if off == 1 else "ok"
        m = re.search(r'__NEXT_DATA__[^>]*>(.*?)</script>', page, re.S)
        try:
            P = json.loads(m.group(1))["props"]["initialState"]["properties"]
        except (AttributeError, KeyError, TypeError, ValueError):
            return "falhou" if off == 1 else "ok"
        for x in P.get("data") or []:
            lidos += 1
            sub = ((x.get("subtype") or {}).get("id")) or ""
            if x.get("transaction") != "VENDA" or (tipo == "casa" and "condominio" not in sub):
                continue
            preco = re.sub(r"\D", "", x.get("price") or "")
            local = ((x.get("address") or {}).get("formatted") or "").split(" - ")
            stripe = (x.get("stripe_text") or "").strip()
            # a faixa às vezes é chamada ("Pronto para morar"): vale se o título cita o nome
            predio = stripe if stripe and name_key(stripe) and name_key(stripe) in name_key(x.get("title_formatted") or "") else None
            imgs = x.get("images") or []
            foto = ((imgs[0].get("file_url") or {}).get("medium")) if imgs else None
            rooms = x.get("rooms") or {}
            rec = _rec_novo("x5", "x5" + str(x.get("reference") or x.get("id")), tipo, predio=predio, endereco=None,
                            bairro=local[0] if len(local) > 1 else None, foto=foto,
                            area=_x5_area(x.get("areas"), tipo), preco=int(preco) if preco else None,
                            quartos=(rooms.get("bedroom") or {}).get("value"),
                            banheiros=(rooms.get("bathroom") or {}).get("value"),
                            vagas=(rooms.get("garage") or {}).get("value"), titulo=x.get("title_formatted"),
                            link=f"{X5_BASE}/imovel/{x.get('url')}", desc=x.get("meta_title"))
            if rec:
                found[rec["id"]] = rec
                n += 1
        pg = (P.get("meta") or {}).get("pagination") or {}
        if off >= (pg.get("total_pages") or 0):
            break
        time.sleep(1)
    log(f"  x5: {n} no filtro ({lidos} lidos)")
    return "ok"


# ---------- desduplicação ----------
# O mesmo apartamento costuma aparecer em vários portais (e às vezes 2× no mesmo portal, por
# imobiliárias diferentes). Cada anúncio fica guardado separado em "listings"; a cada varredura
# eles são agrupados de novo em "imoveis", que é o que a página mostra.
def origem(rec):
    return rec.get("origem") or {"g": "grupo", "o": "olx"}.get(rec["id"][0])


NAME_PREFIX = re.compile(r"^(?:ed|edif|edificio|residencial|res|condominio|cond|torre|l|le|la)\b\.?\s*")
STREET_PREFIX = re.compile(r"^(?:rua|r|avenida|av|alameda|al|travessa|tv)\b\.?\s*")


@functools.lru_cache(maxsize=None)  # depende de CFG["bairros"]: limpo em usar_perfil
def bairro_key(b):
    n = norm(b)
    return next((k for k in CFG["bairros"] if k in n), n)


@functools.lru_cache(maxsize=None)
def name_key(p):
    n = re.sub(r"[^a-z0-9 ]", " ", norm(p))
    n = re.sub(r"\s+", " ", n).strip()
    while True:
        m = NAME_PREFIX.sub("", n)
        if m == n or not m:  # "La Torre": tirando "la" e "torre" não sobra nada, fica "latorre"
            return n.replace(" ", "")
        n = m


def same_name(a, b):
    x, y = name_key(a), name_key(b)
    return bool(x and y) and (x == y or (min(len(x), len(y)) >= 5 and (x in y or y in x)))


@functools.lru_cache(maxsize=None)
def street(e):
    """("joao wyclif", "420") a partir de "Rua João Wyclif, 420"."""
    if not e:
        return None, None
    parts = [p.strip() for p in e.split(",")]
    nome = STREET_PREFIX.sub("", norm(parts[0]))
    num = re.sub(r"\D", "", parts[1]) if len(parts) > 1 else ""
    return nome or None, num or None


@functools.lru_cache(maxsize=None)
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
        ca, cb = cond1(a), cond1(b)
        return bool(ca and cb and ca != cb) or abs(a["area"] - b["area"]) > 3 or preco_conflita(a, b)
    if apto() and CFG.get("por_condominio"):  # prédios-alvo (Nova Prochet): o prédio da lista decide
        ca, cb = cond1(a), cond1(b)
        if ca and cb:
            if ca != cb:
                return True
            for f in ("quartos", "vagas"):
                if a.get(f) and b.get(f) and a[f] != b[f]:
                    return True
            return abs(a["area"] - b["area"]) > 3 or preco_conflita(a, b)
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
    if terrenos() or (apto() and CFG.get("por_condominio")):
        ca = cond1(a)
        if ca or terrenos():
            return bool(ca) and ca == cond1(b)
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


PAGINADOS = ("grupo", "olx", "chavesnamao")  # buscas que param no meio: "visto" não prova que saiu
COMPLETO = {}  # portal de lista inteira -> última varredura em que leu a lista toda (state["completo_em"])


def iso_dias_atras(d):
    return (NOW - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sumido(x):
    """Anúncio que saiu do ar: a página dele deu 404/410 ("retirado"), ou é de uma imobiliária cuja lista
    inteira é lida a cada varredura e ele não aparece nela há 3 dias. Zap/Viva Real, OLX e Chaves na Mão
    nem sempre são lidos até o fim: para eles vale só a página (ver verificar_sumidos)."""
    if x.get("retirado"):
        return True
    o = origem(x) or ""
    if o in PAGINADOS:
        return False
    visto = x.get("visto") or x.get("detectado") or NOW_ISO
    return visto < iso_dias_atras(3) and (COMPLETO.get(o) or "") >= iso_dias_atras(1)


def verificar_sumidos(L, det):
    """Anúncios do Zap/Viva Real, OLX e Chaves na Mão sem aparecer na busca há 7 dias: abre a página.
    404/410 = retirado; página no ar = continua (visto renovado). Cota por varredura."""
    cota = {"grupo": 40, "olx": 80, "chavesnamao": 80}
    velhos = sorted((x for x in L.values() if x.get("visivel") and not x.get("retirado") and origem(x) in cota
                     and (x.get("visto") or "") < iso_dias_atras(7)), key=lambda x: x.get("visto") or "")
    saiu = ficou = 0
    for x in velhos:
        o = origem(x)
        if cota[o] <= 0 or (o == "grupo" and det.falhas >= 3):
            continue
        cota[o] -= 1
        url = (x["links"].get("zap") or x["links"].get("vivareal")) if o == "grupo" else x["links"].get(o)
        if not url:
            continue
        page = fetch(url, tries=1)
        time.sleep(1.5 if o == "grupo" else 1)
        if retirado(x, url):
            saiu += 1
        elif page and (o != "chavesnamao" or x["id"][2:] in page):
            x["visto"] = NOW_ISO
            ficou += 1
            if o == "grupo":
                det.falhas = 0
        elif o == "grupo":
            det.falhas += 1
    if velhos:
        log(f"  sem aparecer há 7 dias: {len(velhos)}; abertos agora: {saiu} saíram do ar, {ficou} continuam")


def tipo_ok(x):
    """Casas e terrenos são abas excludentes: casa não entra em Terrenos, terreno não entra em Casas."""
    return apto() or x.get("tipo") == ("terreno" if terrenos() else "casa")


def _faixa(x):
    """Chaves (tipo, área inteira) de um anúncio. Dois anúncios só podem ser o mesmo imóvel com o mesmo
    tipo e área até 3 m² de diferença (conflito): a comparação em pares fica só entre os da mesma faixa."""
    return (x.get("tipo"), int(x["area"] // 1))


def _vizinhas(chaves):
    return {(t, k + d) for t, k in chaves for d in range(-4, 5)}


def group_imoveis(L):
    COND1.clear()
    ads = [x for x in L.values() if x.get("links") and x.get("area") and tipo_ok(x)]
    ads.sort(key=lambda x: (not x.get("visivel"), x.get("detectado") or "", x["id"]))
    grupos = []
    # Índice faixa -> posições dos grupos com algum anúncio nela. O resultado é o mesmo de comparar
    # todos com todos (só ficam de fora pares que conflito() descartaria), em segundos em vez de minutos.
    indice = {}
    for a in ads:
        # Entra no grupo se casar com algum anúncio dele e não conflitar com nenhum; havendo mais de
        # um grupo possível (unidades parecidas no mesmo prédio), fica no de preço mais próximo.
        cand = sorted({i for f in _vizinhas([_faixa(a)]) for i in indice.get(f, ())})
        aptos = [grupos[i] for i in cand
                 if any(pair_ok(a, b) for b in grupos[i]) and not any(conflito(a, b) for b in grupos[i])]
        g = min(aptos, key=lambda g: min(dif_preco(a, b) for b in g)) if aptos else None
        if g is None:
            pos = len(grupos)
            grupos.append([a])
        else:
            pos = next(i for i in cand if grupos[i] is g)
            g.append(a)
        indice.setdefault(_faixa(a), set()).add(pos)
    # A ordem de chegada pode deixar separados grupos que se casam (A~C só se nota depois de B entrar)
    faixas = [{_faixa(x) for x in g} for g in grupos]
    viz = [_vizinhas(f) for f in faixas]
    juntou = True
    while juntou:
        juntou = False
        for i in range(len(grupos)):
            for j in range(i + 1, len(grupos)):
                if viz[i].isdisjoint(faixas[j]):
                    continue
                gi, gj = grupos[i], grupos[j]
                if any(pair_ok(a, b) for a in gi for b in gj) and not any(conflito(a, b) for a in gi for b in gj):
                    gi.extend(gj)
                    faixas[i] |= faixas[j]
                    viz[i] = _vizinhas(faixas[i])
                    del grupos[j], faixas[j], viz[j]
                    juntou = True
                    break
            if juntou:
                break

    # "excluir_predios": prédios que não entram nesta aba nem na média dela (Oase fica só na aba Nova Prochet)
    if CFG.get("excluir_predios"):
        # "@lista" = todos os nomes de uma lista de config.json (Gleba Palhano: "@predios_alvo")
        nomes = [c for n in CFG["excluir_predios"] for c in
                 ([c for a in CONFIG[n[1:]] for c in a["chaves"]] if n.startswith("@") else [n])]
        rx = re.compile(r"\b(?:" + "|".join(re.escape(norm(n)) for n in nomes) + r")\b")
        grupos = [g for g in grupos if not any(rx.search(norm(f"{x.get('predio') or ''} {x.get('titulo') or ''}"))
                                               for x in g)]
    medias = medias_m2(grupos) if CFG.get("media") else {}
    out = []
    for g in grupos:
        vis = [x for x in g if x.get("visivel") and not sumido(x)]
        if not vis:
            continue
        # Só condomínio-alvo (o nome às vezes só aparece depois de abrir o anúncio: "condominio")
        cond = chave_cond(g) if CFG.get("por_condominio") else None
        if CFG.get("por_condominio") and (not alvo(cond) or not contexto_ok(g) or any(
                excluido(x.get("bairro"), x.get("predio"), x.get("titulo")) for x in g)):
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
        # Imóvel já conhecido que ganhou anúncio depois (outro corretor, anúncio refeito): a página mostra
        # "NOVO ANÚNCIO" — sem isso a movimentação do dia ficava invisível (o imóvel não é novo)
        ultimo = max(x["detectado"] for x in vis)
        if ultimo > im["detectado"]:
            im["anuncio_novo"] = ultimo
        fontes, vistos = [], set()
        for x in vis + [x for x in g if not x.get("visivel")]:
            for portal, url in x["links"].items():
                if url and url not in vistos:
                    vistos.add(url)
                    fontes.append({"portal": portal, "url": url, "preco": x.get("preco"), "area": x.get("area"),
                                   "area_anunciada": x.get("area_anunciada"),
                                   "novo": bool(x.get("visivel") and x["detectado"] > im["detectado"]
                                                and x["detectado"] >= iso_dias_atras(3)) or None})
        im["fontes"] = fontes
        # sem nome ou com o do bairro (Vivendas do Arvoredo); prédios-alvo sempre com o nome da lista
        if cond and (apto() or alvo(im["predio"]) != cond):
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
COND1 = {}  # id do anúncio -> chave_cond([anúncio]), durante group_imoveis (comparações em pares)


def cond1(x):
    k = id(x)
    if k not in COND1:
        COND1[k] = chave_cond([x])
    return COND1[k]


def chave_cond(g):
    """Condomínio do grupo (alvo ou outro): o mais citado nos anúncios (ver cond_rec)."""
    pesos = {}
    for nome, p in (cond_rec(x, peso=True) for x in g):
        if nome:
            pesos[nome] = pesos.get(nome, 0) + p
    return max(pesos, key=pesos.get) if pesos else None


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
        gravar(GEOCACHE, json.dumps(self.cache, ensure_ascii=False, indent=0, sort_keys=True))

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


def gravar(path, texto):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(texto)
    tmp.replace(path)


def enxuto(v):
    if isinstance(v, dict):
        return {k: enxuto(x) for k, x in v.items() if x not in (None, "", [], {})}
    if isinstance(v, list):
        return [enxuto(x) for x in v]
    return v


# Fichas dos prédios (scraper/predios.py): cada imóvel ganha "ficha" (o arquivo em data/pagina/predios/), que o
# painel do prédio no mapa abre
PREDIOS_JSON = DATA_DIR / "predios.json"
_INDICE = []


def fichas(imoveis):
    if not _INDICE:
        _INDICE.append(predios.Indice(PREDIOS_JSON))
    idx = _INDICE[0]
    if not idx:
        return
    todas = None
    pasta = PAGINA_DIR / "predios"
    pasta.mkdir(exist_ok=True)
    for im in imoveis:
        fid = idx.casar(im.get("predio"), im.get("bairro"), im.get("endereco"))
        if not fid:
            continue
        nome = fid.replace("/", "-")
        im["ficha"] = nome
        arq = pasta / (nome + ".json")
        if not arq.exists() or arq.stat().st_mtime < predios.modificado(PREDIOS_JSON):
            if todas is None:
                todas = predios.carregar(PREDIOS_JSON)
            gravar(arq, json.dumps(todas[fid], ensure_ascii=False, separators=(",", ":")))


def pagina(state):
    PAGINA_DIR.mkdir(exist_ok=True)
    p = {"atualizado": state["atualizado"], "runs": state["runs"][-1:], "imoveis": enxuto(state["imoveis"])}
    fichas(p["imoveis"])
    gravar(PAGINA_DIR / CFG["dados"], json.dumps(p, ensure_ascii=False, separators=(",", ":")))
    f = PAGINA_DIR / "resumo.json"
    try:
        resumo = json.loads(f.read_text())
    except (OSError, ValueError):
        resumo = {}
    resumo[CFG["dados"]] = len(state["imoveis"])
    gravar(f, json.dumps(resumo, ensure_ascii=False, indent=0, sort_keys=True))


# ---------- principal ----------
FONTES_AO_MESMO_TEMPO = 8
FILA_DA_FONTE = {"88imoveis": "arbo"}  # mesma API (api-site.arboimoveis.com.br): uma depois da outra

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
    ("humaita", lambda f: _scan_kenlo("humaita", f), False),
    ("cruciol", lambda f: _scan_kenlo("cruciol", f), False),
    ("arbo", scan_arbo, False),
    ("88imoveis", scan_88imoveis, False),
    ("loft", scan_loft, False),
    ("galmo", scan_galmo, False),
    ("x5", scan_x5, False),
]


def main(perfil):
    usar_perfil(perfil)
    FORA.clear()
    DATA = DATA_DIR / CFG["dados"]
    state = json.loads(DATA.read_text()) if DATA.exists() else {"runs": [], "listings": {}}
    L = state["listings"]
    initial = not L
    AREAS_PREDIO.clear()
    AREAS_PREDIO.update(state.get("areas_predio") or {})
    # Varredura completa por portal paginado, uma por dia (ver PORTAIS_PAGINADOS)
    hoje = (NOW - timedelta(hours=3)).strftime("%Y-%m-%d")
    feitas = dict(state.get("completa_portais") or {})
    if state.pop("completa", None) == hoje:  # formato antigo: um dia para todos os portais
        feitas.update({p: hoje for p in PORTAIS_PAGINADOS})
    tentativas = {p: t for p, t in (state.get("completa_tentativas") or {}).items() if t[0] == hoje}
    COMPLETA.clear()
    if not initial:
        # "completa_dias": de quantos em quantos dias cada portal faz a completa (padrão 1). A OLX faz a cada
        # 3: é a busca mais longa (~25 min) e serve só para renovar "visto"; quem some é conferido abrindo a
        # página do anúncio depois de 7 dias sem aparecer (verificar_sumidos), e os novos vêm em toda varredura.
        dias = CFG.get("completa_dias", {})
        def em_dia(p):
            f = feitas.get(p)
            return bool(f) and (datetime.fromisoformat(hoje) - datetime.fromisoformat(f)).days < dias.get(p, 1)
        COMPLETA.update(p for p in PORTAIS_PAGINADOS
                        if not em_dia(p) and (tentativas.get(p) or [hoje, 0])[1] < 2)
    PAGINAS["completa"] = CFG.get("max_pages_completa", 30)
    PAGINAS["normal"] = CFG["max_pages_initial"] if initial else CFG["max_pages"]
    max_pages = PAGINAS["normal"]
    BUSCAS.clear()
    REPETIR.clear()
    log(f"Varredura {perfil} {NOW_ISO} ("
        + ("inicial" if initial else f"completa: {', '.join(sorted(COMPLETA))}" if COMPLETA else "incremental") + ")")
    conhecidos = {origem(x) for x in L.values()}
    CONHECIDOS.clear()
    CONHECIDOS.update(L)

    found = {}
    status = {}
    coletas = {}  # fonte -> anúncios que ela achou (cada fonte escreve só na sua; ver juntar)

    def rodar(chave, fn, paginado):
        coleta = coletas.setdefault(chave, {})
        try:
            st = fn(L, max_pages, coleta) if paginado else {chave: fn(coleta)}
        except Exception as e:  # noqa: BLE001 — um portal quebrado não derruba os outros
            log(f"  {chave}: erro {e!r}")
            st = {chave: "falhou"}
        status.update(st)

    def juntar():
        """Junta as coletas em found na ordem de SCANNERS (resultado igual ao de rodar uma fonte por vez)."""
        for chave, _, _ in SCANNERS:
            for k, rec in (coletas.get(chave) or {}).items():
                if k not in found:
                    rec["origem"] = chave
                    found[k] = rec

    def fila(itens):
        for chave, fn, paginado in itens:
            if chave in FORA_DO_AR:
                log(f"  {chave}: pulado ({FORA_DO_AR[chave]}, visto antes nesta execução)")
                status[chave] = "bloqueado" if chave in BLOQUEADO else "falhou"
                continue
            rodar(chave, fn, paginado)

    # Sites diferentes ao mesmo tempo; cada site continua com uma requisição por vez e as mesmas pausas.
    # Fontes do mesmo servidor ficam na mesma fila (FILA_DA_FONTE).
    filas = {}
    for item in SCANNERS:
        filas.setdefault(FILA_DA_FONTE.get(item[0], item[0]), []).append(item)
    with ThreadPoolExecutor(max_workers=FONTES_AO_MESMO_TEMPO) as ex:
        for f in [ex.submit(fila, itens) for itens in filas.values()]:
            f.result()
    juntar()

    # Segunda tentativa, no fim: buscas paginadas que não responderam (403/429 costuma passar depois de
    # alguns minutos, com sessão nova) e imobiliárias que falharam inteiras.
    falhas = [(c, fn) for c, fn, pag in SCANNERS if not pag and status.get(c) == "falhou" and c not in FORA_DO_AR]
    if REPETIR or falhas:
        partes = ([f"{len(REPETIR)} buscas ({', '.join(sorted({p for p, _ in REPETIR}))})"] if REPETIR else []) + [
            c for c, _ in falhas]
        log(f"  segunda tentativa: {', '.join(partes)}")
        time.sleep(20)
        nova_sessao()
        for portal, refazer in list(REPETIR):  # refazer escreve na coleta da fonte (ver conta_busca)
            try:
                n = refazer()
            except Exception as e:  # noqa: BLE001
                log(f"  {portal}: erro {e!r}")
                n = 0
            if n:
                BUSCAS[portal][1] += 1
        status.update({p: status_buscas(p) for p in BUSCAS})
        for chave, fn in falhas:
            rodar(chave, fn, False)
        juntar()
    for p in COMPLETA:
        if status.get(p) == "ok":
            feitas[p] = hoje
        else:
            tentativas[p] = [hoje, (tentativas.get(p) or [hoje, 0])[1] + 1]
    state["completa_portais"] = feitas
    state["completa_tentativas"] = tentativas

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
            novo, velho = rec.get("preco"), cur.get("preco")
            if novo and novo != velho:
                if velho and not 0.4 <= novo / velho <= 2.5:
                    log(f"  preço ignorado {k}: {velho} -> {novo} (aluguel ou erro do anúncio?)")
                    continue
                if velho and not cur.get("preco_anterior"):
                    cur["preco_anterior"] = velho
                cur["preco"] = novo
                if cur.get("preco_anterior") == novo:
                    del cur["preco_anterior"]

    # Grupo Zap: IDs crescem com o tempo, então os maiores são os mais novos. Ao encontrar
    # vários anúncios seguidos mais velhos que o limite, os restantes não precisam ser abertos.
    grupo_new = sorted((k for k in new_ids if found[k]["origem"] == "grupo"), key=lambda k: -int(k[1:]))
    outros_new = [k for k in new_ids if found[k]["origem"] != "grupo"]
    # Perfil com "inicial_mostra_tudo": na estreia todo o estoque vira card (mercado, não só lançamentos).
    tudo = initial and CFG.get("inicial_mostra_tudo")
    det = Detalhes(3 if tudo else 1)
    pontos = pontos_id_data(L)
    old_streak = 0
    for k in grupo_new + outros_new:
        rec = found[k]
        estreante = rec["origem"] not in conhecidos  # primeira varredura deste portal
        limite_dias = CFG["initial_recent_days"] if initial or estreante else CFG["max_age_days_new"]
        decidir = antigo = False
        if rec["origem"] == "grupo":
            if tudo or old_streak < 6:
                decidir = not det.abrir(rec) and not tudo  # sem a data: decide quando a página for lida
                if decidir and not rec.get("publicado"):
                    # página bloqueada: data estimada pelo id (a página é lida depois e corrige)
                    est = data_por_id(k, pontos)
                    if est:
                        rec["publicado"], rec["data_estimada"], decidir = est, True, False
                idade = age_days(rec.get("publicado"))
                old_streak = old_streak + 1 if idade is not None and idade > limite_dias else 0
            recente = tudo or (old_streak < 6 and (age_days(rec.get("publicado")) or 0) <= limite_dias)
            if decidir:
                recente, rec["decidir"] = False, limite_dias
        else:
            idade = age_days(rec.get("publicado"))
            # Sem data de publicação: na estreia do portal não dá para saber se é novo, então o
            # estoque atual entra só como referência (links extras em imóveis já mostrados).
            recente = tudo or (idade <= limite_dias if idade is not None else not estreante)
            # Portal estreando: o estoque dele entra na página, mas como anterior (não "novo"),
            # para não encher a seção de novos com o que já estava à venda
            antigo = estreante and not tudo and not recente
            if antigo:
                recente = True
            if recente and rec["origem"] == "olx":
                olx_detail(rec)
                time.sleep(1)
        corrige_area(rec)
        if not area_ok(rec.get("area")):  # a área útil do texto ficou fora da faixa do perfil
            recente = False
        rec["visivel"] = recente and not rec.get("retirado")
        if not recente and not decidir:
            rec.pop("foto", None)
            rec.pop("titulo", None)
        rec["detectado"] = min(rec.get("publicado") or NOW_ISO, iso_dias_atras(1)) if antigo else NOW_ISO
        rec["visto"] = NOW_ISO
        L[k] = rec

    # Anúncios novos que esperavam a página para saber a data (bloqueio do Zap): decididos pela data
    # estimada pelo id, sem esperar mais (a página continua pendente e corrige a data quando abrir)
    presos = 0
    for rec in L.values():
        if "decidir" in rec and not rec.get("publicado"):
            est = data_por_id(rec["id"], pontos)
            if not est:
                continue
            rec["publicado"], rec["data_estimada"] = est, True
            ok = age_days(est) <= rec.pop("decidir") and area_ok(rec.get("area")) and not rec.get("retirado")
            rec["visivel"] = ok
            presos += 1
            if ok:
                rec["detectado"] = NOW_ISO
            else:
                rec.pop("foto", None)
                rec.pop("titulo", None)
    if presos:
        log(f"  {presos} anúncios que esperavam a página decididos pela data estimada pelo id")

    # Páginas de anúncio que ficaram para depois (bloqueio do Zap), só dos que estão na página
    pend = sorted((x for x in L.values() if x.get("detalhe_pendente") and (x.get("visivel") or x.get("decidir"))
                   and x["id"] not in new_ids), key=lambda x: x.get("visto") or "", reverse=True)
    for rec in pend[:DETALHES_POR_VARREDURA]:
        estimada = rec.get("data_estimada")
        if not det.abrir(rec):
            if det.falhas >= 3:
                break
            continue
        if estimada and not rec.get("data_estimada") and rec.get("visivel"):
            # a data lida na página substitui a estimada: anúncio velho sai da lista de novos
            if (age_days(rec.get("publicado")) or 0) > CFG["max_age_days_new"] or not area_ok(rec.get("area")):
                rec["visivel"] = False
        if "decidir" in rec:  # anúncio novo que esperava a data de publicação
            idade = age_days(rec.get("publicado"))
            ok = (idade is None or idade <= rec.pop("decidir")) and area_ok(rec.get("area")) and not rec.get("retirado")
            rec["visivel"] = ok
            if ok:
                rec["detectado"] = NOW_ISO  # aparece como novo agora
            else:
                rec.pop("foto", None)
                rec.pop("titulo", None)

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

    # Imobiliárias de lista inteira que responderam: quem não aparece nelas há 3 dias saiu (ver sumido)
    state.pop("portal_ok", None)
    COMPLETO.clear()
    COMPLETO.update(state.get("completo_em") or {})
    for chave, st in status.items():
        if st == "ok" and chave not in ("zap", "vivareal", "olx", "chavesnamao"):
            COMPLETO[chave] = NOW_ISO
    state["completo_em"] = dict(COMPLETO)
    verificar_sumidos(L, det)
    # Guardados há mais de 90 dias sem aparecer: fora do estado (já não servem nem para a média)
    for k in [k for k, x in L.items() if (x.get("visto") or x.get("detectado") or NOW_ISO) < iso_dias_atras(90)]:
        del L[k]

    for x in L.values():
        if x.get("predio"):
            x["predio"] = limpa_predio(x["predio"])
    aprende_por_preco(L)
    aplica_areas_predio(L)
    state["areas_predio"] = {k: AREAS_PREDIO[k] for k in sorted(AREAS_PREDIO)}
    state["imoveis"] = group_imoveis(L)
    novos = sum(1 for im in state["imoveis"] if im["detectado"] == NOW_ISO)
    run = {"em": NOW_ISO, "novos": novos, "portais": status, "inicial": initial}
    if FORA:  # bairros descartados pela zona, para revisar a lista em config.json
        run["fora_da_zona"] = dict(sorted(FORA.items(), key=lambda kv: -kv[1])[:40])
    state["runs"].append(run)
    state["runs"] = state["runs"][-60:]
    state["atualizado"] = NOW_ISO
    DATA.parent.mkdir(parents=True, exist_ok=True)
    gravar(DATA, json.dumps(state, ensure_ascii=False, indent=1))
    pagina(state)
    log(f"Status: {status}. Imóveis novos: {novos}. Imóveis na página: {len(state['imoveis'])}. Anúncios armazenados: {len(L)}")
    return not all(v == "falhou" for v in status.values())


if __name__ == "__main__":
    perfis = sys.argv[1:] or list(CONFIG["perfis"])
    falhos = []
    predios.atualizar(PREDIOS_JSON, requests.Session(impersonate="chrome"), log)  # 1x por semana
    for p in perfis:
        try:
            if not main(p):
                falhos.append(p)
        except Exception as e:  # noqa: BLE001 — os outros perfis seguem; os dados deles são salvos
            log(f"Perfil {p}: erro {e!r}")
            import traceback
            traceback.print_exc()
            falhos.append(p)
    if falhos:
        sys.exit(f"Todos os portais falharam: {', '.join(falhos)}")
