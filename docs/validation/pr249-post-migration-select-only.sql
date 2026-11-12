-- Somente SELECT. NÃO executado em produção. Usar após schema instalado.
-- Resultado esperado nas consultas de inconsistência: zero linhas.
SELECT to_regclass('public.sale_item_cancellations'),to_regclass('public.sale_items_net'),to_regclass('public.sales_net');
SELECT i.id,i.sale_id,i.quantity AS remaining_sold_quantity,COALESCE(d.quantity,0) AS net_delivered
FROM sale_items_net i LEFT JOIN (
 SELECT sid.sale_item_id,SUM(sid.quantity-COALESCE(r.quantity,0)) AS quantity
 FROM sale_item_deliveries sid LEFT JOIN sale_item_delivery_restorations r ON r.delivery_id=sid.id
 GROUP BY sid.sale_item_id
) d ON d.sale_item_id=i.id
WHERE i.quantity<0 OR COALESCE(d.quantity,0)<0 OR COALESCE(d.quantity,0)>i.quantity;

SELECT c.id,c.sale_id,c.sale_item_id FROM sale_item_cancellations c
JOIN sale_items i ON i.id=c.sale_item_id JOIN sales s ON s.id=c.sale_id
JOIN bar_credit_transactions t ON t.id=c.credit_transaction_id
WHERE i.sale_id<>c.sale_id OR t.sale_id<>c.sale_id OR t.player_id<>s.player_id
   OR t.type<>'REFUND' OR t.amount_cents<>c.amount_cents
   OR c.amount_cents<>c.quantity*i.unit_price_cents OR c.stock_restored_quantity<>c.quantity
   OR NOT EXISTS (SELECT 1 FROM bar_credit_audit a WHERE a.transaction_id=t.id
                  AND a.action='CANCELAMENTO_ITEM' AND a.amount_cents=c.amount_cents
                  AND a.actor_user_id=c.canceled_by);

SELECT id,total_cents,canceled_cents,net_total_cents FROM sales_net
WHERE net_total_cents<0 OR canceled_cents<0;

SELECT idempotency_key,COUNT(*) FROM sale_item_cancellations GROUP BY idempotency_key HAVING COUNT(*)>1;
SELECT credit_transaction_id,COUNT(*) FROM sale_item_cancellations GROUP BY credit_transaction_id HAVING COUNT(*)>1;

-- Contagem usada na decisão de rollback; se >0, NÃO voltar ao código antigo.
SELECT COUNT(*) AS irreversible_business_operations FROM sale_item_cancellations;

-- RLS e acesso das views, esperado: tabela com relrowsecurity=true;
-- views sem privilégio de PUBLIC/anon/authenticated.
SELECT relname,relrowsecurity FROM pg_class WHERE oid=to_regclass('public.sale_item_cancellations');
SELECT grantee,table_name,privilege_type FROM information_schema.table_privileges
WHERE table_schema='public' AND table_name IN ('sale_items_net','sales_net')
  AND grantee IN ('PUBLIC','anon','authenticated');
