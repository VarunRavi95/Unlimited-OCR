output "instance_id" {
  description = "GPU EC2 instance ID."
  value       = aws_instance.app.id
}

output "public_ip" {
  description = "Stable Elastic IP."
  value       = aws_eip.app.public_ip
}

output "web_url" {
  description = "Self-signed HTTPS PoC URL."
  value       = "https://${aws_eip.app.public_ip}:8443"
}

output "ssh_command" {
  description = "OpenSSH command."
  value       = "ssh -i ${trimsuffix(var.ssh_public_key_path, ".pub")} ubuntu@${aws_eip.app.public_ip}"
}

output "s3_bucket" {
  description = "Private invoice artifact bucket."
  value       = aws_s3_bucket.artifacts.bucket
}

output "resolved_dlami_id" {
  description = "DLAMI resolved from the public SSM parameter."
  value       = data.aws_ssm_parameter.dlami.value
  sensitive   = true
}

output "cloudwatch_log_group" {
  description = "Application CloudWatch Logs group."
  value       = aws_cloudwatch_log_group.app.name
}
