"""
Auto-manage CloudWatch alarms over the EC2 instance lifecycle.

Triggered by EventBridge "EC2 Instance State-change Notification" events:
  - running                 -> create alarms (only for brand-new launches)
  - terminated / shutting-down -> delete all alarms for that instance

Alarm naming convention is the cleanup key (no database needed):
    {ALARM_PREFIX}-{instance-id}-{metric}-{severity}

Metrics covered:
  - CPUUtilization        (AWS/EC2, native)            Warning + Critical
  - mem_used_percent      (CWAgent, needs the agent)   Warning + Critical
  - disk_used_percent     (CWAgent, one pair per FS)   Warning + Critical
  - StatusCheckFailed_System   (AWS/EC2)  Critical + EC2 auto-recovery
  - StatusCheckFailed_Instance (AWS/EC2)  Critical + EC2 auto-reboot
"""

import json
import os
import re
import time
from datetime import datetime, timedelta

import boto3

cloudwatch = boto3.client("cloudwatch")
ec2 = boto3.client("ec2")
scheduler = boto3.client("scheduler")

REGION = os.environ["AWS_REGION"]
ALARM_PREFIX = os.environ.get("ALARM_PREFIX", "AutoAlarm")
WARNING_TOPIC_ARN = os.environ["WARNING_TOPIC_ARN"]
CRITICAL_TOPIC_ARN = os.environ["CRITICAL_TOPIC_ARN"]

# Defaults (overridable per-instance via tags, see _threshold()).
DEFAULTS = {
    "cpu_warning": float(os.environ.get("CPU_WARNING", "70")),
    "cpu_critical": float(os.environ.get("CPU_CRITICAL", "90")),
    "mem_warning": float(os.environ.get("MEM_WARNING", "80")),
    "mem_critical": float(os.environ.get("MEM_CRITICAL", "90")),
    "disk_warning": float(os.environ.get("DISK_WARNING", "80")),
    "disk_critical": float(os.environ.get("DISK_CRITICAL", "90")),
    # Windows: memory uses a used-% counter; disk reports FREE %, so these are low-water
    # marks (alarm when free space drops to/below them).
    "win_mem_warning": float(os.environ.get("WIN_MEM_WARNING", "80")),
    "win_mem_critical": float(os.environ.get("WIN_MEM_CRITICAL", "90")),
    "win_disk_free_warning": float(os.environ.get("WIN_DISK_FREE_WARNING", "20")),
    "win_disk_free_critical": float(os.environ.get("WIN_DISK_FREE_CRITICAL", "10")),
}

# CloudWatch-agent metric names differ by OS. Linux: mem_used_percent / disk_used_percent.
# Windows publishes native perf counters under the CWAgent namespace.
WIN_MEM_METRIC = os.environ.get("WIN_MEM_METRIC", "Memory % Committed Bytes In Use")
WIN_DISK_METRIC = os.environ.get("WIN_DISK_METRIC", "LogicalDisk % Free Space")

# Evaluate over PERIOD seconds x EVAL datapoints to avoid flapping.
PERIOD = int(os.environ.get("PERIOD", "300"))
EVAL_PERIODS = int(os.environ.get("EVAL_PERIODS", "3"))
STATUS_PERIOD = int(os.environ.get("STATUS_PERIOD", "60"))
STATUS_EVAL_PERIODS = int(os.environ.get("STATUS_EVAL_PERIODS", "2"))

# Disk metrics only appear after the CloudWatch agent has reported at least once.
# Poll ListMetrics for up to DISK_DISCOVERY_MAX_WAIT seconds before giving up.
DISK_DISCOVERY_MAX_WAIT = int(os.environ.get("DISK_DISCOVERY_MAX_WAIT", "180"))
DISK_DISCOVERY_INTERVAL = int(os.environ.get("DISK_DISCOVERY_INTERVAL", "30"))

# Tag selecting which instances the daily reconcile sweeps.
AGENT_TAG_KEY = os.environ.get("AGENT_TAG_KEY", "AutoAlarmAgent")
AGENT_TAG_VALUE = os.environ.get("AGENT_TAG_VALUE", "enabled")

# Filesystem types never worth a disk alarm (e.g. vfat = the static /boot/efi partition).
IGNORE_DISK_FSTYPES = set(filter(None, os.environ.get("IGNORE_DISK_FSTYPES", "vfat").split(",")))

# One-shot disk recheck scheduled at launch (handles the agent reporting after the
# launch event). Empty role ARN disables scheduling.
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")
RECHECK_DELAY_SECONDS = int(os.environ.get("RECHECK_DELAY_SECONDS", "600"))

# Composite alarms roll the per-metric child alarms into one notification per instance.
# The Critical composite uses the System status alarm as an ActionsSuppressor, so when an
# instance is simply DOWN the symptom flood (cpu/mem/disk) is muted and only the System
# alarm sends the single root-cause "instance down" alert.
ENABLE_COMPOSITE_ALARMS = os.environ.get("ENABLE_COMPOSITE_ALARMS", "true").lower() == "true"
COMPOSITE_SUPPRESSOR_WAIT = int(os.environ.get("COMPOSITE_SUPPRESSOR_WAIT_SECONDS", "60"))


def lambda_handler(event, context):
    # One-shot recheck fired by EventBridge Scheduler ~RECHECK_DELAY after launch,
    # once the CloudWatch agent has had time to start reporting mem/disk.
    if event.get("action") == "agent-recheck":
        _agent_recheck(event["instance-id"])
        return

    # Daily safety sweep: catches volumes attached after launch and self-heals.
    if event.get("detail-type") == "Scheduled Event":
        _reconcile()
        return

    detail = event.get("detail", {})
    instance_id = detail.get("instance-id")
    state = detail.get("state")
    print(f"Event: instance={instance_id} state={state}")

    if not instance_id or not state:
        print("Missing instance-id or state; ignoring.")
        return

    if state == "running":
        _on_running(instance_id, context)
    elif state in ("terminated", "shutting-down"):
        _on_terminated(instance_id)
    else:
        print(f"State {state} not handled.")


# --------------------------------------------------------------------------- #
# Running: create alarms (brand-new launches only)
# --------------------------------------------------------------------------- #
def _on_running(instance_id, context):
    prefix = f"{ALARM_PREFIX}-{instance_id}-"

    if _alarms_exist(prefix):
        print(f"Alarms already exist for {instance_id} -> start-after-stop, skipping.")
        return

    tags = _instance_tags(instance_id)
    print(f"New launch {instance_id}; creating alarms.")

    # Only agent-independent alarms at launch. Memory and disk come from the CloudWatch
    # agent, which usually isn't reporting yet; creating them now (with breaching) would
    # fire false alarms during warm-up. They're created by the recheck once the agent reports.
    _put_cpu_alarms(instance_id, tags)
    _put_status_check_alarms(instance_id)
    _put_composite_alarms(instance_id)  # CPU-only for now; refreshed once mem/disk arrive
    _schedule_agent_recheck(instance_id, context)

    print(f"Done creating launch alarms for {instance_id}.")


def _alarms_exist(prefix):
    resp = cloudwatch.describe_alarms(AlarmNamePrefix=prefix, MaxRecords=1)
    return bool(resp.get("MetricAlarms") or resp.get("CompositeAlarms"))


# --------------------------------------------------------------------------- #
# Scheduled reconcile: ensure mem/disk alarms exist for every monitored instance
# --------------------------------------------------------------------------- #
def _reconcile():
    instances = _monitored_running_instances()
    print(f"Reconcile: {len(instances)} monitored running instance(s).")
    for instance_id, tags, instance_os in instances:
        # No wait — create alarms only for metrics reporting now. Skips existing alarms,
        # so late-arriving agents / volumes get picked up on a later sweep.
        _create_agent_alarms(instance_id, tags, wait=False, instance_os=instance_os)


# --------------------------------------------------------------------------- #
# Agent-dependent alarms (memory + per-filesystem disk), created only once the
# CloudWatch agent is reporting so they never fire falsely during warm-up.
# --------------------------------------------------------------------------- #
def _create_agent_alarms(instance_id, tags, wait, instance_os):
    mem_metric = WIN_MEM_METRIC if instance_os == "windows" else "mem_used_percent"
    disk_metric = WIN_DISK_METRIC if instance_os == "windows" else "disk_used_percent"

    disk_metrics = (
        _discover_disk_metrics(instance_id, disk_metric)
        if wait
        else _list_disk_metrics(instance_id, disk_metric)
    )
    mem_present = _metric_exists(instance_id, mem_metric)

    if not disk_metrics and not mem_present:
        print(f"Agent not reporting for {instance_id} ({instance_os}) yet; skipping mem/disk alarms.")
        return

    existing = _existing_alarm_names(f"{ALARM_PREFIX}-{instance_id}-")
    if mem_present:
        _put_mem_alarms(instance_id, tags, existing, instance_os)
    if disk_metrics:
        _put_disk_alarms_from(instance_id, tags, disk_metrics, existing, instance_os)

    # Refresh the composite rule so the new mem/disk children roll into it.
    _put_composite_alarms(instance_id)


def _metric_exists(instance_id, metric_name):
    resp = cloudwatch.list_metrics(
        Namespace="CWAgent",
        MetricName=metric_name,
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
    )
    return bool(resp.get("Metrics"))


def _monitored_running_instances():
    instances = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(
        Filters=[
            {"Name": f"tag:{AGENT_TAG_KEY}", "Values": [AGENT_TAG_VALUE]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    ):
        for reservation in page["Reservations"]:
            for inst in reservation["Instances"]:
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                instance_os = "windows" if inst.get("Platform") == "windows" else "linux"
                instances.append((inst["InstanceId"], tags, instance_os))
    return instances


# --------------------------------------------------------------------------- #
# One-shot agent recheck: a single self-deleting schedule per launch
# --------------------------------------------------------------------------- #
def _schedule_agent_recheck(instance_id, context):
    if not SCHEDULER_ROLE_ARN:
        print("SCHEDULER_ROLE_ARN unset; skipping one-shot recheck.")
        return

    when = (datetime.utcnow() + timedelta(seconds=RECHECK_DELAY_SECONDS)).strftime(
        "at(%Y-%m-%dT%H:%M:%S)"
    )
    try:
        scheduler.create_schedule(
            Name=_schedule_name(instance_id),
            ScheduleExpression=when,
            FlexibleTimeWindow={"Mode": "OFF"},
            ActionAfterCompletion="DELETE",  # self-cleanup after it fires
            Target={
                "Arn": context.invoked_function_arn,
                "RoleArn": SCHEDULER_ROLE_ARN,
                "Input": json.dumps({"action": "agent-recheck", "instance-id": instance_id}),
            },
        )
        print(f"  scheduled agent recheck for {instance_id} {when}")
    except scheduler.exceptions.ConflictException:
        print(f"  recheck already scheduled for {instance_id}")


def _agent_recheck(instance_id):
    print(f"Agent recheck for {instance_id}.")
    tags = _instance_tags(instance_id)
    _create_agent_alarms(instance_id, tags, wait=True, instance_os=_instance_os(instance_id))
    _delete_schedule(instance_id)  # explicit fallback if ActionAfterCompletion is unavailable


def _delete_schedule(instance_id):
    try:
        scheduler.delete_schedule(Name=_schedule_name(instance_id))
        print(f"  deleted recheck schedule for {instance_id}")
    except scheduler.exceptions.ResourceNotFoundException:
        pass


def _schedule_name(instance_id):
    return f"{ALARM_PREFIX}-diskcheck-{instance_id}"


# --------------------------------------------------------------------------- #
# Terminated: delete every alarm for the instance
# --------------------------------------------------------------------------- #
def _on_terminated(instance_id):
    prefix = f"{ALARM_PREFIX}-{instance_id}-"
    metric_names = []
    composite_names = []
    paginator = cloudwatch.get_paginator("describe_alarms")
    # DescribeAlarms returns ONLY metric alarms unless AlarmTypes is given explicitly —
    # without this the composites are never seen, never deleted, and the metric deletes
    # below fail because composites still reference them.
    for page in paginator.paginate(AlarmNamePrefix=prefix, AlarmTypes=["CompositeAlarm", "MetricAlarm"]):
        metric_names.extend(a["AlarmName"] for a in page.get("MetricAlarms", []))
        composite_names.extend(a["AlarmName"] for a in page.get("CompositeAlarms", []))

    # Remove any pending one-shot recheck schedule (instance may terminate before it fires).
    _delete_schedule(instance_id)

    total = len(metric_names) + len(composite_names)
    if not total:
        print(f"No alarms to delete for {instance_id}.")
        return

    # Composites first: AWS rejects deleting a metric alarm still referenced by a
    # composite's AlarmRule (or as its ActionsSuppressor). DeleteAlarms also allows only
    # ONE composite alarm per request, so delete composites individually; metric alarms
    # can be batched up to 100 per call.
    for name in composite_names:
        cloudwatch.delete_alarms(AlarmNames=[name])
    for i in range(0, len(metric_names), 100):
        cloudwatch.delete_alarms(AlarmNames=metric_names[i : i + 100])
    print(f"Deleted {total} alarms for {instance_id} ({len(composite_names)} composite).")


# --------------------------------------------------------------------------- #
# Alarm builders
# --------------------------------------------------------------------------- #
def _put_cpu_alarms(instance_id, tags):
    dims = [{"Name": "InstanceId", "Value": instance_id}]
    # Child symptom alarms carry NO notify action; the per-instance composite alarms own
    # notification (see _put_composite_alarms). This is what tames the alarm flood.
    for severity in ("Warning", "Critical"):
        threshold = _threshold(tags, "CPUUtilization", severity, DEFAULTS[f"cpu_{severity.lower()}"])
        _put_alarm(
            name=_alarm_name(instance_id, "CPUUtilization", severity),
            namespace="AWS/EC2",
            metric="CPUUtilization",
            dimensions=dims,
            threshold=threshold,
            period=PERIOD,
            eval_periods=EVAL_PERIODS,
            treat_missing="missing",
            actions=[],
            description=f"{severity}: CPU on {instance_id}",
        )


def _put_mem_alarms(instance_id, tags, existing, instance_os):
    dims = [{"Name": "InstanceId", "Value": instance_id}]
    if instance_os == "windows":
        metric, label, defkey = WIN_MEM_METRIC, "mem_committed_percent", "win_mem"
        # The Windows Memory counter is published with InstanceId + objectname=Memory; the
        # alarm MUST watch the real dimensions or it sees no data and (breaching) stays ALARM.
        dims = _metric_dims(instance_id, metric) or dims
    else:
        metric, label, defkey = "mem_used_percent", "mem_used_percent", "mem"
    for severity in ("Warning", "Critical"):
        name = _alarm_name(instance_id, label, severity)
        if name in existing:
            continue
        threshold = _threshold(tags, label, severity, DEFAULTS[f"{defkey}_{severity.lower()}"])
        _put_alarm(
            name=name,
            namespace="CWAgent",
            metric=metric,
            dimensions=dims,
            threshold=threshold,
            period=PERIOD,
            eval_periods=EVAL_PERIODS,
            treat_missing="breaching",
            actions=[],
            description=f"{severity}: memory on {instance_id}",
        )


def _put_disk_alarms_from(instance_id, tags, metrics, existing, instance_os):
    windows = instance_os == "windows"
    for m in metrics:
        dims = m["Dimensions"]
        by_name = {d["Name"]: d["Value"] for d in dims}

        if windows:
            # Windows reports per-drive FREE %; alarm when free space drops LOW.
            drive = by_name.get("instance", "")
            if not drive or drive == "_Total":
                continue
            fs_label = re.sub(r"[^A-Za-z0-9]+", "_", drive).strip("_") or "drive"
            metric_name, override_token = WIN_DISK_METRIC, "disk_free_percent"
            defkey, comparison = "win_disk_free", "LessThanOrEqualToThreshold"
        else:
            if by_name.get("fstype") in IGNORE_DISK_FSTYPES:
                print(f"  skipping {by_name.get('path')} (fstype {by_name.get('fstype')})")
                continue
            fs_label = _filesystem_label(dims)
            metric_name, override_token = "disk_used_percent", "disk_used_percent"
            defkey, comparison = "disk", "GreaterThanOrEqualToThreshold"

        for severity in ("Warning", "Critical"):
            name = _alarm_name(instance_id, f"{override_token}-{fs_label}", severity)
            if name in existing:
                continue
            threshold = _threshold(
                tags, override_token, severity, DEFAULTS[f"{defkey}_{severity.lower()}"]
            )
            _put_alarm(
                name=name,
                namespace="CWAgent",
                metric=metric_name,
                dimensions=dims,
                threshold=threshold,
                period=PERIOD,
                eval_periods=EVAL_PERIODS,
                treat_missing="breaching",
                actions=[],
                description=f"{severity}: disk {fs_label} on {instance_id}",
                comparison=comparison,
            )


def _put_status_check_alarms(instance_id):
    dims = [{"Name": "InstanceId", "Value": instance_id}]

    # Alert-only: status checks notify the Critical topic (-> enrichment email) but take NO
    # EC2 auto-recover/auto-reboot action. treat_missing="missing" so a still-initializing
    # instance (no status metric yet) sits in INSUFFICIENT_DATA rather than ALARM.
    for metric in ("StatusCheckFailed_System", "StatusCheckFailed_Instance"):
        kind = "system" if metric.endswith("System") else "instance"
        _put_alarm(
            name=_alarm_name(instance_id, metric, "Critical"),
            namespace="AWS/EC2",
            metric=metric,
            dimensions=dims,
            threshold=1,
            period=STATUS_PERIOD,
            eval_periods=STATUS_EVAL_PERIODS,
            treat_missing="missing",
            statistic="Maximum",
            actions=[CRITICAL_TOPIC_ARN],
            description=f"Critical: {kind} status check on {instance_id}",
        )


def _put_composite_alarms(instance_id):
    """Roll the per-metric child alarms into one Warning + one Critical composite.

    Rebuilt (idempotently) whenever children change. Status-check alarms are excluded
    from the symptom rule: they keep their own notify action as the root-cause "instance
    down" alert, and the System alarm doubles as the Critical composite's ActionsSuppressor.
    """
    if not ENABLE_COMPOSITE_ALARMS:
        return

    prefix = f"{ALARM_PREFIX}-{instance_id}-"
    children = {"Warning": [], "Critical": []}
    paginator = cloudwatch.get_paginator("describe_alarms")
    for page in paginator.paginate(AlarmNamePrefix=prefix, AlarmTypes=["MetricAlarm"]):
        for alarm in page.get("MetricAlarms", []):
            name = alarm["AlarmName"]
            if "StatusCheckFailed_" in name:
                continue
            if name.endswith("-Critical"):
                children["Critical"].append(name)
            elif name.endswith("-Warning"):
                children["Warning"].append(name)

    system_alarm = _alarm_name(instance_id, "StatusCheckFailed_System", "Critical")
    for severity, topic in (("Warning", WARNING_TOPIC_ARN), ("Critical", CRITICAL_TOPIC_ARN)):
        names = sorted(children[severity])
        if not names:
            continue  # an empty AlarmRule is invalid
        composite_name = f"{ALARM_PREFIX}-{instance_id}-Composite-{severity}"
        rule = " OR ".join(f'ALARM("{n}")' for n in names)
        kwargs = {
            "AlarmName": composite_name,
            "AlarmDescription": f"{severity}: any symptom alarm on {instance_id}",
            "AlarmRule": rule,
            "AlarmActions": [topic],
            "OKActions": [topic],
            "ActionsEnabled": True,
        }
        if severity == "Critical":
            # Mute the symptom flood while the instance is DOWN; the System status alarm
            # sends the single root-cause alert instead.
            kwargs["ActionsSuppressor"] = system_alarm
            kwargs["ActionsSuppressorWaitPeriod"] = COMPOSITE_SUPPRESSOR_WAIT
            kwargs["ActionsSuppressorExtensionPeriod"] = COMPOSITE_SUPPRESSOR_WAIT
        cloudwatch.put_composite_alarm(**kwargs)
        print(f"  put composite: {composite_name} ({len(names)} children)")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _put_alarm(
    name,
    namespace,
    metric,
    dimensions,
    threshold,
    period,
    eval_periods,
    treat_missing,
    actions,
    description,
    statistic="Average",
    comparison="GreaterThanOrEqualToThreshold",
):
    cloudwatch.put_metric_alarm(
        AlarmName=name,
        AlarmDescription=description,
        Namespace=namespace,
        MetricName=metric,
        Dimensions=dimensions,
        Statistic=statistic,
        Period=period,
        EvaluationPeriods=eval_periods,
        DatapointsToAlarm=eval_periods,
        Threshold=threshold,
        ComparisonOperator=comparison,
        TreatMissingData=treat_missing,
        AlarmActions=actions,
        OKActions=actions,
        ActionsEnabled=True,
    )
    print(f"  put alarm: {name} (>= {threshold})")


def _existing_alarm_names(prefix):
    names = set()
    paginator = cloudwatch.get_paginator("describe_alarms")
    for page in paginator.paginate(AlarmNamePrefix=prefix, AlarmTypes=["MetricAlarm"]):
        names.update(a["AlarmName"] for a in page.get("MetricAlarms", []))
    return names


def _discover_disk_metrics(instance_id, metric_name):
    """Poll ListMetrics until the agent has reported disk metrics (or timeout)."""
    waited = 0
    while True:
        metrics = _list_disk_metrics(instance_id, metric_name)
        if metrics or waited >= DISK_DISCOVERY_MAX_WAIT:
            return metrics
        print(f"  no disk metrics yet; waiting {DISK_DISCOVERY_INTERVAL}s ({waited}/{DISK_DISCOVERY_MAX_WAIT})")
        time.sleep(DISK_DISCOVERY_INTERVAL)
        waited += DISK_DISCOVERY_INTERVAL


def _list_disk_metrics(instance_id, metric_name):
    metrics = []
    paginator = cloudwatch.get_paginator("list_metrics")
    for page in paginator.paginate(
        Namespace="CWAgent",
        MetricName=metric_name,
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
    ):
        metrics.extend(page.get("Metrics", []))
    return metrics


def _metric_dims(instance_id, metric_name):
    """Actual published dimensions for a CWAgent metric (Windows counters add objectname).
    Returns None if the agent hasn't reported it yet."""
    resp = cloudwatch.list_metrics(
        Namespace="CWAgent",
        MetricName=metric_name,
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
    )
    metrics = resp.get("Metrics", [])
    return metrics[0]["Dimensions"] if metrics else None


def _filesystem_label(dimensions):
    """Build a short, alarm-name-safe label from the disk metric's dimensions."""
    by_name = {d["Name"]: d["Value"] for d in dimensions}
    raw = by_name.get("path") or by_name.get("device") or "fs"
    label = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_")
    return label or "root"


def _alarm_name(instance_id, metric, severity):
    return f"{ALARM_PREFIX}-{instance_id}-{metric}-{severity}"


def _instance_tags(instance_id):
    try:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        reservations = resp.get("Reservations", [])
        if not reservations:
            return {}
        instance = reservations[0]["Instances"][0]
        return {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
    except Exception as exc:  # instance may already be gone on fast terminate
        print(f"Could not read tags for {instance_id}: {exc}")
        return {}


def _instance_os(instance_id):
    """'windows' for Windows instances, else 'linux'. EC2 sets Platform='windows' only for Windows."""
    try:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        inst = resp["Reservations"][0]["Instances"][0]
    except Exception as exc:
        print(f"Could not read platform for {instance_id}: {exc}; assuming linux.")
        return "linux"
    return "windows" if inst.get("Platform") == "windows" else "linux"


def _threshold(tags, metric, severity, default):
    """
    Per-instance override via tag named {ALARM_PREFIX}-{metric}-{severity}.
    Example tag: AutoAlarm-disk_used_percent-Critical = "85"
    """
    key = f"{ALARM_PREFIX}-{metric}-{severity}"
    if key in tags:
        try:
            return float(tags[key])
        except ValueError:
            print(f"  invalid override {key}={tags[key]!r}; using default {default}")
    return default
