param([string]$AwsProfile='default',[string]$KeyPath=(Join-Path $env:USERPROFILE '.ssh\unlimited-ocr-poc'),[switch]$InstallTerraform)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Command aws
Assert-Command ssh-keygen
if (-not (Get-Command terraform -ErrorAction SilentlyContinue)) {
    if (-not $InstallTerraform) { throw 'Terraform is not installed. Re-run with -InstallTerraform or install it manually.' }
    Assert-Command winget
    & winget install --id Hashicorp.Terraform --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw 'Terraform installation failed.' }
}
$sshDirectory=Split-Path -Parent $KeyPath
if (-not (Test-Path -LiteralPath $sshDirectory)) { New-Item -ItemType Directory -Path $sshDirectory | Out-Null }
if (-not (Test-Path -LiteralPath $KeyPath)) { & ssh-keygen -t ed25519 -f $KeyPath -N '""' -C 'unlimited-ocr-poc'; if ($LASTEXITCODE -ne 0) { throw 'SSH key generation failed.' } }
$profiles=@(& aws configure list-profiles)
if ($profiles -notcontains $AwsProfile) { Write-Host "AWS profile '$AwsProfile' is not configured. Starting aws configure." -ForegroundColor Yellow; & aws configure --profile $AwsProfile }
& aws sts get-caller-identity --profile $AwsProfile
if ($LASTEXITCODE -ne 0) { throw "AWS profile '$AwsProfile' could not call STS." }
Write-Host 'Workstation prerequisites are ready.' -ForegroundColor Green
Write-Host "Public key: $KeyPath.pub"
