# Inicia (ou apenas esconde) o Chrome com CDP na porta 9222, usando um PERFIL
# SEPARADO ("User Data Debug").
#
# Chrome >= 136 ignora --remote-debugging-port no perfil PADRAO. Por aqui usamos
# um caminho nao-padrao. O login e feito UMA VEZ (scripts/login_linkedin_debug.py)
# e, enquanto o processo nao for encerrado, o cookie de sessao (li_at) continua
# valido para as sondagens.
#
# REGRA DE OURO: nunca feche a janela deste Chrome. Ela fica OCULTA (SW_HIDE),
# sem aparecer na barra de tarefas. O probe abre/fecha abas em segundo plano.
$chromeExe = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
$profile = 'C:\Users\addam\AppData\Local\Google\Chrome\User Data Debug'
$diretorio = Split-Path -Parent $MyInvocation.MyCommand.Path

# 1) Se ja ha CDP ativo, apenas esconde TODAS as janelas debug e sai.
#    (O Chrome tem multiplas janelas; MainWindowHandle so pega uma — por isso
#     usamos hide_debug_windows.ps1 com EnumWindows.)
$v = $null
try { $v = Invoke-RestMethod -Uri 'http://127.0.0.1:9222/json/version' -TimeoutSec 3 } catch {}
if ($v) {
  & (Join-Path $diretorio 'hide_debug_windows.ps1') | Out-Null
  Write-Output ('CDP_JA_ATIVO=' + $v.Browser)
  Write-Output 'JANELAS_DEBUG=OCULTAS'
  exit 0
}

# 2) Se ha processo debug sem CDP, encerra apenas ele e relanca.
Get-CimInstance Win32_Process -Filter "name='chrome.exe'" |
    Where-Object { $_.CommandLine -like '*User Data Debug*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3

$argLine = '--remote-debugging-port=9222 --user-data-dir="' + $profile + '" --lang=en-US --no-first-run --no-default-browser-check'
Start-Process -FilePath $chromeExe -ArgumentList $argLine
Start-Sleep -Seconds 10

# 3) Esconde TODAS as janelas debug (SW_HIDE=0 via EnumWindows).
& (Join-Path $diretorio 'hide_debug_windows.ps1') | Out-Null

try {
  $v = Invoke-RestMethod -Uri 'http://127.0.0.1:9222/json/version' -TimeoutSec 5
  Write-Output ('CDP_OK=' + $v.Browser)
} catch {
  Write-Output ('CDP_FALHOU=' + $_.Exception.Message)
}
