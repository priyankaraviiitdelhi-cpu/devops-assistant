
"""
infra_agent.py - turns a plain-English request into Terraform, checks it, and runs it.

Flow:
  request -> LLM writes Terraform (following team policies from RAG)
          -> terraform init + validate (the LLM gets one chance to fix errors)
          -> guardrails check (allowed resources + per-service limits)
          -> terraform plan
          -> a human clicks Approve in the UI -> terraform apply
"""
import json
import os
import re
import secrets
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from llm import chat
from rag import search

WORKSPACE_DIR = Path("terraform_workspace")
PLUGIN_CACHE = Path.home() / ".terraform.d" / "plugin-cache"

# ---------------- Guardrails (these match data/docs/team_policies.md) ----------------
ALLOWED_REGION = "ap-south-1"
ALLOWED_INSTANCE_TYPES = {"t3.micro", "t3.small", "t4g.micro", "t4g.small"}
ALLOWED_DB_CLASSES = {"db.t3.micro", "db.t4g.micro"}
MAX_DB_STORAGE_GB = 20
MAX_LAMBDA_MEMORY_MB = 512
ROLE_PREFIX = "devops-assistant-"
ALLOWED_ROLE_POLICIES = {"arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"}
BOUNDARY_POLICY_NAME = "devops-assistant-boundary"
ALLOWED_ROLE_ACTION_PREFIXES = ("logs:", "dynamodb:", "s3:")


# Resource types allowed in normal mode (exact names)
ALLOWED_RESOURCE_TYPES = {
    "aws_instance", "aws_security_group",
    "aws_vpc_security_group_ingress_rule", "aws_vpc_security_group_egress_rule",
    "aws_db_instance", "aws_db_subnet_group",
    "aws_lambda_function", "aws_lambda_permission", "aws_cloudwatch_log_group",
    "aws_dynamodb_table",
    "aws_cloudfront_distribution", "aws_cloudfront_origin_access_control",
    "aws_lb", "aws_lb_listener", "aws_lb_target_group", "aws_lb_target_group_attachment",
    "aws_iam_role", "aws_iam_role_policy", "aws_iam_role_policy_attachment",
}
# Families allowed by prefix (the resource and all its settings resources)
ALLOWED_RESOURCE_PREFIXES = ("aws_s3_bucket", "aws_apigatewayv2_")

# Expensive or complex resources: only allowed when Expert mode is on
EXPERT_ONLY_PREFIXES = ("aws_nat_gateway", "aws_eip", "aws_eks_", "aws_vpc", "aws_subnet",
                        "aws_route", "aws_internet_gateway")

# Shown as warnings (not blocking) because they cost money even when idle
COST_WARNINGS = {
    "aws_db_instance": "RDS runs 24/7: a micro instance is about $15-25/month.",
    "aws_lb": "A load balancer costs about $16+/month even with no traffic.",
    "aws_cloudfront_distribution": "CloudFront is billed per request and per GB transferred.",
    "aws_nat_gateway": "A NAT Gateway costs about $32+/month plus data charges.",
    "aws_eks_cluster": "An EKS control plane costs about $73/month before any nodes.",
}

TERRAFORM_PROMPT = """You are a Terraform code generator for AWS.
Write one complete, working Terraform file (main.tf) for the user's request.

Rules:
- Reply with ONLY one ```hcl code block. No explanations.
- Include a terraform block with required_providers: aws = { source = "hashicorp/aws", version = "~> 6.0" }.
  If you use data "archive_file", also add archive = { source = "hashicorp/archive" }.
- Use one provider "aws" block with region = "ap-south-1" and default_tags:
  Project = "devops-assistant", Owner = "priyanka", Environment = "dev", ManagedBy = "terraform".
- Allowed resources: S3 (aws_s3_bucket*), EC2 (aws_instance), security groups,
  RDS (aws_db_instance, aws_db_subnet_group), Lambda (aws_lambda_function, aws_lambda_permission,
  aws_cloudwatch_log_group), API Gateway HTTP APIs (aws_apigatewayv2_*), DynamoDB (aws_dynamodb_table),
  CloudFront (aws_cloudfront_distribution, aws_cloudfront_origin_access_control),
  load balancers (aws_lb, aws_lb_listener, aws_lb_target_group, aws_lb_target_group_attachment),
    IAM (only aws_iam_role, aws_iam_role_policy and aws_iam_role_policy_attachment). Data sources are allowed.
- Limits:
  * EC2 instance_type: t3.micro, t3.small, t4g.micro or t4g.small.
  * RDS: instance_class db.t3.micro or db.t4g.micro, allocated_storage <= 20, multi_az = false,
    publicly_accessible = false, skip_final_snapshot = true. Use manage_master_user_password = true.
  * Lambda: memory_size <= 512. For code, zip a small inline handler with data "archive_file".
  * DynamoDB: billing_mode = "PAY_PER_REQUEST".
    * IAM role names must start with "devops-assistant-" and every role MUST set
    permissions_boundary = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/devops-assistant-boundary"
    (add data "aws_caller_identity" "current" {}).
  * The only managed policy you may attach is arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole.
  * To let Lambda use DynamoDB or S3, add ONE aws_iam_role_policy written with jsonencode, using only
    logs:, dynamodb: and s3: actions (never "*" or iam:), scoped to this project's table and bucket ARNs.
  * Name DynamoDB tables and S3 buckets starting with "devops-assistant-".
  * Never open SSH (port 22) to 0.0.0.0/0. Never put credentials in the code.
- S3 bucket names must be globally unique: end every bucket name with -{suffix}
- Use the default VPC (data "aws_vpc" with default = true) unless expert mode allows otherwise.
- Add outputs for the important values.
- Follow the team policies below.
{expert_rules}
- If the request needs something that is NOT allowed, do NOT substitute something else.
  Reply with exactly one line: NOT_ALLOWED: <short reason>

TEAM POLICIES AND DOCS:
{context}
"""

EXPERT_RULES_ON = """- EXPERT MODE IS ON: you may also use NAT gateways, Elastic IPs, EKS, custom VPCs, subnets,
  route tables and internet gateways, and larger EC2/RDS sizes if the request needs them."""
EXPERT_RULES_OFF = "- Expert mode is OFF: do not use NAT gateways, EKS, Elastic IPs or custom VPCs."


# ---------------- Helpers ----------------

def _run_tf(workdir, *args):
    """Run a terraform command inside workdir. Returns (success, output text)."""
    env = {**os.environ, "TF_PLUGIN_CACHE_DIR": str(PLUGIN_CACHE), "TF_IN_AUTOMATION": "1"}
    result = subprocess.run(
        ["terraform", *args, "-no-color"],
        cwd=workdir, capture_output=True, text=True, env=env,
    )
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def _extract_hcl(text):
    """Pull the code out of the ```hcl ... ``` block in the LLM reply."""
    match = re.search(r"```(?:hcl|terraform)?\s*\n(.*?)```", text, re.DOTALL)
    return (match.group(1) if match else text).strip() + "\n"


def _resource_blocks(code):
    """Split Terraform code into (type, name, body) for each resource block (approximate)."""
    matches = list(re.finditer(r'resource\s+"([^"]+)"\s+"([^"]+)"', code))
    blocks = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(code)
        blocks.append((m.group(1), m.group(2), code[m.end():end]))
    return blocks


def _attr(body, name):
    """Read a simple attribute value like  name = "value"  or  name = 20  from a block."""
    match = re.search(rf'^\s*{name}\s*=\s*"?([^"\n]+?)"?\s*$', body, re.MULTILINE)
    return match.group(1).strip() if match else None


def _is_allowed_type(rtype):
    return rtype in ALLOWED_RESOURCE_TYPES or rtype.startswith(ALLOWED_RESOURCE_PREFIXES)


# ---------------- Guardrails ----------------

def check_guardrails(code, expert=False):
    """Return a list of policy violations. An empty list means the code is allowed."""
    issues = []

    for rtype, rname, body in _resource_blocks(code):
        label = f"{rtype}.{rname}"
        if _is_allowed_type(rtype):
            pass
        elif rtype.startswith(EXPERT_ONLY_PREFIXES):
            if not expert:
                issues.append(f"'{rtype}' is expensive/complex and only allowed in Expert mode.")
            continue
        else:
            issues.append(f"Resource type '{rtype}' is not allowed.")
            continue

        if rtype == "aws_db_instance":
            db_class = _attr(body, "instance_class")
            if db_class not in ALLOWED_DB_CLASSES and not expert:
                issues.append(f"{label}: instance_class '{db_class}' is not allowed. "
                              f"Use {' or '.join(sorted(ALLOWED_DB_CLASSES))}.")
            storage = _attr(body, "allocated_storage")
            if storage and storage.isdigit() and int(storage) > MAX_DB_STORAGE_GB and not expert:
                issues.append(f"{label}: allocated_storage {storage} GB is over the {MAX_DB_STORAGE_GB} GB limit.")
            if _attr(body, "multi_az") == "true" and not expert:
                issues.append(f"{label}: multi_az is only allowed in Expert mode.")
            if _attr(body, "publicly_accessible") == "true":
                issues.append(f"{label}: databases must never be publicly accessible.")

        if rtype == "aws_lambda_function":
            memory = _attr(body, "memory_size")
            if memory and memory.isdigit() and int(memory) > MAX_LAMBDA_MEMORY_MB and not expert:
                issues.append(f"{label}: memory_size {memory} MB is over the {MAX_LAMBDA_MEMORY_MB} MB limit.")

        if rtype == "aws_dynamodb_table" and _attr(body, "billing_mode") != "PAY_PER_REQUEST":
            issues.append(f"{label}: DynamoDB tables must use billing_mode = \"PAY_PER_REQUEST\".")

        if rtype == "aws_iam_role":
            role_name = _attr(body, "name")
            if not role_name or not role_name.startswith(ROLE_PREFIX):
                issues.append(f"{label}: IAM role names must start with '{ROLE_PREFIX}'.")
            boundary = _attr(body, "permissions_boundary")
            if not boundary or BOUNDARY_POLICY_NAME not in boundary:
                issues.append(f"{label}: IAM roles must have the {BOUNDARY_POLICY_NAME} permissions boundary.")
        if rtype == "aws_iam_role_policy_attachment":
            policy_arn = _attr(body, "policy_arn")
            if policy_arn not in ALLOWED_ROLE_POLICIES:
                issues.append(f"{label}: only AWSLambdaBasicExecutionRole may be attached to roles.")

    if any(rtype == "aws_iam_role_policy" for rtype, _, _ in _resource_blocks(code)):
        actions = []
        for group in re.findall(r'[Aa]ctions?"?\s*[=:]\s*(\[[^\]]*\]|"[^"]*")', code):
            actions += re.findall(r'"([^"]+)"', group)
        if not actions:
            issues.append("Could not read the actions in the inline role policy.")
        for action in actions:
            if action == "*" or not action.startswith(ALLOWED_ROLE_ACTION_PREFIXES):
                issues.append(f"Role policy action '{action}' is not allowed. Only logs:, dynamodb: and s3: actions.")
    if not expert:
        for itype in re.findall(r'instance_type\s*=\s*"([^"]+)"', code):
            if itype not in ALLOWED_INSTANCE_TYPES:
                allowed = ", ".join(sorted(ALLOWED_INSTANCE_TYPES))
                issues.append(f"Instance type '{itype}' is not allowed. Allowed: {allowed}.")

    regions = re.findall(r'\bregion\s*=\s*"([^"]+)"', code)
    if not regions:
        issues.append("No region is set in the provider block.")
    for region in regions:
        if region != ALLOWED_REGION:
            issues.append(f"Region '{region}' is not allowed. Use {ALLOWED_REGION}.")

    if re.search(r'\b(access_key|secret_key)\s*=', code):
        issues.append("Credentials must never be written in Terraform code.")
    if "0.0.0.0/0" in code and re.search(r'\b(from_port|to_port)\s*=\s*22\b', code):
        issues.append("SSH (port 22) must not be open to 0.0.0.0/0.")
    return issues


def get_cost_warnings(code):
    """Non-blocking warnings for resources that cost money even when idle."""
    types = {rtype for rtype, _, _ in _resource_blocks(code)}
    return [msg for rtype, msg in COST_WARNINGS.items() if rtype in types]


# ---------------- Main flow ----------------

def prepare(request, expert=False, duration_days=None, max_attempts=2):
    """Write Terraform for the request, validate it, check guardrails and run plan."""
    PLUGIN_CACHE.mkdir(parents=True, exist_ok=True)
    name = f"stack-{datetime.now():%Y%m%d-%H%M%S}"
    workdir = WORKSPACE_DIR / name
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "request.txt").write_text(request, encoding="utf-8")
    if duration_days:
        expires = datetime.now() + timedelta(days=float(duration_days))
        (workdir / "expires.txt").write_text(expires.strftime("%Y-%m-%d %H:%M"), encoding="utf-8")
    docs = search(request + " tags naming region policy limits", k=4)
    context = "\n\n".join(f"[{d['source']}]\n{d['text']}" for d in docs)
    system = (TERRAFORM_PROMPT
              .replace("{suffix}", secrets.token_hex(3))
              .replace("{expert_rules}", EXPERT_RULES_ON if expert else EXPERT_RULES_OFF)
              .replace("{context}", context))

    messages = [{"role": "user", "content": request}]
    ok, output, code = False, "", ""
    for _ in range(max_attempts):
        reply = chat(messages, system=system)
        if reply.strip().startswith("NOT_ALLOWED"):
            shutil.rmtree(workdir, ignore_errors=True)
            return {"name": name, "workdir": str(workdir), "request": request, "code": "",
                    "issues": [reply.strip()], "warnings": [], "plan_ok": False, "plan": "",
                    "summary": "Refused: this request needs resources outside the team policy."}
        code = _extract_hcl(reply)
        (workdir / "main.tf").write_text(code, encoding="utf-8")
        _run_tf(workdir, "fmt")
        code = (workdir / "main.tf").read_text(encoding="utf-8")

        ok, output = _run_tf(workdir, "init", "-input=false")
        if ok:
            ok, output = _run_tf(workdir, "validate")
        if ok:
            break
        messages += [
            {"role": "assistant", "content": f"```hcl\n{code}```"},
            {"role": "user", "content": f"terraform failed with this error:\n{output}\n\nReturn the full corrected main.tf."},
        ]

    result = {"name": name, "workdir": str(workdir), "request": request, "code": code,
              "issues": check_guardrails(code, expert=expert), "warnings": get_cost_warnings(code)}
    if not ok:
        result.update(plan_ok=False, plan=output, summary="Terraform validation failed.")
    elif result["issues"]:
        result.update(plan_ok=False, plan="", summary="Blocked by guardrails. No plan was run.")
    else:
        ok, output = _run_tf(workdir, "plan", "-input=false", "-out=tfplan")
        match = re.search(r"Plan: .*", output)
        summary = match.group(0) if match else ("No changes." if ok else "terraform plan failed.")
        result.update(plan_ok=ok, plan=output, summary=summary)
    return result


def apply(workdir):
    """Apply the saved plan (only called after a human clicks Approve)."""
    return _run_tf(workdir, "apply", "-input=false", "tfplan")


def discard(workdir):
    """Throw away a plan that was rejected (nothing was created)."""
    shutil.rmtree(workdir, ignore_errors=True)


def destroy(workdir):
    """Delete everything a stack created, then remove its folder."""
    ok, output = _run_tf(workdir, "destroy", "-input=false", "-auto-approve")
    if ok:
        shutil.rmtree(workdir, ignore_errors=True)
    return ok, output


def list_stacks():
    """Return the stacks that currently have real resources in AWS."""
    stacks = []
    if not WORKSPACE_DIR.exists():
        return stacks
    for d in sorted(WORKSPACE_DIR.iterdir(), reverse=True):
        state_file = d / "terraform.tfstate"
        if not state_file.exists():
            continue
        try:
            resources = json.loads(state_file.read_text()).get("resources", [])
        except Exception:
            resources = []
        managed = [r for r in resources if r.get("mode") == "managed"]
        if managed:
            request_file = d / "request.txt"
            stacks.append({
                "name": d.name,
                "workdir": str(d),
                "request": request_file.read_text() if request_file.exists() else "",
                "expires": (d / "expires.txt").read_text() if (d / "expires.txt").exists() else None,
                "resources": [f"{r['type']}.{r['name']}" for r in managed],
            })
    return stacks