"""
infra_agent.py - turns a plain-English request into Terraform, checks it, and runs it.

Flow:
  request -> LLM writes Terraform (following team policies from RAG)
          -> terraform init + validate (the LLM gets one chance to fix errors)
          -> guardrails check (blocked resources, instance types, regions, ...)
          -> terraform plan
          -> a human clicks Approve in the UI -> terraform apply
"""
import json
import os
import re
import secrets
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from llm import chat
from rag import search

WORKSPACE_DIR = Path("terraform_workspace")
PLUGIN_CACHE = Path.home() / ".terraform.d" / "plugin-cache"

# ---- Guardrails (these match data/docs/team_policies.md) ----
ALLOWED_REGION = "ap-south-1"
ALLOWED_INSTANCE_TYPES = {"t3.micro", "t3.small", "t4g.micro", "t4g.small"}
ALLOWED_RESOURCE_PREFIXES = (
    "aws_s3_bucket",  # the bucket and its settings (versioning, encryption, lifecycle, ...)
    "aws_instance",
    "aws_security_group",
    "aws_vpc_security_group_ingress_rule",
    "aws_vpc_security_group_egress_rule",
)

TERRAFORM_PROMPT = """You are a Terraform code generator for AWS.
Write one complete, working Terraform file (main.tf) for the user's request.

Rules:
- Reply with ONLY one ```hcl code block. No explanations.
- Include a terraform block with required_providers: aws = { source = "hashicorp/aws", version = "~> 6.0" }.
- Use one provider "aws" block with region = "ap-south-1" and default_tags:
  Project = "devops-assistant", Owner = "priyanka", Environment = "dev", ManagedBy = "terraform".
- Only use these resource types: aws_s3_bucket and its aws_s3_bucket_* settings resources,
  aws_instance, aws_security_group, aws_vpc_security_group_ingress_rule, aws_vpc_security_group_egress_rule.
  Data sources (for example data "aws_ami") are allowed.
- EC2 instance_type must be one of: t3.micro, t3.small, t4g.micro, t4g.small.
- Never open SSH (port 22) to 0.0.0.0/0.
- S3 bucket names must be globally unique: end every bucket name with -{suffix}
- Never put credentials in the code.
- Add outputs for the important values (bucket name, instance id, ...).
- Follow the team policies below.
- If the request needs a resource type that is NOT in the allowed list, do NOT substitute something else. Reply with exactly one line: NOT_ALLOWED: <short reason>

TEAM POLICIES AND DOCS:
{context}
"""


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
    match = re.search(r"```(?:hcl|terraform)?\s*\n(.*?)```", text, re.S)
    return (match.group(1) if match else text).strip() + "\n"


def check_guardrails(code):
    """Return a list of policy violations. An empty list means the code is allowed."""
    issues = []
    for rtype in re.findall(r'resource\s+"([^"]+)"', code):
        if not rtype.startswith(ALLOWED_RESOURCE_PREFIXES):
            issues.append(f"Resource type '{rtype}' is not allowed.")
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


def prepare(request, max_attempts=2):
    """Write Terraform for the request, validate it, check guardrails and run plan."""
    PLUGIN_CACHE.mkdir(parents=True, exist_ok=True)
    name = f"stack-{datetime.now():%Y%m%d-%H%M%S}"
    workdir = WORKSPACE_DIR / name
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "request.txt").write_text(request, encoding="utf-8")

    # RAG: pull the team policies that matter for this request
    docs = search(request + " tags naming region policy", k=4)
    context = "\n\n".join(f"[{d['source']}]\n{d['text']}" for d in docs)
    system = TERRAFORM_PROMPT.replace("{suffix}", secrets.token_hex(3)).replace("{context}", context)

    messages = [{"role": "user", "content": request}]
    ok, output, code = False, "", ""
    for _ in range(max_attempts):
        
        reply = chat(messages, system=system)
        if reply.strip().startswith("NOT_ALLOWED"):
            shutil.rmtree(workdir, ignore_errors=True)
            return {"name": name, "workdir": str(workdir), "request": request, "code": "",
                    "issues": [reply.strip()], "plan_ok": False, "plan": "",
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
        # Self-correction: show the LLM its error and ask for a fixed version
        messages += [
            {"role": "assistant", "content": f"```hcl\n{code}```"},
            {"role": "user", "content": f"terraform failed with this error:\n{output}\n\nReturn the full corrected main.tf."},
        ]

    result = {"name": name, "workdir": str(workdir), "request": request,
              "code": code, "issues": check_guardrails(code)}
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
                "resources": [f"{r['type']}.{r['name']}" for r in managed],
            })
    return stacks