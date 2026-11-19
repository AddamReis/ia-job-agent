"""Copia os cookies do LinkedIn do perfil Chrome real para o perfil debug.

O Chrome mantem o arquivo Cookies travado enquanto aberto, entao Copy-Item falha.
SQLite consegue abrir o banco em modo somente-leitura com URI (immutable=1),
mesmo com o arquivo em uso pelo outro processo.
"""
import shutil
import sqlite3
import sys
from pathlib import Path

ORIGINAL = Path(r"C:\Users\addam\AppData\Local\Google\Chrome\User Data\Default\Network\Cookies")
DEBUG = Path(r"C:\Users\addam\AppData\Local\Google\Chrome\User Data Debug\Default\Network\Cookies")


def copiar() -> None:
    if not ORIGINAL.exists():
        print("ERRO: perfil original nao encontrado")
        sys.exit(1)

    DEBUG.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Tenta copia direta (rapida) primeiro.
        shutil.copy2(ORIGINAL, DEBUG)
        print("COPIA_DIRETA=OK")
        return
    except (PermissionError, OSError) as e:
        print(f"COPIA_DIRETA=FALHOU ({e})")

    # Fallback: le via SQLite em modo imutavel e reescreve no destino.
    orig_uri = "file:///" + ORIGINAL.as_posix().lstrip("/") + "?immutable=1"
    dst_uri = "file:///" + DEBUG.as_posix().lstrip("/") + "?mode=rw"
    src = sqlite3.connect(orig_uri, uri=True)
    dst = sqlite3.connect(dst_uri, uri=True)

    tabelas = [
        r[0]
        for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    for t in tabelas:
        if t.startswith("sqlite_"):
            continue
        try:
            dst.execute(f'DELETE FROM "{t}"')
            rows = src.execute(f'SELECT * FROM "{t}"').fetchall()
            if rows:
                cols = ",".join(f'"{c[1]}"' for c in src.execute(f'PRAGMA table_info("{t}")').fetchall())
                marks = ",".join("?" for _ in rows[0])
                dst.executemany(f'INSERT INTO "{t}" ({cols}) VALUES ({marks})', rows)
        except sqlite3.Error as e:
            print(f"  tabela {t}: {e}")

    dst.commit()
    dst.close()
    src.close()
    print("COPIA_SQLITE=OK")

    # Verifica se o cookie de sessao veio junto.
    c = sqlite3.connect("file:///" + DEBUG.as_posix().lstrip("/") + "?immutable=1", uri=True)
    n = c.execute("SELECT count(*) FROM cookies WHERE name='li_at'").fetchone()[0]
    c.close()
    print(f"LI_AT={n}")


if __name__ == "__main__":
    copiar()
