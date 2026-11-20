"""Abre o portal de candidatos da BairesDev no Chrome debug para login manual.

Uso:
    python scripts/login_bairesdev_debug.py

O portal exige sessão própria (o login do LinkedIn não basta). O script:
  1. Mostra a janela debug (normalmente oculta) e abre a tela de login.
  2. Você faz login normalmente, incluindo 2FA se a plataforma pedir.
  3. Detecta a saída de /login, esconde a janela e mantém o Chrome aberto.

O Chrome NUNCA é fechado — os cookies de sessão permanecem no perfil debug.
"""
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts.login_linkedin_debug import janelas_debug, set_janela, SW_SHOW
from src.execution.web.linkedin_logged import LinkedInLoggedBrowser

LOGIN_URL = "https://applicants.bairesdev.com/login"


def esconder() -> None:
    subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File",
         str(pathlib.Path(__file__).with_name("hide_debug_windows.ps1"))],
        capture_output=True,
    )


def main() -> None:
    print("JANELAS_DEBUG=", len(janelas_debug()), flush=True)
    set_janela(janelas_debug(), SW_SHOW)
    print("Janela mostrada. Faca login na BairesDev. Aguardando...", flush=True)

    b = LinkedInLoggedBrowser()
    b.connect()
    pagina = b._context.pages[0] if b._context.pages else b._context.new_page()
    try:
        pagina.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        print("ERRO ao abrir login:", exc, flush=True)

    prazo = time.time() + 600
    logado = False
    while time.time() < prazo:
        try:
            urls = [p.url.lower() for p in b._context.pages]
            if any("applicants.bairesdev.com" in u and "/login" not in u for u in urls):
                logado = True
                break
        except Exception:
            pass
        time.sleep(2)

    esconder()
    print("LOGADO=", logado, flush=True)
    print("Janelas escondidas. Chrome continua rodando (nao foi fechado).", flush=True)


if __name__ == "__main__":
    main()
