# Открыть админку vpn-zoo со своего компьютера (Windows PowerShell):
# SSH-туннель до сервера + одноразовая ссылка входа (живёт 3 минуты) в браузере — токен не нужен.
#
# Один раз (ключ, запись «zoo» в ~/.ssh/config, команда zoo-admin в профиле PowerShell):
#   powershell -ExecutionPolicy Bypass -File .\zoo-admin.ps1 -Setup root@СЕРВЕР [-Port 2222] [-Name ИМЯ]
# Потом каждый день (в новом окне PowerShell):
#   zoo-admin                    # админка (сервер «zoo» из ~/.ssh/config)
#   ssh zoo                      # консоль сервера
# Без настройки:
#   powershell -ExecutionPolicy Bypass -File tools\zoo-admin.ps1 root@СЕРВЕР
#   powershell -ExecutionPolicy Bypass -File tools\zoo-admin.ps1 root@СЕРВЕР -Port 2222 -Identity C:\Users\me\.ssh\vps
#
# Порт и ключ — отдельными параметрами: массив вида '-p','2222' через -File не разбирается (приходит одной строкой).
# Прочие опции ssh — в ~/.ssh/config (Host СЕРВЕР).
# Туннель — свёрнутое окно ssh; закрыть окно = закрыть туннель. -NoOpen — только напечатать ссылку.
param(
    [Parameter(Position = 0)][string]$Server = 'zoo',
    [int]$Port = 0,
    [string]$Identity = '',
    [switch]$NoOpen,
    [string]$Setup = '',
    [string]$Name = 'zoo'
)
$ErrorActionPreference = 'Stop'

function Fail([string]$msg, [int]$code = 1) { Write-Host $msg -ForegroundColor Red; exit $code }

if ($Setup) {
    if ($Setup -notmatch '^([A-Za-z0-9_][A-Za-z0-9_.-]*)@(.+)$') {
        Fail 'нужен адрес вида пользователь@сервер, например root@203.0.113.5' 2
    }
    $user = $Matches[1]
    $addr = $Matches[2].Trim('[', ']')
    if ($addr -notmatch '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$' -and -not ($addr.Contains(':') -and $addr -match '^[0-9A-Fa-f:.]+$')) {
        Fail "странный адрес сервера: $addr" 2
    }
    $sshPort = 22
    if ($Port -ne 0) { $sshPort = $Port }
    if ($sshPort -lt 1 -or $sshPort -gt 65535) { Fail 'порт SSH — число от 1 до 65535' 2 }
    if ($Name -cnotmatch '^[A-Za-z][A-Za-z0-9_-]{0,30}$') { Fail 'имя для ssh — буквы, цифры, - и _ (например zoo)' 2 }

    $sshDir = Join-Path $HOME '.ssh'
    New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
    $key = Join-Path $sshDir 'id_ed25519'
    if (Test-Path -LiteralPath $key) {
        Write-Host "1/4 ключ уже есть: $key"
    } else {
        Write-Host "1/4 создаю ключ $key (пароль на ключ - по желанию)"
        & ssh-keygen -t ed25519 -f $key -C "zoo-admin@$env:COMPUTERNAME"
        if ($LASTEXITCODE -ne 0) { Fail 'ssh-keygen не создал ключ' }
    }
    if (-not (Test-Path -LiteralPath "$key.pub")) { Fail "нет $key.pub - удалите $key и повторите, ключ создастся заново" }

    Write-Host '2/4 кладу ключ на сервер (спросит пароль сервера один раз)'
    # без кавычек внутри команды (так же в .sh); tr убирает CR из конца строки PowerShell; дубликат строки не добавляется
    $remote = 'umask 077; mkdir -p ~/.ssh; tr -d \r > ~/.ssh/zoo-key.tmp; grep -qxFf ~/.ssh/zoo-key.tmp ~/.ssh/authorized_keys 2>/dev/null || cat ~/.ssh/zoo-key.tmp >> ~/.ssh/authorized_keys; rm -f ~/.ssh/zoo-key.tmp'
    [IO.File]::ReadAllText("$key.pub").Trim() | & ssh -p $sshPort "$user@$addr" $remote
    if ($LASTEXITCODE -ne 0) { Fail 'не удалось положить ключ на сервер: адрес, порт, пароль верные?' }

    $cfg = Join-Path $sshDir 'config'
    $pattern = '^\s*Host\s+([^#]*\s)?' + [regex]::Escape($Name) + '(\s|$)'
    if ((Test-Path -LiteralPath $cfg) -and (Select-String -LiteralPath $cfg -Pattern $pattern -Quiet)) {
        Write-Host "3/4 в $cfg запись «$Name» уже есть - не трогаю"
    } else {
        Write-Host "3/4 добавляю в $cfg запись «$Name»"
        $block = "Host $Name`n    HostName $addr`n    User $user`n    Port $sshPort`n    IdentityFile ~/.ssh/id_ed25519`n    ServerAliveInterval 20"
        if ((Test-Path -LiteralPath $cfg) -and (Get-Item -LiteralPath $cfg).Length -gt 0) { $block = "`n" + $block }
        Add-Content -LiteralPath $cfg -Value $block
    }

    $dst = Join-Path $HOME 'zoo-admin.ps1'
    if ($PSCommandPath -and ((Resolve-Path -LiteralPath $PSCommandPath).Path -ne $dst)) {
        Copy-Item -LiteralPath $PSCommandPath -Destination $dst -Force
    }
    $profilePath = "$PROFILE"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $profilePath) | Out-Null
    if (-not (Test-Path -LiteralPath $profilePath)) { New-Item -ItemType File -Path $profilePath | Out-Null }
    if (Select-String -LiteralPath $profilePath -SimpleMatch 'function zoo-admin' -Quiet) {
        Write-Host "4/4 команда zoo-admin уже есть в профиле: $profilePath"
    } else {
        Add-Content -LiteralPath $profilePath -Value ("`n" + 'function zoo-admin { powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\zoo-admin.ps1" @args }')
        Write-Host "4/4 команда zoo-admin добавлена в профиль: $profilePath (скрипт: $dst)"
    }
    if ((Get-ExecutionPolicy) -in 'Restricted', 'AllSigned') {
        Write-Host 'профиль не загрузится при текущей политике; один раз: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned' -ForegroundColor Yellow
    }

    & ssh -o BatchMode=yes -o ConnectTimeout=10 $Name true | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host 'вход по ключу работает' } else {
        Write-Host "вход по ключу пока не проверился (пароль на ключе? ssh-agent?) - попробуйте: ssh $Name" -ForegroundColor Yellow
    }
    Write-Host ''
    Write-Host 'готово. В новом окне PowerShell:'
    Write-Host "  ssh $Name                 - консоль сервера"
    if ($Name -eq 'zoo') { Write-Host '  zoo-admin                - админка в браузере' } else { Write-Host "  zoo-admin $Name          - админка в браузере" }
    exit 0
}

if ($Server.StartsWith('-')) { Fail 'сервер не может начинаться с «-»' 2 }
$SshArgs = @()
if ($Port -gt 0) { $SshArgs += @('-p', "$Port") }
if ($Identity) { $SshArgs += @('-i', $Identity) }

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
# ответу сервера не доверяем: порт и ссылка уходят в ssh и в Start-Process ($Port — уже порт SSH: имена без учёта регистра)
$webPort = "$($info.port)"
$link = "$($info.link)"
if ($webPort -notmatch '^[0-9]{1,5}$' -or [int]$webPort -lt 1 -or [int]$webPort -gt 65535 -or
    $link -cnotmatch "^http://127\.0\.0\.1:$webPort/login\?once=[A-Za-z0-9._-]+$") {
    Write-Host 'сервер вернул странный порт или ссылку — остановились (обновите zoo на сервере)' -ForegroundColor Red
    exit 1
}

# Start-Process склеивает массив аргументов пробелами без кавычек: путь «C:\Users\Ivan Petrov\…» распадётся
function Quote-Arg([string]$a) {
    if ($a -and $a -notmatch '[\s"]') { return $a }
    '"' + (($a -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}

$up = $false
try { Invoke-WebRequest "http://127.0.0.1:$webPort/login" -UseBasicParsing -TimeoutSec 3 | Out-Null; $up = $true } catch {}
if ($up) {
    Write-Host "туннель на 127.0.0.1:$webPort уже открыт"
} else {
    $tunnel = @($SshArgs) + @('-N', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=20',
        '-o', 'ServerAliveCountMax=3', '-L', "$($webPort):127.0.0.1:$($webPort)", $Server)
    Start-Process ssh -ArgumentList (($tunnel | ForEach-Object { Quote-Arg $_ }) -join ' ') -WindowStyle Minimized
    for ($i = 0; $i -lt 15 -and -not $up; $i++) {
        Start-Sleep -Seconds 1
        try { Invoke-WebRequest "http://127.0.0.1:$webPort/login" -UseBasicParsing -TimeoutSec 2 | Out-Null; $up = $true } catch {}
    }
    if (-not $up) { Write-Host 'туннель не поднялся — проверьте окно ssh (пароль ключа? порт занят?)' -ForegroundColor Red; exit 1 }
    Write-Host "туннель открыт: 127.0.0.1:$webPort → сервер (свёрнутое окно ssh)"
}

Write-Host "вход (ссылка одноразовая, 3 минуты): $link"
if (-not $NoOpen) { Start-Process $link }
Write-Host "дальше админка открывается по http://127.0.0.1:$webPort/ (вход запоминается до 30 дней, без заходов — на 14; потом снова этот скрипт)"
