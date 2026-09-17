variable "aws_region" {
  description = "AWS region for the PoC."
  type        = string
  default     = "ap-south-1"
}

variable "aws_profile" {
  description = "Local AWS CLI profile used by Terraform."
  type        = string
  default     = "default"
}

variable "project_name" {
  description = "Resource name prefix."
  type        = string
  default     = "unlimited-ocr-poc"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.project_name))
    error_message = "Use 3-31 lowercase alphanumeric or hyphen characters."
  }
}

variable "trusted_cidr" {
  description = "Single trusted public IPv4 address in /32 notation for SSH and HTTPS."
  type        = string

  validation {
    condition     = can(cidrhost(var.trusted_cidr, 0)) && endswith(var.trusted_cidr, "/32")
    error_message = "Use a valid IPv4 /32, for example 203.0.113.10/32."
  }
}

variable "ssh_public_key_path" {
  description = "Path to the local Ed25519 public key."
  type        = string
  default     = "~/.ssh/unlimited-ocr-poc.pub"
}

variable "instance_type" {
  description = "GPU EC2 instance size."
  type        = string
  default     = "g4dn.2xlarge"
}

variable "availability_zone" {
  description = "Optional AZ override for GPU instance capacity."
  type        = string
  default     = ""
}

variable "root_volume_size_gib" {
  description = "Encrypted gp3 root volume size."
  type        = number
  default     = 150

  validation {
    condition     = var.root_volume_size_gib >= 100
    error_message = "Use at least 100 GiB for the image and model cache."
  }
}
