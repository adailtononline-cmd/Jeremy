# Jeremy IPTV

Playlist personalizada para Sparkle TV, separada por país. Gerada automaticamente a partir das listas públicas do [iptv-org](https://github.com/iptv-org/iptv).

## Como usar

- Playlist: `https://raw.githubusercontent.com/adailtononline-cmd/Jeremy/main/jeremy.m3u`
- Guia de programação (EPG): já vem indicado dentro da playlist (`epg.xml`).

## Países (grupos)

United States, Canada, Australia, New Zealand, Brazil, United Kingdom e England. A França é excluída. Contagem atual em `report.json`.

Observações:
- "United Kingdom" reúne canais britânicos sem indicação clara de Inglaterra ou Escócia; "England" tem só os canais que dizem isso no nome.
- Dentro de cada país a ordem é por categoria e depois por nome.

## Metadados de cada canal

`tvg-id`, `tvg-name`, `tvg-logo`, `tvg-language`, `tvg-country`, `tvg-category` e `group-title` (país). A categoria (News, Movies, Kids...) fica em `tvg-category`.

- **Logo:** conferida no teste; se o link original está quebrado, usa outro logo do catálogo do iptv-org para o mesmo canal. Sem logo confiável, fica vazio (lista em `reports/missing_logos.csv`).
- **Guia de programação:** só existe para os canais que as fontes gratuitas de EPG cobrem (ver `report.json`, campo `epg`). Canal sem guia não mostra programação.
- Canais encerrados ou adultos, segundo o catálogo do iptv-org, são excluídos.

## Teste de links

A cada execução cada link é testado por HTTP e só entra na playlist se responder como vídeo. Os rejeitados ficam em `reports/rejected_links.csv`. **Isso não prova que o canal toca na Sparkle TV**, e alguns canais mudam de status de uma execução para outra.

## Atualização

Roda toda segunda-feira, a cada push na `main` e manualmente (Actions → Build Jeremy IPTV).

## Números aprovados pelo Jeremy

Os números aprovados no Sparkle TV se referem à ordem da playlist antiga. Antes de excluir canais por posição, conferir com aquela versão; sem o instantâneo original não dá para casar com segurança.
