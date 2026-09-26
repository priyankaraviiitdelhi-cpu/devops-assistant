# AWS Cost Optimization Guide

## Compute (EC2)
- Idle instances: an instance with average CPU below 5% for 14 days is probably idle. Stop or terminate it.
- Rightsizing: if CPU and memory stay below 40%, move to a smaller instance type.
- Graviton: t4g, m7g and other ARM-based types usually cost less than the equivalent x86 types.
- Schedules: stop dev and test instances outside working hours.
- Spot instances can be much cheaper for workloads that can handle interruptions, like batch jobs and CI runners.

## Storage (EBS and S3)
- Unattached EBS volumes still cost money every month. Delete them or snapshot and delete.
- Old EBS snapshots add up. Delete snapshots older than your retention period.
- gp3 volumes are about 20% cheaper per GB than gp2 and let you set IOPS separately. Migrate gp2 to gp3.
- S3 lifecycle rules can move old objects to S3 Standard-IA or Glacier, or delete them.
- S3 Intelligent-Tiering moves objects between tiers automatically when access patterns are unknown.

## Network
- Unused Elastic IPs and public IPv4 addresses are charged hourly. Release what you do not need.
- NAT Gateways charge per hour and per GB processed. Use VPC endpoints for S3 and DynamoDB traffic.
- Data transfer out to the internet and across regions is charged. Keep services in the same region.

## Monitoring and logs
- Set a retention period on CloudWatch log groups. The default is to keep logs forever.
- Avoid high-resolution custom metrics unless you really need them.

## Pricing models
- Savings Plans and Reserved Instances give large discounts for a 1- or 3-year commitment on steady workloads.
- Only commit after a few weeks of stable usage data.

## Process
- Tag every resource with Project, Owner and Environment so costs can be grouped in Cost Explorer.
- Set AWS Budgets alerts.
- Review Cost Explorer and AWS Trusted Advisor or Compute Optimizer recommendations every week.