# BAR PELADEIROS GPCTA

MVP local para cadastrar peladeiros e produtos, registrar vendas, baixar estoque, controlar reposições, Pix, débito e relatórios.

O cadastro de peladeiros aceita importação de planilhas `.xlsx` ou `.csv` com as colunas `Nome` e `E-mail` na primeira linha.

Produtos podem ter tipo de embalagem e unidades por caixa. Tanto o estoque inicial quanto as reposições aceitam caixas e unidades avulsas; as vendas e a baixa continuam sendo feitas por unidade.

Produtos podem ser editados, excluídos dos cadastros ativos e restaurados sem perder o histórico de vendas ou reposições.

Na venda rápida, pagamentos Pix podem gerar QR Code e código Copia e Cola com o valor total preenchido automaticamente.

Vendas lançadas incorretamente podem ser apagadas na conferência de Pix ou no relatório mensal. A exclusão devolve automaticamente os itens ao estoque.

No primeiro acesso, o sistema solicita a criação de um gerente. O gerente pode criar usuários Cliente (somente venda rápida), Staff (operações sem peladeiros e relatórios) e outros Gerentes (acesso completo).

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

## Backup

Com o sistema parado, copie o arquivo `bar.db` para um local seguro. Ele contém todos os cadastros e movimentos.

## Observação

Esta versão foi pensada para uso em um computador/celular na mesma operação. Antes de publicar na internet, adicione login, HTTPS, backups automáticos e troque a `SECRET_KEY`.
