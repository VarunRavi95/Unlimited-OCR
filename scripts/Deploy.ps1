param(
    [string]$IdentityFile = (Join-Path $env:USERPROFILE '.ssh\unlimited-ocr-poc'),
    [string]$InfraDirectory = (Join-Path $PSScriptRoot '..\infra'),
    [string]$RemoteDirectory = '/opt/unlimited-ocr-poc'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

Assert-Command ssh
Assert-Command scp
if (-not (Test-Path -LiteralPath $IdentityFile)) {
    throw "SSH private key was not found: $IdentityFile"
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$publicIp = Get-TerraformOutput -Name 'public_ip' -InfraDirectory $InfraDirectory
$remote = "ubuntu@$publicIp"
$sshOptions = @(
    '-i', $IdentityFile,
    '-o', 'BatchMode=yes',
    '-o', 'StrictHostKeyChecking=accept-new',
    '-o', 'ConnectTimeout=15',
    '-o', 'ServerAliveInterval=15',
    '-o', 'ServerAliveCountMax=3'
)
$appRoot = Join-Path $projectRoot 'app'
$uploadGroups = @(
    [pscustomobject]@{
        Label       = 'application modules'
        Sources     = @(Get-ChildItem -LiteralPath $appRoot -File | Select-Object -ExpandProperty FullName)
        Destination = "$RemoteDirectory/app/"
    },
    [pscustomobject]@{
        Label       = 'service modules'
        Sources     = @(Get-ChildItem -LiteralPath (Join-Path $appRoot 'services') -File | Select-Object -ExpandProperty FullName)
        Destination = "$RemoteDirectory/app/services/"
    },
    [pscustomobject]@{
        Label       = 'static assets'
        Sources     = @(Get-ChildItem -LiteralPath (Join-Path $appRoot 'static') -File | Select-Object -ExpandProperty FullName)
        Destination = "$RemoteDirectory/app/static/"
    },
    [pscustomobject]@{
        Label       = 'HTML templates'
        Sources     = @(Get-ChildItem -LiteralPath (Join-Path $appRoot 'templates') -File | Select-Object -ExpandProperty FullName)
        Destination = "$RemoteDirectory/app/templates/"
    },
    [pscustomobject]@{
        Label = 'Compose configuration'
        Sources = @(
            (Join-Path $projectRoot 'Caddyfile'),
            (Join-Path $projectRoot 'docker-compose.yml'),
            (Join-Path $projectRoot 'docker-compose.aws.yml'),
            (Join-Path $projectRoot 'pyproject.toml'),
            (Join-Path $projectRoot '.dockerignore')
        )
        Destination = "$RemoteDirectory/"
    }
)

Write-Host "[1/3] Preparing $remote`:$RemoteDirectory" -ForegroundColor Cyan
& ssh @sshOptions $remote "mkdir -p $RemoteDirectory/app/services $RemoteDirectory/app/static $RemoteDirectory/app/templates && echo REMOTE_DIRECTORY_READY"
if ($LASTEXITCODE -ne 0) {
    throw 'Could not prepare the remote directory.'
}

Write-Host '[2/3] Uploading application and Compose configuration' -ForegroundColor Cyan
foreach ($group in $uploadGroups) {
    $sources = @($group.Sources)
    if ($sources.Count -eq 0) {
        throw "No files found for $($group.Label)."
    }
    Write-Host "  Uploading $($group.Label)"
    & scp @sshOptions @sources "${remote}:$($group.Destination)"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upload $($group.Label)."
    }
}

Write-Host '[3/3] Pulling, building, and starting containers' -ForegroundColor Cyan
$compose = 'docker compose -f docker-compose.yml -f docker-compose.aws.yml'
& ssh @sshOptions $remote "cd $RemoteDirectory && export PUBLIC_HOST='$publicIp' && $compose pull && $compose up -d --build && $compose up -d --force-recreate proxy"
if ($LASTEXITCODE -ne 0) {
    throw 'Remote Docker Compose deployment failed.'
}

Write-Host "Deployment complete. Open https://${publicIp}:8443" -ForegroundColor Green
