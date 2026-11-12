# PR #249 — finalização do aviso de sucesso

Implementação final restrita à interface, teste JavaScript e documentação operacional. Nenhuma regra financeira ou migration foi alterada.

Após resposta HTTP bem-sucedida com `ok=true`, valor inteiro positivo confirmado e identificação correspondente da venda/item/chave, a tela apresenta:

> Item cancelado com sucesso. R$ XX,XX adicionados aos créditos do peladeiro. Estoque atualizado.

O valor vem de `operation.amount_cents`, nunca da prévia. O aviso acessível ocupa a largura disponível, quebra texto e é levado à área visível. Permanece até fechamento manual ou início de outro cancelamento, sem expiração pela atualização periódica da fila. Falha de rede, erro HTTP, JSON incompleto, operação diferente ou falta de confirmação não exibem sucesso. Corpo e chave ficam preservados em respostas incertas; o teste executa o handler real e verifica essa repetição. A proteção transacional/idempotência do servidor permanece intacta.

## Resultados executados

```text
PYTHONPYCACHEPREFIX=/tmp/gpcta-pycache PYTHONPATH=tests:. .venv/bin/python -m unittest test_item_cancellations test_item_cancellation_final_validation test_orders_cancellation_feedback -q
Ran 22 tests in 3.139s
OK
```

Inclui cancelamento, quantidades parciais, carteira/estoque/caixa, rollback, histórico preservado, relatórios/rotas, manifesto/service worker, sintaxe JS e novo teste de feedback confirmado/incerto. O handler recebeu valor sintético de R$13,75 diferente da prévia e exibiu o valor do servidor. Teste de lógica JavaScript não equivale a renderização visual.

PostgreSQL descartável: **6/6 aprovados**, concorrência com chaves iguais/diferentes, rollback, carteira concorrente e consultas SELECT. Migration validada duas vezes por fixture. Consultas atualizadas do #813 executadas apenas no schema sintético; PostgreSQL encerrado depois.

Regressão de `test_case_sales` e `test_sports_installment_eligibility`: **29/29 aprovados**, 3,712 s. Exclusivamente o setUp do commit `f80fef0cc10276ec5c11744ed6e3546f850f8400` (#250) foi carregado em memória; nenhum merge/cherry-pick ou mudança de regra de produção. Confirma compatibilidade com a correção separada da fixture.

`git diff --check` aprovado. A suíte completa combinada de 811 testes já foi validada na rodada anterior; nesta alteração de interface foram executadas as verificações focadas acima, sem apresentar o resultado anterior como nova execução.

## Mobile e prontidão

A skill de Browser repetiu `Cannot redefine property: process` antes de disponibilizar navegador. **375 px, 390 px e PWA instalado não foram inspecionados visualmente e não estão aprovados.** Não foi acessado domínio alternativo da aplicação. Ambiente sintético e roteiro em `pr249-mobile-manual.md`.

Conferência manual objetiva: iniciar `tests/manual_item_cancellation_app.py`, abrir localhost e repetir em 375×812 e 390×844. Cancelar a batata sintética, conferir R$12 no aviso, aguardar 15 segundos/atualização da fila e confirmar persistência, fechar pelo botão; repetir offline e comprovar ausência de sucesso. Inspecionar nome, quantidade, justificativa, diálogo, teclado, overflow e modo PWA. Registrar screenshots e responsável.

**Código e testes prontos para revisão final; merge ainda condicionado à inspeção manual mobile/PWA ou decisão expressa do responsável sobre essa pendência.** A antiga ausência de mensagem de sucesso foi resolvida. Nenhum requisito financeiro adicional foi introduzido.

## Produção e pedido #813

Sequência exata preparada em `pr249-production-sequence.md`: backup verificado → migration única → schema/permissões → merge autorizado → deploy autorizado → verificação exclusivamente em `https://GPCTA.com.br` → conciliação de carteira/estoque/caixa.

A consulta exclusivamente SELECT em `pr249-order-813-select-only.sql` contempla identidade/apelido Mascote, Ruffles a R$12, pagamento, entregas/restaurações, cancelamentos e créditos/ajustes anteriores. Se cerveja estiver em outra venda, não confundir a composição. A conferência do #813 NÃO foi executada em produção e nenhum cancelamento real foi realizado.

Permanecem necessárias as autorizações de migration, merge, deploy e eventual operação real. Não foi feito merge, deploy ou alteração em dados reais.
