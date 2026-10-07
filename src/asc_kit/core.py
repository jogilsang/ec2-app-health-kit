"""Read-mostly helpers around EC2 application status checks.

Every function takes boto3 clients as arguments so it can be tested with mocks.
"""
from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass, field

ASG_TAG = "aws:autoscaling:groupName"
MANAGED_ENI_PRINCIPAL = "ec2.application-status-checks.amazonaws.com"
MANAGED_ENI_DESCRIPTION_PREFIX = "ec2-asc-eni"


def _paginate(method, key, **kwargs):
    token = None
    while True:
        page = method(**kwargs, **({"NextToken": token} if token else {}))
        yield from page.get(key, [])
        token = page.get("NextToken")
        if not token:
            return


@dataclass
class AsgCoverage:
    name: str
    health_check_type: str
    has_load_balancer: bool
    check_ids: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.check_ids:
            return "COVERED"
        if self.has_load_balancer and self.health_check_type == "ELB":
            return "ELB_ONLY"  # app health already drives replacement through the load balancer
        return "NO_APP_CHECK"  # only EC2 status checks: a hung app is never replaced


def scan(autoscaling, ec2) -> list[AsgCoverage]:
    """Report, per Auto Scaling group, which application status checks watch its instances.

    Coverage is read from the instances themselves (describe-application-status), not from the
    check's TargetTagAssociations: in our tests describe-application-status-checks returned no
    associations even right after a successful AssociateApplicationStatusCheck.
    """
    groups = list(_paginate(autoscaling.describe_auto_scaling_groups, "AutoScalingGroups"))
    instance_to_group = {i["InstanceId"]: g["AutoScalingGroupName"] for g in groups for i in g.get("Instances", [])}
    checks_by_group: dict[str, set[str]] = {}
    ids = list(instance_to_group)
    for start in range(0, len(ids), 100):
        resp = ec2.describe_application_status(InstanceIds=ids[start:start + 100])
        for inst in resp["ApplicationStatuses"]["Instances"]:
            group = instance_to_group.get(inst["InstanceId"])
            for d in inst.get("ApplicationStatus", {}).get("Details", []) or []:
                checks_by_group.setdefault(group, set()).add(d["ApplicationStatusCheckId"])
    return [
        AsgCoverage(
            name=g["AutoScalingGroupName"],
            health_check_type=g.get("HealthCheckType", "EC2"),
            has_load_balancer=bool(g.get("TargetGroupARNs") or g.get("LoadBalancerNames")),
            check_ids=sorted(checks_by_group.get(g["AutoScalingGroupName"], ())),
        )
        for g in groups
    ]


@dataclass
class PreflightResult:
    security_group_id: str
    ok: bool
    reason: str


def _rule_allows(rule: dict, port: int, source_cidrs: list[str], source_sg: str | None) -> str | None:
    proto = rule.get("IpProtocol")
    if proto not in ("tcp", "-1"):
        return None
    if proto == "tcp" and not (rule.get("FromPort", 0) <= port <= rule.get("ToPort", 65535)):
        return None
    for r in rule.get("IpRanges", []):
        net = ipaddress.ip_network(r["CidrIp"], strict=False)
        if all(ipaddress.ip_network(s).subnet_of(net) for s in source_cidrs):
            return f"{r['CidrIp']} covers {', '.join(source_cidrs)}"
    for p in rule.get("UserIdGroupPairs", []):
        if source_sg and p.get("GroupId") == source_sg:
            return f"source security group {source_sg}"
    return None


def preflight(ec2, autoscaling, asg_name: str, port: int, source_sg: str | None = None) -> list[PreflightResult]:
    """Check that the group's instances accept the health check port from inside the VPC.

    The check is sent from an AWS-managed ENI in your VPC, so a security group that only
    allows the load balancer or a bastion will report every instance as impaired.
    """
    groups = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])["AutoScalingGroups"]
    if not groups:
        raise ValueError(f"Auto Scaling group not found: {asg_name}")
    ids = [i["InstanceId"] for i in groups[0].get("Instances", [])]
    if not ids:
        raise ValueError(f"{asg_name} has no instances to inspect")
    reservations = ec2.describe_instances(InstanceIds=ids)["Reservations"]
    instances = [i for r in reservations for i in r["Instances"]]
    vpc_ids = {i["VpcId"] for i in instances}
    vpc_cidrs = [
        a["CidrBlock"]
        for v in ec2.describe_vpcs(VpcIds=sorted(vpc_ids))["Vpcs"]
        for a in v.get("CidrBlockAssociationSet", [{"CidrBlock": v["CidrBlock"]}])
    ]
    sg_ids = sorted({g["GroupId"] for i in instances for g in i.get("SecurityGroups", [])})
    out = []
    for sg in ec2.describe_security_groups(GroupIds=sg_ids)["SecurityGroups"]:
        reason = next(
            (r for r in (_rule_allows(rule, port, vpc_cidrs, source_sg) for rule in sg.get("IpPermissions", [])) if r),
            None,
        )
        out.append(PreflightResult(sg["GroupId"], reason is not None,
                                   reason or f"no inbound tcp/{port} from VPC {', '.join(vpc_cidrs)}"))
    return out


def instance_status(ec2, instance_ids: list[str]) -> list[dict]:
    # Response nests the list under ApplicationStatuses.Instances (not the shape in the doc examples).
    rows = []
    for inst in ec2.describe_application_status(InstanceIds=instance_ids)["ApplicationStatuses"]["Instances"]:
        status = inst.get("ApplicationStatus", {})
        for d in status.get("Details", []) or [{}]:
            reason = d.get("Reason", {})
            rows.append({
                "InstanceId": inst["InstanceId"],
                "Status": status.get("Status", "-"),
                "CheckId": d.get("ApplicationStatusCheckId", "-"),
                "CheckStatus": d.get("Status", "-"),
                "Reason": reason.get("Code", "-"),
                "HttpCode": reason.get("StatusCode", "-"),
            })
    return rows


def managed_enis(ec2, vpc_id: str) -> list[dict]:
    """ENIs that application status checks create in the VPC. Hidden unless IncludeManagedResources."""
    enis = _paginate(
        ec2.describe_network_interfaces, "NetworkInterfaces",
        IncludeManagedResources=True, Filters=[{"Name": "vpc-id", "Values": [vpc_id]}],
    )
    return [
        e for e in enis
        if e.get("Operator", {}).get("Principal") == MANAGED_ENI_PRINCIPAL
        or e.get("Description", "").startswith(MANAGED_ENI_DESCRIPTION_PREFIX)
    ]


def wait_for_eni_release(ec2, vpc_id: str, timeout_s: int, interval_s: int = 300,
                         sleep=time.sleep, now=time.monotonic, on_poll=None) -> tuple[bool, float]:
    """Poll until no managed ENI is left in the VPC. Returns (released, seconds_waited).

    Deleting a check does not free its ENIs right away (measured: about 3h50m), and while they
    exist the security group and subnets cannot be deleted. They are owned by AWS, so waiting is
    the only option.
    """
    start = now()
    while True:
        left = managed_enis(ec2, vpc_id)
        waited = now() - start
        if on_poll:
            on_poll(len(left), waited)
        if not left:
            return True, waited
        if waited + interval_s > timeout_s:
            return False, waited
        sleep(interval_s)
