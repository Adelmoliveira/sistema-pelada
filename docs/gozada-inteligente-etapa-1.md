# Gozada Inteligente — Etapa 1/6

## Escopo e regras aprovadas

Cadastro institucional de membros da Diretoria e Oficiais Generais, ligados a
`players.id`. Não há alteração de `users.role`, cadastro de pessoas fictícias,
importação automática ou concessão de acesso ao bar.

O futuro rodízio será único entre categorias e entre quartas e sábados, com uma
vaga em DEFESA, MEIO e ATAQUE, somente na primeira partida, definido antes do
sorteio. Ausência de utilização registrada terá prioridade; depois, utilização
válida mais antiga. O histórico inicial será importado do papel em etapa futura.

**RN08: utilização significa confirmação no quadro da primeira partida, mesmo
sem entrada em campo.** A desistência antes disso não consome a vez. A presença
será manual por operador autorizado; observação Wi-Fi não comprova presença.

Nesta entrega, não existem motor, seleção, check-in, confirmação no quadro,
importação do papel, push, cron ou ativação operacional. As estruturas futuras
permanecem vazias; não há mudança em vendas, estoque e financeiro.

## Operação do cadastro

Acesso: **Súmula → Beneficiários da Gozada**, em
`/futebol/gozada/beneficiarios`, para contas ativas com papel `manager` ou
`football_manager`. São os operadores existentes; ser beneficiário não atribui
nenhum desses papéis. Não foi criada uma autorização automática para uma pessoa
por nome (inclusive Lauro).

- Cadastrar peladeiro existente, categoria e início; encerramento opcional.
- Datas são locais, ISO no banco, **inclusivas**. Estado calculado a cada consulta:
  Agendado, Ativo ou Encerrado; não há indicador ativo que possa ficar desatualizado.
- Vigências sobrepostas do mesmo peladeiro são proibidas, inclusive entre categorias.
  Para um novo mandato iniciar no dia seguinte, encerrar o anterior no dia anterior.
- Editar/encerrar requer revisão atual e justificativa de 3 a 1000 caracteres.
- Não há exclusão. Correções geram um novo evento com fotografia antes/depois,
  responsável e horário. Eventos anteriores não podem ser alterados ou removidos
  pelas operações normais do banco. Administradores do banco continuam capazes
  de desabilitar proteções: a garantia não se estende a ações de superusuário.
- Histórico paginado com 100 eventos por página. Respostas privadas `no-store`;
  não foi acrescentado cache offline nem polling.

## Persistência e concorrência

`src/football_benefit_schema.py` define o contrato compartilhado SQLite/PostgreSQL:

- `football_directorships`: mandato, vigência, revisão, autoria e justificativa.
- `football_presence_events`: confirmações/revogações e sinais de rede distintos.
- `football_benefit_sessions`: uma sessão por súmula e vínculo à primeira partida.
- `football_benefit_allocations`: reserva, confirmação no quadro, desistência,
  substituição, cancelamento e correção; posição preservada no registro.
- `football_benefit_events`: auditoria append-only; permite futura utilização
  histórica e correção com referência ao evento anterior.

Reserva/confirmado compartilham unicidade por sessão/posição e sessão/peladeiro.
O vínculo composto impede associar uma concessão ao mandato de outro peladeiro.
FKs RESTRICT preservam os registros; exclusões em cascata da súmula não podem
apagar a trilha. A presença também é append-only: revogação é novo evento.

O serviço de cadastro é proprietário da transação e deve ser chamado sem uma
transação de escrita prévia. PostgreSQL bloqueia a linha do peladeiro antes de
revalidar sobreposições e revisão; o trigger também protege gravações diretas.
SQLite usa `BEGIN IMMEDIATE`. Cadastro e evento de auditoria são atômicos.
Autorização é revalidada no serviço e na rota. CSRF global permanece ativo.

Importante para as próximas etapas: completar as regras operacionais de revisão,
seleção e auditoria de cada transição antes de abrir qualquer escrita nas tabelas
preparatórias. O esquema não implementa sozinho a política de rodízio ou presença.

## Migration e permissões

Arquivo: `supabase/migrations/20261018070000_football_benefits.sql`.

Aditiva, reaplicável sem apagar dados, com timeout de bloqueio de 2 segundos e de
instrução de 30 segundos. Não inclui COMMIT próprio: o executor GPCTA usa uma
transação por arquivo e registra **o nome completo do arquivo** em
`public.schema_migrations`, sob advisory lock `sistema-pelada-schema`.

Foi inspecionado o esquema do repositório e testado em PostgreSQL descartável com
as definições-base do próprio projeto. **Nenhum esquema real foi acessado.** O
preflight SQL confere existência e tipos inteiros reais de `users.id`, `players.id`,
`football_sumulas.id` e `football_matches.id` antes de criar FKs. Não presumir que
os bancos remotos tenham todas as colunas do bootstrap legado.

As cinco tabelas têm RLS sem políticas públicas, revogação de privilégios PUBLIC,
funções SECURITY INVOKER e nenhuma dependência de `auth.uid()`. Não há concessão
automática a `anon`/`authenticated` nem chave administrativa no navegador.

**Pré-requisito de homologação:** conferir `current_user`, proprietário das novas
tabelas e `rolbypassrls` pela conexão runtime, sem imprimir a URL. O padrão é o
papel proprietário/BYPASSRLS já usado no backend. Se runtime for outro papel,
revisar grants de tabelas/sequências e acesso sob RLS antes da implantação; não
resolver criando política pública. BYPASSRLS não substitui os grants necessários.

Nenhum SQL foi executado em Supabase. A migration precisa de aplicação controlada
antes do código PostgreSQL. O executor existente aplica **todas** as pendentes:
revisar a lista e homologar dependências antes de executá-lo. A aplicação HTTP não
executa migration PostgreSQL. Se a operação optar pelo SQL Editor, preparar antes
um script separado com transação, mesmo advisory lock e registro correto no
histórico; não colar apenas o arquivo sem coordenar o executor.

Não é necessário SQL manual adicional além da migration e de eventual ajuste
restrito de privilégios identificado na validação do papel runtime.

## Homologação (somente após autorização)

1. Em banco de homologação, conferir backup, tabelas-base, tipos, papel runtime,
   migrations pendentes e tempo de bloqueio disponível.
2. Aplicar a migration pelo procedimento controlado GPCTA e conferir histórico,
   cinco tabelas, índices, triggers e RLS. Falha implica rollback integral.
3. Publicar código em homologação somente após aprovação; a entrega atual não faz
   deploy. Confirmar que demais páginas funcionam sem consultas ao cadastro novo.
4. Com contas manager/football_manager, cadastrar diretor e oficial, encerrar e
   abrir mandato não sobreposto, corrigir dados e consultar histórico.
5. Com client/staff, testar GET/POST negados e confirmar papéis inalterados.
6. Testar abas concorrentes: uma alteração vence e a revisão antiga retorna 409.
7. Conferir que não surgem sessões, concessões, presença ou notificações e que o
   histórico não desaparece. Conferir vendas, Pix, estoque e financeiro.
8. Repetir a validação em produção somente com autorização específica.

Rollback de código: retornar ao código anterior e **manter as tabelas e os dados**.
Não executar DROP, apagar auditoria ou marcar migrations como não aplicadas.
Exclusão de súmula com dependências do benefício exige política explícita em
etapa posterior; a Etapa 1 não cria essas dependências operacionais.

## Testes locais

```sh
PYTHONPATH=.:tests .venv/bin/python -m unittest test_football_beneficiaries -q
```

Contrato PostgreSQL opcional exige servidor **descartável local**, porta 55439,
socket `/tmp/gpcta-pg.*` e `GPCTA_TEST_POSTGRES_SOCKET` apontando somente para ele.
Não usa DATABASE_URL, arquivos .env ou banco remoto. Cria e remove schemas de teste.
O contrato valida a migration real duas vezes, incluindo suas funções e triggers.

### Resultado desta entrega

- 32 testes específicos: todos passaram, com SQLite e PostgreSQL 17 descartável.
  Incluem concorrência no serviço e INSERT direto, RLS negando papel sem política,
  CSRF válido/inválido, revisão antiga, preservação do histórico e reaplicação SQL.
- Suíte Python completa: 753 testes; 120 ignorados por condições de ambiente;
  uma falha preexistente em
  `test_sports_installment_eligibility.SportsEligibilityTest.test_existing_plan_survives_disable`
  (`no such table: pix_checkout_closures`). A mesma falha foi reproduzida em cópia
  temporária limpa do commit `c20f4fc`, sem as alterações desta etapa. Não foi
  corrigida aqui para preservar o escopo.
- Cinco regressões focadas em Futebol (dashboard, escalação por tempo, terceira
  partida e importação válida/inválida) passaram.
- 27 testes JavaScript de PWA/patrocinadores passaram.
- `git diff --check` sem erros. Sem commit, PR, deploy ou alteração de banco real.

### Revisão técnica final da Etapa 1

Corrigida a presença indevida da navegação do histórico dentro do bloco `title`
do template. A navegação permanece somente no conteúdo. Adicionado teste de
regressão e ampliada a negação de POSTs diretos de create/edit/end por usuários
sem autorização. Nenhum fluxo operacional foi acrescentado.

Validação final:

- 33 testes específicos passaram com SQLite e PostgreSQL 17 descartável.
- 27 testes JavaScript passaram.
- Suíte completa: 757 testes, 122 ignorados e somente o erro preexistente
  `test_existing_plan_survives_disable`, reproduzido novamente em cópia limpa
  de `c20f4fc` (`pix_checkout_closures` ausente no banco do teste).
- Executor real testado em bancos PostgreSQL locais descartáveis: aplicação com
  registro do nome completo em schema_migrations, segunda execução sem reaplicar,
  preservação dos dados-base e rollback sem registro da migration em caso de chave
  incompatível. A migration permanece sem COMMIT próprio.
- Nenhuma inspeção ou modificação de banco remoto. Validação do esquema efetivo,
  papel runtime e privilégios continua sendo pré-requisito para homologação.

Parecer: apta para commit da Etapa 1; nenhuma autorização de merge/deploy ou
ativação operacional decorre deste parecer.
