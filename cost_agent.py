"""
cost_agent.py - scans an AWS account for waste and estimates monthly savings.

All checks are READ-ONLY: this file never changes or deletes anything in AWS.
Prices are approximate on-demand prices for ap-south-1 (Mumbai), used only for estimates.
"""
import json
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from llm import chat
from rag import search

REGION = "ap-south-1"
REQUIRED_TAGS = {"Project", "Owner", "Environment"}
HOURS_PER_MONTH = 730
IDLE_CPU_PERCENT = 5
SNAPSHOT_MAX_AGE_DAYS = 90

# Approximate prices in USD - good enough for estimates, not for billing
EC2_HOURLY = {"t3.micro": 0.0112, "t3.small": 0.0224, "t3.medium": 0.0448, "t3.large": 0.0896,
              "t4g.micro": 0.0084, "t4g.small": 0.0168, "m5.large": 0.101, "c5.large": 0.085}
EBS_GB_MONTH = {"gp2": 0.114, "gp3": 0.0912, "io1": 0.131, "io2": 0.131,
                "st1": 0.051, "sc1": 0.0174, "standard": 0.08}
SNAPSHOT_GB_MONTH = 0.05
PUBLIC_IP_MONTH = 0.005 * HOURS_PER_MONTH  # about $3.65


def _finding(category, resource, detail, action, savings=0.0):
    return {"category": category, "resource": resource, "detail": detail,
            "action": action, "monthly_savings": round(savings, 2)}


def _tags(resource):
    return {t["Key"]: t["Value"] for t in resource.get("Tags", [])}


# ---------------- Individual checks ----------------

def check_ec2(ec2, cloudwatch):
    """Idle running instances (low CPU for 14 days) and stopped instances."""
    findings = []
    now = datetime.now(timezone.utc)
    reservations = ec2.describe_instances(
        Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped"]}]
    )["Reservations"]
    for r in reservations:
        for inst in r["Instances"]:
            iid, itype, state = inst["InstanceId"], inst["InstanceType"], inst["State"]["Name"]
            name = _tags(inst).get("Name", iid)
            if state == "stopped":
                findings.append(_finding(
                    "Stopped instance", name,
                    f"{itype} is stopped, but its EBS volumes are still billed.",
                    "Terminate it if it is no longer needed (snapshot first)."))
                continue
            datapoints = cloudwatch.get_metric_statistics(
                Namespace="AWS/EC2", MetricName="CPUUtilization",
                Dimensions=[{"Name": "InstanceId", "Value": iid}],
                StartTime=now - timedelta(days=14), EndTime=now,
                Period=86400, Statistics=["Average"],
            )["Datapoints"]
            if not datapoints:
                continue
            avg_cpu = sum(d["Average"] for d in datapoints) / len(datapoints)
            if avg_cpu < IDLE_CPU_PERCENT:
                cost = EC2_HOURLY.get(itype, 0.05) * HOURS_PER_MONTH
                findings.append(_finding(
                    "Idle EC2 instance", name,
                    f"{itype}, average CPU {avg_cpu:.1f}% over the last 14 days.",
                    "Stop or terminate it, or move to a smaller instance type.", cost))
    return findings


def check_ebs(ec2):
    """Unattached volumes and gp2 volumes that should be gp3."""
    findings = []
    for vol in ec2.describe_volumes()["Volumes"]:
        vid, size, vtype = vol["VolumeId"], vol["Size"], vol["VolumeType"]
        name = _tags(vol).get("Name", vid)
        if vol["State"] == "available":
            findings.append(_finding(
                "Unattached EBS volume", name,
                f"{size} GB {vtype} volume is not attached to any instance.",
                "Snapshot it if needed, then delete it.",
                size * EBS_GB_MONTH.get(vtype, 0.1)))
        elif vtype == "gp2":
            findings.append(_finding(
                "gp2 volume", name,
                f"{size} GB gp2 volume. gp3 is about 20% cheaper per GB.",
                "Change the volume type to gp3 (no downtime needed).",
                size * (EBS_GB_MONTH["gp2"] - EBS_GB_MONTH["gp3"])))
    return findings


def check_snapshots(ec2):
    """Snapshots older than the retention period."""
    findings = []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=SNAPSHOT_MAX_AGE_DAYS)
    for snap in ec2.describe_snapshots(OwnerIds=["self"])["Snapshots"]:
        if snap["StartTime"] < cutoff:
            age = (now - snap["StartTime"]).days
            findings.append(_finding(
                "Old snapshot", snap["SnapshotId"],
                f"{snap['VolumeSize']} GB snapshot, {age} days old.",
                "Delete it if it is past your retention period.",
                snap["VolumeSize"] * SNAPSHOT_GB_MONTH))
    return findings


def check_elastic_ips(ec2):
    """Elastic IPs that are not attached to anything."""
    return [
        _finding("Unused Elastic IP", a.get("PublicIp", a.get("AllocationId")),
                 "Elastic IP is not attached to anything, but it is billed every hour.",
                 "Release it.", PUBLIC_IP_MONTH)
        for a in ec2.describe_addresses()["Addresses"]
        if "AssociationId" not in a
    ]


def check_s3(s3):
    """Buckets that have no lifecycle rule (objects are kept forever)."""
    findings = []
    for bucket in s3.list_buckets()["Buckets"]:
        name = bucket["Name"]
        try:
            s3.get_bucket_lifecycle_configuration(Bucket=name)
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchLifecycleConfiguration":
                findings.append(_finding(
                    "S3 bucket without lifecycle rule", name,
                    "Objects stay in S3 Standard forever.",
                    "Add a lifecycle rule to move old objects to cheaper storage or delete them."))
    return findings


def check_tags(ec2):
    """EC2 instances and EBS volumes missing the required tags."""
    findings = []
    resources = [(i["InstanceId"], i)
                 for r in ec2.describe_instances()["Reservations"]
                 for i in r["Instances"] if i["State"]["Name"] != "terminated"]
    resources += [(v["VolumeId"], v) for v in ec2.describe_volumes()["Volumes"]]
    for rid, res in resources:
        missing = REQUIRED_TAGS - set(_tags(res))
        if missing:
            findings.append(_finding(
                "Missing tags", rid,
                "Missing required tags: " + ", ".join(sorted(missing)),
                "Add the tags so costs can be tracked by project and owner."))
    return findings


def get_spend(ce, days=30):
    """Spend per AWS service for the last `days` days (from Cost Explorer)."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    result = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY", Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    spend = {}
    for period in result["ResultsByTime"]:
        for group in period["Groups"]:
            service = group["Keys"][0]
            spend[service] = spend.get(service, 0) + float(group["Metrics"]["UnblendedCost"]["Amount"])
    return {k: round(v, 2) for k, v in sorted(spend.items(), key=lambda x: -x[1]) if v >= 0.01}


# ---------------- Full scans ----------------

def scan(region=REGION):
    """Run every check against the real AWS account (read-only)."""
    session = boto3.Session(region_name=region)
    ec2 = session.client("ec2")
    cloudwatch = session.client("cloudwatch")
    s3 = session.client("s3")
    ce = session.client("ce", region_name="us-east-1")  # Cost Explorer lives in us-east-1

    checks = [
        ("EC2 instances", lambda: check_ec2(ec2, cloudwatch)),
        ("EBS volumes", lambda: check_ebs(ec2)),
        ("Snapshots", lambda: check_snapshots(ec2)),
        ("Elastic IPs", lambda: check_elastic_ips(ec2)),
        ("S3 buckets", lambda: check_s3(s3)),
        ("Tags", lambda: check_tags(ec2)),
    ]
    findings, errors = [], []
    for label, run_check in checks:
        try:
            findings += run_check()
        except Exception as e:
            errors.append(f"{label} check failed: {e}")
    try:
        spend = get_spend(ce)
    except Exception as e:
        spend = {}
        errors.append(f"Cost Explorer: {e}")

    return {"mode": "live", "region": region, "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "findings": findings, "spend": spend, "errors": errors}


def demo_scan():
    """Realistic sample data for demos (your new account has almost nothing running)."""
    findings = [
        _finding("Idle EC2 instance", "old-jenkins-server", "m5.large, average CPU 1.8% over the last 14 days.",
                 "Stop or terminate it, or move to a smaller instance type.", EC2_HOURLY["m5.large"] * HOURS_PER_MONTH),
        _finding("Idle EC2 instance", "test-api-dev", "t3.medium, average CPU 3.2% over the last 14 days.",
                 "Stop or terminate it, or move to a smaller instance type.", EC2_HOURLY["t3.medium"] * HOURS_PER_MONTH),
        _finding("Unattached EBS volume", "vol-0a1b2c3d4e5f60001", "200 GB gp2 volume is not attached to any instance.",
                 "Snapshot it if needed, then delete it.", 200 * EBS_GB_MONTH["gp2"]),
        _finding("Unattached EBS volume", "vol-0a1b2c3d4e5f60002", "50 GB gp3 volume is not attached to any instance.",
                 "Snapshot it if needed, then delete it.", 50 * EBS_GB_MONTH["gp3"]),
        _finding("gp2 volume", "prod-db-data", "500 GB gp2 volume. gp3 is about 20% cheaper per GB.",
                 "Change the volume type to gp3 (no downtime needed).", 500 * (EBS_GB_MONTH["gp2"] - EBS_GB_MONTH["gp3"])),
        _finding("Old snapshot", "snap-0f9e8d7c6b5a40001", "100 GB snapshot, 412 days old.",
                 "Delete it if it is past your retention period.", 100 * SNAPSHOT_GB_MONTH),
        _finding("Old snapshot", "snap-0f9e8d7c6b5a40002", "100 GB snapshot, 380 days old.",
                 "Delete it if it is past your retention period.", 100 * SNAPSHOT_GB_MONTH),
        _finding("Unused Elastic IP", "13.233.10.25", "Elastic IP is not attached to anything, but it is billed every hour.",
                 "Release it.", PUBLIC_IP_MONTH),
        _finding("Unused Elastic IP", "65.0.120.44", "Elastic IP is not attached to anything, but it is billed every hour.",
                 "Release it.", PUBLIC_IP_MONTH),
        _finding("Stopped instance", "ml-experiment-box", "g4dn.xlarge is stopped, but its EBS volumes are still billed.",
                 "Terminate it if it is no longer needed (snapshot first)."),
        _finding("S3 bucket without lifecycle rule", "app-logs-archive-2024", "Objects stay in S3 Standard forever.",
                 "Add a lifecycle rule to move old objects to cheaper storage or delete them."),
        _finding("Missing tags", "i-0abc123def4567890", "Missing required tags: Environment, Owner",
                 "Add the tags so costs can be tracked by project and owner."),
    ]
    spend = {"Amazon Elastic Compute Cloud - Compute": 142.60, "EC2 - Other": 61.35,
             "Amazon Relational Database Service": 48.20, "Amazon Simple Storage Service": 18.75,
             "Amazon Virtual Private Cloud": 11.40, "AmazonCloudWatch": 6.10}
    return {"mode": "demo", "region": REGION, "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "findings": findings, "spend": spend, "errors": []}


# ---------------- AI report ----------------

COST_PROMPT = """You are a FinOps (cloud cost optimization) expert.
You receive the results of an automated AWS cost scan as JSON.
Write a short report in Markdown with these sections:
## Summary - 2-3 sentences, including the total estimated monthly savings.
## Top 3 actions - biggest savings first, with the exact resource names and $/month.
## Other findings - a short bullet list.
## Before you act - risks and checks (snapshot before deleting, confirm with the owner, etc.).
Use only the data given. Say that savings are estimates. Keep it under 350 words.

Relevant best practices from the team knowledge base:
{context}
"""


def summarize(result):
    """Ask the LLM to turn scan results into a readable report."""
    total = sum(f["monthly_savings"] for f in result["findings"])
    docs = search("reduce AWS costs idle EC2 EBS snapshots Elastic IP lifecycle", k=3)
    context = "\n\n".join(d["text"] for d in docs)
    payload = {
        "total_estimated_monthly_savings_usd": round(total, 2),
        "spend_last_30_days_usd": result["spend"],
        "findings": result["findings"],
    }
    system = COST_PROMPT.replace("{context}", context)
    return chat([{"role": "user", "content": json.dumps(payload, indent=2)}], system=system)