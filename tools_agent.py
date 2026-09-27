"""
tools_agent.py - lets the AI use live AWS "tools" (function calling).

Gemini reads the question, decides which Python function(s) to call,
the SDK runs them (boto3 -> real AWS data), and Gemini answers from the results.

Safety:
- Read-only tools (list / describe / cost) run automatically.
- Tools that CHANGE AWS never run directly. They only create a "pending action"
  that the user must confirm with a button in the app.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import boto3
from dotenv import load_dotenv

import pricing

load_dotenv()
REGION = "ap-south-1"
MODEL = os.getenv("LLM_MODEL", "gemini-3.8-flash")

_session = boto3.Session(region_name=REGION)
_pending = []  # actions waiting for the user's confirmation


def _name(tags):
    return {t["Key"]: t["Value"] for t in (tags or [])}.get("Name", "")


# ---------------- Read-only tools ----------------

def list_ec2_instances() -> dict:
    """List all EC2 instances in the account with id, name, type, state and launch time."""
    try:
        ec2 = _session.client("ec2")
        instances = []
        for r in ec2.describe_instances()["Reservations"]:
            for i in r["Instances"]:
                if i["State"]["Name"] == "terminated":
                    continue
                instances.append({
                    "id": i["InstanceId"], "name": _name(i.get("Tags")),
                    "type": i["InstanceType"], "state": i["State"]["Name"],
                    "launched": i["LaunchTime"].isoformat(),
                })
        return {"region": REGION, "count": len(instances), "instances": instances}
    except Exception as e:
        return {"error": str(e)}


def get_instance_cpu(instance_id: str) -> dict:
    """Get the average and maximum CPU utilization (%) of one EC2 instance over the last 24 hours."""
    try:
        cw = _session.client("cloudwatch")
        now = datetime.now(timezone.utc)
        points = cw.get_metric_statistics(
            Namespace="AWS/EC2", MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=now - timedelta(hours=24), EndTime=now,
            Period=3600, Statistics=["Average", "Maximum"],
        )["Datapoints"]
        if not points:
            return {"instance_id": instance_id,
                    "message": "No CPU data in the last 24 hours (the instance may be new or stopped)."}
        return {"instance_id": instance_id,
                "avg_cpu_percent": round(sum(p["Average"] for p in points) / len(points), 2),
                "max_cpu_percent": round(max(p["Maximum"] for p in points), 2)}
    except Exception as e:
        return {"error": str(e)}


def list_ebs_volumes() -> dict:
    """List EBS volumes with id, size in GB, type, state and the instance they are attached to."""
    try:
        ec2 = _session.client("ec2")
        volumes = [{
            "id": v["VolumeId"], "size_gb": v["Size"], "type": v["VolumeType"], "state": v["State"],
            "attached_to": v["Attachments"][0]["InstanceId"] if v["Attachments"] else None,
        } for v in ec2.describe_volumes()["Volumes"]]
        return {"region": REGION, "count": len(volumes), "volumes": volumes}
    except Exception as e:
        return {"error": str(e)}


def list_s3_buckets() -> dict:
    """List all S3 buckets with their region and creation date."""
    try:
        s3 = _session.client("s3")
        buckets = []
        for b in s3.list_buckets()["Buckets"]:
            try:
                region = s3.get_bucket_location(Bucket=b["Name"])["LocationConstraint"] or "us-east-1"
            except Exception:
                region = "unknown"
            buckets.append({"name": b["Name"], "region": region, "created": b["CreationDate"].isoformat()})
        return {"count": len(buckets), "buckets": buckets}
    except Exception as e:
        return {"error": str(e)}


def get_month_to_date_spend() -> dict:
    """Get this month's AWS spend so far, broken down by service, from Cost Explorer."""
    try:
        ce = _session.client("ce", region_name="us-east-1")
        today = datetime.now(timezone.utc).date()
        start = today.replace(day=1)
        if start == today:  # on the 1st of the month, show last month instead
            start = (start - timedelta(days=1)).replace(day=1)
        result = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": today.isoformat()},
            Granularity="MONTHLY", Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        spend = {}
        for period in result["ResultsByTime"]:
            for g in period["Groups"]:
                spend[g["Keys"][0]] = spend.get(g["Keys"][0], 0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
        spend = {k: round(v, 2) for k, v in spend.items() if v >= 0.01}
        return {"from": start.isoformat(), "to": today.isoformat(),
                "total_usd": round(sum(spend.values()), 2), "by_service_usd": spend}
    except Exception as e:
        return {"error": str(e)}


# ---------------- Tool that needs confirmation ----------------

def request_instance_action(instance_id: str, action: str) -> dict:
    """Ask to START or STOP an EC2 instance. action must be "start" or "stop".
    This does NOT change anything by itself: it creates a request that the user
    must confirm with a button in the app."""
    action = action.lower().strip()
    if action not in ("start", "stop"):
        return {"error": "action must be 'start' or 'stop'."}
    try:
        ec2 = _session.client("ec2")
        inst = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    except Exception as e:
        return {"error": f"Could not find instance {instance_id}: {e}"}
    state = inst["State"]["Name"]
    if action == "stop" and state != "running":
        return {"error": f"Instance {instance_id} is {state}, not running."}
    if action == "start" and state != "stopped":
        return {"error": f"Instance {instance_id} is {state}, not stopped."}
    _pending.append({"kind": "instance", "instance_id": instance_id, "name": _name(inst.get("Tags")),
                     "action": action, "current_state": state})
    return {"status": "waiting_for_user_confirmation",
            "message": f"A request to {action} {instance_id} was created. The user must click Confirm in the app."}

# ---------------- Architect tools ----------------

def estimate_architecture_cost(resources_json: str, duration_days: float) -> dict:
    """Estimate the cost of a proposed architecture for a number of days.
    resources_json is a JSON list. Each item looks like:
    {"service": "ec2", "size": "t3.micro", "count": 1, "storage_gb": 8,
     "requests_per_month": 0, "gb_transfer_per_month": 0}
    service is one of: ec2, rds, s3, lambda, api_gateway, dynamodb, alb, cloudfront,
    nat_gateway, eks, security_group, iam_role."""
    try:
        return pricing.estimate(json.loads(resources_json), duration_days)
    except Exception as e:
        return {"error": f"Could not estimate cost: {e}"}


def propose_deployment(summary: str, terraform_request: str, estimated_cost_usd: float,
                       duration_days: float, budget_usd: float) -> dict:
    """Create a deployment request ONLY AFTER the user has clearly agreed to the proposed architecture.
    This does NOT create anything: the user must click Confirm, then review the Terraform plan and Approve.
    summary: one sentence describing the architecture.
    terraform_request: a precise description of every resource and its settings, for the Terraform generator.
    budget_usd: the user's budget for the whole duration, or 0 if they did not give one."""
    if budget_usd and estimated_cost_usd > budget_usd:
        return {"error": f"Estimated ${estimated_cost_usd:.2f} is over the budget of ${budget_usd:.2f}. "
                         "Suggest a cheaper design instead."}
    _pending.append({"kind": "provision", "summary": summary, "request": terraform_request,
                     "estimated_cost_usd": round(estimated_cost_usd, 2), "duration_days": duration_days})
    return {"status": "waiting_for_user_confirmation",
            "message": "Tell the user to click Confirm below to generate the Terraform plan."}


def execute_action(action):
    """Actually start/stop the instance. Only called after the user clicks Confirm."""
    ec2 = _session.client("ec2")
    if action["action"] == "stop":
        ec2.stop_instances(InstanceIds=[action["instance_id"]])
        return f"Stopping instance {action['instance_id']}. It takes about a minute."
    ec2.start_instances(InstanceIds=[action["instance_id"]])
    return f"Starting instance {action['instance_id']}. It takes about a minute."


# ---------------- The agent ----------------

TOOLS = [list_ec2_instances, get_instance_cpu, list_ebs_volumes, list_s3_buckets,
         get_month_to_date_spend, request_instance_action,
         estimate_architecture_cost, propose_deployment]

SYSTEM_PROMPT = """You are an AWS DevOps assistant and cloud architect for the devops-assistant project (region ap-south-1).

LIVE ACCOUNT QUESTIONS
- Use the tools to answer questions about the user's live AWS account. Never guess account data.
- To start or stop an instance, call request_instance_action, then tell the user to click Confirm below.

ARCHITECT MODE - when the user describes a project, app or scenario:
1. If key facts are missing, ask at most 3 short questions in ONE message: expected users/traffic,
   how long they need it (duration), budget, and data size. Do not ask what they already told you.
2. Propose the simplest low-cost architecture as a table: Resource | Purpose | Size.
   Prefer serverless and small sizes (S3, Lambda, API Gateway, DynamoDB, t3.micro, db.t3.micro).
   Only use allowed services: S3, EC2, security groups, RDS, Lambda, API Gateway, DynamoDB,
   CloudFront, load balancers, IAM roles. NAT gateways, EKS and custom VPCs need Expert mode - say so.
3. Call estimate_architecture_cost with the resources and the duration. Show a table
   Resource | Assumption | Cost, then the total for the whole duration. Say it is an estimate.
   If it is over their budget, propose a cheaper option.
4. Ask: "Do you want me to create this, or change something?"
5. ONLY after the user clearly agrees (for example "yes, create it"), call propose_deployment with a precise
   terraform_request, then tell them to click Confirm below. Never call propose_deployment before they agree.

GENERAL
- For general DevOps questions that need no account data, answer normally without tools.
- If a tool returns an error, explain it in simple words. Keep answers short and friendly."""

def run(messages, extra_context=None):
    """Answer the conversation using tools. Returns (reply, tools_used, pending_actions)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    contents = [
        types.Content(role="user" if m["role"] == "user" else "model",
                      parts=[types.Part(text=m["content"])])
        for m in messages
    ]
    system = SYSTEM_PROMPT
    if extra_context:
        system += "\n\nTeam knowledge base excerpts (use if relevant):\n" + extra_context
    config = types.GenerateContentConfig(system_instruction=system, tools=TOOLS)

    for attempt in range(4):  # retry if Google's servers are busy
        _pending.clear()
        try:
            response = client.models.generate_content(model=MODEL, contents=contents, config=config)
            break
        except Exception as e:
            if ("503" not in str(e) and "429" not in str(e)) or attempt == 3:
                raise
            time.sleep(2 ** attempt * 3)

    tools_used = []
    for content in (response.automatic_function_calling_history or []):
        for part in (content.parts or []):
            if getattr(part, "function_call", None):
                tools_used.append(part.function_call.name)
    return response.text or "", tools_used, list(_pending)