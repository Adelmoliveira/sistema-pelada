# Push inteligente de reposição — gestores

## Escopo e estado inicial

A implementação usa os episódios da observação, sem modificar regras de previsão, vendas, Pix, compras, reposições ou saldos. O monitoramento pode continuar ativo e os alertas permanecerem desligados. A migration cria `purchase_alert_control.enabled=0`; deploy, cron, consentimento de dispositivo e inicialização SQLite **não habilitam alertas**. Habilitação exige ação explícita de gerente autenticado com CSRF. Reaplicar a migration preserva decisões já persistidas, sem reativar ou desativar configurações anteriores.

A migration é estritamente aditiva. O `CHECK(notifiable=0)` dos episódios originais e todos os seus registros são preservados. Esse campo continua pertencendo à observação original; a elegibilidade da nova fila é representada separadamente por `purchase_alert_product_state.eligible`, geração e episódio. Isso evita relaxar a proteção antiga ou reconstruir tabelas SQLite.

## Arquitetura e persistência

1. Triggers e processamento de observação existentes continuam intactos.
2. Após o commit de `run_monitor`, o mesmo endpoint POST executa `process_alerts`, inclusive em chamadas periódicas duplicadas. A classificação tem transação própria, snapshot consistente e o advisory lock `pg_try_advisory_xact_lock(1196442452,2)` existente. Grava em lote alertas, destinatários e cursores; falha desfaz apenas essa classificação.
3. Consumidores reservam entregas em transações curtas, com token e lease persistentes; não dependem de memória Vercel. Nenhum HTTP push acontece dentro da transação ou segurando o lock do observador.
4. Antes de cada envio, uma consulta nova revalida papel, atividade, consentimento, versão da subscription, geração, monitoramento, habilitação, produto, episódio e ausência de invalidações pendentes desse produto.
5. O resultado da chamada cron mantém os campos do monitor e acrescenta `alerts`. Falha no módulo de alertas não converte um monitoramento já confirmado em rollback/erro de venda. A resposta registra `failed`, `error_code`, `deferred` ou `telemetry_pending` quando aplicável; falha capturável também aparece em `purchase_alert_control.last_error`.

Tabelas novas:

| Tabela | Finalidade |
| --- | --- |
| `manager_push_subscriptions` | Usuário proprietário, endpoint, chaves, versão, hashes da sessão/dispositivo, consentimento e revogação |
| `manager_push_revocations` | Marcas persistentes de revogação que impedem recadastro por requisições antigas em andamento |
| `purchase_alert_control` | Habilitação independente, geração, ativação do monitor de referência, responsável e último processamento/erro |
| `purchase_alert_product_state` | Referência silenciosa, episódio elegível, maior patamar, nível e evento observado por produto |
| `purchase_stock_alerts` | Alerta lógico único por episódio, tipo e nível |
| `purchase_alert_deliveries` | Destinatário e dispositivo fixados na criação, situação, tentativas, disponibilidade e lease |
| `purchase_alert_attempts` | Tentativas numeradas, início/fim, aceitação, falha, cancelamento ou resultado desconhecido |

Um alerta lógico pode alcançar vários gestores/dispositivos. A unicidade por alerta, usuário, subscription e versão impede duplicação dessa entrega; dispositivos diferentes do mesmo gestor recebem suas próprias entregas consentidas. Não se usa `notification_outbox`, `push_inbox` ou `push_dispatches` dos peladeiros. Não há peladeiro fictício nem novos custos ou compras.

## Regras de negócio

- Somente `users.role='manager' AND users.active=1`. Os papéis `staff`, `client`, `football_manager`, `infra`, `maintenance` e `display` não são destinatários.
- Na habilitação, uma geração nova enfileira reconciliação de produtos, sem calcular forecast no formulário. O primeiro estado consolidado de cada produto nessa geração é silencioso; episódios já abertos não notificam nem por agravamento. Mudanças absorvidas antes dessa fotografia também são silenciosas: escolha conservadora para evitar retroatividade.
- Um novo episódio de transição, após essa referência, gera abertura. O patamar é `suggested_purchase_units`, em unidades físicas, segundo a política original de mês atual/3 meses/10%.
- Agravamento: `novo - maior_patamar >= max(ceil(maior_patamar × 25%), max(1, unidades_por_caixa))`. O patamar é o maior alerta lógico registrado, não cada oscilação observada nem confirmação de leitura. Alterações insuficientes não elevam a referência; pequenas quedas não a reduzem.
- Exemplo com embalagem de 6: 12 → 18 gera agravamento; 18 → 12 → 18 não repete; 18 → 24 pode gerar outro. Sem embalagem, o incremento mínimo é uma unidade, além dos 25%.
- Encerrar episódio não envia push. Entregas pendentes são canceladas; níveis antigos ainda não enviados são substituídos pelo agravamento mais recente.
- Destinatários são fixados na geração do alerta. Novo consentimento ou novo gestor não recebe alertas anteriores. Sem destinatário, o alerta fica no histórico com zero dispositivos.
- Desabilitar alertas cancela pendências. Reabilitar estabelece nova geração silenciosa. Desativar monitoramento pelo painel também desabilita alertas; desativação via CLI/SQL é respeitada pelo consumidor. Reativação do monitor muda sua referência de ativação e exige nova habilitação explícita dos alertas.

## Consentimento e segurança de subscriptions

Rotas novas, restritas a gerente ativo:

- GET `/notifications/manager-push/public-key`.
- POST `/notifications/manager-push/prepare`: CSRF, prepara uma associação nova após invalidar a anterior neste dispositivo.
- POST `/notifications/manager-push/subscribe`: CSRF e `consent=true`, somente após ação do usuário/Push API.
- POST `/notifications/manager-push/unsubscribe`: CSRF, revogação da associação desta sessão.

A interface usa VAPID e o service worker existentes. Permissão `granted` sozinha não cadastra gerente automaticamente. As chaves recebidas são validadas (P-256 e auth de 16 bytes). Endpoints aceitam HTTPS, porta padrão e provedores conhecidos: FCM, Mozilla, Apple e Windows Notification Service; URLs internas, credenciais embutidas, portas alternativas e destinos arbitrários são recusados. Novo provedor deverá ser auditado antes de ampliar essa lista.

Cada consentimento vincula endpoint ao usuário/sessão/dispositivo, com nova versão. A preparação usa POST com CSRF e um cookie privado `gpcta_manager_push_device` (HttpOnly, SameSite=Lax, Secure em HTTPS/ambientes remotos, duração de um ano), que não concede acesso à conta e permite revogar a associação mesmo após expirar a sessão autenticada. Logout é idempotente e também pode limpar essa associação sem sessão ativa. Logout/revogação apagam esse cookie após confirmar a invalidação; outro dispositivo mantém sua própria associação. Marcas persistentes de revogação e o mutex de cadastro impedem que uma inscrição antiga já autenticada ressuscite a associação após logout; o PWA cancela o consentimento em andamento quando a saída começa. Trocar de conta exige logout no fluxo atual; logout revoga a associação antes de limpar a sessão. Cadastrar o mesmo endpoint para outro gerente ou para um peladeiro invalida a associação anterior, atomicamente. O cadastro legado de peladeiro continua usando sua tabela, mas invalida eventual associação gerencial desse endpoint; o inverso remove a associação legada. Cadastros usam o mutex transacional `(1196442452,3)` para serializar essa transferência. O envio recusa ainda qualquer endpoint presente na tabela legada, protegendo inclusive a coexistência temporária com instâncias da versão anterior durante o rollout. Não há migração automática de subscriptions de peladeiros para gestores.

Um trigger adicional em `users`, somente para atualização de papel/atividade de gerente, revoga as subscriptions quando houver perda de autorização. Reativar ou restaurar o papel não restaura consentimento. Ele não é acionado por leitura de sessão, vendas ou movimentações de estoque. O cadastro PostgreSQL mantém um lock FOR SHARE curto no usuário até o commit para serializar consentimento com mudanças de autorização; SQLite usa BEGIN IMMEDIATE. A verificação no envio permanece obrigatória mesmo com o trigger. Ao detectar `Notification.permission='denied'` em página gerencial online, o PWA solicita revogação sem polling; 404/410 do provedor também invalidam a subscription.

Payload de tela bloqueada é genérico: “Há uma nova necessidade de reposição. Consulte o planejamento.” Abre `/stock/purchase-monitoring`, cuja autorização é independente. Não leva preço, custo, margem, faturamento, credenciais ou chaves. Logs e tentativas guardam classe de erro e status HTTP, sem corpo bruto de exceção ou endpoint.

## Envio, recuperação e limites

- Até 100 produtos classificados por lote, com consultas/gravações em lote; sem forecast novo/N+1 de catálogo.
- Até 5 entregas tentadas por chamada. Até 5 tentativas por entrega, incluindo reserva interrompida. Backoff de 15, 30, 60 e 120 minutos, observado na próxima chamada cron disponível.
- Lease de 90 segundos. Interrupção deixa tentativa `started`; após expiração vira `unknown` e volta à fila, ou falha terminal ao atingir o limite.
- Alerta pendente expira após 24h. O provedor recebe TTL de 15 minutos, para limitar mensagens atrasadas em dispositivos offline.
- Orçamento compartilhado do endpoint: 24s desde antes da conexão. O monitor mantém seus 15s; classificação usa o tempo restante, até 10s, reservando margem para envio. Cada push usa timeout HTTP de 2s; as transações têm limites de instrução/lock. Sem margem, retorna `deferred`; os dados permanecem para a próxima chamada.
- O timeout HTTP de rede não é um relógio absoluto para DNS/transportes defeituosos. Vercel Hobby com Fluid Compute e padrão de 300s foi confirmado na auditoria anterior; o cron-job.org continua encerrando a espera em 30s. Conferir duração real em homologação, principalmente cold start e backlog. Se o monitor consumir sempre todo o orçamento, alertas poderão atrasar; avaliar executor separado somente mediante nova autorização, sem mudar jobs agora.
- Sucesso de negócio significa **aceito pelo serviço push**, não exibido/lido. Essa distinção permanece no painel.
- Não existe atomicidade distribuída entre o provedor e o banco: se o provedor aceitar e a gravação falhar, pode haver nova transmissão. O ID do alerta é estável; Topic/tag e deduplicação IndexedDB no worker v8 reduzem repetições visuais. Armazenamento local indisponível e interrupção do worker impedem garantia absoluta de exatamente uma exibição. A revalidação é feita imediatamente antes da chamada HTTP; alterações posteriores à verificação não recolhem uma mensagem já em trânsito ou aceita pelo provedor.

O worker guarda apenas IDs opacos e horários em IndexedDB, com limpeza de entradas superiores a sete dias a cada novo push, nunca payloads ou páginas privadas; não os coloca no cache offline. Cache v8 e URL versionada de `pwa.js` mantêm o fluxo de atualização segura. Push e notificações antigas continuam no caminho original. Instalar/atualizar o PWA para v8 antes de habilitar alertas.

## Migration e compatibilidade

Arquivo: `supabase/migrations/20261018040000_purchase_intelligent_alerts.sql`.

Aplicar somente depois das migrations anteriores reconciliadas, incluindo observação e monitoramento. Cria as sete tabelas, índices, função/trigger de revogação e um índice parcial por produto na fila existente. **Não executa atualização de estoque, vendas, compras, episódios ou flags preexistentes.** RLS é habilitada nas tabelas novas, sem políticas públicas. A função de trigger usa os privilégios do invocador, sem SECURITY DEFINER. Seu `search_path` é fixado em `pg_catalog`; a tabela de subscriptions é qualificada pelo `TG_TABLE_SCHEMA` com identificador escapado. Uma tabela temporária homônima não pode desviar a revogação. A alteração do usuário e a revogação pertencem à mesma transação e são revertidas juntas.

O papel runtime precisa de SELECT/INSERT/UPDATE nas novas tabelas, USAGE nas sequences correspondentes e condição válida de proprietário/BYPASSRLS, conforme o padrão atual. O trigger também exige UPDATE em `manager_push_subscriptions` quando o runtime alterar autorização de usuários. Não conceder acesso a `anon`/`authenticated` nem desabilitar RLS. VAPID existente e `PURCHASE_MONITOR_SECRET` são reutilizados; nenhuma credencial administrativa é usada como segredo do agendamento.

O executor original registra a migration e mantém transação/lock de schema. O arquivo limita espera por locks a 2s e instruções a 30s. Criação do índice na fila e trigger de usuários pode exigir locks: escolher janela de manutenção/baixo movimento; se falhar, reconciliar o estado e repetir, sem marcar execução falsa no ledger. Não usar CREATE INDEX CONCURRENTLY dentro do executor transacional. SQLite usa apenas inicialização explícita local, com tabelas e trigger equivalentes; nenhuma tabela antiga precisa ser reconstruída.

Rollback operacional: desabilitar alertas, deixar leases terminarem/cancelarem e reverter código sob autorização. Manter tabelas e histórico. Não fazer DROP nem reativar filas antigas. O cadastro gerencial nunca recebe automaticamente subscriptions legadas.

## Checklist de homologação e implantação autorizada

1. Confirmar ambiente, branch/commit, backup, migrations registradas e permissões runtime. Comparar agregados de estoque antes/depois numa janela sem movimentações. Não executar pendências desconhecidas em lote.
2. Aplicar a migration pelo executor original **antes do código**, somente com autorização no ambiente escolhido. Verificar sete tabelas/RLS, sequences, trigger de usuários e `enabled=0` numa instalação nova. Uma reaplicação preserva configuração já autorizada.
3. Publicar código autorizado com alertas desligados. Não alterar o job existente: mesmo POST, URL e `PURCHASE_MONITOR_SECRET`, a cada 15 minutos. Confirmar 401 sem segredo, GET 405 e o bloco `alerts` no JSON.
4. Confirmar PWA v8 em dispositivos reais. Como gerente, autorizar este dispositivo; verificar confirmação explícita e revogação. Testar atendente/peladeiro recusados, logout, troca de conta, alteração de papel e inativação. Conferir VAPID sem expor valores.
5. Em homologação, usar gestores/dispositivos de teste. Esta fila **não simula sucesso em APP_ENV=homologation**: quando habilitada e consentida, envia push real aos gestores de teste. Os testes automatizados substituem o transporte, sem acesso à rede push.
6. Habilitar alertas explicitamente no painel. Processar lotes manuais até terminar o baseline e a fila operacional. Confirmar silêncio de episódios existentes, inclusive agravamentos. Se houver atividade durante a fotografia inicial, ela poderá ser absorvida silenciosamente.
7. Com produto fictício: encerrar necessidade, abrir episódio novo, conferir um alerta; repetir cron; agravar segundo o limiar; oscilar; repor; validar cancelamento sem push de encerramento. Exercitar reserva interrompida, falha/transporte e sucesso parcial entre dispositivos.
8. Acompanhar duas ou mais janelas do cron, última execução, pendências, aceitações/falhas e tempo. Só então autorizar a habilitação em produção. Deploy por si só não habilita alertas.
9. Em falha, desabilitar somente alertas pelo painel; monitoramento e vendas permanecem independentes. Investigar tentativas sem copiar endpoints/chaves. Falhas terminais não são reenviadas automaticamente após cinco tentativas.

## Arquivos envolvidos e testes

Configuração/autenticação: `app.py` (logout idempotente após expiração da sessão).
Persistência: `src/purchase_alert_schema.py`, migration nova e `src/db.py`.
Serviços: `src/services/manager_push.py`, `src/services/purchase_alerts.py`.
Integração: `src/routes/auth.py`, `src/routes/products.py`, `src/routes/finance.py`.
Interface/PWA: `templates/base.html`, `templates/purchase_monitoring.html`, `templates/purchase_alert_panel.html`, `static/pwa.js`, `static/service-worker.js`.
Testes: `tests/test_purchase_alerts.py`, `tests/test_purchase_monitor.py`, `tests/test_mercadopago.py`, `tests/js/pwa.test.cjs`.

```sh
APP_ENV=local DATABASE_URL='' SUPABASE_DB_URL='' PYTHONPATH=tests:. \
 .venv/bin/python -m unittest discover -s tests -q
node --test tests/js/pwa.test.cjs
```

PostgreSQL opt-in: `GPCTA_TEST_POSTGRES_SOCKET=/tmp/gpcta-pg.<identificador>`, porta 55439, somente cluster descartável local. Testes criam/removem schemas próprios. Nunca apontar para Supabase. Testes de serviço cobrem baseline, critérios, regressões/oscilações, autorização em cada dispositivo, backoff, parcial, lease, janela de resultado desconhecido, concorrência e rollback isolado. A migration é reaplicada em ambos os bancos; PostgreSQL verifica RLS. JavaScript cobre consentimento explícito, CSRF, renovação legada, revogação, deduplicação persistente e regressões de cache/atualização.

## Resultado da validação local

- Suíte relevante: **428 testes aprovados**, incluindo PostgreSQL descartável e SQLite, vendas/Pix, estoque, custos, monitoramento e alertas.
- Revalidação final de alertas e monitoramento: **79 testes aprovados**.
- JavaScript/PWA: **17 testes aprovados**; sintaxe Python/JavaScript e `git diff --check` validados.
- Descoberta completa com os bancos temporários preparados: **677 testes executados, 676 aprovados e 1 erro; nenhum ignorado**. O erro é `SportsEligibilityTest.test_existing_plan_survives_disable`: seu fixture SQLite não cria `pix_checkout_closures`, consultada pelo relatório. Foi reproduzido isoladamente em uma cópia limpa da master `5874a62`, sem estes arquivos. A suíte completa, portanto, não está integralmente verde; não se alterou esse teste/fluxo esportivo nesta entrega.

A preparação PostgreSQL adicional foi inteiramente descartável: banco novo por execução, remoção no final. Para testar os módulos Pix opt-in com `PIX_CLOSURE_TEST_POSTGRES_DSN`, o bootstrap legado exigiu compatibilização **somente nesse banco vazio**: recriação de uma FK duplicada pela migration histórica de outbox e conversão dos campos de confirmação/cancelamento/reembolso para TIMESTAMPTZ, conforme sua migration nativa. Antes disso, a preparação produzia erros de tipos/fixtures. Nenhuma alteração correspondente foi feita no código de produção ou em migrations anteriores. Os testes de alertas usam também schemas próprios, independentes desse bootstrap, e validam a migration nova reaplicada e as proteções RLS/trigger.

Não houve envio real a provedor push, migration em banco real, alteração de variáveis remotas, jobs, PR, merge ou deploy. Permanecem necessários: validar permissões runtime em homologação, aceitar o PWA v8 nos dispositivos de teste e conferir o envio real com gestores consentidos antes de autorizar produção. A falha preexistente da suíte geral deve ser tratada separadamente.

Documentação alterada: `docs/compras-inteligentes-push-gestores.md` e `docs/compras-inteligentes-operacao-pwa.md`. Ao todo são 20 arquivos desta entrega; `vercel.json`, workflows, jobs externos e serviços de vendas/estoque não foram alterados.


## Revisão final para publicação da PR

A revisão adicional confirma que o executor `run_postgres_migrations` aplica o arquivo e o registro em `schema_migrations` na mesma transação, sob o advisory lock de schema existente. O arquivo não deve receber `COMMIT` interno. No SQL Editor, uma futura aplicação manual exige uma transação explícita e o mesmo protocolo de histórico/lock; não executar o arquivo isolado em autocommit. Os limites de lock/instrução são locais à transação. O índice parcial na fila existente é a única indexação em tabela operacional; não altera seus registros, mas pode bloquear inserções durante sua criação. Timeout deve abortar toda a aplicação, incluindo o registro no histórico.

As sete tabelas novas têm RLS habilitada, sem políticas públicas e sem `FORCE ROW LEVEL SECURITY`. As unicidades de episódio/tipo/nível, destinatário/versão e número de tentativa sustentam a idempotência junto às transações e leases; não garantem entrega externa exatamente uma vez. A restrição antiga `notifiable=0` permanece intacta e não é consultada para tornar um alerta elegível.

A consulta somente de leitura à configuração Vercel confirmou a presença de `DATABASE_URL` sensível para produção e preview; seu conteúdo não é recuperável por essa interface. O código conecta com essa variável e não executa `SET ROLE`. Logo, os privilégios do papel `postgres` informados pelo operador **não comprovam a identidade efetiva do runtime**. Antes da implantação, executar pela conexão realmente usada pela aplicação (não apenas pelo SQL Editor administrativo):

```sql
BEGIN READ ONLY;
SELECT current_user, session_user, rolbypassrls
FROM pg_roles WHERE rolname = current_user;
ROLLBACK;
```

Se o runtime for `postgres` e a migration for criada por esse mesmo papel, ele será proprietário das novas tabelas/sequences. Se for outro papel, validar USAGE no schema e sequences, SELECT/INSERT/UPDATE nas novas tabelas e proprietário/BYPASSRLS (ou uma política privada expressamente revisada). Validar também SELECT/UPDATE em `users`, DELETE em `push_subscriptions` e os privilégios já usados na observação. Não conceder BYPASSRLS nem acesso público automaticamente como solução. Como as novas tabelas ainda não existem em produção, seus privilégios efetivos só poderão ser verificados após a aplicação autorizada, antes do deploy. Essa checagem continua pendente; nenhum banco real foi acessado nesta revisão.

A integração Git da Vercel está com criação automática de deployments habilitada. Publicar esta branch/PR requer impedir previamente o preview automático para cumprir a instrução de não realizar deploy. Não foram alterados jobs, configurações remotas ou credenciais.


Revalidação desta revisão: **81 testes de alertas/monitoramento aprovados** (SQLite e PostgreSQL descartável), **17 testes JavaScript/PWA aprovados**, sintaxe Python/JavaScript e `git diff --check` sem erros. A suíte completa executou **679 testes: 678 aprovados, 1 erro, nenhum ignorado**, mantendo apenas o erro esportivo preexistente descrito acima. Os dois testes novos verificam schema correto diante de tabela temporária homônima e rollback transacional da revogação. Nenhum push real foi enviado.


## Publicação da branch sem Preview automático

Antes do push de publicação, foi acrescentada ao `vercel.json` somente a seguinte regra:

```json
"git": {
  "deploymentEnabled": {
    "feat/push-compras-inteligentes-gestores": false
  }
}
```

Conforme a documentação oficial <https://vercel.com/docs/project-configuration/git-configuration>, branches não listadas continuam habilitadas. A regra impede o deployment automático desta branch por push/PR; não impede um deployment manual expressamente solicitado. Não usar `git.deploymentEnabled=false` global, nem curingas que atinjam outras branches.

Os metadados consultados confirmaram que `sistema-pelada` e `gpcta-homologacao` usam a raiz do repositório e não configuram um arquivo alternativo. A branch de produção é `master` no primeiro e `desenvolvimento` no segundo; ambas permanecem fora da regra. Nenhuma configuração remota, variável, build, rota ou cron foi alterado. A regra é versionada em commit adicional a `7a87b13` e deve estar no HEAD antes do primeiro push.

Procedimento: validar JSON e diferença restrita; conferir a regra no commit; publicar a branch; abrir PR contra master; consultar os deployments dos dois projetos para o novo SHA. Se a regra for removida posteriormente, um novo push poderá criar Preview. Um futuro merge na master continuará sujeito ao deploy automático já configurado: este procedimento não autoriza merge nem deploy.
