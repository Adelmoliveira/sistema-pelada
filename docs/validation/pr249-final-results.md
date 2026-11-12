# GPCTA — validação final do PR #249

Registro da rodada anterior. A finalização posterior do aviso de sucesso e os testes recentes estão em [pr249-urgent-final-results.md](pr249-urgent-final-results.md); esse documento posterior prevalece para prontidão de liberação.

## Etapa 1 — concluída em PR separado

[PR #250](https://github.com/Adelmoliveira/sistema-pelada/pull/250), branch `test/fix-sports-eligibility-fixture`, commit `f80fef0cc10276ec5c11744ed6e3546f850f8400`.

A fixture de `SportsEligibilityTest` agora carrega a migration oficial `20261005010000_pix_checkout_closures.sql`, adaptando somente tipos para o SQLite temporário. Nenhum código/regra de produção foi alterado. Configuração adicional apenas impede deploy automático desta branch de testes.

- Teste isolado `test_existing_plan_survives_disable`: **1/1 aprovado**, 0,244 s.
- Suíte completa nessa branch: **784 testes, 666 aprovados, 118 ignorados**, sem falhas, 88,806 s.
- `git diff --check`: aprovado.

## Etapa 2 — pendente de inspeção visual

Branch do hotfix restaurada. Browser não inicializou (`Cannot redefine property: process`). **375 px, 390 px e PWA instalado não foram aprovados visualmente.** Não há screenshots ou medidas de overflow.

O servidor local sintético foi iniciado e conferido por HTTP com login normal, senha sintética e CSRF: `/orders` 200, dois pedidos no feed, itens canceláveis e diálogo presentes. Foi encerrado após a verificação. Testes confirmam manifesto standalone, service worker sem cache para POST e elementos obrigatórios do formulário; isso não é prova visual.

Roteiro completo, matriz de inspeção e comando para recriar o ambiente: [pr249-mobile-manual.md](pr249-mobile-manual.md). O script manual está em `tests/manual_item_cancellation_app.py` e usa somente SQLite temporário.

Achado de código pendente: não há mensagem textual explícita de sucesso; o diálogo fecha e a lista atualiza. Erros são apresentados no diálogo. Resolver ou obter aceite explícito desse requisito antes da liberação.

## Etapa 3 — núcleo financeiro validado localmente

`tests/test_item_cancellation_final_validation.py` acrescenta um fluxo completo pelas rotas reais: venda em dinheiro, recebimento R$20/troco zero, cerveja R$8 entregue e cancelamento da batata R$12. Comprova venda/pagamentos/entregas originais preservados; um REFUND/auditoria/operação; estoque 10→9→10; caixa físico R$20; receita líquida R$8; repetição sem duplicidade. Relatórios HTML/PDF respondem; consumo previsto da cerveja é 1 e da batata é 0; reposição invalida a observação de estoque existente.

Comandos e resultados:

```text
PYTHONPYCACHEPREFIX=/tmp/gpcta-pycache PYTHONPATH=tests:. .venv/bin/python -m unittest test_item_cancellations test_item_cancellation_final_validation -q
Ran 21 tests in 3.469s
OK

GPCTA_TEST_POSTGRES_SOCKET=/tmp/gpcta-pg.srlopt PYTHONPYCACHEPREFIX=/tmp/gpcta-pycache PYTHONPATH=tests:. .venv/bin/python -m unittest test_item_cancellations_postgres -q
Ran 6 tests in 1.302s
OK
```

Os seis PostgreSQL incluem concorrência real, rollback financeiro/estoque, crédito concorrente na carteira, migration reaplicada e execução dos roteiros SELECT somente no schema sintético. O servidor PostgreSQL descartável foi encerrado. Nenhum endpoint ou banco de produção foi usado.

## Integração dos dois PRs sem merge

`tests/run_pr249_combined_validation.py` carrega exclusivamente o setUp corrigido do commit #250 em memória e roda toda a suíte do hotfix. Não modifica a fixture nesta branch, não faz cherry-pick/merge e não altera produção. Requer que o objeto Git do commit #250 tenha sido obtido por fetch.

Resultado final da suíte combinada: **811 testes, 687 aprovados e 124 ignorados, sem falhas**, 93,272 s.

```text
PYTHONPYCACHEPREFIX=/tmp/gpcta-pycache PYTHONPATH=tests:. .venv/bin/python tests/run_pr249_combined_validation.py
Ran 811 tests in 93.272s
OK (skipped=124)
```

Os testes PostgreSQL opt-in ficam ignorados nesse comando sem socket; os seis foram executados separadamente, como registrado acima. Não somar esses números como testes únicos, pois as suítes focadas também estão na suíte completa.

## Artefatos e decisão

- [SQL de consulta futura do pedido #813](pr249-order-813-select-only.sql), exclusivamente SELECT. Separação antes/depois da presença do schema. Não executado em produção; composição real permanece desconhecida.
- [SQL de verificação posterior](pr249-post-migration-select-only.sql), exclusivamente SELECT.
- [Checklist de aplicação e rollback](pr249-release-checklist.md): exige autorização futura, validação do papel do servidor e proíbe retorno cego ao código antigo se já houve cancelamento parcial.

**Recomendação: manter o #249 em rascunho e não liberar ainda.** Evidência financeira e testes estão favoráveis; faltam inspeção mobile/PWA efetiva e decisão sobre feedback explícito de sucesso. A correção de fixture está isolada no #250 e sua integração foi testada sem merge. Ao integrar os PRs no futuro, preservar ambas as entradas de `git.deploymentEnabled`.

Sem merge, deploy, migration de produção, consulta do #813 em produção ou alterações em dados reais. A branch final de trabalho é `hotfix/cancelamento-parcial-itens-credito`.
