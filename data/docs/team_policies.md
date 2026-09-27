# Team Cloud Policies (devops-assistant project)

These are the rules every engineer on this project must follow when creating AWS resources.

## Regions
- Default region: ap-south-1 (Mumbai).
- us-east-1 is used only for billing, budgets and other global services.
- Do not create resources in any other region.

## Allowed EC2 instance types
- Development: t3.micro, t3.small, t4g.micro, t4g.small.
- Anything larger (for example m5.large or c5.xlarge) needs approval from the project owner.
- GPU instances are never allowed.

## Required tags
Every resource must have these tags:
- Project = devops-assistant
- Owner = the engineer's name
- Environment = dev, staging or prod
- ManagedBy = terraform (or manual if created by hand)

Resources without these tags will be flagged in the cost report.

## Naming convention
Use the pattern <project>-<environment>-<resource>, for example devops-assistant-dev-logs-bucket.

## S3 rules
- Block all public access on every bucket.
- Enable default encryption (SSE-S3).
- Enable versioning on prod buckets.
- Log buckets need a lifecycle rule: move objects to cheaper storage after 30 days and delete them after 90 days.

## Cost rules
- The monthly budget alert is set at $5.
- Stop dev EC2 instances when they are not in use.
- Destroy test resources at the end of the day with terraform destroy.
- Delete unattached EBS volumes and release unused Elastic IPs.

## Security rules
- Never commit access keys, API keys or .env files to Git.
- Give IAM users and roles only the permissions they need.
- Turn on MFA for the root account and never use root for daily work.


## Allowed services and limits
- RDS: only db.t3.micro or db.t4g.micro, at most 20 GB storage, no Multi-AZ, never publicly accessible.
- Lambda: memory at most 512 MB. API Gateway HTTP APIs are allowed.
- DynamoDB: on-demand billing (PAY_PER_REQUEST) only.
- CloudFront and Application Load Balancers are allowed but cost money even when idle.
- IAM: only roles named devops-assistant-*, always with the devops-assistant-boundary permissions boundary. Only AWSLambdaBasicExecutionRole may be attached. Inline role policies may only use logs:, dynamodb: and s3: actions on devops-assistant-* resources.
- NAT Gateways, EKS, Elastic IPs, custom VPCs and larger instance sizes need Expert mode.