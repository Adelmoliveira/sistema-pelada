# Regularização gerencial de custos do estoque

## Escopo

A Visão Financeira continua sendo uma estimativa gerencial do estoque operacional, não um inventário contábil certificado. Reservas e pedidos pendentes podem já ter reduzido o saldo disponível. Não há rastreamento dos lotes remanescentes. Materiais esportivos permanecem excluídos.

Ordem de preferência por produto:

1. Média histórica ponderada original, se confiável pelas regras existentes.
2. Última referência regularizada, se ainda válida, rotulada **Referência regularizada pelo gestor**.
3. **Sem custo apurado**, com os motivos apresentados e totais parciais.

O custo cadastrado (`products.cost_cents`) é somente sugestão no formulário. Nunca é aplicado automaticamente e não é modificado pela confirmação. A origem fica registrada como `catalog_confirmed` ou `manager_entered`. Se o gestor alterar a sugestão, deve selecionar valor informado pelo gestor. Valores monetários usam vírgula decimal, até quatro casas, arredondamento HALF_UP para centavos e limite de 2.147.483.647 centavos; o resultado deve ser positivo.

## Fluxo do gestor

Somente `manager` ativo tem acesso à Visão Financeira e à rota `/stock/financial/<id>/regularize`. O serviço confere novamente papel e atividade no banco, sem confiar em campos enviados pelo navegador. Produtos inexistentes, inativos ou esportivos não podem receber regularização.

Reposições individuais com quantidade/custo ausente ou inconsistente, ou aquisição positiva com custo zero, oferecem **Corrigir reposição** no fluxo existente; uma referência independente fica bloqueada enquanto esses registros puderem ser corrigidos. Esse fluxo original continua com suas regras de auditoria e eventual diferença de estoque. A regularização não chama esse serviço automaticamente. Compras por solicitação com rateio global continuam sem comprovação de custo individual mesmo depois de uma correção: para elas e outros casos sem solução individual no histórico, pode-se confirmar a referência independente.

O formulário exige custo positivo, origem, justificativa entre 5 e 2000 caracteres e checkbox de confirmação explicitamente marcado. O documento de suporte é opcional: guarda identificação ou link textual, até 500 caracteres, sem upload, download remoto ou execução de links. A referência não garante autenticidade/preservação do documento externo; o gestor deve manter o comprovante no arquivo administrativo.

O estado exibido é assinado com a chave da aplicação e expira em 30 minutos. Abrange produto, saldo, revisão, última referência e custo cadastrado. Um POST desatualizado ou repetido retorna 409 e exige recarregar/revisar; erros não renovam silenciosamente a assinatura. CSRF é o mecanismo Flask-WTF já existente (o handler global responde com redirecionamento em caso de falha). O checkbox não vem pré-marcado, inclusive após erros.

## Persistência e validade

`stock_cost_references` guarda ID/produto e nome na confirmação, custo em centavos, saldo/revisão de referência, referência anterior, ID/nome do responsável, horário UTC, justificativa, origem e referência textual do documento. Cada confirmação acrescenta uma linha. Não há edição, exclusão ou substituição de registros anteriores. A tela mostra os últimos 50 registros do produto, e todos permanecem no banco. Produto e responsável são identificados por IDs e nomes copiados, sem cascatas que apaguem o histórico ao remover entidades.

A coluna independente `products.cost_reference_revision` é monotônica sob as operações suportadas. Não substitui `stock_revision` da Conferência Mensal. Invalidadores transacionais incrementam essa revisão quando ocorre:

- mudança real de saldo, atividade ou categoria do produto;
- inclusão, edição ou exclusão de reposição, correção de reposição ou ajuste de estoque associado;
- reassociação desses registros: invalida os produtos antigos e novos.

É uma regra deliberadamente conservadora: até edição de notas de aquisição ou ajuste de diferença zero exige reavaliação. Uma saída e posterior retorno ao mesmo saldo não revalidam a referência. Rollback de uma movimentação também desfaz seu incremento de revisão. Alterar somente preço de venda, custo cadastrado ou descrição do produto não invalida a confirmação independente já prestada pelo gestor. Um formulário antigo com sugestão de cadastro alterada, porém, é recusado.

A referência vale somente se for a **mais recente**, o produto continuar ativo e saldo/revisão coincidirem com a confirmação. Nunca se procura uma referência antiga para substituir outra desatualizada. Quando o histórico se tornar confiável, sua média prevalece. O horizonte é “até a próxima alteração relevante”, sem renovação automática nem prazo artificial. Consequência: produtos com giro podem exigir reconfirmação frequente; isso é intencional nesta versão.

O consumidor financeiro faz uma única consulta de snapshot para saldos, histórico e referência. O POST bloqueia o produto (`FOR UPDATE` em PostgreSQL, transação `BEGIN IMMEDIATE` em SQLite), relê o estado e valida a referência anterior. Uma restrição única `(product_id, previous_reference_id)` complementa a serialização: duas confirmações baseadas na mesma referência não são aceitas. A leitura do responsável tem `FOR SHARE` no PostgreSQL durante a confirmação.

Triggers rejeitam UPDATE e DELETE do histórico; PostgreSQL também rejeita TRUNCATE. Administradores capazes de alterar/remover DDL continuam sendo uma fronteira de confiança: isto não é um arquivo criptográfico inviolável nem certificação contábil. Backups continuam necessários.

Não são criados `restocks`, `stock_adjustments`, compras, lançamentos de caixa, vendas ou pagamentos. A confirmação não muda estoque, revisão de estoque operacional ou custo cadastrado. Os invalidadores acrescentam apenas metadados de revisão às movimentações futuras, dentro da transação original; não executam cálculos de custo.

## Totais

A tela separa valor com custo histórico, valor com custo regularizado e o total estimado da base coberta. Mostra cobertura em unidades (unidades com custo / unidades operacionais), número de produtos cobertos e exclusões. Totais continuam parciais se faltar custo; se nenhum saldo positivo tiver custo, o total não é apresentado como zero. A margem usa a mesma base coberta, preserva valores negativos e não representa lucro realizado.

## Migration e homologação

Migration aditiva: `supabase/migrations/20261018020000_stock_cost_references.sql`, gerada por `src.cost_reference_schema.schema_sql(True)`. Acrescenta tabela, índice, coluna de revisão e triggers; não preenche custos ou altera saldos/histórico existentes. Reaplicação não zera revisões ou registros, mas recria triggers e adquire locks de DDL. A tabela nova tem RLS habilitado, sem policies públicas, e funções `SECURITY INVOKER`.

Antes de aplicar em homologação, confirmar banco/ambiente, backup, histórico de migrations GPCTA e papel runtime. Pré-requisitos: `products`, `restocks`, `restock_corrections`, `stock_adjustments` e colunas usadas pelo SQL. O executor precisa de CREATE/USAGE no schema e autorização para ALTER nas tabelas e substituição de funções/triggers. O runtime precisa ler/inserir referências e utilizar a sequence, ler usuários e ter UPDATE na nova coluna de revisão quando acionado por triggers de aquisições/ajustes. Para enxergar a tabela sob RLS, precisa de titularidade efetiva ou BYPASSRLS com privilégios de tabela correspondentes; grants isolados não bastam sem policy. Não conceder acesso público a `anon`/`authenticated` nem usar SECURITY DEFINER para contornar uma configuração incorreta.

Sequência recomendada, sob autorização de implantação separada:

1. Inventariar `public.schema_migrations` e todas as dependências; não executar automaticamente migrations antigas apenas porque não constam no ledger.
2. Aplicar a migration em janela sem movimentações, dentro de transação, com `search_path=public`, `lock_timeout` e `statement_timeout` adequados. Usar o lock de migrations existente `pg_advisory_xact_lock(hashtext('sistema-pelada-schema'))` em aplicação manual; registrar a versão completa `20261018020000_stock_cost_references.sql` no ledger GPCTA na mesma transação. No runner oficial, o lock e registro já são feitos pelo executor. Não duplicar o ledger Supabase nativo.
3. Conferir existência da coluna/tabela, funções/triggers, RLS e privilégios do papel real da aplicação, inclusive sequence. Comparar saldos e aquisições antes/depois na janela controlada. Testar movimentações existentes com a migration instalada e o código anterior: invalidadores já são ativos nesta fase.
4. Só então publicar o código autorizado. A nova consulta financeira depende da coluna e tabela; não publicar antes da migration. Não há ativação automática de custos: cada referência precisa de confirmação do gestor.
5. Usar dados fictícios em homologação: histórico individual confiável de custos diferentes; compra de custo zero corrigível; rateio global; produto sem histórico; inativo; esportivo; custo com meio centavo; produto sem preço de venda; saldo zero; produtos cobertos e sem custo na mesma tela.
6. Confirmar sem marcar checkbox, sem justificativa, com CSRF ausente e como staff/cliente: nenhum registro deve ser criado. Confirmar como gestor e conferir por nova conexão a linha, responsável e totais. Verificar que estoque, custo cadastrado, compras, caixa, pagamentos e vendas não mudaram.
7. Abrir dois formulários do mesmo produto: somente a primeira confirmação sobre o mesmo estado deve ser aceita. Movimentar o estoque e devolver ao saldo original: a referência deve continuar desatualizada. Acrescentar/corrigir aquisição, lançar ajuste e alterar atividade/categoria: exigir nova confirmação, sem reaproveitar linhas antigas.
8. Executar regressões de vendas, Pix, cancelamentos, reposições e Conferência Mensal, medindo duração e locks. Invalidadores participam das transações operacionais; erro de permissão ou deadlock pode abortar a operação inteira. Usar os fluxos existentes que bloqueiam produto antes de corrigir reposições; edições SQL administrativas em ordem inversa de locks exigem cuidado adicional.

Não foi aplicada migration em Supabase por esta implementação. No SQLite, bootstrap/upgrade é apenas o inicializador explícito `initialize_sqlite_database`, fora de requisições; conectar não executa DDL. O mesmo contrato de validade usa triggers SQLite e serialização de escritores. Não usar SQLite efêmero como banco de produção na Vercel.

## Rollback sem perda de dados

Reverter o código ao release anterior e manter a migration aditiva instalada. As referências permanecem intactas e o código anterior volta a considerar somente seu histórico original. **Não remover tabela/coluna nem desabilitar invalidadores**: isso preserva auditoria e evita que um movimento ocorrido durante o rollback permita reutilizar referência antiga quando o recurso retornar. Não há migration “down” destrutiva. Se for necessária reversão de DDL por emergência, planejar separadamente invalidação de todas as referências e backup/exportação; não apagar dados para desfazer a funcionalidade.

## Testes isolados

```sh
APP_ENV=local DATABASE_URL='' SUPABASE_DB_URL='' \
 PYTHONPATH=tests:. PYTHONPYCACHEPREFIX=/tmp/gpcta-pycache \
 .venv/bin/python -m unittest test_stock_cost_references test_stock_cost_reference_routes \
 test_stock_financial test_stock_conferences test_stock_writes test_stock_conferences_postgres \
 test_purchase_observation test_purchase_observation_routes test_purchase_observation_stock_writes \
 test_purchase_forecast test_purchase_planning -q
```

PostgreSQL é opt-in por `GPCTA_TEST_POSTGRES_SOCKET=/tmp/gpcta-pg.<id>`: somente servidor descartável local via socket Unix, porta 55439. Schemas de teste aleatórios são removidos após o uso. Não apontar a suíte ao Supabase. Os testes validam schema anterior/reaplicação, autorização, CSRF, interface, arredondamento, preferência histórica, histórico imutável, rollback, confirmação concorrente e regressões de escritores reais. Sintaxe PostgreSQL/PLpgSQL pode ser adicionalmente validada por parser offline sem conexão a banco.
