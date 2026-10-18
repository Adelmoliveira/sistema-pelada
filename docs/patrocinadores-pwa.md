# Patrocinadores no PWA

## Auditoria e escopo

Base verificada: master/origin/master `08e04a0`, após a incorporação da PR #234. A branch `feat/patrocinadores-pwa` já existia exatamente nessa base, com árvore limpa; foi preservada. Não se alteraram branches anteriores.

O GPCTA utiliza Flask com blueprints registrados em `app.py`, `roles_allowed` em `src/routes/auth.py`, carga de usuário ativo por request e CSRF global. Os templates compartilham `templates/base.html` e navegação Bootstrap. Na base auditada não havia copyright nem footer nesse template; este módulo acrescenta copyright GPCTA e as logomarcas logo abaixo. O novo item fica em Administração, apenas para manager, com destaque ativo também na navegação responsiva existente.

`src/db.py` adapta parâmetros e persistência SQLite/PostgreSQL, inicializa SQLite explicitamente e aplica migrations PostgreSQL fora de requests, por arquivo, com advisory lock e registro transacional em `schema_migrations`. Não há criação automática do schema PostgreSQL por request. O projeto já possui Pillow e armazenamento de imagens pequenas no banco (`material_photos.py`, branding), sem uma integração genérica obrigatória com Supabase Storage.

O service worker só coloca o shell permitido no cache offline; páginas autenticadas e APIs não são cacheadas. O novo módulo preserva integralmente essa regra, manifest, instalação, atualização, push e worker. A rota offline não consulta o catálogo. Não há polling, jobs ou integrações com compras inteligentes, estoque, financeiro ou notificações.

O plano apresentado antes das mudanças estruturais foi: persistência aditiva com posições únicas, imagens verificadas e limitadas no banco, gerência com CSRF, revisão otimista para concorrência, rodapé compartilhado responsivo e cache público curto. Não foram criadas dependências nem variáveis novas.

## Dados e acesso

Migration: `supabase/migrations/20261018050000_sponsors.sql`, gerada por `src/sponsor_schema.py`. Cria:

- `sponsor_catalog`: revisão única do catálogo para serializar alterações concorrentes.
- `sponsors`: identificação/alt text, posição principal opcional, ordem comum, ativo, link, WebP, hash do conteúdo, data e responsável pela última alteração.

As únicas posições principais são 1 e 2, com unicidade no banco. A migration reserva VINAC e Prudention nessas posições, desativadas e sem imagem. São os patrocinadores reais indicados na solicitação, não exemplos. Não há logomarca, link ou patrocinador comum pré-cadastrado. Reaplicar não modifica os registros existentes.

Todas as operações de gestão exigem usuário ativo e papel exatamente `manager`, além de CSRF. Papéis staff, client, football_manager, infra, maintenance e display não podem ler a gerência nem gravar. A revisão enviada precisa corresponder à atual; conflito retorna 409 e exige recarregar. A alteração de revisão e dados é atômica. A ordenação recebe o conjunto exato dos patrocinadores comuns e usa um único UPDATE em lote. Posições principais não são aceitas no conjunto e não são alteráveis por campos forjados.

RLS habilitada nas duas tabelas PostgreSQL, sem policies públicas, sem SECURITY DEFINER ou trigger novo. A aplicação deve utilizar papel proprietário ou BYPASSRLS e privilégios SELECT/INSERT/UPDATE em sponsors, SELECT/UPDATE em sponsor_catalog, USAGE no schema e na sequence de sponsors. Confirmar com o papel runtime real antes do deploy; verificar postgres administrativamente não comprova a identidade runtime. Não conceder acesso a anon/authenticated nem ampliar BYPASSRLS como solução automática. O acesso público acontece somente pela aplicação, filtrando ativo, logomarca e versão exata.

## Imagens, links e armazenamento

Uploads: PNG, JPEG e WebP, tipo MIME e extensão coerentes com o conteúdo decodificado pelo Pillow. Até 2 MiB de entrada, 12 megapixels e 8000 pixels por lado; requisições declaradas acima de 3 MiB são recusadas. SVG, animações e arquivos inválidos/corrompidos são rejeitados. A imagem é reprocessada, orientada e convertida para WebP, até 800×320 e 128 KiB, preservando transparência e removendo metadados. O banco também limita o tamanho armazenado. Nenhum nome de arquivo é usado para gravar no filesystem.

Armazenamento escolhido: BYTEA no PostgreSQL/Supabase e BLOB no SQLite. O limite de 100 patrocinadores comuns, além dos dois principais, limita o conteúdo binário a 12,75 MiB mais overhead. Isso evita filesystem efêmero da Vercel, bucket/chave administrativa ou uploads órfãos: imagem e cadastro são confirmados na mesma transação. Para um catálogo muito maior, reavaliar armazenamento de objetos em outra etapa. Nada é enviado para serviços externos durante o processamento das imagens.

Links opcionais aceitam somente HTTPS público, sem credenciais, portas alternativas, controles, barras invertidas, hosts locais ou IPs privados. O servidor não acessa o endereço informado. O link abre com `noopener noreferrer sponsored`. O nome é usado como alt text, sem texto visível no rodapé. Imagem indisponível é ocultada junto do seu link, evitando ícone quebrado/nome visual como substituição.

O upload das imagens oficiais da VINAC e Prudention permanece pendente. Os gestores devem enviá-las pela interface: não buscar, inventar ou gerar marcas automaticamente.

## Cache, responsividade e PWA

A leitura do rodapé busca somente metadados públicos, sem carregar BLOBs, numa consulta única por janela de até 60 segundos por instância/ambiente. O cache é limitado e protegido por lock. Gravações invalidam o cache local imediatamente; outras instâncias atualizam em até 60 segundos. Uma falha de catálogo gera log sanitizado e rodapé vazio, com savepoint para não deixar a transação PostgreSQL abortada nem afetar telas existentes.

As imagens são servidas por `GET /sponsors/<id>/logo/<sha256>`, sem sessão e apenas quando ativas e com versão correspondente. Resposta WebP, `nosniff`, ETag/304 e cache HTTP público com max-age/s-maxage 60 segundos. Desativação/remoção pode demorar esse prazo nos caches existentes; não se trata de conteúdo privado nem de mecanismo de revogação de segredos. Metadados e páginas gerenciais não recebem cache público. A gerência responde private/no-store.

O footer só mostra a área de patrocinadores se existir alguma logomarca ativa. Quando há logos, a primeira linha tem sempre duas colunas fixas: VINAC à esquerda e Prudention à direita, inclusive quando uma imagem ainda falta. As demais ficam abaixo, menores, com quebra responsiva. Sem imagem cadastrada, nenhum nome de patrocinador ou placeholder aparece no rodapé. A gerência oferece arrastar/soltar e botões acessíveis de mover para cima/baixo; a confirmação da ordem recarrega as revisões dos formulários.

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
