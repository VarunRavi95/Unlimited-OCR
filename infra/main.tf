data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_ssm_parameter" "dlami" {
  name = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
}

locals {
  availability_zone = var.availability_zone != "" ? var.availability_zone : data.aws_availability_zones.available.names[0]
  bucket_name       = "${var.project_name}-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  log_group_name    = "/${var.project_name}/app"
  common_tags = {
    Project     = var.project_name
    Environment = "poc"
    ManagedBy   = "terraform"
  }
}

resource "aws_vpc" "this" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.project_name}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.project_name}-igw" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = "10.42.1.0/24"
  availability_zone       = local.availability_zone
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.project_name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "${var.project_name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "app" {
  name        = "${var.project_name}-app"
  description = "Trusted single-user access to the invoice PoC"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "VS Code Remote SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.trusted_cidr]
  }

  ingress {
    description = "Invoice PoC HTTPS"
    from_port   = 8443
    to_port     = 8443
    protocol    = "tcp"
    cidr_blocks = [var.trusted_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project_name}-app" }
}

resource "aws_key_pair" "this" {
  key_name   = var.project_name
  public_key = file(pathexpand(var.ssh_public_key_path))
}

resource "aws_cloudwatch_log_group" "app" {
  name              = local.log_group_name
  retention_in_days = 30
}

resource "aws_instance" "app" {
  ami                         = data.aws_ssm_parameter.dlami.value
  instance_type               = var.instance_type
  availability_zone           = local.availability_zone
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.app.id]
  key_name                    = aws_key_pair.this.key_name
  iam_instance_profile        = aws_iam_instance_profile.app.name
  associate_public_ip_address = true
  monitoring                  = true

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size_gib
    encrypted             = true
    delete_on_termination = true
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    aws_region     = var.aws_region
    s3_bucket      = aws_s3_bucket.artifacts.bucket
    log_group_name = aws_cloudwatch_log_group.app.name
  })

  user_data_replace_on_change = true
  depends_on = [
    aws_iam_role_policy.app,
    aws_iam_role_policy_attachment.ssm,
    aws_route_table_association.public,
  ]
  tags = { Name = "${var.project_name}-gpu" }
}

resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.app.id
  tags     = { Name = "${var.project_name}-eip" }
}
