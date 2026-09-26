# Terraform Basics and Best Practices

## Workflow
1. terraform init - downloads providers and sets up the working folder.
2. terraform plan - shows what will be created, changed or destroyed. Always read it.
3. terraform apply - makes the changes after you type yes.
4. terraform destroy - deletes everything this configuration created.

## State
- Terraform keeps track of real resources in a state file (terraform.tfstate).
- Never commit state files to Git. They can contain secrets.
- For teams, store state remotely in an S3 bucket. Terraform 1.11+ can lock state with use_lockfile = true; older setups use a DynamoDB table for locking.

## Good practices
- Pin provider versions in a required_providers block.
- Use variables for values that change between environments and outputs for values other tools need.
- Use default_tags in the AWS provider so every resource gets the required tags.
- Run terraform fmt and terraform validate before every plan.

## Example: private, encrypted S3 bucket with required tags

```hcl
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = "ap-south-1"
  default_tags {
    tags = {
      Project     = "devops-assistant"
      Owner       = "priyanka"
      Environment = "dev"
      ManagedBy   = "terraform"
    }
  }
}

resource "aws_s3_bucket" "logs" {
  bucket = "devops-assistant-dev-logs-bucket"
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
```