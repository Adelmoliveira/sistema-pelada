# PR #249 — sequência exata preparada, não executada

Somente a interface e seus testes foram ajustados nesta finalização; regras financeiras e migration permanecem as revisadas. O domínio de verificação da aplicação será exclusivamente **https://GPCTA.com.br**. Não abrir URLs de preview, domínio do provedor ou outros aliases; se houver redirecionamento para outro domínio, parar e verificar a configuração.

## 1. Backup e verificação do banco

O responsável autorizado configura perfis libpq distintos para backup, migration e leitura do mesmo banco GPCTA, sem expor credenciais no terminal/PR. Conferir host/database/papel com SELECT antes de prosseguir. Manter operadores em janela controlada e preservar o estado dos pedidos em voo.

Comandos preparados, NÃO executados:

```sh
pg_dump --dbname='service=gpcta_backup' --format=custom --file=/caminho-seguro/gpcta-pre-hotfix249.dump
pg_restore --list /caminho-seguro/gpcta-pre-hotfix249.dump > /caminho-seguro/gpcta-pre-hotfix249.contents
shasum -a 256 /caminho-seguro/gpcta-pre-hotfix249.dump
```

Exigir saída zero, arquivo não vazio e inventário com vendas, itens, partes de pagamento, entregas/restaurações, produtos e carteira/ledger/auditoria. Verificar restaurabilidade em PostgreSQL descartável, fora de produção; listar o arquivo sozinho não prova recuperação. Registrar checksum, horário e responsável. Não prosseguir sem backup recuperável.

## 2. Aplicar somente `20261111090000_sale_item_cancellations.sql`

Somente após autorização explícita da migration, usar o executor oficial `run_postgres_migrations`, com diretório temporário contendo apenas esse arquivo. Isso mantém lock de schema e registro em `schema_migrations` e evita aplicar outras migrations pendentes. Não usar bootstrap monolítico.

Trecho preparado para a sessão autorizada (não executado):

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from shutil import copy2
from src.db import run_postgres_migrations

name = '20261111090000_sale_item_cancellations.sql'
with TemporaryDirectory() as stage:
    copy2(Path('supabase/migrations') / name, Path(stage) / name)
    applied = run_postgres_migrations('service=gpcta_migration', stage)
    assert applied in ([name], []), applied
    print(applied)
```

Se já constar como aplicada, verificar schema/conteúdo em vez de presumir sucesso. Interromper em qualquer erro. Nenhuma correção de #813 faz parte dessa aplicação.

## 3. Verificar tabela, views e permissões

Executar `pr249-post-migration-select-only.sql` com papel de leitura autorizado. Conferir tabela `sale_item_cancellations`, índices únicos de chave/transação, views `sale_items_net`/`sales_net`, RLS e ausência de acesso PUBLIC/anon/authenticated às views. Confirmar também o registro da migration:

```sql
SELECT version,applied_at FROM schema_migrations
WHERE version='20261111090000_sale_item_cancellations.sql';
```

O papel real do servidor precisa conseguir ler views e registrar operações sob o mecanismo de acesso já usado pela aplicação. Confirmar isso em homologação com esse papel; o teste local como owner não comprova permissões de produção. Consultas de inconsistência devem ser comparadas ao baseline e investigadas, sem ajustes automáticos.

## 4. Merge autorizado

Antes do merge, concluir a inspeção manual em 375/390 px/PWA ou obter decisão expressa do responsável sobre essa pendência. Revisar testes e commit final do #249, e compatibilidade com #250. Preservar ambas as flags de branch que impedem preview automático.

**Merge e deploy são autorizações distintas.** A master possui deploy automático por padrão; sob autorização do responsável, pausar esse automatismo na plataforma antes do merge para que a etapa 4 não antecipe a etapa 5. Se não houver esse controle, não executar o merge isoladamente. Nenhuma configuração de produção foi alterada nesta preparação.

## 5. Deploy autorizado

Com autorização expressa separada, liberar somente o commit revisado/merge aprovado, após schema validado. Não executar migrations em requests. Confirmar que todas as instâncias usam leitores de quantidades líquidas; não manter versões antigas e novas processando entregas/cancelamentos simultaneamente. Não abrir domínio de preview para verificar a aplicação.

## 6. Verificar exclusivamente em `https://GPCTA.com.br`

Conferir HTTPS, domínio, login, saúde (`/health`), Bar → Pedidos (`/orders`), formulário, aviso de sucesso e atualização PWA. Nenhuma visita ou requisição a esse domínio foi realizada nesta tarefa. Não cancelar #813 nem criar venda sintética em produção como smoke test. A operação real exige autorização própria após a conferência somente de leitura.

## 7. Conferir créditos, estoque e caixa

Conciliar somente uma operação real explicitamente autorizada: uma transação de crédito e auditoria vinculadas ao item/venda, quantidade restaurada correta, entrega da cerveja preservada, recebimento original e caixa físico preservados, receita líquida correta e atualização do monitoramento de estoque. Não interpretar crédito como saída de dinheiro. Reexecutar SELECT de invariantes e conferir ausência de duplicidade.

## Pedido #813 — conferência exclusivamente de leitura, sem cancelamento

Usar `pr249-order-813-select-only.sql` com credencial read-only. Conferir pessoalmente:

- ID/nome/apelido identificam Mascote; apelido sozinho não resolve homônimos.
- A batata realmente consta no #813, produto correto, preço histórico R$12, quantidade e pendência suficientes. Se não constar, não presumir composição: localizar a venda correta pelas consultas próximas.
- Pagamento aprovado e partes somando o total original; revisar se o pagamento R$20 está dividido em pedidos. O numerário entregue em mãos não é reconstruído apenas pelo total de uma venda.
- Retiradas/restaurações e qualquer delivered_at legado. A cerveja R$8 pode estar em outro pedido; identificar e preservar sua entrega sem cancelá-la, restaurá-la ou excluir sua venda.
- Nenhum cancelamento parcial/total, REFUND, crédito positivo vinculado ou ajuste manual anterior referente ao caso. Revisar ledger e auditoria do titular; ausência de vínculo `sale_id` não prova que nunca houve ajuste manual.

Se identidade, valores, entrega ou créditos anteriores não forem inequívocos, bloquear a ação real e reconciliar antes. Não foi feita essa consulta em produção e nenhum cancelamento real foi executado.

## Rollback

Antes de qualquer operação parcial, código anterior pode ser considerado pelo responsável sob manutenção, após provar contagem zero e ausência de requisições em voo; manter schema aditivo é preferível. **Após uma operação parcial, não voltar cegamente ao código antigo:** ele pode reentregar ou recreditar os originais. Manter views/ledger/auditoria e leitores líquidos, suspender operações afetadas e corrigir progressivamente. Não apagar créditos/cancelamentos nem restaurar apenas estoque/carteira isoladamente. Detalhes em `pr249-release-checklist.md`.
