# Conferência mensal do Bar

## Regra preservada

Registrar salva a contagem e as divergências, sem alterar `products.stock`. O gerente deve abrir os detalhes e confirmar a aplicação. O mês é uma referência administrativa, não uma reconstrução do saldo no fechamento daquele mês.

A comparação continua usando o saldo operacional disponível. Pedidos pendentes podem já ter debitado esse saldo; itens comprometidos devem ser separados antes da contagem. Não há alteração no fluxo de vendas nem tentativa de inferir reservas físicas a partir de pagamentos.

A conferência exclui a categoria de materiais esportivos, assim como a tela de estoque.

## Identidade, histórico e concorrência

`stock_conference_applications` representa a aplicação, com uma chave única por `conference_id`, data e responsável. A inserção dessa aplicação, as atualizações de saldo, os registros em `stock_adjustments` e a auditoria pertencem à mesma transação. Os ajustes novos têm `conference_id` explícito. Alertas são executados após o commit; falha no alerta não desfaz nem oculta o ajuste confirmado.

No PostgreSQL, a operação bloqueia a conferência e os produtos com `FOR UPDATE`, em ordem de ID. No SQLite, `BEGIN IMMEDIATE` adquire o bloqueio de escrita antes das leituras. A restrição única da aplicação e a atualização condicional do estoque complementam esses bloqueios. Exclusão e aplicação adquirem o mesmo bloqueio da conferência.

`products.stock_revision` é incrementado pelo banco quando saldo, situação ativa ou categoria mudam. O formulário leva um retrato assinado do conjunto de produtos, saldos e versões. Os itens armazenam a versão registrada. Uma saída seguida de devolução ao mesmo saldo também invalida a conferência. Outros campos do produto não invalidam a contagem.

A proteção pressupõe os triggers habilitados. Escritas administrativas que desabilitem triggers ou alterem versões manualmente ficam fora dessa garantia.

## Correção de reposição e edição manual

`src/services/stock_writes.py` utiliza a mesma fronteira transacional da conferência. A correção de reposição bloqueia primeiro o produto e depois a reposição; somente então relê o saldo e a última correção. Aplica a diferença com `stock=stock+?`, valida a versão e a disponibilidade, e grava correção e ajuste na mesma transação. Uma leitura anterior com saldo 20 não é reutilizada: se a conferência aplicar 17, uma correção de +2 resulta em 19, com auditoria 17→19. Duas correções são serializadas e calculam suas diferenças sobre a última correção efetivamente confirmada.

A edição manual envia `stock_revision` no formulário. Dentro da transação, o produto é bloqueado e a versão conferida antes de atualizar qualquer campo. Uma leitura antiga é recusada inclusive após saída/devolução ao mesmo saldo. O gerente deve recarregar, revisar o saldo e enviar novamente. Formulários abertos antes da implantação, sem versão, também precisam ser recarregados. Ajustes manuais confirmados e seus históricos são atômicos; falha posterior no alerta não desfaz o ajuste.

Essas alterações não exigem outra migration: utilizam a versão e os triggers já previstos pelo hotfix. A proteção é de estoque; alterações simultâneas apenas em nome/preço/foto não são versionadas por esse mecanismo. Não há estorno nem aplicação retroativa.

## Compatibilidade histórica

Não há backfill de aplicações, alterações de contagens antigas ou aplicação retroativa. O código reconhece aplicações legadas pelo texto exato emitido pela versão anterior: `Conferência #<id> aplicada ao estoque.`. Nunca usa apenas o mês para decidir o estado. Eventos alterados manualmente ou gerados por outro formato exigem análise humana antes da implantação.

Conferências antigas pendentes têm `expected_revision` nulo: sua aplicação é bloqueada, pois não é possível provar a ausência de movimentações intermediárias. O gerente deve revisar, excluir a pendente de forma auditada e registrar uma nova contagem atual. Conferências aplicadas, inclusive as reconhecidas pela auditoria legada, não podem ser excluídas. Cancelamento/estorno exige procedimento próprio e não foi implementado.

## Relatórios

O saldo atual continua vindo de `products.stock`. Compras corrigidas, vendas pagas, ajustes positivos e negativos aparecem separadamente; a variação listada inclui esses ajustes.

Correções de reposição já substituem a quantidade da compra no relatório. Seus registros de ajuste não são somados outra vez. Para os registros existentes, essa identificação utiliza o prefixo padronizado `Correção da reposição #` do motivo. Esse prefixo é reservado ao sistema; motivos manuais com o mesmo prefixo exigem revisão. Não há alteração nos registros históricos para classificá-los.

O relatório não constitui um razão completo de estoque: pedidos pendentes e cancelamentos também afetam o saldo operacional. Essa limitação aparece em HTML e PDF. As correções de compra mantêm a regra existente de atribuição à data da compra original.

## Preparação para homologação e implantação

1. Revisar o diff e fazer backup do banco do ambiente de destino pelo procedimento habitual.
2. Em uma cópia isolada de homologação, executar a migration `supabase/migrations/20261018000000_stock_conference_safety.sql` antes de disponibilizar o código novo. A migration é aditiva, cria os triggers e não aplica conferências históricas. O bootstrap legado PostgreSQL não substitui a execução dessa migration.
3. Para SQLite, usar o inicializador explícito `scripts/init_local_db.py --database <caminho-da-copia>`; não há DDL em requests. Nenhum banco existente foi atualizado durante esta implementação.
4. Confirmar a presença do trigger e conferir uma saída/devolução: a versão deve aumentar duas vezes mesmo retornando ao saldo inicial.
5. Com gerente e staff, validar registro sem alteração, detalhes e resumo, aplicação 20→17 e 20→23, diferença zero, bloqueio de repetição e permissões. Verificar o saldo por nova conexão.
6. Validar movimentação intermediária, formulário antigo, rollback de vários produtos e exclusão apenas de pendentes. Conferir aplicações legadas por ID, inclusive meses excluídos/recriados.
7. Reproduzir uma correção que leu saldo 20 antes da conferência: aplicar 17 e corrigir a compra em +2 deve persistir 19. Recusar edição manual com versão antiga; após recarregar, permitir 17→19. Validar duas correções e dois ajustes manuais simultâneos, auditoria e rollback por falha de gravação.
8. Conferir as colunas e os filtros do relatório, sem duplicar correções de compras.

Não publicar código dependente do schema antes da migration. Não efetivar automaticamente conferências antigas para resolver pendências.

## Testes locais

Bancos de testes são temporários; notificações externas são substituídas nos cenários de aplicação.

```sh
PYTHONPATH=tests:. .venv/bin/python -m unittest tests.test_stock_writes tests.test_stock_conferences tests.test_purchase_forecast tests.test_purchase_planning tests.test_case_sales -q
```

A integração PostgreSQL aceita somente um socket descartável com caminho `/tmp/gpcta-pg.*`, porta 55439, sem TCP. Cada teste cria e remove seu próprio schema. A migration é executada somente dentro desse schema temporário, duas vezes, para verificar idempotência.

```sh
GPCTA_TEST_POSTGRES_SOCKET=/tmp/gpcta-pg.<identificador> .venv/bin/python -m unittest tests.test_stock_conferences_postgres -q
```

A suíte PostgreSQL fica ignorada quando a variável não está definida. Nunca apontar esses testes para serviços remotos ou bancos reais.
