# Radar Imóveis

Varredura automática (2× ao dia) de apartamentos à venda em Londrina/PR — Gleba Palhano, Santa Rosa, Guanabara e arredores, 100–200 m².

Fontes: Zap Imóveis, Viva Real, OLX e as imobiliárias Raul Fulgêncio, Gleba Imóveis, Santamerica, Perez, Catuaí e Inglaterra.

- Página: https://pedriali.github.io/radar-imoveis/
- Busca: `scraper/radar.py` (configuração em `scraper/config.json`)
- Dados: `docs/data/listings.json` — `listings` guarda cada anúncio; `imoveis` é o que a página mostra, com os anúncios do mesmo apartamento (em portais diferentes ou repetidos no mesmo portal) agrupados num card só. Clicar no card mostra todos os anúncios.
- Agendamento: `.github/workflows/radar.yml` (08h e 20h, horário de Brasília). Para rodar agora: aba **Actions → varredura → Run workflow**.

Portal estreando: anúncios com data de publicação seguem a regra da varredura inicial (só os da última semana viram card). Os sem data (Gleba, Santamerica, Catuaí, Inglaterra) entram como estoque de referência — não viram card, mas aparecem como anúncio extra quando o mesmo apartamento surge em outro lugar. Daí em diante, todo anúncio que aparecer neles é novo.
