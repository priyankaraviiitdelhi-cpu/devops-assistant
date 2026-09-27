"""
pricing.py - rough cost estimates for an architecture over a given duration.

Prices are APPROXIMATE on-demand prices for ap-south-1 (Mumbai) in USD.
Free tier, credits and taxes are not included.
"""
HOURS_PER_MONTH = 730

EC2_HOURLY = {"t3.micro": 0.0112, "t3.small": 0.0224, "t3.medium": 0.0448, "t3.large": 0.0896,
              "t4g.micro": 0.0084, "t4g.small": 0.0168, "t4g.medium": 0.0336,
              "m5.large": 0.101, "c5.large": 0.085}
RDS_HOURLY = {"db.t3.micro": 0.021, "db.t4g.micro": 0.019, "db.t3.small": 0.042,
              "db.t4g.small": 0.038, "db.m5.large": 0.19}
EBS_GP3_GB_MONTH = 0.0912
RDS_STORAGE_GB_MONTH = 0.131
PUBLIC_IPV4_HOURLY = 0.005
S3_GB_MONTH = 0.025
S3_PUT_PER_1000 = 0.005
LAMBDA_PER_REQUEST = 0.20 / 1_000_000 + 0.128 * 0.2 * 0.0000166667  # 128 MB, ~200 ms
API_GW_PER_MILLION = 1.00
DYNAMO_PER_MILLION = 0.625
DYNAMO_GB_MONTH = 0.25
ALB_HOURLY = 0.0239 + 0.008  # load balancer hour + about 1 LCU
CLOUDFRONT_PER_GB = 0.109
CLOUDFRONT_PER_10K = 0.009
NAT_HOURLY = 0.056
NAT_PER_GB = 0.056
EKS_HOURLY = 0.10
FREE_SERVICES = {"security_group", "iam_role", "vpc", "subnet", "cloudwatch_logs"}


def _item_cost(item, hours, months):
    """Return (cost_usd, assumption_text) for one resource."""
    service = str(item.get("service", "")).lower()
    size = item.get("size") or ""
    count = max(int(item.get("count") or 1), 1)
    storage = float(item.get("storage_gb") or 0)
    requests = float(item.get("requests_per_month") or 0)
    gb_out = float(item.get("gb_transfer_per_month") or 0)

    if service in FREE_SERVICES:
        return 0.0, "free"
    if service == "ec2":
        size = size or "t3.micro"
        disk = storage or 8
        cost = count * (EC2_HOURLY.get(size, 0.05) * hours + PUBLIC_IPV4_HOURLY * hours
                        + disk * EBS_GP3_GB_MONTH * months)
        return cost, f"{size} running 24/7, {disk:g} GB gp3 disk, public IPv4"
    if service == "rds":
        size = size or "db.t3.micro"
        disk = storage or 20
        cost = count * (RDS_HOURLY.get(size, 0.05) * hours + disk * RDS_STORAGE_GB_MONTH * months)
        return cost, f"{size} running 24/7, {disk:g} GB storage, single-AZ"
    if service == "s3":
        cost = storage * S3_GB_MONTH * months + requests * months / 1000 * S3_PUT_PER_1000
        return cost, f"{storage:g} GB stored, {requests:,.0f} uploads/month"
    if service == "lambda":
        return requests * months * LAMBDA_PER_REQUEST, f"{requests:,.0f} requests/month, 128 MB, ~200 ms"
    if service == "api_gateway":
        return requests * months / 1e6 * API_GW_PER_MILLION, f"HTTP API, {requests:,.0f} requests/month"
    if service == "dynamodb":
        cost = requests * months / 1e6 * DYNAMO_PER_MILLION + storage * DYNAMO_GB_MONTH * months
        return cost, f"on-demand, {requests:,.0f} requests/month, {storage:g} GB"
    if service == "alb":
        return count * ALB_HOURLY * hours, "runs 24/7, about 1 LCU of traffic"
    if service == "cloudfront":
        cost = gb_out * months * CLOUDFRONT_PER_GB + requests * months / 10_000 * CLOUDFRONT_PER_10K
        return cost, f"{gb_out:g} GB/month delivered, {requests:,.0f} requests/month"
    if service == "nat_gateway":
        return count * NAT_HOURLY * hours + gb_out * months * NAT_PER_GB, "runs 24/7 plus data processed"
    if service == "eks":
        return count * EKS_HOURLY * hours, "control plane only - worker nodes are extra EC2"
    return 0.0, f"No price data for '{service}' - check the AWS Pricing Calculator."


def estimate(resources, duration_days):
    """Estimate the total cost of a list of resources for duration_days."""
    hours = float(duration_days) * 24
    months = hours / HOURS_PER_MONTH
    items, total = [], 0.0
    for r in resources:
        cost, note = _item_cost(r, hours, months)
        items.append({"service": r.get("service"), "size": r.get("size", ""), "count": r.get("count", 1),
                      "estimated_cost_usd": round(cost, 2), "assumption": note})
        total += cost
    return {"duration_days": duration_days, "items": items, "total_usd": round(total, 2),
            "note": "Approximate on-demand prices for ap-south-1. Free tier, credits and taxes are not included."}