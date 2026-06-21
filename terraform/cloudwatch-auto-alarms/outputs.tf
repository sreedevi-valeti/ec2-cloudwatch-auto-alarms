output "warning_topic_arn" {
  description = "SNS topic for Warning alarms."
  value       = aws_sns_topic.warning.arn
}

output "critical_topic_arn" {
  description = "SNS topic for Critical alarms."
  value       = aws_sns_topic.critical.arn
}

output "lambda_function_name" {
  value = aws_lambda_function.auto_alarms.function_name
}

output "instance_profile_name" {
  description = "Attach this instance profile to monitored EC2 instances."
  value       = aws_iam_instance_profile.instance.name
}

output "agent_target_tag" {
  description = "Tag EC2 instances with this key/value to receive the CloudWatch agent."
  value       = "${var.agent_target_tag_key}=${var.agent_target_tag_value}"
}

output "alarm_prefix" {
  value = var.alarm_prefix
}
