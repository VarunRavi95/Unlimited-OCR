param([string]$IdentityFile=(Join-Path $env:USERPROFILE '.ssh\unlimited-ocr-poc'),[string]$InfraDirectory=(Join-Path $PSScriptRoot '..\infra'))
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Command ssh
$publicIp=Get-TerraformOutput -Name 'public_ip' -InfraDirectory $InfraDirectory;$remote="ubuntu@$publicIp"
& ssh -i $IdentityFile $remote 'nvidia-smi';if($LASTEXITCODE -ne 0){throw 'nvidia-smi smoke test failed.'}
& ssh -i $IdentityFile $remote 'docker run --rm --gpus all ubuntu:24.04 nvidia-smi';if($LASTEXITCODE -ne 0){throw 'Docker GPU smoke test failed.'}
& ssh -i $IdentityFile $remote 'cd /opt/unlimited-ocr-poc && docker compose ps';if($LASTEXITCODE -ne 0){throw 'Docker Compose service check failed.'}
