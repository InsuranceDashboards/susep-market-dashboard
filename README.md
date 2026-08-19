# Brazilian Insurance Market Radar

Dashboard público e bilíngue do mercado segurador brasileiro, elaborado a partir de dados públicos do Sistema de Estatísticas da SUSEP (SES).

## Acesso

Após habilitar o GitHub Pages, o endereço público será:

`https://insurancedashboards.github.io/susep-market-dashboard/`

O seletor `PT / EN` no cabeçalho alterna o idioma sem alterar os filtros.

## Primeira publicação no GitHub

1. Extraia o pacote no computador.
2. No repositório, clique em **Add file → Upload files**.
3. Arraste todos os itens extraídos, inclusive as pastas `.github`, `data` e `docs`.
4. Clique em **Commit changes**.
5. Abra **Settings → Pages** e selecione **GitHub Actions** em **Source**.
6. Abra a aba **Actions**, acompanhe o fluxo `Atualizar e publicar dashboard` e aguarde o indicador verde.

Não envie a Base Completa, os ZIPs da SUSEP ou credenciais para o repositório.

## Conteúdo

- produção mensal desde 2019;
- visão anual, mês isolado e acumulado no ano;
- seleção de produtos, subprodutos, ramos e múltiplas seguradoras;
- seguradora foco configurável;
- KPIs executivos, rankings, participação e posição;
- indicadores técnicos, de resseguro e Gross/Net Ratios;
- rankings financeiros por prêmio emitido, índice combinado, lucro líquido e ROE.

## Atualização automática

O fluxo em `.github/workflows/pages.yml` consulta periodicamente a Base Completa da SUSEP, valida os arquivos, recalcula o painel e publica uma nova versão apenas quando a competência ou os arquivos-fonte mudam. A base ZIP de aproximadamente 557 MB não é armazenada no repositório.

Também é possível executar manualmente o fluxo `Atualizar e publicar dashboard` na aba **Actions** do GitHub.

## Fonte e independência

Fonte dos dados: Sistema de Estatísticas da SUSEP (SES), base FIP.

Este é um painel independente elaborado a partir de dados públicos. Não possui vínculo, chancela, patrocínio ou validação da SUSEP. Os dados podem ser revisados ou republicados pela fonte oficial. Indicadores e rankings são cálculos do painel.

- [Consulta SES/SUSEP](https://www2.susep.gov.br/menuestatistica/SES/premiosesinistros.aspx?id=54)
- [Dados abertos da SUSEP](https://www.gov.br/susep/pt-br/acesso-a-informacao/dados-abertos)

## Metodologia resumida

- Prêmio direto: `premio_direto`.
- Prêmio emitido na visão de produtos: `premio_emitido2`.
- Prêmio ganho: `premio_ganho`.
- Sinistralidade: `sinistro_ocorrido ÷ premio_ganho`.
- Índice combinado financeiro: negativo da soma de sinistros ocorridos, custos de aquisição, outras receitas/despesas operacionais, resultado com resseguro, despesas administrativas e tributos, dividido pelos prêmios ganhos.
- Lucro líquido: conta 518 do quadro 23.
- ROE anualizado: lucro líquido YTD × `12 ÷ mês de referência`, dividido pelo patrimônio líquido de fechamento.

Os detalhes adicionais estão documentados no rodapé do próprio dashboard.
