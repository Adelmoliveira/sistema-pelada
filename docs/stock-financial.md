# Estoque → Visão Financeira

Consulta gerencial em `GET /stock/financial`, exclusiva de `manager`. O menu usa o submenu de estoque existente. Não há gravação, exportação, alteração de preços/custos, migration ou mudança nos fluxos operacionais.

## Fonte e alcance

O saldo vem de `products.stock`, o estoque operacional disponível: pedidos pendentes podem já ter debitado unidades. Não equivale necessariamente à contagem física ou ao estoque contábil. O preço de venda é `products.price_cents` por unidade avulsa; caixas não mudam a unidade da avaliação.

São exibidos os produtos ativos do bar, inclusive sem saldo, e os inativos com saldo positivo. Inativos são identificados e seu faturamento é apenas teórico, sem disponibilizá-los à venda. A categoria `Material Esportivo` é excluída integralmente. Os totais cobrem todas as linhas exibidas, não apenas a página visível.

Saldo, preço e histórico são consultados por um único SELECT, usando o mesmo snapshot da instrução nos dois bancos. O serviço não assume controle da transação do chamador nem realiza DDL ou escrita. Nenhuma notificação ou integração externa é chamada.

## Custo médio histórico de referência

Para cada reposição (`restocks`), a última correção por ID em `restock_corrections` substitui sua quantidade e custo unitário. A compra original e suas correções não são somadas entre si. Compras integralmente corrigidas para quantidade zero são ignoradas. Todo o histórico disponível é considerado, sem filtro de período.

```text
custo médio em centavos = Σ(quantidade efetiva × custo unitário efetivo)
                         ÷ Σ(quantidade efetiva)
valor estimado em estoque = saldo operacional × custo médio
faturamento potencial = saldo operacional × preço unitário atual
margem bruta potencial = faturamento potencial − valor estimado
margem bruta percentual = margem ÷ faturamento potencial × 100
```

Exemplo: 10 unidades adquiridas a R$ 1,00 e 30 a R$ 3,00 produzem referência de R$ 2,50/unidade. Se o saldo atual for 10, o valor gerencial estimado é R$ 25,00. Isso não prova que as unidades remanescentes custaram esse valor.

O cálculo usa `Decimal`, arredondamento comercial (`ROUND_HALF_UP`) para centavos por linha e percentuais com duas casas. O custo unitário exibido é arredondado somente para apresentação: o valor do estoque usa a média não arredondada. Os consolidados somam os valores monetários arredondados das linhas, preservando a soma exibida. A margem consolidada usa o faturamento do mesmo conjunto com custo, nunca a média dos percentuais individuais. Com faturamento zero, o percentual é não apurado. Margens monetárias negativas permanecem visíveis.

## Classificação e ausência de custo

O resultado utilizável recebe a classificação **Referência histórica estimada**. O produto inteiro fica **Sem custo apurado** se houver qualquer um destes fatores:

- Ausência de aquisições individuais efetivas.
- Aquisição com custo zero: o modelo não distingue falta de informação de gratuidade.
- Custo/quantidade negativos ou valores ausentes/inconsistentes.
- Compra via solicitação cujo custo foi rateado globalmente, sem discriminação por produto.
- Acréscimo positivo em `stock_adjustments` sem aquisição monetária vinculada.
- Saldo atual superior às quantidades adquiridas rastreadas.

Não são aproveitadas apenas as compras conhecidas de um produto com histórico misto: isso esconderia a parte sem custo. Não há fallback automático para `products.cost_cents`, pois esse campo é editável e normalmente reflete o último custo, não uma apuração ponderada. Quando o histórico não permite apuração e não há reposição individual corrigível, o gestor pode confirmar uma referência independente, com validade conservadora e auditoria imutável. Consulte [Regularização de custos](stock-cost-regularization.md).

A origem das solicitações legadas é identificada pelo prefixo emitido pelo sistema em `restocks.notes`: `Reposição via solicitação #`. Mesmo uma correção posterior não comprova discriminação individual do documento de aquisição. Ajustes de correção de compra, cujo motivo começa por `Correção da reposição #`, não são tratados como acréscimos sem custo: seu efeito já está na compra corrigida.

Esses prefixos são evidências disponíveis no modelo atual, não chaves relacionais formais. Notas ou motivos alterados manualmente, registros importados sem origem e padrões históricos diferentes exigem revisão humana. Um aumento sem custo bloqueia conservadoramente a referência mesmo que possa ter sido consumido depois; não há rastreamento de lotes para provar isso.

## Totais e ressalvas

O faturamento potencial total inclui os produtos sem custo e os inativos com saldo positivo. Custo e margem abrangem somente produtos com saldo positivo e referência utilizável. São apresentados separadamente os produtos, unidades e faturamento potencial excluídos do custeio. Havendo saldo sem custo, os totais de custo e margem são identificados como parciais. Se nenhum saldo positivo tiver custo apurado, esses totais são **não apurados**, não zero. Estoque vazio tem valor zero e percentual de margem não apurado.

Não há reconstrução do custo de abertura, média móvel perpétua, FIFO, atribuição das saídas aos lotes ou prova de que um saldo inicial foi inteiramente consumido. Mesmo um histórico utilizável fornece apenas uma referência estimada, não custo contábil certificado. A margem potencial não é lucro realizado, não garante vendas e não desconta impostos, taxas, despesas ou perdas futuras.

## Testes e homologação

```sh
PYTHONPYCACHEPREFIX=/tmp/gpcta-pycache .venv/bin/python -m unittest tests.test_stock_financial -q
```

Os testes próprios usam o schema base em memória ou em arquivo temporário, sem inicializador/migrations. Cobrem ponderação, precisão, correções sucessivas, compras anuladas, custo desconhecido, rateio, acréscimos, ausência de histórico, consolidados parciais, inativos, esportivos, margens negativas, preço zero, estado vazio, permissões, escape HTML e ausência de gravação.

Para homologar: acessar com gerente, confirmar o submenu aberto, comparar duas compras individuais de preços diferentes, verificar avisos de produtos sem custo e os totais parciais. Testar um produto inativo com saldo, uma compra por solicitação e um saldo zerado. Conferir que staff/cliente não acessam a rota. O POST na consulta financeira não possui handler de persistência; a confirmação de referência usa rota própria, protegida e auditada. A atualização dos dados ocorre ao recarregar a consulta; não há edição ou atualização automática do estoque. O procedimento de migration, confirmação e rollback está no guia de regularização.
