# CloudWatch agent configs pushed into the CWAgent namespace, dimensioned by InstanceId so the
# Lambda can discover them. Linux reports mem_used_percent / disk_used_percent; Windows reports
# the native perf counters "Memory % Committed Bytes In Use" and "LogicalDisk % Free Space".
locals {
  cw_agent_config_linux = jsonencode({
    agent = {
      metrics_collection_interval = 60
    }
    metrics = {
      namespace = "CWAgent"
      append_dimensions = {
        InstanceId = "$${aws:InstanceId}"
      }
      metrics_collected = {
        mem = {
          measurement = ["mem_used_percent"]
        }
        disk = {
          measurement = ["disk_used_percent"]
          resources   = ["*"]
          drop_device = true
          # vfat = static /boot/efi; efivarfs = EFI vars pseudo-fs — neither worth monitoring.
          ignore_file_system_types = ["sysfs", "devtmpfs", "tmpfs", "overlay", "squashfs", "vfat", "efivarfs"]
        }
      }
    }
  })

  cw_agent_config_windows = jsonencode({
    agent = {
      metrics_collection_interval = 60
    }
    metrics = {
      namespace = "CWAgent"
      append_dimensions = {
        InstanceId = "$${aws:InstanceId}"
      }
      metrics_collected = {
        Memory = {
          measurement = ["% Committed Bytes In Use"]
        }
        LogicalDisk = {
          # % Free Space per drive; the Lambda alarms when this drops LOW.
          measurement = ["% Free Space"]
          resources   = ["*"]
        }
      }
    }
  })
}

resource "aws_ssm_parameter" "cw_agent_config" {
  count = var.manage_cloudwatch_agent ? 1 : 0
  name  = "/${var.name_prefix}/cloudwatch-agent-config"
  type  = "String"
  value = local.cw_agent_config_linux
  tags  = var.tags
}

resource "aws_ssm_parameter" "cw_agent_config_windows" {
  count = var.manage_cloudwatch_agent ? 1 : 0
  name  = "/${var.name_prefix}/cloudwatch-agent-config-windows"
  type  = "String"
  value = local.cw_agent_config_windows
  tags  = var.tags
}

# Cross-OS setup: install the agent via the AWS-managed package (works on Windows, RPM and
# Debian/Ubuntu), then fetch+start the config. The fetch-config step is platform-gated with a
# precondition so SSM runs only the step matching each instance's OS -- one association, no OS tag.
resource "aws_ssm_document" "cw_agent_setup" {
  count           = var.manage_cloudwatch_agent ? 1 : 0
  name            = "${var.name_prefix}-cw-agent-setup"
  document_type   = "Command"
  document_format = "YAML"
  tags            = var.tags

  content = <<-DOC
    schemaVersion: '2.2'
    description: Install and configure the Amazon CloudWatch agent from SSM Parameter Store (Linux + Windows).
    parameters:
      linuxConfigParam:
        type: String
        description: SSM parameter holding the Linux CloudWatch agent config.
        default: ${aws_ssm_parameter.cw_agent_config[0].name}
      windowsConfigParam:
        type: String
        description: SSM parameter holding the Windows CloudWatch agent config.
        default: ${aws_ssm_parameter.cw_agent_config_windows[0].name}
    mainSteps:
      - action: aws:configurePackage
        name: installCloudWatchAgent
        inputs:
          name: AmazonCloudWatchAgent
          action: Install
      - action: aws:runShellScript
        name: configureLinux
        precondition:
          StringEquals:
            - platformType
            - Linux
        inputs:
          runCommand:
            - /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s -c ssm:{{ linuxConfigParam }}
      - action: aws:runPowerShellScript
        name: configureWindows
        precondition:
          StringEquals:
            - platformType
            - Windows
        inputs:
          runCommand:
            - '& "C:\Program Files\Amazon\AmazonCloudWatchAgent\amazon-cloudwatch-agent-ctl.ps1" -a fetch-config -m ec2 -s -c ssm:{{ windowsConfigParam }}'
  DOC
}

resource "aws_ssm_association" "cw_agent" {
  count            = var.manage_cloudwatch_agent ? 1 : 0
  association_name = "${var.name_prefix}-cw-agent"
  name             = aws_ssm_document.cw_agent_setup[0].name

  targets {
    key    = "tag:${var.agent_target_tag_key}"
    values = [var.agent_target_tag_value]
  }

  # Re-run daily so config drift / failed first-boot installs self-heal.
  schedule_expression = "rate(1 day)"
}
