"""
Unit tests - no Gemini or AWS calls, so they are free and run in CI.
Run locally with:  pytest -v
"""
from cost_agent import demo_scan
from infra_agent import _extract_hcl, check_guardrails, get_cost_warnings
from rag import chunk_text

GOOD = '''
provider "aws" { region = "ap-south-1" }
resource "aws_s3_bucket" "logs" { bucket = "devops-assistant-dev-logs" }
resource "aws_instance" "web" { instance_type = "t3.micro" }
'''

RDS_OK = '''
resource "aws_db_instance" "db" {
  instance_class      = "db.t3.micro"
  allocated_storage   = 20
  multi_az            = false
  publicly_accessible = false
}
'''


# ---- Basic guardrails ----

def test_allowed_config_passes():
    assert check_guardrails(GOOD) == []


def test_blocks_disallowed_resource():
    issues = check_guardrails(GOOD + 'resource "aws_iam_user" "u" {}')
    assert any("aws_iam_user" in i for i in issues)


def test_blocks_large_instance_type():
    issues = check_guardrails(GOOD.replace("t3.micro", "m5.large"))
    assert any("m5.large" in i for i in issues)


def test_blocks_other_region():
    issues = check_guardrails(GOOD.replace("ap-south-1", "us-east-1"))
    assert any("us-east-1" in i for i in issues)


def test_requires_region():
    code = 'resource "aws_s3_bucket" "logs" { bucket = "x" }'
    assert any("No region" in i for i in check_guardrails(code))


def test_blocks_hardcoded_credentials():
    issues = check_guardrails(GOOD + 'provider "aws" { access_key = "AKIA..." region = "ap-south-1" }')
    assert any("Credentials" in i for i in issues)


def test_blocks_ssh_open_to_world():
    rule = '''
    resource "aws_vpc_security_group_ingress_rule" "ssh" {
      cidr_ipv4 = "0.0.0.0/0"
      from_port = 22
      to_port   = 22
    }'''
    assert any("SSH" in i for i in check_guardrails(GOOD + rule))


# ---- New service limits ----

def test_small_rds_is_allowed():
    assert check_guardrails(GOOD + RDS_OK) == []


def test_blocks_big_or_public_rds():
    bad = RDS_OK.replace("db.t3.micro", "db.m5.large").replace("= 20", "= 100") \
                .replace("publicly_accessible = false", "publicly_accessible = true")
    issues = check_guardrails(GOOD + bad)
    assert any("instance_class" in i for i in issues)
    assert any("allocated_storage" in i for i in issues)
    assert any("publicly accessible" in i for i in issues)


def test_dynamodb_must_be_on_demand():
    table = 'resource "aws_dynamodb_table" "t" {\n  billing_mode = "PROVISIONED"\n}'
    assert any("PAY_PER_REQUEST" in i for i in check_guardrails(GOOD + table))


def test_iam_role_name_and_policy_limits():
    role = 'resource "aws_iam_role" "r" {\n  name = "admin-role"\n}'
    attach = 'resource "aws_iam_role_policy_attachment" "a" {\n  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"\n}'
    issues = check_guardrails(GOOD + role + attach)
    assert any("must start with" in i for i in issues)
    assert any("AWSLambdaBasicExecutionRole" in i for i in issues)


ROLE_OK = '''
resource "aws_iam_role" "r" {
  name                 = "devops-assistant-dev-lambda"
  permissions_boundary = "arn:aws:iam::123:policy/devops-assistant-boundary"
}
'''


def test_role_needs_permissions_boundary():
    role = 'resource "aws_iam_role" "r" {\n  name = "devops-assistant-x"\n}'
    assert any("permissions boundary" in i for i in check_guardrails(GOOD + role))


def test_scoped_inline_policy_is_allowed():
    policy = ('resource "aws_iam_role_policy" "p" {\n'
              '  policy = jsonencode({ Statement = [{ Action = ["dynamodb:PutItem", "s3:PutObject"] }] })\n}')
    assert check_guardrails(GOOD + ROLE_OK + policy) == []


def test_dangerous_inline_policy_is_blocked():
    policy = ('resource "aws_iam_role_policy" "p" {\n'
              '  policy = jsonencode({ Statement = [{ Action = ["iam:*"] }] })\n}')
    assert any("iam:*" in i for i in check_guardrails(GOOD + ROLE_OK + policy))


def test_nat_gateway_needs_expert_mode():
    code = GOOD + 'resource "aws_nat_gateway" "nat" {}'
    assert any("Expert mode" in i for i in check_guardrails(code))
    assert check_guardrails(code, expert=True) == []


def test_cost_warnings_for_idle_cost_resources():
    warnings = get_cost_warnings(GOOD + RDS_OK + 'resource "aws_lb" "alb" {}')
    assert len(warnings) == 2


# ---- Helpers ----

def test_extract_hcl_from_llm_reply():
    reply = 'Here you go:\n```hcl\nresource "aws_s3_bucket" "b" {}\n```\nDone.'
    assert _extract_hcl(reply).strip() == 'resource "aws_s3_bucket" "b" {}'


def test_chunk_text_splits_long_documents():
    text = "\n\n".join(["word " * 50] * 10)
    chunks = chunk_text(text, max_chars=600)
    assert len(chunks) > 1
    assert all(len(c) <= 700 for c in chunks)


# ---- Cost engine ----

def test_demo_scan_total_savings():
    total = sum(f["monthly_savings"] for f in demo_scan()["findings"])
    assert round(total, 2) == 162.49




# ---- Pricing ----

def test_ec2_estimate_for_30_days():
    from pricing import estimate
    result = estimate([{"service": "ec2", "size": "t3.micro"}], 30)
    assert 11 < result["total_usd"] < 14


def test_free_and_unknown_services():
    from pricing import estimate
    result = estimate([{"service": "security_group"}, {"service": "quantum_computer"}], 7)
    assert result["total_usd"] == 0
    assert "No price data" in result["items"][1]["assumption"]