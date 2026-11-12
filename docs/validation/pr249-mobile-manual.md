# PR #249 — inspeção mobile e PWA

## Resultado atual

**Pendente de inspeção visual real nas duas larguras.** A skill de Browser falhou antes de fornecer navegador: `Cannot redefine property: process`. Não foram obtidos screenshots, medidas de layout nem uma sessão instalada de PWA. Nenhum desses itens está visualmente aprovado.

Verificações não visuais executadas: 21 testes focados aprovados; login real com senha sintética e CSRF via HTTP em localhost; `/orders` HTTP 200; dois pedidos no feed com itens elegíveis; template com diálogo/justificativa/confirmação; manifesto `display=standalone`; service worker sem cache para POST e páginas autenticadas. Isso não comprova ausência de overflow, aspecto do modal, interação com teclado nem instalação PWA.

**Achado por leitura do código:** após sucesso, o diálogo fecha e a fila é atualizada, mas não há mensagem textual explícita de sucesso. Erros são escritos no diálogo. Como a solicitação inclui mensagens de sucesso, registrar esse ponto para ajuste/aceite antes de liberar. Não houve alteração de comportamento do produto nesta validação.

## Ambiente sintético reproduzível

Na branch `hotfix/cancelamento-parcial-itens-credito`, execute a partir da raiz:

```sh
PYTHONPYCACHEPREFIX=/tmp/gpcta-pycache PYTHONPATH=tests:. .venv/bin/python tests/manual_item_cancellation_app.py
```

Abra **http://127.0.0.1:5057/login**. Usuário sintético: `teste`; senha sintética: `gpcta-local-test`. O script prepara um SQLite temporário, remove URLs de banco externo, desabilita pagamentos externos e credenciais de envio, habilita CSRF e serve somente em loopback. Não é entrypoint de produção. Ctrl-C encerra o servidor e remove o banco; reiniciar restaura os cenários. O roteiro foi conferido por HTTP local, sem navegador.

Pedidos sintéticos:

- #1: R$ 20,00 aprovados, cerveja R$ 8,00 entregue, uma batata R$ 12,00 pendente.
- #2: R$ 44,00 aprovados, cerveja R$ 8,00 entregue, três batatas R$ 12,00 cada pendentes.
- Nome exibido: `Peladeiro sintético GPCTA` / apelido `Teste mobile`. Produtos claramente identificados como sintéticos. Não são dados do pedido real #813.

## Roteiro obrigatório em 375 e 390 px

Execute cada passo nas duas larguras, com banco reiniciado entre ciclos. Use DevTools com viewport 375×812 e 390×844, zoom 100%, orientação retrato. Registre navegador, versão, largura, horário, screenshots e resultado de cada linha. Repetir com teclado aberto e, se disponível, com fonte ampliada.

| Inspeção | 375 px | 390 px | Evidência esperada |
|---|---|---|---|
| Botão Cancelar item | Pendente | Pendente | Legível e acionável apenas na batata pendente; cerveja entregue não cancelável |
| Modal | Pendente | Pendente | Abre centralizado, campos e botões acessíveis, sem cortes, inclusive com teclado |
| Peladeiro | Pendente | Pendente | Nome completo no destinatário, sem confusão com outros pedidos |
| Quantidade/crédito | Pendente | Pendente | #1: 1 / R$12; #2: alterar de 1 para 2 muda crédito para R$24 |
| Justificativa/confirmação | Pendente | Pendente | Vazio, menos de 5 caracteres, quantidade 0/maior que pendente e confirmação desmarcada impedem operação |
| Sucesso | Pendente | Pendente | Confirmar #1: fecha diálogo, remove pedido sem pendências; registrar ausência atual de mensagem explícita |
| Erro | Pendente | Pendente | Offline durante envio: erro legível no diálogo, possibilidade de tentar novamente; nenhum sucesso falso |
| Atualização parcial | Pendente | Pendente | #2 cancelar duas: uma batata pendente, R$24 creditados, cerveja intacta; sem recriar pedido |
| Rolagem horizontal | Pendente | Pendente | Nenhuma na página, cards ou diálogo; conteúdo longo não empurra botões |
| PWA instalado | Pendente | Pendente | Mesmo fluxo em modo standalone; teclado, voltar/fechar, retorno do background e atualização da fila |

Para medir overflow, além da inspeção visual, executar no console local:

```js
({viewport: innerWidth, page: document.documentElement.scrollWidth,
  horizontalOverflow: document.documentElement.scrollWidth > innerWidth})
```

Com modal aberto, conferir também seus limites (`getBoundingClientRect`) e `scrollWidth <= clientWidth`. Capturar screenshots antes, durante e após cancelamento. Não declarar aprovação por existir CSS responsivo.

## Mensagens e segurança em falhas

1. Abra o diálogo, desligue rede pelo DevTools, tente confirmar e fotografe o erro. Religue rede e tente novamente. Conferir na aba Network que a mesma operação incerta mantém a chave de idempotência.
2. Após sucesso, repetir a requisição capturada (mesma chave e corpo) deve devolver a mesma operação, sem novo crédito. Uma nova chave para a unidade já totalmente cancelada deve retornar 409. Isso só deve ser feito neste servidor sintético.
3. Inspecionar carteira, estoque e relatório local: #1 concede R$12 e repõe uma unidade; recebimento original continua R$20, venda preservada R$8. Na fixture manual o estoque corrente inicial da batata é 5 e deve passar a 6 nesse primeiro cancelamento.
4. O teste automatizado financeiro separado usa venda real pelas rotas e verifica estoque 10 → 9 → 10, caixa R$20 e venda líquida R$8.

## PWA

Conferir manifesto e registro ativo em Application → Manifest/Service Workers, então instalar pelo navegador que suporte localhost. Repetir o ciclo em janela standalone e após encerrar/reabrir. Para Safari/iOS físico, usar somente laboratório com HTTPS local e dados sintéticos; não usar produção para essa validação. Em modo offline a operação não deve ser enfileirada ou apresentada como concluída; o service worker não intercepta POST.

A aprovação PWA exige inspeção efetiva nessa modalidade, não apenas o teste de manifesto. Registrar o ambiente exato e screenshots antes de encerrar esta pendência.
