# Compras Inteligentes e PWA — operação

A etapa opcional de push exclusivo para gestores está documentada em [Push inteligente](compras-inteligentes-push-gestores.md). Ela nasce desabilitada e mantém os controles e a fila separados da observação. As instruções abaixo descrevem a etapa de observação sem alertas habilitados.

## Diagnóstico e integração

A etapa 2 já captura invalidações na transação das movimentações e calcula somente produtos afetados. `enabled=0` é a desativação deliberada da captura, não um erro. O cálculo permanece mês atual de São Paulo, três meses completos e margem de 10%, com as regras existentes de consumo, reservas, caixas, esportivos, inativos e dados legados. A tela Planejamento de Compras mantém filtros e cálculo próprios; o novo link abre `/stock/purchase-monitoring`, exclusivo do gerente.

O painel permite ativar, desativar, executar um lote, consultar estado, pendentes, produtos monitorados e episódios paginados. Não faz polling. Estados: desativado; falha (última execução falhou ou ficou `running` por mais de cinco minutos); processamento pendente (fila, execução em curso, mês ainda não recalculado ou nenhuma execução); ativo. Datas são UTC. Produtos monitorados são produtos ativos do bar com estado persistido. Episódios mostram a fotografia observada, não um saldo em tempo real. Todos continuam `notifiable=0`; não há push, mensagem ou compra automática.

Inventário de integrações encontrado no repositório (nenhuma configuração remota foi consultada/alterada):

| Endpoint / configuração | Finalidade | Autenticação / executor preservado |
| --- | --- | --- |
| `/cron/payment-reminders` GET | Lembretes financeiros | `CRON_SECRET`; Vercel diário 12h UTC |
| `/cron/process-notification-outbox` GET/POST | Outbox de notificações existente | `CRON_SECRET`; Vercel diário 13h UTC |
| `/cron/expire-pix-checkouts` GET | Expiração de Pix, condicionada às flags de pagamentos | `CRON_SECRET` |
| `/cron/weekly-tribute` GET | Homenagens semanais | Segredo geral ou hash específico; workflow GitHub existente |
| `.github/workflows/supabase-backup.yml` | Backup de banco | Workflow existente, sem alterações |
| `/cron/process-purchase-observation` | Compras Inteligentes | **Agora somente POST, segredo exclusivo**, cron-job.org |

Os jobs já existentes no cron-job.org não ficam versionados aqui. Antes de criar o novo, conferir seus nomes/URLs e finalidades no painel, sem substituir nenhum. A antiga chamada GET com `CRON_SECRET` à observação deixa de funcionar: atualizar apenas eventual job identificado para essa finalidade. Não foram criados Supabase Cron, pg_net, Vault ou workflow de agendamento novo.

## Configuração exata do cron-job.org

Preparar um job separado por ambiente, inicialmente **desabilitado**:

- Título: `GPCTA — Compras Inteligentes — produção` (ou homologação).
- URL de produção: `https://gpcta.com.br/cron/process-purchase-observation`. URL de homologação: `https://homolog.gpcta.com.br/cron/process-purchase-observation`. Domínios verificados nos respectivos projetos via consulta somente leitura à Vercel em 18/10/2026; não reutilizar a origem de produção no job de homologação.
- Método HTTP: **POST**.
- Cabeçalho `Authorization`: `Bearer <segredo exclusivo deste ambiente>`.
- Cabeçalho `Content-Type`: `application/json`; corpo `{}`. Não há parâmetros de URL.
- Segredo correspondente na Vercel: **`PURCHASE_MONITOR_SECRET`**, aleatório de pelo menos 32 bytes, diferente de `CRON_SECRET` e entre ambientes. Nunca usar senha, chave administrativa, service-role ou URL do Supabase. Não registrar o cabeçalho em logs/capturas. A rota não aceita cookies como autorização e é isenta de CSRF somente por usar esse Bearer; os formulários do gerente continuam protegidos por CSRF.
- Periodicidade personalizada: minutos **0, 15, 30 e 45**, todas as horas/dias/meses, fuso UTC (`*/15 * * * *`). Não há compromisso de execução em segundo exato.
- Timeout da requisição: **30 segundos**. HTTPS, sem redirecionamento para login; usar domínio final. Em preview protegido, resolver a proteção Vercel separadamente; o segredo da aplicação não a contorna.
- Manter histórico do job e acompanhar falhas. O monitor não envia notificações de negócio; alertas operacionais opcionais do próprio cron-job.org são uma configuração administrativa separada.

Respostas: 200 com contagens (`processed`, `pending`, `products`, conforme aplicável); desativado retorna `enabled=false, processed=0`; consumidor concorrente retorna `busy=true`; repetição no mesmo intervalo UTC retorna `duplicate=true`, sem repetir trabalho. 401 = segredo ausente/incorreto; GET = 405; 503 = falha de processamento ou conexão. Um intervalo que falhou permanece identificado como falho; a próxima janela ou execução manual recupera os eventos. `telemetry_pending=true` significa que a finalização do registro não pôde ser confirmada: consultar painel/fila. Não interpretar HTTP 200 sozinho como fila esvaziada.

## Limites, transações e recuperação

Uma chamada consome **um lote de até 100 eventos**; não faz loop até esvaziar o catálogo. A deduplicação periódica usa chave única da janela UTC de 15 minutos. Manual usa chave própria. Toda execução usa conexão exclusiva e o mesmo advisory lock transacional `pg_try_advisory_xact_lock(1196442452,2)` da etapa 2; SQLite usa `BEGIN IMMEDIATE`. Estado, episódios, transições e conclusão de eventos continuam na transação original, sem reescrever saldo. Invalidações concorrentes não são perdidas. O registro de execução é confirmado antes do lote e finalizado depois; morte da instância deixa `running`, detectável como falha após cinco minutos. A janela seguinte retoma eventos pendentes, sem depender de memória. Falhas persistem apenas a classe da exceção, sem SQL/segredos. Banco indisponível pode impedir registrar a própria falha.

Orçamento de processamento: **15 segundos**, com `statement_timeout` PostgreSQL limitado ao tempo restante, `lock_timeout` de 1 segundo, conexão de até 5 segundos e finalização de até 2 segundos. SQLite usa progress handler e espera de escrita de 1 segundo. Esses limites controlam SQL/contenda, não são uma garantia de tempo real para DNS, CPU ou falha de transporte. A ativação administrativa limita espera por locks a 1 segundo e cada instrução a 10 segundos; fazer em janela sem movimentações, pois seu baseline utiliza brevemente SHARE nas seis tabelas-fonte. Se falhar, transação desfaz e o gerente pode tentar novamente.

**Pré-requisito Vercel antes de habilitar o job:** confirmar no projeto a duração efetiva da função Python **de pelo menos 30 segundos** (Functions → Function Max Duration). O repositório usa `builds` legado; não misturar `functions` com essa configuração nem migrar todo o deploy só para isso. O `vercel.json` e seus crons existentes ficam intactos. Sem confirmar esse limite, não ativar o monitor automático. O plano não foi comprovado; cron-job.org elimina a dependência da frequência dos crons Vercel. Cada job de 15 minutos gera até 96 chamadas/dia; conferir consumo e backlog real. Se entradas excederem 100 eventos/15min, os eventos continuam seguros, mas haverá atraso: executar lotes manuais controlados e reavaliar capacidade, sem aumentar frequência silenciosamente.

## Migration e permissões

Nova migration aditiva: `supabase/migrations/20261018030000_purchase_monitoring.sql`, após `20261018010000_purchase_observation.sql` e as migrations anteriores aplicáveis (incluindo regularização de custos da master). Cria somente `purchase_monitor_runs` e habilita RLS nela, sem políticas públicas, funções, triggers, atualizações de saldo ou baseline. É reaplicável em estrutura compatível e não limpa execuções anteriores. SQLite cria a mesma tabela apenas na inicialização explícita local, nunca durante requisições.

Usar o executor **já existente** de migrations, que registra o nome completo do arquivo no `public.schema_migrations` e usa o lock original. Não aplicar migrations anteriores desconhecidas em lote sem auditar o histórico. Antes, conferir conexão/ambiente, backup e migrations registradas com consultas somente leitura:

```sql
SELECT current_user, session_user;
SELECT rolname, rolbypassrls FROM pg_roles WHERE rolname=current_user;
SELECT version FROM public.schema_migrations ORDER BY version;
```

A tabela nova, como as tabelas de observação existentes, usa RLS sem políticas para clientes. O papel runtime precisa ser proprietário ou possuir BYPASSRLS, **e** privilégios de tabela/sequence. Homologar com a conexão runtime, não apenas com o usuário do SQL Editor. Não conceder acesso a `anon`/`authenticated` nem desabilitar RLS para contornar. Se runtime for separado, a concessão mínima para a tabela nova é SELECT/INSERT/UPDATE e USAGE na sequence `purchase_monitor_runs_id_seq`; isso não substitui a análise de RLS. As tabelas-fonte e de observação mantêm suas permissões anteriores. A nova migration não resolve permissões faltantes preexistentes.

Reversão segura: desabilitar o novo job, desativar pelo gerente se necessário e reverter o código sob autorização; **manter a tabela e os registros**. Não fazer DROP para rollback. A versão anterior da aplicação ignora a tabela. Se reverter para código que aceitava GET/segredo geral, não reativar job antigo por engano.

## Homologação e ativação de produção (somente sob autorização)

1. Confirmar ambiente/branch/commit, backup, ledger e permissões runtime. Verificar agregados de estoque antes/depois em janela sem movimentações. Aplicar migrations pendentes auditadas pelo executor original, sem ativar monitoramento.
2. Configurar segredo exclusivo e duração da função, publicar código autorizado. Configuração de ambiente só passa a valer na implantação correspondente. Criar job ainda desabilitado.
3. Testar POST sem token e com token errado (401), GET (405), token exclusivo (200), segredo geral recusado. Não colocar o token no histórico do shell. Confirmar painel restrito ao gerente e POST sem CSRF recusado.
4. Pelo Planejamento → Monitoramento, confirmar a ativação. Ela enfileira baseline e ativa captura atomicamente. Processar lotes manuais até pendentes=0, em janela controlada. Conferir episódios iniciais `baseline`, `notifiable=0`, saldos preservados. Se já estava ativo, não desligar/reinicializar apenas para repetir o teste: preservar estado e backlog.
5. Com dados fictícios de homologação, exercitar 20 → 17 → 19, reposição suficiente/queda, vendas, Pix, entregas, restaurações e conferência; comparar com forecast filtrado na política fixa. Testar falha/rollback, duas chamadas simultâneas, repetição no intervalo e recuperação na janela seguinte. Não produzir notificações de Compras Inteligentes.
6. Habilitar **somente o novo job**, observar pelo menos duas janelas reais e correlacionar histórico do cron-job.org/painel. Conferir `pending`, datas, erros e consumo Supabase/Vercel. Desativar pelo painel impede captura/processamento, mas preserva fila/histórico; reativação enfileira reconciliação de todos os produtos.
7. Repetir em produção apenas com autorização específica. Nenhum desses passos externos foi executado por esta implementação.

## PWA e validação de atualização

Manifest e ícones existentes foram preservados. Worker v7 instala apenas assets públicos e `/offline`; navegação GET usa rede, com fallback informativo somente quando não há conexão. HTML autenticado e APIs nunca são gravados; POST não é interceptado. Escritas de cache pertencem ao `waitUntil`, inclusive o `cache.put`. Ativação limpa apenas caches com prefixo `peladeiros-gpcta-` e preserva caches de outros sistemas. Push já existente é preservado, sem integração com Compras Inteligentes.

Nova versão aguarda confirmação no banner. Não há `skipWaiting` automático. Formulários alterados (inclusive quantidades ajustadas do planejamento), submissões e fetch/XHR em andamento bloqueiam a atualização. Concluir/desfazer edições libera o botão. Troca de worker em outra aba não recarrega esta aba automaticamente; a atualização exige ação segura do usuário. Primeira instalação não causa reload. Campo alterado por script sem evento input/change deve emitir esse evento para participar da proteção; não existe sincronização de formulário entre abas.

Homologar em navegador/dispositivo real: instalação HTTPS/standalone; carregar online, ir offline e navegar (página informativa); voltar online e recuperar acesso; confirmar ausência de HTML/API privado nos caches; manter cache alheio e trocar versão; editar formulário e iniciar requisição lenta enquanto nova versão aguarda; garantir que não haja reload/perda; concluir edição e aceitar atualização uma única vez. A migração do worker v6 pode exigir uma navegação adicional/fechamento de todas as abas antigas: o script de inicialização v7 tem URL versionada para evitar servir sua cópia antiga do cache. Não há venda offline.

## Testes locais

```sh
APP_ENV=local DATABASE_URL='' SUPABASE_DB_URL='' PYTHONPATH=tests:. \
 .venv/bin/python -m unittest test_purchase_monitor test_purchase_observation \
 test_purchase_observation_routes test_purchase_observation_stock_writes \
 test_purchase_forecast test_purchase_planning test_stock_writes \
 test_stock_conferences test_stock_conferences_postgres test_stock_financial \
 test_stock_cost_references test_stock_cost_reference_routes -q
node --test tests/js/pwa.test.cjs
```

PostgreSQL: somente cluster descartável por socket `/tmp/gpcta-pg.*`, porta 55439, variável `GPCTA_TEST_POSTGRES_SOCKET`. Cada teste cria/remove schema isolado; nunca usar Supabase. Testes de worker/UI executam o JavaScript real em VM com rede/cache/DOM simulados, sem substituir homologação em dispositivos reais.

Referências oficiais: [cron-job.org: criação de jobs](https://docs.cron-job.org/creating-cron-jobs.html), [FAQ e timeout](https://cron-job.org/en/faq/), [duração de funções Vercel](https://vercel.com/docs/functions/configuring-functions/duration).

## Arquivos desta entrega

| Área | Arquivos |
| --- | --- |
| Configuração e conexão | `.env.example`, `app.py`, `src/db.py` |
| Monitoramento | `src/services/purchase_monitor.py`, `src/services/purchase_observation.py`, `src/purchase_monitor_schema.py` |
| Persistência PostgreSQL | `supabase/migrations/20261018030000_purchase_monitoring.sql` |
| Rotas | `src/routes/finance.py`, `src/routes/products.py` |
| Interface | `templates/base.html`, `templates/purchase_planning.html`, `templates/purchase_monitoring.html` |
| PWA | `static/service-worker.js`, `static/pwa.js`, `static/pwa-update.js` |
| Testes | `tests/test_purchase_monitor.py`, `tests/test_purchase_observation_routes.py`, `tests/test_mercadopago.py`, `tests/js/pwa.test.cjs` |
| Documentação | `docs/compras-inteligentes-etapa-2.md`, `docs/compras-inteligentes-operacao-pwa.md` |

Validação final local em 18/10/2026: **377 testes Python aprovados**, incluindo PostgreSQL descartável e SQLite; **12 testes JavaScript aprovados**. Testes abrangem autorizações/CSRF, baseline, desativação/reativação, idempotência, concorrência, rollback, prazo SQL real, recuperação de execução abandonada, estados do painel e episódios, movimento 20 → 17 → 19, regressões de pagamentos/estoque/custos, cache offline e atualização protegida. Logs de falhas simuladas em testes negativos são esperados. Nenhuma migration em banco real, job, variável remota, deploy ou PR foi criado/alterado.

Após auditar **todas** as pendências e confirmar uma conexão de homologação previamente injetada no ambiente (não copiar URL para comando/histórico), o executor existente é:

```sh
APP_ENV=homologation APPLY_POSTGRES_MIGRATIONS=1 \
 .venv/bin/python scripts/migrate_postgres_schema.py
```

Esse comando pode aplicar **todas** as migrations pendentes, não só a nova. Não executar se o histórico não estiver reconciliado. Depois da aplicação autorizada, verificar separadamente com o papel runtime:

```sql
SELECT has_table_privilege(current_user,'public.purchase_monitor_runs','SELECT') AS can_select,
       has_table_privilege(current_user,'public.purchase_monitor_runs','INSERT') AS can_insert,
       has_table_privilege(current_user,'public.purchase_monitor_runs','UPDATE') AS can_update,
       has_sequence_privilege(current_user,'public.purchase_monitor_runs_id_seq','USAGE') AS can_use_sequence;
```

Esses privilégios não dispensam a verificação de proprietário/BYPASSRLS descrita acima. Os comandos são procedimento preparado; não foram executados em homologação/produção.

Pendências operacionais: duração efetiva e permissões runtime precisam ser comprovadas em homologação; ativar o job só após baseline; testar instalação/troca entre versões em Android/iOS reais. O histórico de eventos/execuções cresce com o uso: acompanhar armazenamento e definir eventual retenção em trabalho separado, sem apagar auditoria nesta entrega. Em futuras versões do PWA, atualizar conjuntamente o nome do cache e a versão da URL de `pwa.js` quando alterar os assets do shell.
