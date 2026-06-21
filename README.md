# EC2 Lifecycle-Driven CloudWatch Auto-Alarms

Automatically create CloudWatch alarms (**CPU, memory, per-filesystem disk, and status
checks** — each Warning + Critical) the moment an EC2 instance launches, and delete **all**
of them automatically when the instance is terminated. Event-driven, serverless, and
delivered as Terraform.

## Architecture & workflow

![Architecture](docs/architecture.png)

**In one paragraph:** CPU and status checks are available from AWS instantly, so they're
created at launch. Memory and disk need the CloudWatch agent (installed/configured via SSM),
which takes a few minutes to start reporting — so those alarms are **deferred** to a one-shot
recheck (~30 min after launch) and created only once the agent is confirmed reporting. A weekly
safety sweep catches volumes attached later. On termination, every alarm named
`AutoAlarm-<instance-id>-*` is deleted, so nothing is orphaned.

## Key design points

- **Event-driven, ~$0 orchestration** — EventBridge + Lambda + a self-deleting one-shot
  schedule. Scheduled triggers aren't billed; the only real cost is alarms + custom metrics.
- **No false alarms during warm-up** — agent-dependent alarms (mem/disk) are created only
  after metrics exist; status-check alarms use `TreatMissingData=missing` so a still-initializing
  instance never trips auto-recovery.
- **Brand-new launches only** — a stop→start is detected and skipped (alarms persist across stop).
- **Self-healing** — weekly sweep + daily agent re-config association recover from drift.

## Deploy

See [`terraform/cloudwatch-auto-alarms/README.md`](terraform/cloudwatch-auto-alarms/README.md)
for full deploy, configuration, and verification steps.

```bash
cd terraform/cloudwatch-auto-alarms
cp terraform.tfvars.example terraform.tfvars   # set your notification emails
terraform init && terraform apply
```

Then attach the output `instance_profile_name` to instances and tag them
`AutoAlarmAgent=enabled`.
