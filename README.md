# Radar Imóveis

Varredura automática (2× ao dia) de imóveis à venda em Londrina/PR, em duas abas:

- **Gleba Palhano** — apartamentos de 100–200 m² na Gleba Palhano, Santa Rosa, Guanabara e arredores.
- **Casas em condomínio** — casas e terrenos em condomínio fechado na zona sul (zona norte e leste ficam de fora).

Fontes: Zap Imóveis, Viva Real, OLX e as imobiliárias Raul Fulgêncio, Gleba Imóveis, Santamerica, Perez, Catuaí e Inglaterra.

- Página: https://pedriali.github.io/radar-imoveis/
- Busca: `scraper/radar.py` (configuração em `scraper/config.json`: cada item de `perfis` é uma busca/aba). `python scraper/radar.py casas` roda só uma.
- Zona sul: em condomínio o "bairro" do anúncio costuma ser o nome do condomínio, então `perfis.casas.bairros` lista bairros **e** condomínios da zona sul; `excluir` derruba condomínios da zona leste/norte, assim como anúncios que dizem "zona/região norte ou leste". Os bairros descartados em cada varredura ficam em `runs[].fora_da_zona` de `docs/data/casas.json`, para revisar a lista.
- Dados: `docs/data/listings.json` (apartamentos) e `docs/data/casas.json` — `listings` guarda cada anúncio; `imoveis` é o que a página mostra, com os anúncios do mesmo apartamento (em portais diferentes ou repetidos no mesmo portal) agrupados num card só. Clicar no card mostra todos os anúncios.
- Agendamento: `.github/workflows/radar.yml` (08h e 20h, horário de Brasília). Para rodar agora: aba **Actions → varredura → Run workflow**.

Portal estreando: anúncios com data de publicação seguem a regra da varredura inicial (só os da última semana viram card). Os sem data (Gleba, Santamerica, Catuaí, Inglaterra) entram como estoque de referência — não viram card, mas aparecem como anúncio extra quando o mesmo apartamento surge em outro lugar. Daí em diante, todo anúncio que aparecer neles é novo.
