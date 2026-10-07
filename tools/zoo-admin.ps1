# Открыть админку vpn-zoo со своего компьютера (Windows PowerShell):
# SSH-туннель до сервера + одноразовая ссылка входа (живёт 3 минуты) в браузере — токен не нужен.
#
#   powershell -ExecutionPolicy Bypass -File tools\zoo-admin.ps1 root@СЕРВЕР
#   powershell -ExecutionPolicy Bypass -File tools\zoo-admin.ps1 root@СЕРВЕР -SshArgs '-p','2222','-i','C:\Users\me\.ssh\vps'
#
# Туннель — свёрнутое окно ssh; закрыть окно = закрыть туннель. -NoOpen — только напечатать ссылку.
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$Server,
    [string[]]$SshArgs = @(),
    [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'

# root — напрямую, иначе через sudo; без кавычек: Windows PowerShell 5.1 портит кавычки в аргументах ssh
$remote = 'zoo web --info --json 2>/dev/null || sudo zoo web --info --json'
$out = & ssh @SshArgs $Server $remote
if ($LASTEXITCODE -ne 0 -or -not $out) {
    Write-Host 'не удалось спросить сервер: zoo установлен? SSH-ключ добавлен? sudo без пароля?' -ForegroundColor Red
    exit 1
}
try { $info = ($out -join "`n") | ConvertFrom-Json } catch {
    Write-Host 'сервер ответил неожиданно — обновите zoo на сервере (sudo zoo upgrade --pull --apply)' -ForegroundColor Red
    exit 1
}
$port = $info.port
$link = $info.link

$up = $false
try { Invoke-WebRequest "http://127.0.0.1:$port/login" -UseBasicParsing -TimeoutSec 3 | Out-Null; $up = $true } catch {}
if ($up) {
    Write-Host "туннель на 127.0.0.1:$port уже открыт"
} else {
    $tunnel = @($SshArgs) + @('-N', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=20',
        '-o', 'ServerAliveCountMax=3', '-L', "$($port):127.0.0.1:$($port)", $Server)
    Start-Process ssh -ArgumentList $tunnel -WindowStyle Minimized
    for ($i = 0; $i -lt 15 -and -not $up; $i++) {
        Start-Sleep -Seconds 1
        try { Invoke-WebRequest "http://127.0.0.1:$port/login" -UseBasicParsing -TimeoutSec 2 | Out-Null; $up = $true } catch {}
    }
    if (-not $up) { Write-Host 'туннель не поднялся — проверьте окно ssh (пароль ключа? порт занят?)' -ForegroundColor Red; exit 1 }
    Write-Host "туннель открыт: 127.0.0.1:$port → сервер (свёрнутое окно ssh)"
}

Write-Host "вход (ссылка одноразовая, 3 минуты): $link"
if (-not $NoOpen) { Start-Process $link }
Write-Host "дальше админка открывается по http://127.0.0.1:$port/ (вход запоминается на 30 дней)"
