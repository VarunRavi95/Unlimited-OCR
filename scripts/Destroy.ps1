param(
    [string]$InfraDirectory = (Join-Path $PSScriptRoot '..\infra'),
    [switch]$AutoApprove,
    [switch]$DeleteArtifacts
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Command terraform
if ($DeleteArtifacts) {
    Assert-Command aws
}
if(-not $AutoApprove){
    $scope = if ($DeleteArtifacts) {
        'all AWS PoC resources and every version of every S3 artifact'
    } else {
        'EC2, EIP, VPC, and supporting resources (a non-empty S3 bucket remains protected)'
    }
    $confirmation=Read-Host "Type DESTROY to remove $scope"
    if($confirmation -cne 'DESTROY'){throw 'Destroy cancelled.'}
}

if ($DeleteArtifacts) {
    $bucket = Get-TerraformOutput -Name 's3_bucket' -InfraDirectory $InfraDirectory
    Write-Host "Purging all object versions from s3://$bucket" -ForegroundColor Yellow

    do {
        $listingJson = & aws s3api list-object-versions --bucket $bucket --output json
        if ($LASTEXITCODE -ne 0) {
            throw "Could not list object versions in s3://$bucket."
        }
        $listing = $listingJson | ConvertFrom-Json
        $versions = if ($listing.PSObject.Properties['Versions']) {
            @($listing.Versions)
        } else {
            @()
        }
        $deleteMarkers = if ($listing.PSObject.Properties['DeleteMarkers']) {
            @($listing.DeleteMarkers)
        } else {
            @()
        }
        $objects = @()
        foreach ($entry in $versions + $deleteMarkers) {
            if ($null -ne $entry -and $entry.Key -and $entry.VersionId) {
                $objects += [ordered]@{ Key = $entry.Key; VersionId = $entry.VersionId }
            }
        }
        for ($offset = 0; $offset -lt $objects.Count; $offset += 1000) {
            $last = [Math]::Min($offset + 999, $objects.Count - 1)
            $batch = @($objects[$offset..$last])
            $payloadPath = [System.IO.Path]::GetTempFileName()
            try {
                @{ Objects = $batch; Quiet = $true } |
                    ConvertTo-Json -Depth 5 -Compress |
                    ForEach-Object {
                        [System.IO.File]::WriteAllText(
                            $payloadPath,
                            $_,
                            [System.Text.UTF8Encoding]::new($false)
                        )
                    }
                $payloadUri = 'file://' + ($payloadPath -replace '\\', '/')
                & aws s3api delete-objects --bucket $bucket --delete $payloadUri | Out-Null
                if ($LASTEXITCODE -ne 0) {
                    throw "Could not delete an object-version batch from s3://$bucket."
                }
            }
            finally {
                Remove-Item -LiteralPath $payloadPath -Force -ErrorAction SilentlyContinue
            }
        }
    } while ($objects.Count -gt 0)

    $uploadsJson = & aws s3api list-multipart-uploads --bucket $bucket --output json
    if ($LASTEXITCODE -ne 0) {
        throw "Could not list multipart uploads in s3://$bucket."
    }
    $uploadsResponse = $uploadsJson | ConvertFrom-Json
    $uploads = if ($uploadsResponse.PSObject.Properties['Uploads']) {
        @($uploadsResponse.Uploads)
    } else {
        @()
    }
    foreach ($upload in @($uploads)) {
        if ($null -ne $upload) {
            & aws s3api abort-multipart-upload --bucket $bucket --key $upload.Key --upload-id $upload.UploadId
            if ($LASTEXITCODE -ne 0) {
                throw "Could not abort multipart upload $($upload.UploadId)."
            }
        }
    }
    Write-Host 'S3 artifact bucket is empty.' -ForegroundColor Green
}

$arguments=@("-chdir=$InfraDirectory",'destroy');if($AutoApprove){$arguments+='-auto-approve'}
& terraform @arguments
if($LASTEXITCODE -ne 0){throw 'Terraform destroy failed.'}
