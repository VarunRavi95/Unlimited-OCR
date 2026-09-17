Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Assert-Command { param([Parameter(Mandatory)][string]$Name) if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "Required command '$Name' was not found." } }
function Get-TerraformOutput {
    param([Parameter(Mandatory)][string]$Name,[string]$InfraDirectory=(Join-Path $PSScriptRoot '..\infra'))
    Assert-Command terraform
    $value = & terraform "-chdir=$InfraDirectory" output -raw $Name
    if ($LASTEXITCODE -ne 0 -or -not $value) { throw "Could not read Terraform output '$Name'. Has terraform apply completed?" }
    return $value.Trim()
}
function Get-InstanceId { param([string]$InfraDirectory=(Join-Path $PSScriptRoot '..\infra')) return Get-TerraformOutput -Name 'instance_id' -InfraDirectory $InfraDirectory }

