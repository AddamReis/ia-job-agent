"""Migra todas as tabelas entre bancos SQLite/SQLAlchemy.

Uso típico (exportar aqui e importar no DGX Spark):

    python scripts/migrate_data.py export \
        --source data/agent.db --output data/dgx-snapshot.json

    python scripts/migrate_data.py import \
        --input data/dgx-snapshot.json --target sqlite:///data/agent.db

O alvo também pode ser Postgres:
    --target postgresql+psycopg://user:pass@host:5432/jobs

Atalho sem JSON, direto SQLite -> Postgres (lendo a URL de AI_JOB_DATABASE_URL):
    python scripts/migrate_data.py copy --source sqlite:////app/data/agent.db

Todas as formas são idempotentes: linhas já existentes (mesma PK) são ignoradas.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.storage.database import Base  # noqa: E402
from src.storage.database import _ensure_columns  # noqa: E402


def _dump_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    return value


def _load_value(value: Any) -> Any:
    if isinstance(value, dict) and "__bytes__" in value:
        return base64.b64decode(value["__bytes__"])
    return value


def export_database(source: str, output: Path) -> dict[str, int]:
    engine = create_engine(source)
    inspector = inspect(engine)
    snapshot: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            # Bancos antigos podem não ter colunas adicionadas depois; lê só
            # o que existe fisicamente para não quebrar a exportação.
            available = [column["name"] for column in inspector.get_columns(table.name)]
            columns = [table.c[name] for name in available if name in table.c]
            rows = connection.execute(select(*columns)).mappings().all()
            snapshot[table.name] = [
                {key: _dump_value(row[key]) for key in row.keys()} for row in rows
            ]
            counts[table.name] = len(rows)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    engine.dispose()
    return counts


def import_database(target: str, input_path: Path) -> dict[str, int]:
    snapshot = json.loads(input_path.read_text(encoding="utf-8"))
    engine = create_engine(target)
    Base.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        _ensure_columns(engine)  # migrations incrementais só existem no SQLite
    imported: dict[str, int] = {}
    with engine.begin() as connection:
        for table_name, rows in snapshot.items():
            table = Base.metadata.tables.get(table_name)
            if table is None:
                continue
            ok = 0
            for row in rows:
                values = {key: _load_value(value) for key, value in row.items()}
                try:
                    with connection.begin_nested():
                        connection.execute(table.insert().values(**values))
                    ok += 1
                except IntegrityError:
                    pass  # já existe: seed/migração idempotente
            imported[table_name] = ok
    engine.dispose()
    return imported


def copy_database(source: str, target: str) -> dict[str, int]:
    """Copia todas as tabelas direto entre bancos, sem arquivo intermediário."""
    src = create_engine(source)
    dst = create_engine(target)
    Base.metadata.create_all(dst)
    if dst.dialect.name == "sqlite":
        _ensure_columns(dst)

    src_inspector = inspect(src)
    copied: dict[str, int] = {}
    with src.connect() as incoming, dst.begin() as out:
        for table in Base.metadata.sorted_tables:
            if not src_inspector.has_table(table.name):
                continue
            available = [
                column["name"]
                for column in src_inspector.get_columns(table.name)
                if column["name"] in table.c
            ]
            columns = [table.c[name] for name in available]
            rows = incoming.execute(select(*columns)).mappings().all()
            ok = 0
            for row in rows:
                values = {key: row[key] for key in row.keys()}
                try:
                    with out.begin_nested():
                        out.execute(table.insert().values(**values))
                    ok += 1
                except IntegrityError:
                    pass
            copied[table.name] = ok
    src.dispose()
    dst.dispose()
    return copied


def _database_url(value: str) -> str:
    if "://" in value:
        return value
    return f"sqlite:///{Path(value).resolve()}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    export_cmd = sub.add_parser("export", help="banco -> JSON")
    export_cmd.add_argument("--source", default=os.getenv("SOURCE_DATABASE_URL", "data/agent.db"))
    export_cmd.add_argument("--output", default="data/dgx-snapshot.json")

    import_cmd = sub.add_parser("import", help="JSON -> banco")
    import_cmd.add_argument("--input", required=True)
    import_cmd.add_argument(
        "--target", default=os.getenv("TARGET_DATABASE_URL", "sqlite:///data/agent.db")
    )

    copy_cmd = sub.add_parser("copy", help="SQLite -> Postgres direto")
    copy_cmd.add_argument(
        "--source", default=os.getenv("SOURCE_DATABASE_URL", "sqlite:////app/data/agent.db")
    )
    copy_cmd.add_argument(
        "--target",
        default=os.getenv("AI_JOB_DATABASE_URL", os.getenv("TARGET_DATABASE_URL", "")),
    )

    args = parser.parse_args()
    if args.command == "export":
        counts = export_database(_database_url(args.source), Path(args.output))
        print(f"Exportado para {args.output}")
    elif args.command == "import":
        counts = import_database(_database_url(args.target), Path(args.input))
        print(f"Importado para {args.target}")
    else:
        if not args.target:
            parser.error("copy exige --target ou AI_JOB_DATABASE_URL")
        counts = copy_database(_database_url(args.source), args.target)
        print("Copiado para Postgres")
    for table, total in counts.items():
        print(f"  {table}: {total}")


if __name__ == "__main__":
    main()
