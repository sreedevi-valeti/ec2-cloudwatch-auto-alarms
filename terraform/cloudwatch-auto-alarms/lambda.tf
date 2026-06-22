data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/src"
  output_path = "${path.module}/build/auto_alarms.zip"
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name_prefix}"
  retention_in_days = 30
  tags              = var.tags
}

resource "aws_lambda_function" "auto_alarms" {
  function_name    = var.name_prefix
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "auto_alarms.lambda_handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = var.lambda_timeout
  memory_size      = 128

  environment {
    variables = {
      ALARM_PREFIX            = var.alarm_prefix
      WARNING_TOPIC_ARN       = aws_sns_topic.warning.arn
      CRITICAL_TOPIC_ARN      = aws_sns_topic.critical.arn
      CPU_WARNING             = tostring(var.cpu_warning)
      CPU_CRITICAL            = tostring(var.cpu_critical)
      MEM_WARNING             = tostring(var.mem_warning)
      MEM_CRITICAL            = tostring(var.mem_critical)
      DISK_WARNING            = tostring(var.disk_warning)
      DISK_CRITICAL           = tostring(var.disk_critical)
      WIN_MEM_WARNING         = tostring(var.win_mem_warning)
      WIN_MEM_CRITICAL        = tostring(var.win_mem_critical)
      WIN_DISK_FREE_WARNING   = tostring(var.win_disk_free_warning)
      WIN_DISK_FREE_CRITICAL  = tostring(var.win_disk_free_critical)
      PERIOD                  = tostring(var.period)
      EVAL_PERIODS            = tostring(var.eval_periods)
      STATUS_PERIOD           = tostring(var.status_period)
      STATUS_EVAL_PERIODS     = tostring(var.status_eval_periods)
      DISK_DISCOVERY_MAX_WAIT = tostring(var.disk_discovery_max_wait)
      AGENT_TAG_KEY           = var.agent_target_tag_key
      AGENT_TAG_VALUE         = var.agent_target_tag_value
      SCHEDULER_ROLE_ARN      = aws_iam_role.scheduler.arn
      RECHECK_DELAY_SECONDS   = tostring(var.recheck_delay_seconds)
      IGNORE_DISK_FSTYPES     = join(",", var.ignore_disk_fstypes)

      ENABLE_COMPOSITE_ALARMS           = tostring(var.enable_composite_alarms)
      COMPOSITE_SUPPRESSOR_WAIT_SECONDS = tostring(var.composite_suppressor_wait_seconds)
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
  tags       = var.tags
}
