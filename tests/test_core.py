"""
Unit tests - no Gemini or AWS calls, so they are free and run in CI.
Run locally with:  pytest -v
"""
from cost_agent import demo_scan
from infra_agent import _extract_hcl, check_guardrails
from rag import chunk_text

GOOD = '''
provider "aws" { region = "ap-south-1" }
resource "aws_s3_bucket" "logs" { bucket = "devops-assistant-dev-logs" }
resource "aws_instance" "web" { instance_type = "t3.micro" }
'''


# ---- Guardrails ----

def test_allowed_config_passes():
    assert check_guardrails(GOOD) == []


def test_blocks_disallowed_resource():
    issues = check_guardrails(GOOD + 'resource "aws_db_instance" "db" {}')
    assert any("aws_db_instance" in i for i in issues)


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


# ---- Helpers ----

def test_extract_hcl_from_llm_reply():
    reply = 'Here you go:\n```hcl\nresource "aws_s3_bucket" "b" {}\n```\nDone.'
    assert _extract_hcl(reply).strip() == 'resource "aws_s3_bucket" "b" {}'


def test_chunk_text_splits_long_documents():
    text = "\n\n".join(["word " * 50] * 10)  # 10 paragraphs of ~250 characters
    chunks = chunk_text(text, max_chars=600)
    assert len(chunks) > 1
    assert all(len(c) <= 700 for c in chunks)


# ---- Cost engine ----

def test_demo_scan_total_savings():
    total = sum(f["monthly_savings"] for f in demo_scan()["findings"])
    assert round(total, 2) == 162.49