# Mo Cloudflare Quick Tunnel (URL https://xxx.trycloudflare.com tam thoi) toi MCP server o cong 8000.
# Dung de thu nghiem khi chua co domain. URL doi moi lan chay lai.
# (File chi dung ky tu ASCII de Windows PowerShell 5.1 doc dung.)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$dir = Join-Path $env:LOCALAPPDATA 'cloudflared'
$exe = Join-Path $dir 'cloudflared.exe'

if (-not (Test-Path $exe)) {
    Write-Host 'Dang tai cloudflared tu GitHub cua Cloudflare...'
    New-Item -ItemType Directory -Force $dir | Out-Null
    Invoke-WebRequest -UseBasicParsing -OutFile $exe `
        -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe'
}

$sig = Get-AuthenticodeSignature $exe
if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notlike '*O="Cloudflare, Inc."*') {
    Remove-Item $exe -Force
    throw "cloudflared.exe khong co chu ky hop le cua Cloudflare - da xoa. Chay lai script de tai lai."
}

$port = 8000
Write-Host "Tunnel toi http://127.0.0.1:$port - tim dong co 'trycloudflare.com' ben duoi. Ctrl+C de dung."
& $exe tunnel --no-autoupdate --url "http://127.0.0.1:$port"
