$ErrorActionPreference = 'Stop'

$sdk = 'C:\Users\addam\AppData\Local\Android\Sdk'
if (Test-Path $sdk) {
    $env:ANDROID_HOME = $sdk
    $env:ANDROID_SDK_ROOT = $sdk
    $env:PATH = "$sdk\platform-tools;$sdk\cmdline-tools\latest\bin;$env:PATH"
    Write-Host "ANDROID_HOME=$env:ANDROID_HOME"
}

Write-Host 'Iniciando Appium (UiAutomator2) na porta 4723...'
appium --base-path /wd/hub --port 4723
