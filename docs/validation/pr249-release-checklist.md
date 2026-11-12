# PR #249 — decisão de liberação e implantação futura

**Recomendação atual: não liberar para produção ainda.** O núcleo financeiro foi aprovado em cenários sintéticos e a fixture tem correção separada no PR #250, mas faltam inspeção visual em 375/390 px e PWA instalado. Há também ausência de mensagem explícita de sucesso no fluxo atual, identificada por leitura do código. Confirmar ajuste/aceite desse requisito e concluir a inspeção antes de autorizar implantação.

## Evidências da revisão

Base revisada do #249: `7f8d1dc03f99dea7ef0a829c2e75764aa1b8200a`. As mudanças desta validação acrescentam testes e documentação, sem mudar regras de produção.

| Regra | Evidência local |
|---|---|
| Recebimento original R$20 preservado | Teste cria venda em dinheiro, confirma `amount_received_cents=2000` e troco zero; `sales` e `sale_payment_parts` permanecem byte a byte iguais após cancelamento |
| Cerveja entregue R$8 preservada | Histórico de entrega intacto, estoque da cerveja 9 após venda de uma unidade, receita líquida 800 centavos |
| Crédito R$12 pela batata | Uma transação REFUND de 1200 centavos, saldo +1200 e auditoria vinculada à operação/item/venda |
| Uma unidade volta ao estoque | Venda pelas rotas reduz 10→9; cancelamento repõe 9→10 |
| Sem duplicidade | Mesma chave retorna mesma operação; chave nova após esgotar pendência retorna 409; testes PostgreSQL com requisições concorrentes e carteira concorrente |
| Receita e caixa | Relatório mensal bruto 2000, cancelado/crédito 1200, líquido 800; caixa esperado e vendas em dinheiro físico 2000; nenhum movimento de saída de numerário criado |
| Relatórios | Pedidos, comprovante, relatório de vendas, detalhe HTML e PDF respondem; totais e quantidades líquidas conferidos |
| Compras Inteligentes | Consumo físico: cerveja 1, batata 0; reposição gera invalidação durável de estoque; previsão e regras de monitoramento não foram modificadas |

Pagamentos automáticos suportados: dinheiro, créditos e dinheiro+créditos com partes aprovadas somando o original. Desconto registrado no preço original por unidade é suportado; desconto global sem rateio comprovado e outros meios são bloqueados. Nenhuma condição depende do pedido #813 ou de um nome de peladeiro.

Migration revisada: `20261111090000_sale_item_cancellations.sql`. Cria tabela de operações, índices e views; sem backfill/atualização de vendas, carteira ou estoque. RLS na tabela nova e revogação de acesso às views para PUBLIC/anon/authenticated. Conexão do servidor deve ter acesso e operar com o mecanismo atual de owner/BYPASSRLS. As views calculam quantidades e receita líquidas sem apagar os originais.

Limites: o schema não é um ledger completo de todas as baixas históricas; o serviço exige as evidências do fluxo normal de venda, pagamento e entrega. A migration protege chaves e vínculos básicos; limites agregados entre entrega/cancelamento dependem do serviço e do lock da venda. Escritas diretas fora do serviço não fazem parte do fluxo suportado.

## Consultas futuras, não executadas em produção

- `pr249-order-813-select-only.sql`: venda original, itens, pagamentos, entregas/restaurações, cancelamentos, carteira/auditoria e pedidos próximos do mesmo titular. A seção posterior ao schema é opcional e só deve ser executada quando a migration existir.
- `pr249-post-migration-select-only.sql`: presença do schema, limites de quantidade/valor, correspondência com ledger/auditoria, duplicidade, RLS e privilégios.

Ambos contêm exclusivamente SELECT e comentários. Foram executados somente no PostgreSQL descartável com dados sintéticos para verificar sintaxe/colunas. Não se verificou a composição real do #813. Saldo/estoque atuais não comprovam, sozinhos, o estado histórico; `amount_received_cents` do numerário entregue e troco não convertido não são persistidos como campos próprios desse fluxo.

## Antes da janela autorizada

- [ ] Revisar/aprovar separadamente o PR #250 e a integração da fixture com o hotfix. Não depende de mudança financeira em produção.
- [ ] Concluir e anexar a matriz visual em `pr249-mobile-manual.md`; decidir/implementar feedback explícito de sucesso e retestar, se exigido.
- [ ] Aprovar explicitamente commit, equipe/projeto/ambiente, migration e responsável pela janela. Esta documentação não autoriza execução.
- [ ] Confirmar backup recuperável de vendas, itens, partes, entregas, carteira, auditoria e produtos, além do procedimento de recuperação integral consistente. Não registrar credenciais nos artefatos.
- [ ] Registrar baseline com SELECT, versões/migrations instaladas, schema `public`, índices, grants e papéis usados pelo servidor. Verificar que o nome das views não conflita com objetos existentes: CREATE OR REPLACE não garante equivalência do objeto anterior.
- [ ] Executar em homologação sintética a migration e o commit exatos, testes e inspeção. Verificar que nenhum consumidor externo depende de grants revogados das views novas.
- [ ] Definir suspensão das operações de cancelamento e entrega durante a troca para evitar instâncias antigas e novas processando o mesmo pedido. A migration sozinha não habilita o botão; o código novo depende dela.

## Aplicação futura (somente com autorização)

- [ ] Aplicar exclusivamente a migration versionada pelo executor oficial e registrar sua versão; não rodar bootstrap monolítico em produção. Avaliar locks/timeouts e transação da ferramenta utilizada antes da janela.
- [ ] Confirmar tabela, índices, views e privilégios com o SELECT de pós-verificação usando o papel do servidor e um papel sem acesso. A aplicação deve enxergar views/ledger; clientes diretos anon/authenticated não devem ler as views.
- [ ] Somente após o schema validado, liberar o commit novo por implantação separadamente autorizada e garantir que todas as instâncias usam leitores de quantidades líquidas.
- [ ] Validar fluxos com dados sintéticos em ambiente apropriado; não criar venda fictícia ou modificar o #813 em produção como smoke test.
- [ ] Retomar operadores após verificação de saúde/rotas, ausência de erros de schema e aprovação do responsável. Confirmar cache/atualização PWA em dispositivos do atendimento.

## Verificação posterior

- [ ] Revisar as consultas de inconsistência: esperado zero linhas. Investigar divergências preexistentes sem corrigi-las automaticamente.
- [ ] Conciliar operação real autorizada: valor bruto original, valor cancelado, crédito e saldo, quantidade reposta, item entregue preservado; rastrear IDs de venda/item/operação/ledger/auditoria.
- [ ] Confirmar dinheiro físico e fechamento de caixa preservados; crédito concedido não pode virar saída fictícia de caixa.
- [ ] Conferir relatórios mensal/detalhado/PDF e histórico do peladeiro; quantidades canceladas não podem reaparecer como pendentes.
- [ ] Confirmar invalidações de estoque processadas por Compras Inteligentes, sem erros de schema, consumo fictício ou reposições/alertas duplicados; manter o fluxo agendado normal.
- [ ] Monitorar erros/latência do feed, carteira, cancelamento e entregas. Parar novas operações se houver inconsistência financeira ou de estoque.

## Rollback seguro

**Não fazer rollback cego para o código anterior depois de qualquer cancelamento parcial.** O código antigo considera as quantidades e totais originais e pode reentregar unidades canceladas ou estornar novamente. Crédito pode já ter sido utilizado; eliminar linhas não é reversão financeira.

- Se a migration falhar antes do código novo: manter a versão anterior, impedir liberação do hotfix e investigar. Confirmar rollback transacional da ferramenta; se houver objetos parciais, revisar individualmente, sem DROP CASCADE automático.
- Se a migration foi aplicada, mas não houve nenhum cancelamento parcial e não há requisições em voo: sob manutenção, o responsável pode autorizar retorno do aplicativo à versão anterior. Confirmar `COUNT(*)=0` de operações em snapshot estável. Preferir manter o schema aditivo para retomar a correção depois, sem removê-lo automaticamente.
- Se existe ao menos uma operação confirmada: manter schema, ledger, auditoria e leitores líquidos. Suspender novos cancelamentos e, conforme incidente, entregas/estornos, por manutenção ou patch revisado; preparar correção progressiva compatível com o histórico. O PR não implementa feature flag de rollback: não presumir que existe um toggle pronto.
- Não apagar operações, não zerar carteira e não subtrair estoque manualmente. Qualquer compensação deve ser uma nova operação financeira/estoque auditada e explicitamente autorizada, considerando créditos já consumidos.
- Restauração de backup exige decisão excepcional e recuperação coordenada de todos os domínios; um restore isolado de estoque ou carteira produz divergência. Não está autorizado por este checklist.

Nenhum desses passos de produção, merge, aplicação de migration ou deploy foi executado nesta tarefa. Deploy automático das branches de revisão permanece desabilitado em seus commits.
