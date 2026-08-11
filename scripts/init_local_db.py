#!/usr/bin/env python3
"""Create or safely upgrade the local SQLite development database."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import initialize_sqlite_database


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inicializa o banco SQLite local explicitamente.")
    parser.add_argument(
        "--database",
        default=str(Path(__file__).resolve().parents[1] / "bar.db"),
        help="Caminho do SQLite (padrão: bar.db na raiz do projeto).",
    )
    args = parser.parse_args(argv)
    try:
        path = initialize_sqlite_database(args.database)
    except Exception as exc:
        print(f"Falha ao preparar SQLite ({type(exc).__name__}).", file=sys.stderr)
        return 1
    print(f"Banco SQLite preparado com sucesso: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
