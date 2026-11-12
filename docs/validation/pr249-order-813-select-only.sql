-- PostgreSQL: roteiro futuro de consulta, NÃO executado em produção.
-- Somente SELECT, sem locks explícitos, DDL, correções ou funções com efeitos colaterais.
-- Executar com credencial read-only e transação/snapshot read-only configurados pelo cliente.
-- A composição e o peladeiro devem ser identificados pelo banco, não presumidos.

-- 1. Venda original, titular e recebimento contabilizado (centavos).
SELECT s.id,s.player_id,p.name,p.war_name,s.event_id,s.guest_name,
       s.payment_method,s.total_cents,s.paid,s.payment_status,
       s.created_at,s.paid_at,s.ready_for_delivery,s.delivered_at,s.delivered_by,
       s.mercadopago_order_id,s.mercadopago_payment_id
FROM sales s LEFT JOIN players p ON p.id=s.player_id WHERE s.id=813;

-- 2. Itens originais e preços históricos; preço atual não comprova valor vendido.
SELECT i.id AS sale_item_id,i.sale_id,i.product_id,p.name,
       i.quantity,i.unit_price_cents,i.quantity*i.unit_price_cents AS original_line_cents,
       i.unit_cost_cents,p.stock AS current_stock,p.price_cents AS current_catalog_price_cents
FROM sale_items i JOIN products p ON p.id=i.product_id
WHERE i.sale_id=813 ORDER BY i.id;

-- 3. Composição financeira, incluindo partes não aprovadas e estornadas.
SELECT id,sale_id,method,amount_cents,status,payment_id,external_reference,
       created_at,confirmed_at,canceled_at,refunded_at
FROM sale_payment_parts WHERE sale_id=813 ORDER BY id;

-- 4. Conciliação de totais sem multiplicar itens por pagamentos/entregas.
SELECT s.id,s.total_cents,
       (SELECT COALESCE(SUM(i.quantity*i.unit_price_cents),0)
        FROM sale_items i WHERE i.sale_id=s.id) AS original_items_cents,
       (SELECT COALESCE(SUM(pp.amount_cents),0) FROM sale_payment_parts pp
        WHERE pp.sale_id=s.id AND pp.status='approved') AS approved_parts_cents
FROM sales s WHERE s.id=813;

-- 5. Entregas e restaurações auditadas separadamente das devoluções físicas ao estoque.
SELECT d.id AS delivery_id,d.delivery_operation_id,i.id AS sale_item_id,i.product_id,
       d.quantity,d.delivered_at,d.delivered_by,u.name AS delivered_by_name,
       r.quantity AS restored_delivery_quantity,r.restored_at,r.restored_by,r.reason,
       d.quantity-COALESCE(r.quantity,0) AS net_delivered_quantity
FROM sale_items i JOIN sale_item_deliveries d ON d.sale_item_id=i.id
LEFT JOIN sale_item_delivery_restorations r ON r.delivery_id=d.id
LEFT JOIN users u ON u.id=d.delivered_by
WHERE i.sale_id=813 ORDER BY d.id;

SELECT sc.*,u.name AS operator_name FROM sale_cancellations sc
LEFT JOIN users u ON u.id=sc.canceled_by WHERE sc.sale_id=813;

-- 6. Transações vinculadas à venda e respectivas auditorias.
SELECT t.id,t.player_id,t.type,t.amount_cents,t.balance_after_cents,t.description,
       t.sale_id,t.topup_id,t.created_by,t.created_at
FROM bar_credit_transactions t WHERE t.sale_id=813 ORDER BY t.id;

SELECT a.* FROM bar_credit_audit a
WHERE a.transaction_id IN (SELECT t.id FROM bar_credit_transactions t WHERE t.sale_id=813)
ORDER BY a.id;

-- 7. Saldo atual e últimas transações do titular: contexto, não prova de saldo à época.
SELECT a.* FROM bar_credit_accounts a
WHERE a.player_id=(SELECT player_id FROM sales WHERE id=813);

SELECT t.* FROM bar_credit_transactions t
WHERE t.player_id=(SELECT player_id FROM sales WHERE id=813)
ORDER BY t.created_at DESC,t.id DESC LIMIT 200;

SELECT r.* FROM bar_credit_reservations r WHERE r.sale_id=813;

-- 8. Pedidos próximos do MESMO titular (cerveja pode estar em outro pedido).
-- Janela exploratória de +/- um dia; ausência aqui não prova inexistência fora da janela.
SELECT s.id,s.player_id,s.created_at,s.paid_at,s.payment_method,s.paid,s.payment_status,
       s.total_cents,i.id AS sale_item_id,p.name,i.quantity,i.unit_price_cents
FROM sales target JOIN sales s ON s.player_id=target.player_id
LEFT JOIN sale_items i ON i.sale_id=s.id LEFT JOIN products p ON p.id=i.product_id
WHERE target.id=813
  AND s.created_at::timestamp BETWEEN target.created_at::timestamp-INTERVAL '1 day'
                                 AND target.created_at::timestamp+INTERVAL '1 day'
ORDER BY s.created_at,s.id,i.id;

-- Sinais para conferência humana do #813 ANTES de qualquer cancelamento real.
-- Não autorizam escrita e não substituem a leitura de todas as linhas acima.
SELECT s.id,p.name,p.war_name,
       lower(trim(COALESCE(p.war_name,'')))='mascote' AS nickname_matches_mascote,
       s.paid=1 AND s.payment_status='approved' AS payment_approved,
       EXISTS(SELECT 1 FROM sale_items i JOIN products prod ON prod.id=i.product_id
              WHERE i.sale_id=s.id AND prod.name ILIKE '%Ruffles%'
                AND i.unit_price_cents=1200) AS has_ruffles_at_1200,
       EXISTS(SELECT 1 FROM sale_cancellations c WHERE c.sale_id=s.id) AS has_whole_cancellation,
       EXISTS(SELECT 1 FROM bar_credit_transactions t
              WHERE t.sale_id=s.id AND t.amount_cents>0) AS has_any_prior_linked_credit
FROM sales s LEFT JOIN players p ON p.id=s.player_id WHERE s.id=813;

-- Créditos manuais sem sale_id podem existir: revisar auditoria do titular;
-- texto contendo 813 é apenas indício, não vínculo contábil comprovado.
SELECT a.* FROM bar_credit_audit a
WHERE a.player_id=(SELECT player_id FROM sales WHERE id=813)
  AND (a.amount_cents>0 OR a.reason ILIKE '%813%')
ORDER BY a.created_at DESC,a.id DESC LIMIT 200;

-- 9. Inspeção da presença do schema do hotfix; não executar a seção 10 se ausente.
SELECT to_regclass('public.sale_item_cancellations') AS cancellations_table,
       to_regclass('public.sale_items_net') AS items_view,
       to_regclass('public.sales_net') AS sales_view;

-- 10. SOMENTE APÓS a migration existir no ambiente consultado.
SELECT c.*,u.name AS operator_name,t.type,t.player_id,t.amount_cents AS ledger_cents,
       t.balance_after_cents
FROM sale_item_cancellations c JOIN bar_credit_transactions t ON t.id=c.credit_transaction_id
LEFT JOIN users u ON u.id=c.canceled_by WHERE c.sale_id=813 ORDER BY c.id;

SELECT * FROM sale_items_net WHERE sale_id=813 ORDER BY id;
SELECT id,total_cents,canceled_cents,net_total_cents FROM sales_net WHERE id=813;

-- Limites: sales.total_cents/partes não provam o numerário entregue em mãos se houve
-- troco não persistido. Estoque atual não reconstitui um ledger histórico completo.
-- Não inferir que R$20, cerveja e batata pertencem à mesma venda sem os resultados.
