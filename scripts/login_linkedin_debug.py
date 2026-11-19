"""Abre o LinkedIn no Chrome debug para login manual e ESCONDE a janela ao terminar.

Uso:
    python scripts/login_linkedin_debug.py

O script:
  1. Mostra a janela debug (que normalmente fica oculta) e vai ate /login.
  2. Voce faz login normalmente (inclui 2FA, se houver).
  3. O script detecta o login (URL sai de /login) e esconde a janela automaticamente.
     O Chrome NAO e fechado, entao o cookie de sessao (li_at) continua valido.
"""
import ctypes
import pathlib
import subprocess
import sys
import time
from ctypes import wintypes

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src.execution.web.linkedin_logged import LinkedInLoggedBrowser

SW_SHOW = 5
SW_HIDE = 0

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.EnumWindows.argtypes = [
    ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
    wintypes.LPARAM,
]


def janelas_debug() -> list[int]:
    """Handles das janelas do Chrome debug."""
    out = subprocess.check_output(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -like '*User Data Debug*' -and "
            "$_.CommandLine -notlike '*--type=*' } | "
            "Select-Object -ExpandProperty ProcessId",
        ],
        text=True,
    )
    pids = {int(x) for x in out.split() if x.strip().isdigit()}
    encontrados: list[int] = []

    def cb(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in pids:
            encontrados.append(hwnd)
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    return encontrados


def set_janela(handles: list[int], cmd: int) -> None:
    for h in handles:
        user32.ShowWindow(h, cmd)


def main() -> None:
    handles = janelas_debug()
    print("JANELAS_DEBUG=", len(handles), flush=True)
    set_janela(handles, SW_SHOW)
    print("Janela mostrada. Faca login no LinkedIn. Aguardando...", flush=True)

    b = LinkedInLoggedBrowser()
    b.connect()
    page = b._context.pages[0] if b._context.pages else b._context.new_page()
    try:
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print("ERRO ao abrir login:", e, flush=True)

    prazo = time.time() + 600
    logado = False
    while time.time() < prazo:
        try:
            urls = [p.url.lower() for p in b._context.pages]
            # Detecta login em QUALQUER aba (o usuario pode ter logado em outra).
            if any(
                "/login" not in u and "linkedin.com" in u and any(
                    trecho in u for trecho in ("/feed", "/jobs", "/mypreferences", "/checkpoint")
                )
                for u in urls
            ):
                logado = True
                break
        except Exception:
            pass
        time.sleep(2)

    # Usa o helper que esconde TODAS as janelas (MainWindowHandle so pega uma).
    subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File",
         str(pathlib.Path(__file__).with_name("hide_debug_windows.ps1"))],
        capture_output=True,
    )
    print("LOGADO=", logado, flush=True)
    print("Janelas escondidas. Chrome continua rodando (nao foi fechado).", flush=True)


if __name__ == "__main__":
    main()
