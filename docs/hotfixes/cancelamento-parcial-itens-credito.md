# Cancelamento parcial de itens com crédito

Branch: `hotfix/cancelamento-parcial-itens-credito`.
Base auditada: `2afb1bff87a853c86558742368ad4607d3defefa` (PR #248).
O checkout estava limpo em master; somente a branch remota solicitada foi ativada para implementar.

## Auditoria e decisão financeira

- `sale_items.unit_price_cents` preserva o preço por unidade vendido. Não existe campo de desconto/rateio por item; divergência entre soma original dos itens e total da venda bloqueia o automático. Desconto já representado no preço original por unidade é suportado. Preço atual do produto nunca entra no cálculo.
- A venda normal do Bar baixa estoque na mesma transação que cria itens. `ready_for_delivery`, pagamento aprovado, composição integral e ausência de parcelamento/cancelamento são exigidos. Fluxos esportivos e parcelados usam outra reserva de estoque e ficam excluídos.
- `sale_payment_parts` mantém a composição aprovada. O hotfix suporta dinheiro, créditos e dinheiro + créditos; exige partes aprovadas somando o total original. Pix, débito, cortesia, convidados sem peladeiro e registros sem composição segura ficam bloqueados.
- Entregas são a soma de `sale_item_deliveries` menos `sale_item_delivery_restorations`. Restauração de entrega reabre a retirada; não significa reposição física. Histórico inválido e `sales.delivered_at` preenchido bloqueiam cancelamento automático.
- O cancelamento total repõe todos os itens e pode estornar todas as partes. Ele não é reutilizado para o parcial e passa a recusar vendas com cancelamento parcial. Exclusão dessas vendas também é bloqueada para preservar a trilha financeira.
- Carteira usa `bar_credit_transactions` e `bar_credit_audit`. O parcial gera REFUND específico vinculado à venda e uma operação vinculada ao item e à transação. Locks da carteira também foram aplicados a troco, recarga e ajuste manual para evitar perda de saldo em concorrência.

Nenhum dado real foi consultado ou corrigido. A composição efetiva do pedido #813 permanece por verificar em uma eventual operação administrativa autorizada; não há lógica específica para esse pedido ou peladeiro.

## Comportamento e consistência

`POST /orders/<sale_id>/cancel-item`: manager/staff, proteção CSRF existente, quantidade inteira positiva, justificativa de 5–500 caracteres, confirmação e chave de idempotência obrigatórias. Interface mobile usa diálogo com produto, quantidade disponível, quantidade solicitada, crédito, destinatário e confirmação.

Transação única: bloqueia venda (PostgreSQL FOR UPDATE / SQLite BEGIN IMMEDIATE), relê pendências, bloqueia produto e carteira, grava crédito e auditoria, repõe apenas unidades ainda vendidas e não entregues, grava operação. Falha desfaz tudo. UNIQUE na chave protege repetição; reapresentação exige os mesmos parâmetros e operador. Pedidos entregues e cancelamentos usam a mesma trava de venda.

`sales` e `sale_items` originais e `sale_payment_parts` permanecem intactos. Views `sales_net` e `sale_items_net` fornecem venda líquida e quantidade restante. Entregas, pendências, histórico do peladeiro, relatórios e painel usam essas quantidades/valores. Caixa físico mantém a entrada original. Relatórios e detalhes distinguem bruto, cancelado/crédito e líquido; cancelamento registra operador, horário, motivo, quantidade, crédito e transação.

Exemplo sintético testado: bruto recebido R$ 20, cerveja R$ 8 entregue, cancelamento do item R$ 12 → carteira +R$ 12, estoque +1, venda líquida R$ 8, recebimento original R$ 20 e entregas intactas.

## Migration

`supabase/migrations/20261111090000_sale_item_cancellations.sql` cria tabela de operações, índices e views. RLS na tabela nova; views sem acesso PUBLIC/anon/authenticated, destinadas à conexão do servidor. Não contém backfill nem mudanças em vendas/estoque/saldos reais. Validada duas vezes em cada fixture PostgreSQL (idempotência do DDL).

O bootstrap SQLite explícito inclui o schema novo; nenhuma conexão HTTP executa DDL. A migration precisa ser aplicada em uma futura janela autorizada antes de liberar o código. Não foi aplicada em produção. `vercel.json` desativa deploy automático especificamente desta branch, permitindo revisão sem preview automático.

## Compras Inteligentes

A previsão permanece baseada nas entregas físicas, sem alteração no algoritmo. Cancelamento não cria entrega nem preenche delivered_at: evita consumo fictício pelo fallback legado. UPDATE de products.stock aciona os triggers existentes de revisão de estoque e invalidação durável da observação. Não cria reposição nem envia alertas diretamente. Teste verifica evento de estoque, saldo atualizado e consumo zero do item cancelado.

## Validação e limites

Os comandos e resultados finais estão registrados na descrição do PR. Testes específicos cobrem itens entregues/não entregues, quantidades parciais, preços originais e descontos, pagamentos mistos, idempotência, concorrência, rollback, permissão, caixa, relatórios, comprovante, exclusão e cancelamento total.

A suíte original tem um erro em `test_existing_plan_survives_disable`: a fixture não cria `pix_checkout_closures`. Foi reproduzido na base sem hotfix. A cópia em /tmp também apresentou um teste de configuração local dependente de arquivos não versionados; isso não apareceu na execução no workspace.

Verificação visual mobile/PWA permanece pendente: a skill de Browser falhou ao inicializar com `Cannot redefine property: process`, antes de disponibilizar conexão. Renderização HTTP dos templates e sintaxe JavaScript (`node --check`) foram verificadas. Não houve merge, deploy ou acesso ao banco de produção.

Resultados executados:

- `PYTHONPATH=tests:. .venv/bin/python -m unittest test_item_cancellations -q`: **19/19 aprovados** (execução final, inclui node --check e renderização dos templates).
- `GPCTA_TEST_POSTGRES_SOCKET=/tmp/gpcta-pg.srlopt PYTHONPATH=tests:. .venv/bin/python -m unittest test_item_cancellations_postgres -q`: **5/5 aprovados**, PostgreSQL local descartável, migration aplicada duas vezes por fixture.
- `PYTHONPATH=tests:. .venv/bin/python -m unittest discover -s tests -q`: **807 testes**, **683 aprovados**, **123 ignorados**, **1 erro preexistente** (`pix_checkout_closures` ausente na fixture). A suíte foi executada antes da adição do 19º teste específico; os 19 específicos foram executados novamente após os ajustes finais de interface e proteção contra exclusão.
- Comparação sem hotfix em cópia temporária da base: 784 testes, mesmo erro de fixture, 118 ignorados e uma falha adicional de configuração causada pela ausência dos arquivos locais não versionados na cópia.
- `git diff --check`: aprovado.
