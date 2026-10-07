"""asc-kit: find Auto Scaling groups without application health checks, check prerequisites,
show per-instance status, and wait out the managed ENIs left after a check is deleted."""
from __future__ import annotations

import argparse
import sys
import time

import boto3

from . import core


def _table(rows: list[dict]) -> str:
    if not rows:
        return "(none)"
    cols = list(rows[0])
    width = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    line = lambda r: "  ".join(str(r[c]).ljust(width[c]) for c in cols)  # noqa: E731
    return "\n".join([line({c: c for c in cols}), *(line(r) for r in rows)])


def cmd_scan(session, args) -> int:
    rows = [
        {"AutoScalingGroup": c.name, "HealthCheckType": c.health_check_type,
         "LoadBalancer": "yes" if c.has_load_balancer else "no",
         "AppChecks": ",".join(c.check_ids) or "-", "Verdict": c.verdict}
        for c in core.scan(session.client("autoscaling"), session.client("ec2"))
    ]
    print(_table(rows))
    missing = [r for r in rows if r["Verdict"] == "NO_APP_CHECK"]
    if missing:
        print(f"\n{len(missing)} group(s) replace instances only on EC2 status checks: a hung app stays in service.")
    return 0


def cmd_preflight(session, args) -> int:
    results = core.preflight(session.client("ec2"), session.client("autoscaling"),
                             args.asg, args.port, args.source_sg)
    print(_table([{"SecurityGroup": r.security_group_id, "OK": "yes" if r.ok else "NO", "Detail": r.reason}
                  for r in results]))
    return 0 if any(r.ok for r in results) else 1


def cmd_status(session, args) -> int:
    ec2 = session.client("ec2")
    ids = args.instance_ids
    if args.asg:
        g = session.client("autoscaling").describe_auto_scaling_groups(
            AutoScalingGroupNames=[args.asg])["AutoScalingGroups"]
        ids = [i["InstanceId"] for i in (g[0]["Instances"] if g else [])]
    if not ids:
        print("No instances.", file=sys.stderr)
        return 1
    print(_table(core.instance_status(ec2, ids)))
    return 0


def cmd_wait_eni(session, args) -> int:
    start = time.strftime("%H:%M:%S")

    def report(n, waited):
        print(f"[{time.strftime('%H:%M:%S')}] managed ENIs left: {n} (waited {waited / 60:.0f} min)", flush=True)

    ok, waited = core.wait_for_eni_release(session.client("ec2"), args.vpc_id, args.timeout * 60,
                                           args.interval, on_poll=report)
    if ok:
        print(f"Released. Started {start}, waited {waited / 60:.0f} min. Security groups and subnets can be deleted now.")
        return 0
    print(f"Timed out after {waited / 60:.0f} min. Run again later; the ENIs are owned by AWS and cannot be detached.")
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="asc-kit", description=__doc__)
    p.add_argument("--profile")
    p.add_argument("--region")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="list Auto Scaling groups and whether an application status check covers them")

    pf = sub.add_parser("preflight", help="check that instance security groups allow the health check port from the VPC")
    pf.add_argument("--asg", required=True)
    pf.add_argument("--port", type=int, default=80)
    pf.add_argument("--source-sg", help="security group of the check's health check path, if you set one")

    st = sub.add_parser("status", help="per-instance application status")
    g = st.add_mutually_exclusive_group(required=True)
    g.add_argument("--asg")
    g.add_argument("--instance-ids", nargs="+")

    we = sub.add_parser("wait-eni", help="wait until managed ENIs are released after deleting a check")
    we.add_argument("--vpc-id", required=True)
    we.add_argument("--timeout", type=int, default=360, help="minutes (default 360)")
    we.add_argument("--interval", type=int, default=300, help="seconds between polls (default 300)")
    return p


COMMANDS = {"scan": cmd_scan, "preflight": cmd_preflight, "status": cmd_status, "wait-eni": cmd_wait_eni}


def main(argv=None, session_factory=boto3.Session) -> int:
    args = build_parser().parse_args(argv)
    session = session_factory(profile_name=args.profile, region_name=args.region)
    try:
        return COMMANDS[args.command](session, args)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
