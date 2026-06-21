variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for resource names (Lambda, roles, rules, topics)."
  type        = string
  default     = "ec2-auto-alarms"
}

variable "alarm_prefix" {
  description = "Prefix for every managed alarm name. Cleanup deletes alarms by this prefix."
  type        = string
  default     = "AutoAlarm"
}

variable "warning_subscription_emails" {
  description = "Emails subscribed to the Warning SNS topic."
  type        = list(string)
  default     = []
}

variable "critical_subscription_emails" {
  description = "Emails subscribed to the Critical SNS topic."
  type        = list(string)
  default     = []
}

# --- Alarm thresholds (per-instance overridable via tags) ------------------- #
variable "cpu_warning" {
  type    = number
  default = 70
}
variable "cpu_critical" {
  type    = number
  default = 90
}
variable "mem_warning" {
  type    = number
  default = 80
}
variable "mem_critical" {
  type    = number
  default = 90
}
variable "disk_warning" {
  type    = number
  default = 80
}
variable "disk_critical" {
  type    = number
  default = 90
}

# --- Evaluation tuning ------------------------------------------------------ #
variable "period" {
  description = "Seconds per datapoint for CPU/mem/disk alarms."
  type        = number
  default     = 300
}
variable "eval_periods" {
  description = "Datapoints required to alarm for CPU/mem/disk."
  type        = number
  default     = 3
}
variable "status_period" {
  type    = number
  default = 60
}
variable "status_eval_periods" {
  type    = number
  default = 2
}

variable "disk_discovery_max_wait" {
  description = "Max seconds the one-shot recheck waits for the agent to report disk metrics."
  type        = number
  default     = 120
}

variable "lambda_timeout" {
  description = "Lambda timeout. Must exceed disk_discovery_max_wait."
  type        = number
  default     = 180
}

variable "enable_reconcile" {
  description = "Run a low-frequency safety sweep that ensures disk alarms exist for all monitored instances (catches volumes attached after launch / self-heals)."
  type        = bool
  default     = true
}

variable "reconcile_interval_minutes" {
  description = "How often the safety sweep runs. Disk alarms are normally created by the per-launch one-shot recheck, so this only needs to be infrequent (default weekly)."
  type        = number
  default     = 10080
}

variable "recheck_delay_seconds" {
  description = "Delay after launch before the one-shot disk recheck fires (gives the agent time to start reporting)."
  type        = number
  default     = 1800
}

variable "manage_cloudwatch_agent" {
  description = "If true, create SSM resources to install/configure the CloudWatch agent."
  type        = bool
  default     = true
}

variable "ignore_disk_fstypes" {
  description = "Filesystem types to skip for disk alarms (vfat = the static /boot/efi partition)."
  type        = list(string)
  default     = ["vfat"]
}

variable "agent_target_tag_key" {
  description = "EC2 tag key selecting which instances get the CloudWatch agent via SSM."
  type        = string
  default     = "AutoAlarmAgent"
}

variable "agent_target_tag_value" {
  description = "EC2 tag value selecting instances for the CloudWatch agent SSM associations."
  type        = string
  default     = "enabled"
}

variable "tags" {
  description = "Tags applied to created resources."
  type        = map(string)
  default     = {}
}
