resource "aws_sns_topic" "warning" {
  name = "${var.name_prefix}-warning"
  tags = var.tags
}

resource "aws_sns_topic" "critical" {
  name = "${var.name_prefix}-critical"
  tags = var.tags
}

resource "aws_sns_topic_subscription" "warning_email" {
  for_each  = toset(var.warning_subscription_emails)
  topic_arn = aws_sns_topic.warning.arn
  protocol  = "email"
  endpoint  = each.value
}

resource "aws_sns_topic_subscription" "critical_email" {
  for_each  = toset(var.critical_subscription_emails)
  topic_arn = aws_sns_topic.critical.arn
  protocol  = "email"
  endpoint  = each.value
}

# Allow CloudWatch alarms to publish. Each topic policy must reference only its own ARN.
data "aws_iam_policy_document" "warning_publish" {
  statement {
    sid     = "AllowCloudWatchAlarmsPublish"
    actions = ["sns:Publish"]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    resources = [aws_sns_topic.warning.arn]
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:cloudwatch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:alarm:${var.alarm_prefix}-*"]
    }
  }
}

data "aws_iam_policy_document" "critical_publish" {
  statement {
    sid     = "AllowCloudWatchAlarmsPublish"
    actions = ["sns:Publish"]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    resources = [aws_sns_topic.critical.arn]
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:cloudwatch:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:alarm:${var.alarm_prefix}-*"]
    }
  }
}

resource "aws_sns_topic_policy" "warning" {
  arn    = aws_sns_topic.warning.arn
  policy = data.aws_iam_policy_document.warning_publish.json
}

resource "aws_sns_topic_policy" "critical" {
  arn    = aws_sns_topic.critical.arn
  policy = data.aws_iam_policy_document.critical_publish.json
}
