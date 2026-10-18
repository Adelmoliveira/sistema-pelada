# Patrocinadores no PWA

## Auditoria e escopo

Base verificada: master/origin/master `08e04a0`, após a incorporação da PR #234. A branch `feat/patrocinadores-pwa` já existia exatamente nessa base, com árvore limpa; foi preservada. Não se alteraram branches anteriores.

O GPCTA utiliza Flask com blueprints registrados em `app.py`, `roles_allowed` em `src/routes/auth.py`, carga de usuário ativo por request e CSRF global. Os templates compartilham `templates/base.html` e navegação Bootstrap. Na base auditada não havia copyright nem footer nesse template; o refinamento identificou o copyright original em `login.html`, moveu seu texto para o footer compartilhado e removeu o copyright adicional do módulo. As logomarcas ficam imediatamente antes do original, com o título APOIADORES GPCTA. O novo item fica em Administração, apenas para manager, com destaque ativo também na navegação responsiva existente.

`src/db.py` adapta parâmetros e persistência SQLite/PostgreSQL, inicializa SQLite explicitamente e aplica migrations PostgreSQL fora de requests, por arquivo, com advisory lock e registro transacional em `schema_migrations`. Não há criação automática do schema PostgreSQL por request. O projeto já possui Pillow e armazenamento de imagens pequenas no banco (`material_photos.py`, branding), sem uma integração genérica obrigatória com Supabase Storage.

O service worker só coloca o shell permitido no cache offline; páginas autenticadas e APIs não são cacheadas. O novo módulo preserva integralmente essa regra, manifest, instalação, atualização, push e worker. A rota offline não consulta o catálogo. Não há polling, jobs ou integrações com compras inteligentes, estoque, financeiro ou notificações.

O plano apresentado antes das mudanças estruturais foi: persistência aditiva com posições únicas, imagens verificadas e limitadas no banco, gerência com CSRF, revisão otimista para concorrência, rodapé compartilhado responsivo e cache público curto. Não foram criadas dependências nem variáveis novas.

## Dados e acesso

Migration: `supabase/migrations/20261018050000_sponsors.sql`, gerada por `src/sponsor_schema.py`. Cria:

- `sponsor_catalog`: revisão única do catálogo para serializar alterações concorrentes.
- `sponsors`: identificação/alt text, posição principal opcional, ordem comum, ativo, link, WebP, hash do conteúdo, data e responsável pela última alteração.

As únicas posições principais são 1 e 2, com unicidade no banco. A migration reserva VINAC e Prudention nessas posições, desativadas e sem imagem. São os patrocinadores reais indicados na solicitação, não exemplos. Não há logomarca, link ou patrocinador comum pré-cadastrado. Reaplicar não modifica os registros existentes.

Todas as operações de gestão exigem usuário ativo e papel exatamente `manager`, além de CSRF. Papéis staff, client, football_manager, infra, maintenance e display não podem ler a gerência nem gravar. A revisão enviada precisa corresponder à atual; conflito retorna 409 e exige recarregar. A alteração de revisão e dados é atômica. A ordenação recebe o conjunto exato dos patrocinadores comuns e usa um único UPDATE em lote. Posições principais não são aceitas no conjunto e não são alteráveis por campos forjados.

RLS habilitada nas duas tabelas PostgreSQL, sem policies públicas, sem SECURITY DEFINER ou trigger novo. A aplicação deve utilizar papel proprietário ou BYPASSRLS e privilégios SELECT/INSERT/UPDATE/DELETE em sponsors, SELECT/UPDATE em sponsor_catalog, USAGE no schema e na sequence de sponsors. Confirmar com o papel runtime real antes do deploy; verificar postgres administrativamente não comprova a identidade runtime. Não conceder acesso a anon/authenticated nem ampliar BYPASSRLS como solução automática. O acesso público acontece somente pela aplicação, filtrando ativo, logomarca e versão exata.

## Imagens, links e armazenamento

Uploads: PNG, JPEG e WebP, tipo MIME e extensão coerentes com o conteúdo decodificado pelo Pillow. Até 2 MiB de entrada, 12 megapixels e 8000 pixels por lado; requisições declaradas acima de 3 MiB são recusadas. SVG, animações e arquivos inválidos/corrompidos são rejeitados. A imagem é reprocessada, orientada e convertida para WebP, até 800×320 e 128 KiB, preservando transparência e removendo metadados. O banco também limita o tamanho armazenado. Nenhum nome de arquivo é usado para gravar no filesystem.

Armazenamento escolhido: BYTEA no PostgreSQL/Supabase e BLOB no SQLite. O limite de 100 patrocinadores comuns, além dos dois principais, limita o conteúdo binário a 12,75 MiB mais overhead. Isso evita filesystem efêmero da Vercel, bucket/chave administrativa ou uploads órfãos: imagem e cadastro são confirmados na mesma transação. Para um catálogo muito maior, reavaliar armazenamento de objetos em outra etapa. Nada é enviado para serviços externos durante o processamento das imagens.

Links opcionais aceitam somente HTTPS público, sem credenciais, portas alternativas, controles, barras invertidas, hosts locais ou IPs privados. O servidor não acessa o endereço informado. O link abre com `noopener noreferrer sponsored`. O nome é usado como alt text, sem texto visível no rodapé. Imagem indisponível é ocultada junto do seu link, evitando ícone quebrado/nome visual como substituição.

O upload das imagens oficiais da VINAC e Prudention permanece pendente. Os gestores devem enviá-las pela interface: não buscar, inventar ou gerar marcas automaticamente.

## Cache, responsividade e PWA

A leitura do rodapé busca somente metadados públicos, sem carregar BLOBs, numa consulta única por janela de até 60 segundos por instância/ambiente. O cache é limitado e protegido por lock. Gravações invalidam o cache local imediatamente; outras instâncias atualizam em até 60 segundos. Uma falha de catálogo gera log sanitizado e rodapé vazio, com savepoint para não deixar a transação PostgreSQL abortada nem afetar telas existentes.

As imagens são servidas por `GET /sponsors/<id>/logo/<sha256>`, sem sessão e apenas quando ativas e com versão correspondente. Resposta WebP, `nosniff`, ETag/304 e cache HTTP público com max-age/s-maxage 60 segundos. Desativação/remoção pode demorar esse prazo nos caches existentes; não se trata de conteúdo privado nem de mecanismo de revogação de segredos. Metadados e páginas gerenciais não recebem cache público. A gerência responde private/no-store.

O footer só mostra a área de patrocinadores se existir alguma logomarca ativa. Quando há logos principais, a primeira linha tem duas colunas fixas: VINAC à esquerda e Prudention à direita, inclusive quando uma imagem ainda falta. As demais ficam abaixo, menores, com quebra responsiva. Se nenhuma principal tiver imagem ativa, não se reserva uma linha vazia. Sem imagem cadastrada, nenhum nome de patrocinador ou placeholder aparece no rodapé. A gerência oferece arrastar/soltar e botões acessíveis de mover para cima/baixo; a confirmação da ordem recarrega as revisões dos formulários.

Não há garantia de catálogo atualizado offline; continua existindo apenas o fallback informativo do PWA. Logos públicos podem permanecer no cache HTTP normal do navegador; o service worker não adiciona novas regras nem armazena páginas ou dados privados.

## Homologação e implantação posterior, mediante autorização

1. Conferir ambiente, backup, ledger GPCTA e pendências históricas antes de aplicar qualquer migration. Não aplicar migrations anteriores desconhecidas em lote.
2. Aplicar a migration nova pelo executor original, no ambiente autorizado, antes do código. O executor confirma DDL e ledger juntos. O arquivo não contém COMMIT interno; usa lock_timeout 2 segundos e statement_timeout 30 segundos. Se houver falha, rollback integral e nenhum registro falso no ledger. Para SQL Editor, preparar separadamente o wrapper transacional/advisory lock e histórico do GPCTA antes de executar; não aplicar em autocommit.
3. Verificar RLS, índice, posições 1/2, ausência de BLOBs e active=0 numa instalação nova. Confirmar privilégios com a conexão runtime real. Comparar também saldos/filas existentes se houver uma verificação operacional de rotina: a migration não os referencia nem altera.
4. Publicar código somente com autorização. Não há variável nova, bucket, segredo, job ou ajuste de Vercel necessário. Um futuro push desta branch pode gerar Preview pela integração Git existente; a regra específica da antiga branch de push gerencial não bloqueia esta. Não publicar a branch sem avaliar esse efeito e sem autorização.
5. Como manager, abrir Administração → Patrocinadores. Enviar as duas logomarcas oficiais, revisar link e alt text e ativar explicitamente. Não cadastrar exemplos em produção.
6. Em homologação local/isolada, testar patrocinadores comuns, ordenação por arraste e teclado/toque, desativação, remoção e dois gestores com formulários simultâneos (409 no segundo). Conferir rotas com atendente/peladeiro e sessão expirada.
7. Conferir dispositivos reais em 320/375/768 pixels e desktop, duas posições fixas, transparência, alt text, foco, links, imagem indisponível e ausência de nomes visíveis. Aguardar até 60 segundos para propagação entre instâncias. Validar PWA instalado, login/logout e fallback offline.
8. Rollback operacional: reverter somente o código após autorização; manter tabelas e imagens, sem DROP nem perda de dados. O código anterior ignora as novas tabelas. Não existe downgrade destrutivo automático.

## Validação local

```sh
APP_ENV=local DATABASE_URL='' SUPABASE_DB_URL='' PYTHONPATH=tests:. \
 .venv/bin/python -m unittest test_sponsors -q
node --test tests/js/sponsors.test.cjs tests/js/pwa.test.cjs
```

PostgreSQL opt-in: `GPCTA_TEST_POSTGRES_SOCKET=/tmp/gpcta-pg.<identificador>`, porta 55439, exclusivamente cluster descartável local. Os testes criam/removem schemas próprios. Nunca apontar esses testes para Supabase.

A verificação visual automatizada ficou indisponível: o runtime do navegador falhou ao inicializar (`Cannot redefine property: process`) antes de abrir qualquer página. Renderização HTML, ações JavaScript, autorização e persistência foram verificadas por testes; o layout CSS responsivo ainda requer inspeção visual em dispositivos reais antes da implantação.

Para PR: revisar o commit desta branch contra master e a migration antes de publicar. Não fazer merge/deploy automático. Preservar o estado desativado inicial dos patrocinadores e dos alertas inteligentes existentes.


Resultados registrados:

- **24 testes específicos de patrocinadores aprovados**, em SQLite/PostgreSQL descartáveis.
- **112 testes aprovados** na revalidação final conjunta de patrocinadores, planejamento, monitoramento e push inteligente.
- **21 testes JavaScript aprovados**: 17 do PWA e 4 de ordenação/CSRF/erros na interface.
- Suíte completa: **703 testes executados, 702 aprovados, 1 erro, nenhum ignorado**. O único erro é `SportsEligibilityTest.test_existing_plan_survives_disable`, cujo fixture SQLite não cria `pix_checkout_closures`. Foi reproduzido novamente numa cópia limpa da master atual `08e04a0`, sem o módulo de patrocinadores. Nenhuma correção no fluxo esportivo foi incluída.
- Sintaxe Python/JavaScript e `git diff --check` validados. A preparação PostgreSQL da suíte completa seguiu o procedimento descartável documentado na entrega de push; nenhum banco real foi acessado.

Arquivos: `app.py`, `src/db.py`, `src/sponsor_schema.py`, `src/routes/sponsors.py`, `src/services/sponsors.py`, `templates/base.html`, `templates/sponsor_footer.html`, `templates/sponsor_logo.html`, `templates/sponsors.html`, `static/sponsors.css`, `static/sponsors.js`, `supabase/migrations/20261018050000_sponsors.sql`, `tests/test_sponsors.py`, `tests/js/sponsors.test.cjs` e este documento.


## Refinamento de layout e exclusão — branch fix/patrocinadores-layout

Base: master `3c11737`. Não há alteração de schema ou migration nova. A imagem está na própria linha de `sponsors`; excluir qualquer patrocinador, inclusive um principal, remove os dois na mesma transação. O papel runtime passa a precisar também de DELETE em sponsors; o proprietário postgres já possui esse privilégio. Não foram executados GRANTs ou modificações em banco real.

- GET `/admin/sponsors/<id>/delete`: confirmação explícita, private/no-store. POST exige manager, CSRF, `confirm_delete=1` e revisão vigente. A exclusão de primary_slot 1/2 é permitida com a mesma confirmação; somente a posição excluída fica livre, sem mover outros registros. Revisão e exclusão são atômicas; invalidam cache local. Imagens públicas previamente cacheadas podem persistir por até 60 segundos, conforme a política existente. Depois de excluir, novos patrocinadores são acrescentados após a maior ordem existente, sem deslocar os restantes.
- GET `/admin/sponsors/<id>/logo`: prévia privada, inclusive de desativados, sem cache público. POST `/admin/sponsors/preview`: valida e otimiza upload ou imagem cadastrada, sem gravação, com CSRF e manager. Nenhuma imagem privada entra no cache do service worker.
- A remoção de fundo é opt-in, com confirmação para gerar a prévia e confirmação separada após revisá-la para salvar. Alterar arquivo/opção invalida o aceite anterior; respostas de prévias antigas são descartadas. O servidor exige `confirm_background=1` para aplicar a remoção.
- Algoritmo conservador, sem IA: opera após redução para no máximo 800×320, exige cantos opacos semelhantes e pelo menos 95% da borda na cor de fundo (tolerância máxima de 8 por canal), remove somente regiões dessa cor conectadas à borda e recusa resultados quase vazios. Não remove detalhes internos fechados nem reconstrói a marca. Recusa fundo transparente ou não uniforme; não faz segmentação agressiva. Somente margens totalmente transparentes são aparadas. Cores da marca próximas ao fundo e conectadas à borda ainda podem ser afetadas: a revisão humana da prévia é obrigatória; manter o original em caso de dúvida.
- O upload mantém os limites de 2 MiB, 12 MP, 8000 pixels por lado e armazenamento de 128 KiB. Nenhuma imagem existente é reprocessada sem ação e confirmação do gestor. Substituições seguem o mesmo fluxo transacional já existente.
- Um único copyright, com o texto original `Copyright © 2026 | Grupo de Peladas do CTA - GPCTA`, aparece após os apoiadores. O texto extra `© 2026 GPCTA` foi removido. O título discreto APOIADORES GPCTA aparece acima das marcas em todas as telas que usam o rodapé compartilhado, inclusive login; sem logos ativos, não há bloco vazio de apoiadores. Documentos para impressão, carteirinha independente e fallback offline permanecem com seus layouts próprios.
- No celular, caixas de principais com 52 px e comuns com 36 px, todas com object-fit contain. O login usa logo GPCTA menor, espaçamentos compactos, min-height 100dvh e safe areas. Não fixa altura máxima nem bloqueia overflow-y: telas pequenas, zoom, teclado e muitos apoiadores podem exigir rolagem. Conferir dispositivos reais com marcas oficiais antes de publicar.

As alterações permanecem sem commit para revisão do usuário. Não executar commit final, push, PR, merge ou deploy sem a autorização correspondente. A branch nova não tem bloqueio próprio de Preview; não publicar inadvertidamente pela integração Git.


Validação do refinamento:

- 120 testes Python na revalidação final conjunta de patrocinadores, planejamento, monitoramento e push: aprovados, com PostgreSQL/SQLite descartáveis. Inclui exclusão e imagem na mesma transação, confirmação, regras de posições principais, CSRF, revisão concorrente, ordenação após excluir, prévia privada sem gravação e remoção confirmada em imagem já cadastrada.
- 26 testes JavaScript aprovados: 17 PWA, 4 ordenação e 5 de prévia/consentimento/erros/concorrência de respostas.
- Suíte completa executada: 710 testes, 709 aprovados, um erro preexistente em SportsEligibilityTest.test_existing_plan_survives_disable (fixture SQLite sem pix_checkout_closures), nenhum ignorado. A cobertura adicional para remoção de fundo em imagem já cadastrada foi incluída depois e passou na revalidação final focada.
- Sintaxe Python/JavaScript e git diff --check aprovados.
- A inspeção visual automatizada não pôde ser executada: a inicialização do navegador continuou falhando com Cannot redefine property: process. Os testes verificam HTML, ordem dos elementos, copyright único e requisitos CSS de tamanhos/dvh/safe areas/ausência de bloqueio de rolagem; não substituem a conferência em iPhone/Android reais, zoom e teclado aberto.

Arquivos deste refinamento: src/routes/sponsors.py, src/services/sponsors.py, static/sponsors.css, static/sponsor-preview.js, templates/base.html, templates/login.html, templates/sponsor_footer.html, templates/sponsors.html, templates/sponsor_delete.html, tests/test_sponsors.py, tests/js/sponsor-preview.test.cjs e este documento. A proteção contra recriação na inicialização SQLite foi acrescentada posteriormente em src/sponsor_schema.py; nenhuma migration ou serviço de compras inteligentes, push, estoque ou financeiro foi alterado.


### Edição e exclusão de qualquer patrocinador

A decisão mais recente substitui a proteção contra exclusão dos principais: todos os registros podem ser excluídos por manager, inclusive VINAC e Prudention, mediante confirmação nominal, CSRF e revisão corrente. Os botões Editar (lápis) e Excluir (lixeira) ficam lado a lado em cada item. Editar abre o formulário com nome, link, ativo e controles de substituir/remover logomarca; salvar preserva id e primary_slot. A ordenação continua exclusiva dos comuns.

Excluir remove a linha e o BLOB associado, libera sua chave única de posição e invalida o cache local após o commit. O redirecionamento consulta novamente a listagem, exibindo o sucesso. Conflitos e validações continuam apresentando mensagens de erro sem gravação parcial. Uma posição principal excluída permanece vazia; a outra mantém seu número e nenhum comum é promovido automaticamente. A interface indica a vaga, sem preenchimento automático.

A inicialização SQLite passa a semear as duas referências iniciais somente enquanto a revisão do catálogo é 1. Assim, reinicializar um banco local depois de uma exclusão feita pela gerência não ressuscita o principal. O SQL da migration PostgreSQL já aplicada permanece byte a byte inalterado; seu executor usa o ledger e não a reaplica. Não executar manualmente o seed histórico para restaurar posições excluídas. Não há migration nova, alteração de schema ou mudança de dados em bancos reais.


Validação da revisão de edição/exclusão: 35 testes de patrocinadores aprovados em SQLite/PostgreSQL; 123 testes aprovados na revalidação conjunta com planejamento, monitoramento e push; 26 testes JavaScript aprovados. A suíte completa executou 714 testes: 713 aprovados, um erro esportivo preexistente (fixture sem pix_checkout_closures), nenhum ignorado. A confirmação nominal e a revisão agora vêm de uma única consulta, evitando snapshots divergentes durante renomeação concorrente. O ajuste passou na revalidação específica final.

Cobertura adicional: editar nome/link/ativo mantendo id e posição; conservar imagem quando não substituída; substituir/remover imagem; controles Editar/Excluir com ícones e nomes acessíveis; impedir exclusão por todos os demais papéis; excluir VINAC ou Prudention liberando sua chave única; excluir ambos sem promoção dos comuns; não recriar principais na reinicialização SQLite. A integridade da migration PostgreSQL histórica continua validada por comparação com schema_sql(True).

Nenhum commit foi criado nesta revisão; as alterações continuam na branch fix/patrocinadores-layout para aprovação. A inspeção visual em dispositivos reais permanece pendente pela indisponibilidade do navegador automatizado já registrada. Os caches públicos de imagens/metadados em outras instâncias podem conservar conteúdo por até 60 segundos; a listagem gerencial é consultada diretamente no banco após cada salvamento/exclusão, sem cache público.
