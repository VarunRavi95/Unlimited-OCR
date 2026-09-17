param([string]$AwsProfile='default',[string]$AwsRegion='ap-south-1',[string]$InfraDirectory=(Join-Path $PSScriptRoot '..\infra'))
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Command aws
$instanceId=Get-InstanceId -InfraDirectory $InfraDirectory
& aws ec2 stop-instances --instance-ids $instanceId --region $AwsRegion --profile $AwsProfile | Out-Null
if($LASTEXITCODE -ne 0){throw 'EC2 stop request failed.'}
Write-Host "Stop requested for $instanceId. EBS and Elastic IP charges continue." -ForegroundColor Yellow

