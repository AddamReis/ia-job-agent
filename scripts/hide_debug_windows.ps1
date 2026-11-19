# Esconde TODAS as janelas do Chrome debug (o processo tem varias, e o
# MainWindowHandle so aponta para uma delas — por isso esconder so ela falha).
param([int[]]$DebugPids = @())

Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WinEnum {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
}
"@

if ($DebugPids.Count -eq 0) {
  $DebugPids = @(Get-CimInstance Win32_Process -Filter "name='chrome.exe'" |
    Where-Object { $_.CommandLine -like '*User Data Debug*' -and $_.CommandLine -notlike '*--type=*' } |
    Select-Object -ExpandProperty ProcessId)
}

$escondidas = 0
$visiveisApos = 0
$callback = [WinEnum+EnumWindowsProc]{
  param($hwnd, $lparam)
  $pidJanela = [uint32]0
  [WinEnum]::GetWindowThreadProcessId($hwnd, [ref]$pidJanela) | Out-Null
  if ($DebugPids -contains [int]$pidJanela) {
    if ([WinEnum]::IsWindowVisible($hwnd)) {
      [WinEnum]::ShowWindow($hwnd, 0) | Out-Null   # SW_HIDE
      $script:escondidas++
    }
    if ([WinEnum]::IsWindowVisible($hwnd)) { $script:visiveisApos++ }
  }
  return $true
}
[WinEnum]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
Write-Output ("ESCONDIDAS=" + $escondidas)
Write-Output ("AINDA_VISIVEIS=" + $visiveisApos)
