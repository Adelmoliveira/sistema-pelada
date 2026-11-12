# Fixture de elegibilidade esportiva

Branch: `test/fix-sports-eligibility-fixture`, baseada em `2afb1bff87a853c86558742368ad4607d3defefa`.

`SportsEligibilityTest.test_existing_plan_survives_disable` visita a tela de recebíveis, cuja consulta exclui encerramentos terminais de checkout. A fixture reutilizada prepara o banco esportivo, mas não cria `pix_checkout_closures`.

Definição oficial: `supabase/migrations/20261005010000_pix_checkout_closures.sql`. O setUp desta classe executa essa definição no SQLite temporário, adaptando somente BIGSERIAL PRIMARY KEY e TIMESTAMPTZ, como os testes de encerramento de checkout existentes. A tabela vazia permite que a consulta real de recebíveis rode sem alterar critérios de elegibilidade, parcelamento ou produção.

Arquivos funcionais alterados: somente `tests/test_sports_installment_eligibility.py`. A configuração `vercel.json` desabilita deploy automático exclusivamente desta branch de testes para permitir publicar o PR sem deploy.

Resultados:

```text
PYTHONPYCACHEPREFIX=/tmp/gpcta-pycache PYTHONPATH=tests:. .venv/bin/python -m unittest test_sports_installment_eligibility.SportsEligibilityTest.test_existing_plan_survives_disable -v
Ran 1 test in 0.244s
OK

PYTHONPYCACHEPREFIX=/tmp/gpcta-pycache PYTHONPATH=tests:. .venv/bin/python -m unittest discover -s tests -q
Ran 784 tests in 88.806s
OK (skipped=118)
```

São 666 testes aprovados e 118 ignorados (incluindo integrações opt-in). Os logs de cenários de falha simulada não representam falhas da suíte. `git diff --check` aprovado.

Sem migration em produção, consulta a dados reais, merge ou deploy.
