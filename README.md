# BAR PELADEIROS GPCTA

MVP local para cadastrar peladeiros e produtos, registrar vendas, baixar estoque, controlar reposições, Pix, débito e relatórios.

O cadastro de peladeiros aceita importação de planilhas `.xlsx` ou `.csv` com as colunas `Nome` e `E-mail` na primeira linha.

Produtos podem ter tipo de embalagem e unidades por caixa. Tanto o estoque inicial quanto as reposições aceitam caixas e unidades avulsas; as vendas e a baixa continuam sendo feitas por unidade.

Produtos podem ser editados, excluídos dos cadastros ativos e restaurados sem perder o histórico de vendas ou reposições.

Na venda rápida, pagamentos Pix podem gerar QR Code e código Copia e Cola com o valor total preenchido automaticamente.

Com `MERCADOPAGO_ACCESS_TOKEN`, `MERCADOPAGO_WEBHOOK_SECRET` e `APP_BASE_URL` configurados,
o QR Code é criado pelo Mercado Pago. O estoque fica reservado enquanto a cobrança está
pendente e a venda é registrada automaticamente somente após a confirmação do pagamento.
O peladeiro precisa ter e-mail e CPF cadastrados para usar esse fluxo.

Vendas lançadas incorretamente podem ser apagadas na conferência de Pix ou no relatório mensal. A exclusão devolve automaticamente os itens ao estoque.

No primeiro acesso, o sistema solicita a criação de um gerente. O gerente pode criar usuários Cliente (somente venda rápida), Staff (operações sem peladeiros e relatórios) e outros Gerentes (acesso completo).

Clientes podem opcionalmente entrar apenas com o usuário, sem senha. Gerentes e Staff exigem senha, que pode ser redefinida pelo Gerente na tela de usuários.

O endereço `/cliente` oferece uma tela simplificada para clientes com acesso sem senha e os direciona diretamente à Venda rápida.

O Financeiro, exclusivo do gerente, controla a mensalidade de manutenção de R$ 15 por peladeiro. É possível registrar um mês ou até 12 meses de uma vez, acompanhar a situação anual e apagar lançamentos incorretos.

Goleiros e membros da diretoria podem ser classificados como isentos no cadastro de peladeiros e não entram nas previsões ou pendências de mensalidades.

Peladeiros podem ser editados, excluídos dos cadastros ativos e restaurados. A exclusão preserva vendas e mensalidades históricas.

A lista de peladeiros pode ser filtrada por contribuintes, diretoria, goleiros, excluídos ou todos os cadastros.

O cadastro também aceita CPF único. Na listagem ele é mascarado, ficando completo apenas na edição do peladeiro.

## Como executar

No PowerShell, dentro desta pasta:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Abra `http://127.0.0.1:5000`. O banco SQLite `bar.db` é criado automaticamente na primeira execução.

Em desenvolvimento local, `DATABASE_URL` é opcional e o sistema usa automaticamente o arquivo `bar.db`. Em produção na Vercel, `DATABASE_URL` deve apontar para o PostgreSQL/Supabase.

Para recuperar a senha de um Gerente ou Staff no Supabase, configure a mesma `DATABASE_URL` em `.env.local` e execute `python scripts/reset_postgres_password.py`. A senha é solicitada de forma oculta e não fica salva no projeto.

## Backup

Com o sistema parado, copie o arquivo `bar.db` para um local seguro. Ele contém todos os cadastros e movimentos.

## Observação

Esta versão foi pensada para uso em um computador/celular na mesma operação. Antes de publicar na internet, adicione login, HTTPS, backups automáticos e troque a `SECRET_KEY`.
