param([string]$AwsProfile='default',[string]$AwsRegion='ap-south-1',[string]$IdentityFile=(Join-Path $env:USERPROFILE '.ssh\unlimited-ocr-poc'),[string]$InfraDirectory=(Join-Path $PSScriptRoot '..\infra'))
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Command aws
$instanceId=Get-InstanceId -InfraDirectory $InfraDirectory
$publicIp=Get-TerraformOutput -Name 'public_ip' -InfraDirectory $InfraDirectory
& aws ec2 describe-instances --instance-ids $instanceId --region $AwsRegion --profile $AwsProfile --query 'Reservations[0].Instances[0].{State:State.Name,Type:InstanceType,PublicIp:PublicIpAddress,AZ:Placement.AvailabilityZone}' --output table
if($LASTEXITCODE -ne 0){throw 'Could not read EC2 status.'}
if((Test-Path -LiteralPath $IdentityFile)-and(Get-Command ssh -ErrorAction SilentlyContinue)){& ssh -i $IdentityFile -o ConnectTimeout=5 "ubuntu@$publicIp" 'cd /opt/unlimited-ocr-poc && docker compose ps' 2>$null}

