# Fire when an instance enters "running" (new launch handling lives in the Lambda).
resource "aws_cloudwatch_event_rule" "running" {
  name        = "${var.name_prefix}-running"
  description = "EC2 instance reached running state"
  tags        = var.tags

  event_pattern = jsonencode({
    source      = ["aws.ec2"]
    detail-type = ["EC2 Instance State-change Notification"]
    detail = {
      state = ["running"]
    }
  })
}

# Fire when an instance is being torn down -> delete its alarms.
resource "aws_cloudwatch_event_rule" "terminated" {
  name        = "${var.name_prefix}-terminated"
  description = "EC2 instance terminating/terminated"
  tags        = var.tags

  event_pattern = jsonencode({
    source      = ["aws.ec2"]
    detail-type = ["EC2 Instance State-change Notification"]
    detail = {
      state = ["shutting-down", "terminated"]
    }
  })
}

resource "aws_cloudwatch_event_target" "running" {
  rule      = aws_cloudwatch_event_rule.running.name
  target_id = "lambda"
  arn       = aws_lambda_function.auto_alarms.arn
}

resource "aws_cloudwatch_event_target" "terminated" {
  rule      = aws_cloudwatch_event_rule.terminated.name
  target_id = "lambda"
  arn       = aws_lambda_function.auto_alarms.arn
}

resource "aws_lambda_permission" "running" {
  statement_id  = "AllowRunningRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.auto_alarms.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.running.arn
}

resource "aws_lambda_permission" "terminated" {
  statement_id  = "AllowTerminatedRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.auto_alarms.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.terminated.arn
}

# Low-frequency safety sweep (default weekly): backstops the per-launch one-shot recheck
# and picks up volumes attached after launch. Disk alarm creation is normally handled
# event-driven by the one-shot recheck, so this is infrequent.
resource "aws_cloudwatch_event_rule" "reconcile" {
  count               = var.enable_reconcile ? 1 : 0
  name                = "${var.name_prefix}-reconcile"
  description         = "Weekly safety sweep of EC2 disk alarms"
  schedule_expression = "rate(${var.reconcile_interval_minutes} minute${var.reconcile_interval_minutes == 1 ? "" : "s"})"
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "reconcile" {
  count     = var.enable_reconcile ? 1 : 0
  rule      = aws_cloudwatch_event_rule.reconcile[0].name
  target_id = "lambda"
  arn       = aws_lambda_function.auto_alarms.arn
}

resource "aws_lambda_permission" "reconcile" {
  count         = var.enable_reconcile ? 1 : 0
  statement_id  = "AllowReconcileRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.auto_alarms.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reconcile[0].arn
}
