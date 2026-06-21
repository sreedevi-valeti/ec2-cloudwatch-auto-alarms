# CloudWatch agent config: pushes mem_used_percent and disk_used_percent (all mounts)
# into the CWAgent namespace, dimensioned by InstanceId so the Lambda can discover them.
locals {
  cw_agent_config = jsonencode({
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
          # vfat excludes the tiny static /boot/efi partition (not worth monitoring).
          ignore_file_system_types = ["sysfs", "devtmpfs", "tmpfs", "overlay", "squashfs", "vfat"]
        }
      }
    }
  })
}

resource "aws_ssm_parameter" "cw_agent_config" {
  count = var.manage_cloudwatch_agent ? 1 : 0
  name  = "/${var.name_prefix}/cloudwatch-agent-config"
  type  = "String"
  value = local.cw_agent_config
  tags  = var.tags
}

# Single ordered document: ensure the agent is installed, THEN fetch config + start it.
# Doing both in one step avoids the State Manager race where a separate "configure"
# association runs before the "install" association has finished.
resource "aws_ssm_document" "cw_agent_setup" {
  count           = var.manage_cloudwatch_agent ? 1 : 0
  name            = "${var.name_prefix}-cw-agent-setup"
  document_type   = "Command"
  document_format = "YAML"
  tags            = var.tags

  content = <<-DOC
    schemaVersion: '2.2'
    description: Install and configure the Amazon CloudWatch agent from SSM Parameter Store.
    parameters:
      configParam:
        type: String
        description: SSM parameter holding the CloudWatch agent config.
        default: ${aws_ssm_parameter.cw_agent_config[0].name}
    mainSteps:
      - action: aws:runShellScript
        name: installAndConfigure
        inputs:
          runCommand:
            - set -e
            - if ! rpm -q amazon-cloudwatch-agent >/dev/null 2>&1; then (dnf install -y amazon-cloudwatch-agent || yum install -y amazon-cloudwatch-agent); fi
            - /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s -c ssm:{{ configParam }}
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
