param([string]$AwsProfile='default',[string]$AwsRegion='ap-south-1',[string]$InfraDirectory=(Join-Path $PSScriptRoot '..\infra'))
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Command aws
$instanceId=Get-InstanceId -InfraDirectory $InfraDirectory
& aws ec2 start-instances --instance-ids $instanceId --region $AwsRegion --profile $AwsProfile | Out-Null
if($LASTEXITCODE -ne 0){throw 'EC2 start request failed.'}
& aws ec2 wait instance-running --instance-ids $instanceId --region $AwsRegion --profile $AwsProfile
if($LASTEXITCODE -ne 0){throw 'EC2 did not reach running state.'}
Write-Host "Instance $instanceId is running." -ForegroundColor Green

