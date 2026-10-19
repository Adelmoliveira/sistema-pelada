# Instagram dos patrocinadores — plano e SQL para revisão

Branch: `feat/patrocinadores-instagram`, base master `3bb3ded`. A implementação é local e não foi publicada; nenhuma migration foi executada em produção.

## Plano implementado

1. Preservar `link_url` como site opcional; adicionar somente `instagram_username`, vazio por padrão.
2. Normalizar no servidor o identificador com ou sem @, ou URL HTTPS de perfil nos hosts exatos instagram.com, www.instagram.com e m.instagram.com. Gerar sempre `https://www.instagram.com/usuario/`.
3. Manter todas as combinações: nenhum contato, somente site, somente Instagram ou ambos. A logomarca abre site quando presente; caso contrário, Instagram; sem contato, não é um link.
4. Com ambos, renderizar um pequeno ícone SVG de Instagram sobre o canto da caixa existente, sem adicionar linha, texto visível ou mudar a grade. Links separados, sem âncoras aninhadas, com target=_blank, noopener/noreferrer e identificação acessível. Falha da imagem esconde o grupo inteiro, incluindo o ícone secundário.
5. Reutilizar manager ativo, CSRF, revisão concorrente, transações e invalidação de cache já existentes. A listagem gerencial é recarregada após salvar; conteúdo público pode permanecer nos caches existentes por até 60 segundos.

Não se alteraram login.html, base.html, sponsor_footer.html, copyright, manifest, service worker, escudo GPCTA de 160 px, dimensões das logomarcas ou distribuição dos apoiadores. O CSS aprovado foi preservado integralmente; somente regras novas do invólucro/ícone foram acrescentadas ao final. Não há JavaScript novo para navegação dos destinos.

## Regra de validação

O GPCTA usa validação conservadora: até 30 caracteres ASCII, letras/números/sublinhados, pontos internos sem repetição. Normaliza letras para minúsculas, remove @ inicial e descarta parâmetros de compartilhamento/fragmentos de URLs válidas. Recusa credenciais, portas alternativas, domínios parecidos, controles, barras invertidas, paths adicionais, nomes inválidos e rotas conhecidas de publicações, stories, login ou configurações. A política não verifica se a conta existe nem sua titularidade e não faz chamadas ao Instagram.

Exemplos aceitos: `@usuario.cta`, `usuario.cta`, `https://instagram.com/usuario.cta/`, `https://www.instagram.com/usuario.cta/?igsh=...`.

O site continua validado pela regra HTTPS existente. Formulários antigos que omitirem site/Instagram preservam os valores já cadastrados; enviar explicitamente o campo vazio remove esse contato. Links de Instagram anteriormente cadastrados em link_url permanecem intactos; não há extração ou mudança automática de finalidade desses links. Um valor inválido inserido diretamente no banco não gera link de Instagram no rodapé e não oculta o site válido.

## SQL exato proposto

Arquivo: `supabase/migrations/20261018060000_sponsors_instagram.sql`.

```sql
-- Additive: existing sponsor names, positions, images and sites are preserved.
-- Execute through GPCTA's transactional migration runner before deploying code.
SET LOCAL lock_timeout = '2000ms';
SET LOCAL statement_timeout = '30000ms';

ALTER TABLE sponsors
  ADD COLUMN IF NOT EXISTS instagram_username TEXT NOT NULL DEFAULT '';
```

O executor original aplica o SQL e o ledger GPCTA na mesma transação, sob o advisory lock já existente. Não inserir COMMIT interno. Para uma eventual aplicação manual no SQL Editor, preparar e revisar separadamente o wrapper de transação/advisory lock e registro no histórico; não executar este arquivo isolado em autocommit.

A alteração usa a tabela sponsors existente no schema selecionado pelo executor (public na implantação GPCTA). Confirmar esse schema e o ledger antes de aplicar. Não reexecutar pendências anteriores desconhecidas em lote. Se instagram_username já existir, verificar se é TEXT, NOT NULL e tem default vazio: IF NOT EXISTS evita duplicação, não corrige definições incompatíveis.

Não há UPDATE dos cadastros existentes, nova tabela, índice, trigger ou mudança de RLS/policies. Novos registros e registros existentes recebem o campo vazio. Reaplicar preserva valores preenchidos. DDL exige lock de tabela: usar janela de baixo movimento, com espera máxima de 2 segundos e instrução limitada a 30 segundos; uma falha deve desfazer SQL e ledger juntos.

SQLite: a inicialização explícita local verifica PRAGMA table_info antes de acrescentar a mesma coluna. Não há DDL durante requests. As migrations PostgreSQL anteriores permanecem intactas.

## Ordem para implantação futura autorizada

1. Revisar este plano, o SQL e o diff. Confirmar ambiente, backup, tabela sponsors, ledger e schema public.
2. Aplicar somente a migration autorizada pelo executor original, antes do código. Este passo não foi executado em banco real.
3. Verificar tipo/default/nulabilidade, preservação de nomes, imagens, posições e sites; Instagram deve estar vazio nos registros anteriores. Confirmar que o papel runtime pode ler e gravar a coluna. Privilégios de tabela/ownership existentes continuam aplicáveis; se houver GRANTs por coluna, revisar a coluna nova sem ampliar acesso público. RLS continua habilitada.
4. Publicar código somente com autorização. Sem a coluna, a gerência falhará e a leitura opcional do rodapé poderá omitir os patrocinadores: migration antes do código é obrigatória.
5. Em homologação, exercitar as quatro combinações, URLs copiadas do aplicativo Instagram, edição/limpeza, principal e comum, teclado/leitor de tela e navegação nos dois destinos. Conferir iPhones reais: ícone secundário fica dentro da caixa, sem nova linha ou alteração nas dimensões aprovadas.
6. Rollback operacional: reverter apenas o código, mediante autorização, mantendo a coluna e seus dados; o código anterior ignora o campo. Não remover coluna nem perder dados como rollback automático.

Não há variável de ambiente, chave da Meta, integração OAuth, dependência nova ou alteração de agendamento. Não fazer push/PR antes da revisão: esta branch não possui regra própria para bloquear o Preview automático da integração Git.

## Verificação local

Testes em SQLite e PostgreSQL descartáveis cobrem normalização, URLs perigosas, quatro destinos, rel/ARIA, autorização/CSRF, edição, limpeza, preservação de site/imagem, cache, posição principal, upgrade legado, idempotência e rollback da migration, inclusive persistência após outra conexão. A suíte JavaScript cobre PWA, prévias e desaparecimento do acesso secundário quando a logomarca falha.

Nenhuma imagem real ou perfil de patrocinador foi inventado/cadastrado; exemplos existem somente nos testes temporários.


Resultados: 133 testes Python focados aprovados, incluindo 10 específicos de Instagram e 35 de patrocinadores, com PostgreSQL/SQLite descartáveis. A revalidação final dos 10 testes de Instagram também passou após completar a marcação ARIA. JavaScript: 27 testes aprovados. Suíte completa: 724 executados, 723 aprovados e um erro esportivo preexistente em SportsEligibilityTest.test_existing_plan_survives_disable (fixture sem pix_checkout_closures), nenhum ignorado. Sintaxe Python e git diff --check aprovados. A suíte completa utilizou somente bancos temporários, com o bootstrap de teste já documentado na entrega de push.

A comparação estática confirmou que login.html, base.html e sponsor_footer.html estão idênticos à master e que o CSS aprovado foi preservado como prefixo, sem alterar nenhuma dimensão existente. O novo ícone ainda precisa de inspeção visual em aparelhos reais; o navegador automatizado está indisponível nesta sessão, conforme registrado nas revisões anteriores.

Arquivos: src/db.py, src/routes/sponsors.py, src/services/sponsors.py, static/sponsors.css, templates/sponsors.html, templates/sponsor_logo.html, supabase/migrations/20261018060000_sponsors_instagram.sql, tests/test_sponsors.py, tests/test_sponsor_instagram.py, tests/js/sponsors.test.cjs e este documento. As alterações permanecem sem commit, push, PR, merge ou deploy, aguardando revisão do plano e SQL.
