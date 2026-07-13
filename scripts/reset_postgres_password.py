"""Redefine com segurança a senha de um Gerente ou Staff no PostgreSQL.

Lê DATABASE_URL do ambiente ou de .env.local, pede a senha sem exibi-la e
atualiza somente o usuário escolhido. Não imprime nem salva a nova senha.
"""

import getpass
import os
from pathlib import Path

import psycopg2
from werkzeug.security import generate_password_hash


def load_database_url():
    url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if url:
        return url
    env_file = Path(__file__).resolve().parents[1] / ".env.local"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
            if line.startswith("SUPABASE_DB_URL="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("DATABASE_URL não encontrada no ambiente ou em .env.local.")


def main():
    connection = psycopg2.connect(load_database_url(), sslmode="require", connect_timeout=10)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT username,name,role,active FROM users ORDER BY role,name")
        users = cursor.fetchall()
        if not users:
            raise SystemExit("Nenhum usuário existe no Supabase. Acesse /setup para criar o gerente.")
        print("\nUsuários existentes:")
        for username, name, role, active in users:
            print(f"- {username} | {name} | {role} | {'ativo' if active else 'inativo'}")
        username = input("\nUsuário que terá a senha redefinida: ").strip()
        password = getpass.getpass("Nova senha (mínimo 8 caracteres): ")
        confirmation = getpass.getpass("Confirme a nova senha: ")
        if len(password) < 8:
            raise SystemExit("A senha precisa ter ao menos 8 caracteres.")
        if password != confirmation:
            raise SystemExit("As senhas não coincidem.")
        cursor.execute("""UPDATE users SET password_hash=%s,password_required=1,active=1
                          WHERE LOWER(username)=LOWER(%s) AND role IN ('manager','staff')""",
                       (generate_password_hash(password), username))
        if cursor.rowcount != 1:
            connection.rollback()
            raise SystemExit("Gerente ou Staff não encontrado.")
        connection.commit()
        print("Senha redefinida e usuário ativado com sucesso.")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
