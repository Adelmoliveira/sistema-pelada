# Compras Inteligentes — Etapa 2: observação

## Escopo e arquitetura

Esta etapa não envia push, não cria compras/reposições e não altera a tela ou os parâmetros do Planejamento de Compras. A tela continua calculando diretamente pelo serviço existente. O observador usa mês atual de São Paulo, três meses completos de histórico e margem de 10%, sempre por `purchase_forecast(..., product_ids=ids)`.

Triggers de banco gravam invalidações leves em `purchase_observation_events` **dentro da transação que altera os dados**. Rollback da venda também desfaz seus eventos. Nenhum cálculo de planejamento é executado nas transações operacionais. A captura nasce desabilitada; instalar a migration não recalcula nem altera qualquer saldo, venda ou conferência histórica.

Fontes cobertas:

| Origem | Campos/eventos observados | Fluxos alcançados |
| --- | --- | --- |
| `products` | inclusão/exclusão; alteração real de `stock`, `active`, `category`, `units_per_case`, `created_at` | venda/reserva Pix, expiração/liberação, cancelamento, reposição, correção, ajuste manual e conferência aplicada |
| `sales` | inclusão/exclusão; `delivered_at`, `payment_status` | confirmação/cancelamento/legado |
| `sale_items` | inclusão/exclusão; `sale_id`, `product_id`, `quantity` | composição e consumo legado |
| `sale_item_deliveries` | inclusão/exclusão; `sale_item_id`, `quantity`, `delivered_at` | entrega total/parcial |
| `sale_item_delivery_restorations` | inclusão/exclusão; `delivery_id`, `quantity` | restauração total/parcial |
| `sale_cancellations` | inclusão/exclusão; `sale_id` | exclusão do consumo legado |

Não é necessário duplicar eventos em `restocks`/`stock_adjustments`: seu efeito é capturado na alteração efetiva de `products.stock`. Alterações apenas financeiras, sem efeito nos insumos do planejamento, não exigem recálculo. Eventos carregam produto, tabela, ID de origem, operação e instante; são invalidações, não um livro de movimentações nem um mecanismo de replay de saldos. Uma operação pode produzir eventos repetidos, inclusive OLD/NEW. Em entrega/composição, todos os produtos da venda são invalidados: a presença de uma entrega detalhada desabilita o fallback legado **da venda inteira**. A captura anterior a exclusões protege também cascatas.

O consumidor processa até 100 eventos por invocação e coalesce os IDs afetados. Faz uma consulta de estado e até três consultas de forecast, independentemente da quantidade de produtos no lote; gravações são em lote, com até 900 parâmetros por instrução para SQLite antigo. Eventos concorrentes não são marcados por um watermark: apenas os IDs efetivamente selecionados são concluídos. Um evento confirmado depois do snapshot permanece pendente.

Persistência:

- `purchase_observation_control`: ativação e mês da política.
- `purchase_observation_events`: fila durável, com índice parcial de pendentes.
- `purchase_replenishment_state`: estado único por produto e último snapshot do cálculo.
- `purchase_replenishment_episodes`: episódio com origem, quantidades, abertura, atualização e encerramento; unicidade de `(product_id, episode_number)` e índice único parcial de episódio aberto por produto.
- `purchase_replenishment_transitions`: histórico de baseline, abertura, atualização e encerramento, com snapshot e unicidade por produto/evento de origem.

Necessidade significa **sugestão de compra em unidades > 0**, preservando as regras existentes para inativos, esportivos, caixas e histórico insuficiente. Primeira observação estabelece baseline silencioso. Se já houver necessidade, cria episódio `origin=baseline`. Reposição suficiente, inativação, exclusão ou mudança para material esportivo encerram a necessidade; nova insuficiência abre outro episódio. Alterações no snapshot atualizam o episódio aberto. Invalidações sem mudança não geram transições repetidas. O histórico antigo não é apagado.

Todos os episódios têm `notifiable=0`, protegido por `CHECK`; não existe chamada a serviços de notificação. Uma etapa futura precisará de migration e regra explícitas para novos episódios, sem tornar episódios desta etapa retroativamente notificáveis.

## Transações, concorrência e recuperação

No PostgreSQL, o consumidor usa `REPEATABLE READ` e `pg_try_advisory_xact_lock(1196442452,2)`. Essa chave de dois inteiros é própria do observador, diferente do lock de migrations. Existe um único consumidor por banco, sem aguardar outro consumidor: concorrentes retornam `busy=true`. Estado, episódios, transições e marcação dos eventos são confirmados juntos. Falha, timeout ou morte da instância desfaz o lote e libera o lock; a próxima invocação recupera os pendentes, sem memória compartilhada ou lease a expirar. Erros de serialização/deadlock devem ser recuperados repetindo a invocação, não marcando eventos como concluídos.

O consumidor não bloqueia linhas de produtos e não modifica saldos. Movimentações operacionais continuam durante o cálculo no PostgreSQL. Cada entrada do serviço exige conexão sem transação aberta: isso impede que ele confirme uma venda alheia. A rota usa conexão própria e confirma somente o `SET TIME ZONE` inicial antes de iniciar o consumidor.

No SQLite a mesma regra funcional usa `BEGIN IMMEDIATE`; escritores são serializados durante o lote. Isso é adequado a desenvolvimento/testes, mas não equivale à concorrência MVCC do PostgreSQL. O schema é criado apenas pelo inicializador explícito `initialize_sqlite_database`, fora de requisições. Conectar não executa DDL. Não há migration SQLite automática na conexão.

Na primeira ativação há um bloqueio administrativo curto das seis tabelas-fonte (`SHARE` no PostgreSQL), sem forecast, para estabelecer a fronteira entre baseline e captura. Usar uma janela sem movimentações evita espera/deadlock com transações operacionais. O baseline é enfileirado por `INSERT SELECT` e o flag é ativado na mesma transação. O primeiro processamento pode incorporar alterações posteriores à ativação; a observação representa o estado atual, não reconstrói insuficiências transitórias entre invocações. Para uma fotografia inicial controlada, concluir o baseline antes dos testes de movimentação.

Na primeira invocação de cada novo mês, o consumidor enfileira uma invalidação por produto uma única vez, pois a janela histórica muda mesmo sem movimento de estoque. É a única varredura recorrente do catálogo; o forecast continua em lotes por produto. Sem invocação, não há atualização automática da janela.

A integração operacional passou a usar `POST /cron/process-purchase-observation`, com segredo exclusivo `PURCHASE_MONITOR_SECRET` e executor cron-job.org. GET e o segredo geral não autorizam mais essa rota. Consulte [operação e PWA](compras-inteligentes-operacao-pwa.md) para configuração, limites, painel e nova migration de telemetria. O CLI da etapa 2 continua disponível para recuperação administrativa, mas suas execuções não entram no histórico HTTP do painel.

## Migration PostgreSQL e pré-requisitos

Arquivo: `supabase/migrations/20261018010000_purchase_observation.sql`. É gerado por `src.purchase_observation_schema.schema_sql(True)`; os testes verificam equivalência. SQL independente de Python na implantação, transacional e reaplicável: tabelas/índices condicionais, funções substituídas, triggers recriadas. Reaplicar não desativa observação, não limpa eventos/estado e não modifica saldos. Recriar triggers ainda exige locks DDL: usar janela de manutenção.

Dependências: seis tabelas-fonte e seus campos listados acima, incluindo `products.units_per_case`, tabelas de entregas detalhadas e restaurações. As migrations anteriores devem ser inventariadas pelo **histórico GPCTA `public.schema_migrations`**, não pelo ledger nativo do Supabase. A numeração de arquivos não prova que as migrations anteriores foram aplicadas. Não executar cegamente o runner que aplica todos os arquivos pendentes.

As cinco tabelas novas têm RLS habilitado, sem policies públicas, e as funções são `SECURITY INVOKER`. O papel da conexão da aplicação precisa ser proprietário dessas tabelas ou ter `BYPASSRLS` e privilégios adequados (SELECT no controle; INSERT/SELECT/UPDATE nas tabelas, USAGE/SELECT nas sequences), além do acesso existente às fontes. **Verificar o papel antes de aplicar**, pois triggers passam a consultar o controle mesmo desativadas. Aplicar com o mesmo proprietário autorizado utilizado pela aplicação evita incompatibilidade. Não conceder acesso a `anon`/`authenticated`, não tornar funções `SECURITY DEFINER` e não alterar atributos de papéis por conveniência. Se o executor de migrations e o papel da aplicação diferirem, revisar a titularidade/permissões com o administrador antes de prosseguir.

## Homologação: procedimento controlado

Comandos abaixo são instruções; este PR não os executa em banco real. Obter backup e uma conexão comprovadamente de homologação; não imprimir URLs/segredos. Usar credencial já carregada no ambiente seguro.

1. Conferir branch/commit, `APP_ENV=homologation`, projeto Supabase correto e papel runtime. Inventariar histórico e dependências com consultas somente leitura:

```sql
SELECT current_database(), current_user, current_schema();
SELECT version, applied_at FROM public.schema_migrations ORDER BY version;
SELECT table_name,column_name FROM information_schema.columns
WHERE table_schema='public' AND table_name IN
 ('products','sales','sale_items','sale_item_deliveries',
  'sale_item_delivery_restorations','sale_cancellations')
ORDER BY table_name,ordinal_position;
SELECT rolname,rolbypassrls FROM pg_roles WHERE rolname=current_user;
SELECT SUM(stock) AS stock_units,COUNT(*) AS products FROM public.products;
```

2. Auditar todas as migrations pendentes. Para aplicação **somente desta migration**, após confirmar as dependências e papel, usar `psql` na raiz do checkout, com bloqueio compatível com o executor de `src/db.py` e registro do nome completo do arquivo:

```sh
psql "$DATABASE_URL" -X -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
SET LOCAL search_path TO public;
SET LOCAL lock_timeout = '5s';
SELECT pg_advisory_xact_lock(hashtext('sistema-pelada-schema'));
\i supabase/migrations/20261018010000_purchase_observation.sql
INSERT INTO public.schema_migrations(version)
VALUES ('20261018010000_purchase_observation.sql')
ON CONFLICT(version) DO NOTHING;
COMMIT;
SQL
```

`schema_migrations` deve existir com a definição atual do GPCTA; não criar um ledger alternativo. Timeout/erro significa rollback; investigar antes de repetir. O lock xact e o lock de sessão do runner conflitam na mesma chave, impedindo execução paralela de migrations. Para SQL Editor, incorporar o conteúdo literal do arquivo no lugar de `\i` (metacomando exclusivo do psql), mantendo a transação, lock e registro.

3. Verificar, ainda com o papel runtime: `SELECT enabled FROM purchase_observation_control WHERE id=1` deve retornar 0 numa instalação nova; conferir cinco tabelas, RLS, quatro triggers por fonte e privilégios/sequences. Repetir os agregados de estoque e conferir que nada mudou na janela sem movimentações. Uma reaplicação mantém o estado existente, portanto não assumir `enabled=0` se já ativado.

4. Publicar o código por procedimento separado autorizado, **depois** do schema e da validação do papel. Este PR não publica. Enquanto desativada, confirmar vendas/entregas/stock existentes. O rollback do código não remove triggers: tratar a captura como alteração independente do banco.

5. Ativar explicitamente e esvaziar o baseline na janela controlada:

```sh
APP_ENV=homologation .venv/bin/python scripts/purchase_observation.py activate --postgres --confirm-environment homologation
APP_ENV=homologation .venv/bin/python scripts/purchase_observation.py process --postgres --confirm-environment homologation
APP_ENV=homologation .venv/bin/python scripts/purchase_observation.py status --postgres --confirm-environment homologation
```

Repetir `process` até `processed=0` e confirmar `pending=0` em `status`. Nunca passar a URL em argumentos registrados em logs. O CLI não carrega dotenv, não cria banco/schema e exige `DATABASE_URL` já configurada. Para SQLite descartável já inicializado, usar `... process --sqlite /tmp/teste.db` (e `activate` antes).

6. Exercitar com produtos fictícios de homologação: saldo 20, histórico de 60 unidades nos três meses concluídos (necessidade bruta 22); movimentos 20 → 17 → 19, reposição até 30 e nova queda. Conferir um episódio aberto, atualizações, encerramento e reabertura, sem saldo reescrito pelo observador. Testar Pix abandonado/liberação, venda confirmada, entrega parcial, restauração, cancelamento, ajuste manual, correção de reposição e aplicação única da conferência. Verificar estado por nova conexão e equivalência com o forecast filtrado.

7. Invocar a rota com token válido, inválido e simultaneamente; simular falha do consumidor e recuperar pendentes. Conferir que baseline/duplicatas não geram episódios repetidos e que todos continuam `notifiable=0`. Nenhum push de Compras Inteligentes deve ser produzido; notificações já existentes da aplicação permanecem com seu funcionamento original.

```sql
SELECT COUNT(*) FROM purchase_observation_events WHERE processed_at IS NULL;
SELECT product_id,COUNT(*) FROM purchase_replenishment_episodes
WHERE closed_at IS NULL GROUP BY product_id HAVING COUNT(*)>1;
SELECT COUNT(*) FROM purchase_replenishment_episodes WHERE notifiable<>0;
SELECT * FROM purchase_replenishment_state ORDER BY product_id;
SELECT * FROM purchase_replenishment_transitions ORDER BY id;
```

8. Só após homologar, repetir o procedimento em produção sob autorização separada, trocando a confirmação para `production`, com backup, janela e papel corretos. Definir invocador, frequência compatível com o volume, tempo máximo e monitoramento do backlog antes de chamar o modo de observação de contínuo. Não configurar cron/deploy automaticamente neste PR.

## Interrupção e limites

`... deactivate --postgres --confirm-environment homologation` (com `APP_ENV` correspondente) interrompe captura/processamento e preserva o histórico e pendentes. Reativar invalida o catálogo novamente, pois houve uma janela sem captura; não reconstrói transições intermediárias. Os eventos antigos não são apagados. Não remover tabelas/triggers enquanto instâncias antigas/novas operam sem um plano separado.

Uma falha de permissão/espaço/DDL na captura aborta também a operação original, deliberadamente para manter atomicidade; por isso permissões, espaço e rollback operacional devem ser homologados antes da ativação. Mesmo desativadas, triggers fazem leitura leve do controle. Há amplificação por OLD/NEW e por produtos da venda; acompanhar crescimento e planejar retenção futura dos eventos já processados sem apagar auditoria necessária. Um lote de 100 limita produtos/eventos, mas um produto pode ter histórico volumoso; medir duração com dados representativos. O consumidor único prioriza simplicidade e consistência; paralelismo por produto fica para uma etapa futura se métricas o exigirem.

Não existe autorização de gestor nova, assinatura push, cálculo financeiro ou alteração de regra de venda nesta etapa. Alertas serão projetados separadamente, após avaliar a observação.

## Testes

```sh
PYTHONPATH=tests:. PYTHONPYCACHEPREFIX=/tmp/gpcta-pycache \
 .venv/bin/python -m unittest test_purchase_observation test_purchase_observation_routes \
 test_purchase_observation_stock_writes test_purchase_forecast test_purchase_planning \
 test_stock_conferences test_stock_writes test_stock_financial test_stock_conferences_postgres -q
```

PostgreSQL é opt-in: `GPCTA_TEST_POSTGRES_SOCKET=/tmp/gpcta-pg.<identificador>` com servidor **descartável**, somente socket Unix, porta 55439; os testes criam e removem schemas aleatórios. Nunca apontar testes para Supabase. Mesmo contrato de baseline, rollback, duplicatas, transições, exclusões, entregas/restaurações e consumidores concorrentes nas duas bases; PostgreSQL também verifica commit operacional durante o snapshot do consumidor. Testes HTTP ativam captura ao executar regressões reais de vendas/Pix/entregas/conferência.
