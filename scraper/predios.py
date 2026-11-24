"""Fichas de prédios e condomínios: Diário dos Imóveis de Londrina (blog no Blogger, ~1.000 posts desde 2012).

Cada post é um prédio: endereço, construtora, entrega, torres/pavimentos, aptos por andar, áreas, fotos e plantas.
O blog inteiro vem pelo feed oficial do Blogger (~8 requisições), uma vez por semana, para
docs/data/predios.json. A página mostra a ficha no mapa (painel do prédio); as imagens são carregadas do próprio
blog (não são copiadas para o repositório), com o link do post.

O texto dos posts é escrito à mão (quebras de linha soltas, "Const.:", "Área útil de 127m2"): os campos saem
por padrões no texto achatado; o que não bate fica de fora e o texto do post vai junto na ficha.
Precisa continuar compatível com o Python 3.9 do Mac.
"""
import difflib
import html as htmllib
import json
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

BLOG = "https://www.diariodosimoveisdelondrina.com.br"
FEED = BLOG + "/feeds/posts/default?alt=json&max-results=150&start-index={}"
VALIDADE = timedelta(days=7)
OUTRAS_CIDADES = re.compile(r"\b(camb[eé]|ibipor[aã]|rol[aâ]ndia|arapongas|apucarana|maring[aá]|tamarana|sert[aã]n[oó]polis)\b", re.I)


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s).strip()


# Palavras que não distinguem prédios ("Ed. Casa Palhano" = "Casa Palhano" = "Edifício Casa Palhano")
_GENERICAS = r"\b(ed|edif|edificio|edificios|resid|residencial|residence|residences|cond|condominio|condominios|" \
             r"horizontal|vertical|torre|torres|the|le|la|les|di|de|do|da|dos|das|del|l|e|predio|apto|apartamentos|" \
             r"plaenge|yoshii|vectra|galmo|mobiliado|mobiliada)\b"


def chave(nome):
    s = _norm(nome).replace("'", " ").replace("’", " ")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(_GENERICAS, " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def _texto(conteudo):
    c = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr|span)>", "\n", conteudo)
    c = re.sub(r"(?is)<(script|style).*?</\1>", " ", c)
    t = htmllib.unescape(re.sub(r"<[^>]+>", " ", c))
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return re.sub(r"\s*\n\s*", "\n", t).strip()


def _num(s):
    s = s.strip().rstrip(".,")
    if re.search(r"\d\.\d{3}(,|$)", s):  # 2.362,50
        s = s.replace(".", "")
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _int(m):
    return int(m.group(1)) if m else None


def _areas(plano, tipo):
    v = []
    for m in re.finditer(r"[áa]rea\s+(?:" + tipo + r")\s*(?:aprox\.?\s*)?(?:de\s*)?(?:aprox\.?\s*)?(?:de\s*)?(\d[\d.,]*)", plano, re.I):
        n = _num(m.group(1))
        if n and 15 <= n <= 5000 and n not in v:
            v.append(round(n, 2))
    return v


def _imagem(src):
    """URL da imagem do Blogger sem o tamanho (a página escolhe: miniatura ou grande)."""
    src = src.replace("http://", "https://")
    return re.sub(r"/(s\d+(-c)?|w\d+-h\d+(-[a-z]+)?)/", "/s{S}/", src, count=1) if "googleusercontent" in src \
        or "bp.blogspot" in src else src


def ficha(entry):
    titulo = entry["title"]["$t"].strip()
    url = next(l["href"] for l in entry["link"] if l["rel"] == "alternate")
    bruto = entry.get("content", {}).get("$t", "")
    texto = _texto(bruto)
    plano = re.sub(r"\s+", " ", texto)
    f = {"id": re.sub(r"^https?://[^/]+/|\.html$", "", url), "nome": titulo, "url": url,
         "publicado": entry["published"]["$t"][:10]}
    m = re.search(r"Const(?:rutora|ru[çc][ãa]o)?\.?\s*:?\s*(.+?)(?=\s+(?:Entrega|Lan[çc]amento|Matr[ií]cula|\d{2}\s|Fonte|Área|Area|Endere[çc]o|Projeto|Previs[ãa]o)\b|$)", plano, re.I)
    if m:
        f["construtora"] = m.group(1).strip(" :-.")[:80]
    m = re.search(r"Entrega\s*:?\s*((?:\d[ªa]\s*fase\s*:?\s*)?(?:\d{1,2}/\d{1,2}/\d{4}|[A-Za-zç]{3,9}\.?\s*/?\s*\d{4}|\d{1,2}/\d{4}|\d{4}))", plano, re.I)
    if m:
        f["entrega"] = m.group(1).strip()
        ano = re.search(r"(19|20)\d{2}", f["entrega"])
        if ano:
            f["ano"] = int(ano.group(0))
    inicio = re.split(r"\bConst|\bEntrega", plano)[0]
    nome_ = re.sub(r"^(ED\.?|EDIF[ÍI]CIO|EDIF\.|COND\.?|CONDOM[ÍI]NIO|RESID\.?|RESIDENCIAL)\s+", "", titulo, flags=re.I)
    if nome_ and inicio.upper().startswith(nome_.upper()):
        inicio = inicio[len(nome_):]
    if len(inicio) < 140:
        end = re.sub(r"CEP\s*[\d.-]+|\b(Londrina|Pr|PR)\b\.?|/", " ", inicio)
        end = re.sub(r"\s+", " ", end).strip(" -,/")
        if end:
            f["endereco"] = end
    f["torres"] = _int(re.search(r"(\d{1,2})\s*torres?\b", plano, re.I))
    f["pavimentos"] = _int(re.search(r"(\d{1,2})\s*pavimentos(?!\s*tipo)", plano, re.I)) \
        or _int(re.search(r"(\d{1,2})\s*andares", plano, re.I))
    f["aptos_andar"] = _int(re.search(r"(\d{1,2})\s*aptos?\.?\s*(?:por|p/)\s*andar", plano, re.I))
    f["elevadores"] = _int(re.search(r"(\d{1,2})\s*elevadores", plano, re.I))
    f["unidades"] = _int(re.search(r"(\d{1,4})\s*(?:unidades|casas|lotes)\b", plano, re.I))
    f["area_util"] = _areas(plano, r"[úu]til|privativa")
    f["area_total"] = _areas(plano, r"total(?!\s+constru)")
    m = re.search(r"(?:Terreno|Lote)\s*(?:com|de)?\s*(\d[\d.,]*)\s*m", plano, re.I)
    if m:
        f["terreno"] = _num(m.group(1))
    m = re.search(r"Fonte\s*:?\s*(https?://\S+)", plano, re.I)
    if m:
        f["fonte"] = m.group(1).rstrip(".,;)")
    if OUTRAS_CIDADES.search(inicio) or any(OUTRAS_CIDADES.search(c.get("term", "")) for c in entry.get("category", [])):
        f["outra_cidade"] = True
    imgs = []
    for src in re.findall(r"<img[^>]+src=\"([^\"]+)\"", bruto):
        u = _imagem(src)
        if u not in imgs and not re.search(r"(?i)icon|logo|emoji|\.gif$", u):
            imgs.append(u)
    f["imagens"] = imgs
    f["texto"] = texto[:3000]
    return {k: v for k, v in f.items() if v not in (None, "", [], {}, 0)}


def baixar(sessao, log=print):
    entradas, inicio = [], 1
    while True:
        r = sessao.get(FEED.format(inicio), timeout=60)
        r.raise_for_status()
        es = r.json()["feed"].get("entry", [])
        if not es:
            break
        entradas += es
        inicio += len(es)
        time.sleep(2)
    log(f"  prédios: {len(entradas)} posts no Diário dos Imóveis")
    return entradas


def atualizar(path, sessao, log=print):
    """Baixa de novo as fichas se o arquivo tiver mais de 7 dias. Falha não interrompe a varredura."""
    try:
        atual = json.loads(path.read_text())
        quando = datetime.fromisoformat(atual["atualizado"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - quando < VALIDADE:
            return
    except (OSError, ValueError, KeyError):
        pass
    try:
        fichas = [ficha(e) for e in baixar(sessao, log)]
    except Exception as e:  # noqa: BLE001
        log(f"  prédios: não atualizou ({e!r})")
        return
    dados = {"atualizado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "fonte": BLOG,
             "predios": {f["id"]: f for f in fichas}}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(path)


_NAO_LOCAL = {"rua", "avenida", "av", "r", "alameda", "al", "travessa", "jardim", "jd", "gleba", "fazenda", "parque",
              "vila", "conjunto", "residencial", "londrina", "centro", "zona", "sul", "norte", "leste", "oeste"}


def _palavras(s):
    return {w for w in re.findall(r"[a-z0-9]+", _norm(s)) if len(w) >= 4 and w not in _NAO_LOCAL}


class Indice:
    """Nome de prédio do radar -> id da ficha. Igual pela chave; senão (começo do nome igual, ou nome parecido —
    erro de digitação nos anúncios), só com o bairro ou a rua do imóvel no endereço/texto da ficha, e com uma
    ficha só. Posts repetidos: fica o mais completo; outra cidade só se não houver de Londrina."""

    def __init__(self, path):
        try:
            predios = json.loads(path.read_text())["predios"]
        except (OSError, ValueError, KeyError):
            predios = {}
        por = {}
        for f in predios.values():
            if not any(f.get(c) for c in ("construtora", "entrega", "pavimentos", "area_util", "torres", "aptos_andar",
                                          "unidades", "terreno")) \
                    and not re.match(r"(?i)(ed|edif|cond|resid|loteamento|jardim|parque|vila|residencial)\b", f.get("nome", "")):
                continue  # post que não é de um prédio (notícia, artigo)
            k = chave(f.get("nome"))
            if len(k) >= 3:
                por.setdefault(k, []).append(f)
        peso = lambda f: (not f.get("outra_cidade"), len(f.get("imagens", [])) + len(f) * 3)
        self.chaves = {k: max(v, key=peso)["id"] for k, v in por.items()}
        self.local = {f["id"]: _palavras((f.get("endereco") or "") + " " + (f.get("texto") or "")[:400])
                      for f in predios.values()}
        self.cache = {}

    def __bool__(self):
        return bool(self.chaves)

    def casar(self, nome, bairro=None, endereco=None):
        if not nome:
            return None
        rua = (endereco or "").split(",")[0]
        ck = (nome, bairro, rua)
        if ck in self.cache:
            return self.cache[ck]
        k = chave(nome)
        achou = self.chaves.get(k) if len(k) >= 3 else None
        if not achou and len(k) >= 4:
            ctx = _palavras(bairro) | _palavras(rua)
            ctx_bairro = _palavras(bairro) | {"palhano", "centro"}
            perto = lambda i: bool(ctx & self.local.get(i, set()))
            # começo igual: nome curto (Arch, Spot) só se o resto for o bairro ("Arch Palhano", "Spot Centro")
            cand = {i for c, i in self.chaves.items() if len(c) >= 4 and (c.startswith(k) or k.startswith(c))
                    and (min(len(c), len(k)) >= 6 or (c[len(k):] or k[len(c):]) in ctx_bairro)}
            if not cand:
                cand = {self.chaves[c] for c in difflib.get_close_matches(k, self.chaves, n=3, cutoff=0.86)}
            cand = {i for i in cand if perto(i)}
            achou = cand.pop() if len(cand) == 1 else None
        self.cache[ck] = achou
        return achou
