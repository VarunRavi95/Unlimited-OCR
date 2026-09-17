param([string]$IdentityFile=(Join-Path $env:USERPROFILE '.ssh\unlimited-ocr-poc'),[string]$InfraDirectory=(Join-Path $PSScriptRoot '..\infra'),[string]$HostAlias='unlimited-ocr-poc')
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$publicIp=Get-TerraformOutput -Name 'public_ip' -InfraDirectory $InfraDirectory
$sshDirectory=Join-Path $env:USERPROFILE '.ssh';$configPath=Join-Path $sshDirectory 'config'
if(-not(Test-Path -LiteralPath $sshDirectory)){New-Item -ItemType Directory -Path $sshDirectory|Out-Null}
$existing=if(Test-Path -LiteralPath $configPath){Get-Content -LiteralPath $configPath -Raw}else{''}
if($existing -match "(?m)^Host\s+$([regex]::Escape($HostAlias))\s*$"){throw "Host '$HostAlias' already exists in $configPath. Update it manually to $publicIp."}
$entry=@"

Host $HostAlias
    HostName $publicIp
    User ubuntu
    IdentityFile $IdentityFile
    IdentitiesOnly yes
    ServerAliveInterval 30
"@
Add-Content -LiteralPath $configPath -Value $entry
Write-Host "Added VS Code Remote-SSH host '$HostAlias'." -ForegroundColor Green

