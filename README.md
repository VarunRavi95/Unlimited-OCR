# AWS Unlimited-OCR Invoice Extraction PoC

A single-user, asynchronous invoice extraction application hosted on one GPU EC2 instance in Mumbai. Baidu Unlimited-OCR produces traceable Markdown; Amazon Bedrock translates it and returns schema-constrained accounts-payable JSON.

For the complete as-built architecture, data flow, security, interfaces, operations, testing, troubleshooting, and production roadmap, see the [PoC technical documentation](docs/POC_TECHNICAL_DOCUMENTATION.md). An [editable FigJam architecture diagram](https://www.figma.com/board/exL40zbRnwDGkbNTWttrd6?utm_source=other&utm_content=edit_in_figjam&architecture=true) is also available.

## Architecture

```text
Browser :8443
   -> Caddy (private CA HTTPS)
   -> FastAPI + SQLite queue + single worker
      -> private S3 input
      -> Unlimited-OCR vLLM container on NVIDIA T4
      -> Bedrock Converse structured output
      -> private S3 OCR diagnostics, clean Markdown, result JSON, trust assessment, and manifest
```

The runtime is intentionally pinned:

- vLLM Unlimited-OCR image index: `sha256:542961a42d9183813819a23ef3a8b50bfb4f5ef7b0fb4f8e4f56edd8445efb18`
- Hugging Face model revision: `d549bb9d6a055dbe291408916d66acc2cd5920f6`
- OCR dtype: `float16` for NVIDIA T4 compatibility (must be benchmarked against the official BF16 recipe)
- Vision attention backend: `TORCH_SDPA`, because FlashAttention 2 requires an Ampere-or-newer GPU
- Default Bedrock inference profile: `global.anthropic.claude-sonnet-4-6`

## Repository map

- `app/`: FastAPI API, web interface, SQLite job state, OCR/Bedrock clients, and artifact pipeline.
- `infra/`: Terraform for VPC, EC2, S3, IAM, CloudWatch, security group, and Elastic IP.
- `scripts/`: Windows/PowerShell workstation initialization, deployment, VS Code SSH, lifecycle, and smoke tests.
- `tests/`: unit and integration tests that do not require AWS or a GPU.
- `benchmark/`: exact field-value scoring plus offline correctness calibration tooling.

## Prerequisites and account checks

You need an AWS account permitted to:

- launch `g4dn.2xlarge` in `ap-south-1`;
- use the global Claude Sonnet 4.6 inference profile through Bedrock;
- create VPC, IAM, EC2, S3, CloudWatch Logs, SSM, and Elastic IP resources.

The global Bedrock profile can route invoice text outside India. Do not deploy this configuration when India-only processing is mandatory.

From PowerShell, install Terraform, create the SSH key, configure the AWS profile, and verify STS:

```powershell
.\scripts\Initialize-Workstation.ps1 -AwsProfile default -InstallTerraform
```

Confirm EC2 G-family quota and Bedrock model access in the AWS Console before applying. GPU capacity is availability-zone dependent; set `availability_zone` in `terraform.tfvars` if the first AZ has no capacity.

## Provision AWS

Determine your current public IPv4 address, then prepare variables:

```powershell
Copy-Item .\infra\terraform.tfvars.example .\infra\terraform.tfvars
notepad .\infra\terraform.tfvars
Push-Location .\infra
terraform init
terraform fmt -check
terraform validate
terraform plan '-out=poc.tfplan'
terraform apply .\poc.tfplan
Pop-Location
```

Set `trusted_cidr` to that address with `/32`. Terraform creates only ports 22 and 8443 from that CIDR. If your public IP changes, update the variable and apply again before trying to connect.

The EC2 bootstrap installs Docker, configures the NVIDIA runtime, creates `/opt/unlimited-ocr-poc`, and writes the infrastructure-derived `.env`. Wait for cloud-init to finish before deployment.

## Deploy and connect from VS Code

```powershell
.\scripts\Deploy.ps1
.\scripts\Configure-VSCodeSSH.ps1
.\scripts\Smoke-TestEC2.ps1
```

Install the VS Code **Remote - SSH** extension, connect to `unlimited-ocr-poc`, and open `/opt/unlimited-ocr-poc`. The first OCR image and model download is large and model health can take 20 minutes or more. Inspect it with:

```powershell
.\scripts\Status.ps1
ssh unlimited-ocr-poc 'cd /opt/unlimited-ocr-poc && docker compose logs -f ocr'
```

Open the `web_url` Terraform output. Caddy uses its private internal CA because the PoC has an IP rather than a DNS name; the browser will show a certificate warning. Access is still encrypted and restricted to the configured `/32`.

## API and artifacts

- `GET /`: upload and results UI.
- `POST /api/jobs`: multipart `file`; returns `202` with a job ID.
- `GET /api/jobs/{job_id}`: status, stage timings, warnings, extraction result, and trust assessment.
- `POST /api/jobs/{job_id}/retry`: requeues a failed job and resumes at Bedrock when clean OCR was already persisted.
- `GET /api/jobs/{job_id}/artifacts/{source|raw|clean|ocr_metadata|ocr_likelihoods|result|trust|manifest}`: source or traceability artifact.

Files are limited to PDF, JPEG, and PNG, 20 MB, and 20 pages. Encrypted PDFs are rejected. PDFs render at 300 DPI; the measured benchmark is valid only for accepted-quality documents at 150 DPI or better.

Completed jobs write:

```text
s3://<bucket>/incoming/<job-id>/source.<ext>
s3://<bucket>/results/<job-id>/ocr/raw.txt
s3://<bucket>/results/<job-id>/ocr/clean.md
s3://<bucket>/results/<job-id>/ocr/metadata.json
s3://<bucket>/results/<job-id>/ocr/token-likelihoods.json.gz
s3://<bucket>/results/<job-id>/bedrock/result.json
s3://<bucket>/results/<job-id>/quality/trust-assessment.json
s3://<bucket>/results/<job-id>/manifest.json
```

One-page requests default to a 4,096-token OCR output limit; multi-page requests retain the 8,192-token limit. vLLM selected-token log-probabilities are stored as private diagnostics and aligned to exact evidence spans. The customer UI initially shows `Evidence supported`, `Review`, `Unsupported`, or `Not assessed`; it never presents raw model likelihood as correctness probability. Numerical estimated field correctness is enabled only by a promoted calibration artifact. Unavailable fields remain `null`.

## Test and benchmark

Install development dependencies and run the non-GPU suite:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
python -m pytest
python -m ruff check app tests benchmark
```

For acceptance, label at least 20 invoices across English, Hindi, and Tamil, including multi-page, tables, missing fields, and degraded scans. Put one line per job in a copy of `benchmark/labels.example.jsonl`, save downloaded result JSON as `benchmark/results/<job-id>.json`, and run:

```powershell
python .\benchmark\evaluate.py .\benchmark\labels.jsonl .\benchmark\results --output .\benchmark\report.json
```

The target is at least `0.85` field-value accuracy for documents meeting the 150 DPI prerequisite. Translation quality and evidence fidelity still require human review.

For confidence calibration, collect at least 100 labelled invoices (target 150), download both result and trust JSON to `benchmark/results` and `benchmark/trust`, include language, quality bucket, and page count in the labels, then run:

```powershell
python .\benchmark\calibrate_confidence.py .\benchmark\labels.jsonl .\benchmark\results .\benchmark\trust --output .\benchmark\calibration.json --version calibration-v1
```

The generated artifact sets `promoted=true` only when held-out ECE, Brier score, high-confidence critical-field accuracy, and incorrect-field review-recall gates all pass. Until then, keep `TRUST_MODE=signals`.

## Cost and lifecycle controls

```powershell
.\scripts\Stop.ps1
.\scripts\Start.ps1
.\scripts\Status.ps1
.\scripts\Destroy.ps1
```

Stop the GPU instance whenever it is idle. EBS and Elastic IP charges continue while stopped. S3 objects expire after 30 days, including noncurrent versions. The bucket uses `force_destroy = false`, so Terraform deliberately refuses to delete a non-empty evidence bucket; empty it only after confirming the artifacts are no longer required.

## PoC boundaries

There is no autoscaling, high availability, ERP integration, reviewer portal, production identity provider, Textract fallback, or alternate OCR engine. SQLite and the single worker are intentional PoC constraints. Production hardening should replace IP-only access and self-signed TLS with authenticated access behind a managed HTTPS endpoint.
