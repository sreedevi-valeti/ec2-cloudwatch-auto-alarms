# EC2 Auto CloudWatch Alarms

Automatically creates CloudWatch alarms when an EC2 instance is **newly launched**
and deletes them when the instance is **terminated** — no manual upkeep, no orphans.

## What it creates per instance

| Metric | Source | Warning | Critical |
|---|---|---|---|
| CPUUtilization | EC2 native | ≥70% | ≥90% |
| mem_used_percent | CloudWatch agent | ≥80% | ≥90% |
| disk_used_percent (one pair **per filesystem**) | CloudWatch agent | ≥80% | ≥90% |
| StatusCheckFailed_System | EC2 native | — | ≥1 → **auto-recover** |
| StatusCheckFailed_Instance | EC2 native | — | ≥1 → **auto-reboot** |

Warning alarms publish to the **Warning** SNS topic; Critical to the **Critical** topic.

## How it works

```
EC2 -> running      -> EventBridge -> Lambda -> create alarms (new launches only)
EC2 -> terminated   -> EventBridge -> Lambda -> delete alarms (by name prefix)
```

- Alarms are named `AutoAlarm-<instance-id>-<metric>-<severity>`. Cleanup just deletes
  everything matching `AutoAlarm-<instance-id>-` — no state store.
- **New launches only**: a stopped instance also enters `running` when started. The
  Lambda checks for existing alarms first and skips if found (alarms are only deleted on
  terminate, so a stopped instance keeps them — the check is reliable).
- **Memory and disk need the CloudWatch agent.** Terraform installs/configures it via SSM
  on any instance tagged `AutoAlarmAgent=enabled`. Disk alarms are discovered dynamically
  from `ListMetrics`, so every filesystem is covered however many there are.
- **Agent-dependent alarms (memory + disk) are deferred, not created at launch.** They come
  from the CloudWatch agent, which usually isn't reporting at launch — creating them eagerly
  (with `breaching`) would fire false alarms during warm-up. So at launch only CPU + status
  alarms are created; the Lambda schedules a single **one-shot recheck** (EventBridge
  Scheduler, ~30 min later, self-deletes) that creates the memory + per-filesystem disk alarms
  *only once the agent is confirmed reporting*. A low-frequency **weekly safety sweep**
  (`enable_reconcile` / `reconcile_interval_minutes`, default 10080) backstops this and catches
  **volumes attached after launch**. All paths skip existing alarms, so nothing is duplicated.

### Cost notes
- Reconcile/recheck infrastructure (EventBridge schedules, Lambda, `ListMetrics`/`DescribeAlarms`)
  is effectively **$0** — scheduled triggers aren't billed and those CloudWatch APIs are free.
- Real cost = **alarms** (~$0.10 each/month) + **CWAgent custom metrics** (~$0.30 each/month).
  To trim: reduce alarms/metrics, not check frequency. Inode collection is intentionally off.

## Deploy

```bash
cd terraform/cloudwatch-auto-alarms
cp terraform.tfvars.example terraform.tfvars   # edit emails / thresholds
terraform init
terraform apply
```

Then for each instance you want monitored:
1. Attach the output `instance_profile_name` (so the agent can talk to SSM/CloudWatch).
2. Tag it `AutoAlarmAgent=enabled` (so the agent is installed/configured).

Confirm the SNS email subscriptions from your inbox after `apply`.

## Per-instance threshold overrides

Tag an instance to override a default threshold (no redeploy):

```
AutoAlarm-disk_used_percent-Critical = 85
AutoAlarm-CPUUtilization-Warning     = 60
```

Tag format: `AutoAlarm-<metric>-<Warning|Critical>` = number.

## Verify end-to-end

```bash
# 4. agent reporting?
aws cloudwatch list-metrics --namespace CWAgent --dimensions Name=InstanceId,Value=i-XXXX

# 5. alarms created? (expect CPU W/C, mem W/C, per-disk W/C, 2x status checks)
aws cloudwatch describe-alarms --alarm-name-prefix AutoAlarm-i-XXXX-

# 7. stop+start should NOT duplicate alarms (Lambda logs "start-after-stop, skipping")
aws ec2 stop-instances  --instance-ids i-XXXX
aws ec2 start-instances --instance-ids i-XXXX

# 8. terminate -> alarms gone
aws ec2 terminate-instances --instance-ids i-XXXX
aws cloudwatch describe-alarms --alarm-name-prefix AutoAlarm-i-XXXX-   # expect empty
```

Lambda logs: `/aws/lambda/<name_prefix>`.

## Notes / cost

- Each alarm is ~$0.10/month. Per-disk × 2 severities scales with volume count — watch the
  per-account CloudWatch alarm quota on large fleets.
- The Lambda may wait up to `disk_discovery_max_wait` (default 180s) on a new launch for the
  agent's first disk report; `lambda_timeout` must stay above it.
- Set `manage_cloudwatch_agent = false` if you install/configure the agent yourself; the
  agent config still must publish `mem_used_percent` / `disk_used_percent` with an
  `InstanceId` dimension into the `CWAgent` namespace.
