# Watchdog: mantem TODAS as janelas do Chrome debug ocultas, sem parar.
# O probe abre/fecha abas e o Chrome as vezes traz a janela de volta; este
# loop garante que ela volte a ficar invisivel em menos de 1 segundo.
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinHide {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
}
"@

$debugPids = @()
$callback = [WinHide+EnumWindowsProc]{
  param($hwnd, $lparam)
  $pidJanela = [uint32]0
  [WinHide]::GetWindowThreadProcessId($hwnd, [ref]$pidJanela) | Out-Null
  if ($script:debugPids -contains [int]$pidJanela -and [WinHide]::IsWindowVisible($hwnd)) {
    [WinHide]::ShowWindow($hwnd, 0) | Out-Null
  }
  return $true
}

Write-Output "WATCHDOG_INICIADO"
while ($true) {
  $debugPids = @(Get-CimInstance Win32_Process -Filter "name='chrome.exe'" |
    Where-Object { $_.CommandLine -like '*User Data Debug*' -and $_.CommandLine -notlike '*--type=*' } |
    Select-Object -ExpandProperty ProcessId)
  if ($debugPids.Count -gt 0) {
    [WinHide]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
  }
  Start-Sleep -Milliseconds 150
}
