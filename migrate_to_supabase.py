import os
import sys
import sqlite3
import psycopg2
import psycopg2.extras

def migrate():
    sqlite_db_path = "bar.db"
    
    if not os.path.exists(sqlite_db_path):
        print(f"Erro: O banco SQLite local '{sqlite_db_path}' não foi encontrado.")
        sys.exit(1)
        
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("A variável de ambiente DATABASE_URL não está configurada.")
        database_url = input("Digite a URL de conexão do Supabase (ex: postgresql://...): ").strip()
        
    if not database_url:
        print("Erro: A URL de conexão do Supabase é obrigatória.")
        sys.exit(1)
        
    print("\nIniciando processo de migração...")
    
    # Ordem de tabelas respeitando chaves estrangeiras (das dependentes para as principais para deleção/truncamento)
    tables_order = [
        "membership_months",
        "membership_payments",
        "restocks",
        "sale_items",
        "sales",
        "products",
        "players",
        "users"
    ]
    
    confirm = input("\nAtenção: Isso irá apagar todos os dados existentes no Supabase para as tabelas a serem migradas. Deseja continuar? (s/n): ")
    if confirm.lower() != 's':
        print("Migração cancelada pelo usuário.")
        return
        
    try:
        # Conexão SQLite
        conn_sq = sqlite3.connect(sqlite_db_path)
        conn_sq.row_factory = sqlite3.Row
        cur_sq = conn_sq.cursor()
        
        # Conexão Postgres
        conn_pg = psycopg2.connect(database_url)
        cur_pg = conn_pg.cursor()
        
        # 1. Limpar tabelas no Postgres em ordem de dependência
        print("\n[1/3] Limpando tabelas no Supabase...")
        for table in tables_order:
            print(f"  -> Limpando {table}...")
            cur_pg.execute(f"TRUNCATE TABLE {table} CASCADE;")
        conn_pg.commit()
        print("Tabelas limpas com sucesso.")
        
        # 2. Migrar dados da tabela principal para as dependentes
        print("\n[2/3] Copiando dados do SQLite para o Supabase...")
        # Inverter a ordem das tabelas para inserção (das principais para as dependentes)
        for table in reversed(tables_order):
            print(f"  -> Migrando {table}...")
            cur_sq.execute(f"SELECT * FROM {table}")
            rows = cur_sq.fetchall()
            
            if not rows:
                print(f"     Tabela '{table}' vazia no SQLite. Pulando...")
                continue
                
            columns = rows[0].keys()
            # Tratamento de aspas duplas nos nomes de colunas no postgres
            cols_str = ", ".join([f'"{c}"' for c in columns])
            placeholders = ", ".join(["%s"] * len(columns))
            query = f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders})"
            
            insert_data = []
            for r in rows:
                row_data = []
                for col in columns:
                    val = r[col]
                    if isinstance(val, memoryview):
                        val = val.tobytes()
                    row_data.append(val)
                insert_data.append(row_data)
                
            cur_pg.executemany(query, insert_data)
            print(f"     {len(rows)} registros copiados para {table}.")
            
        conn_pg.commit()
        print("Dados copiados com sucesso.")
        
        # 3. Ajustar sequências de IDs no PostgreSQL
        print("\n[3/3] Ajustando sequências de IDs no Supabase...")
        for table in tables_order:
            # Verifica se a tabela possui id
            cur_sq.execute(f"PRAGMA table_info({table})")
            columns_info = cur_sq.fetchall()
            has_id = any(c[1] == 'id' for c in columns_info)
            
            if has_id:
                try:
                    # Ajusta a sequência com o maior valor atual de id
                    seq_query = f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), coalesce(max(id), 1), max(id) IS NOT NULL) FROM {table};"
                    cur_pg.execute(seq_query)
                    new_val = cur_pg.fetchone()[0]
                    print(f"  -> Sequência de {table} ajustada para {new_val}.")
                except Exception as seq_err:
                    print(f"  -> Aviso ao ajustar sequência de {table}: {seq_err}")
                    conn_pg.rollback()
            
        conn_pg.commit()
        print("\nMigração concluída com sucesso!")
        
    except Exception as err:
        print(f"\nErro durante a migração: {err}")
        if 'conn_pg' in locals():
            conn_pg.rollback()
    finally:
        if 'conn_sq' in locals():
            conn_sq.close()
        if 'conn_pg' in locals():
            conn_pg.close()

if __name__ == "__main__":
    migrate()
