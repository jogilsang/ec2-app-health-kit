from unittest.mock import MagicMock

import pytest

from asc_kit import core


def _asg(name, instances=(), hc="EC2", tgs=()):
    return {"AutoScalingGroupName": name, "HealthCheckType": hc, "TargetGroupARNs": list(tgs),
            "LoadBalancerNames": [], "Instances": [{"InstanceId": i} for i in instances]}


def _status(iid, *check_ids):
    return {"InstanceId": iid, "ApplicationStatus": {"Status": "ok", "Details": [
        {"ApplicationStatusCheckId": c} for c in check_ids]}}


def test_scan_reads_coverage_from_instances():
    asg, ec2 = MagicMock(), MagicMock()
    asg.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": [
        _asg("web", ["i-1", "i-2"]), _asg("api", ["i-3"], hc="ELB", tgs=["tg"]), _asg("legacy", ["i-4"]),
        _asg("empty")]}
    ec2.describe_application_status.return_value = {"ApplicationStatuses": {"Instances": [
        _status("i-1", "asc-1"), _status("i-2", "asc-1", "asc-2"), {"InstanceId": "i-3"}, {"InstanceId": "i-4"}]}}
    verdicts = {c.name: (c.verdict, c.check_ids) for c in core.scan(asg, ec2)}
    assert verdicts == {"web": ("COVERED", ["asc-1", "asc-2"]), "api": ("ELB_ONLY", []),
                        "legacy": ("NO_APP_CHECK", []), "empty": ("NO_APP_CHECK", [])}


def test_scan_batches_instance_lookups_by_100():
    asg, ec2 = MagicMock(), MagicMock()
    asg.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": [_asg("big", [f"i-{n}" for n in range(250)])]}
    ec2.describe_application_status.return_value = {"ApplicationStatuses": {"Instances": []}}
    core.scan(asg, ec2)
    assert [len(c.kwargs["InstanceIds"]) for c in ec2.describe_application_status.call_args_list] == [100, 100, 50]


def _preflight_clients(rules):
    asg, ec2 = MagicMock(), MagicMock()
    asg.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": [{"Instances": [{"InstanceId": "i-1"}]}]}
    ec2.describe_instances.return_value = {"Reservations": [{"Instances": [
        {"InstanceId": "i-1", "VpcId": "vpc-1", "SecurityGroups": [{"GroupId": "sg-1"}]}]}]}
    ec2.describe_vpcs.return_value = {"Vpcs": [{"CidrBlock": "10.60.0.0/16"}]}
    ec2.describe_security_groups.return_value = {"SecurityGroups": [{"GroupId": "sg-1", "IpPermissions": rules}]}
    return asg, ec2


def test_preflight_passes_when_vpc_cidr_is_allowed():
    asg, ec2 = _preflight_clients([{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "10.0.0.0/8"}]}])
    [r] = core.preflight(ec2, asg, "web", 80)
    assert r.ok and "10.0.0.0/8" in r.reason


def test_preflight_fails_when_only_the_load_balancer_sg_is_allowed():
    asg, ec2 = _preflight_clients([{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
                                    "UserIdGroupPairs": [{"GroupId": "sg-alb"}]}])
    [r] = core.preflight(ec2, asg, "web", 80)
    assert not r.ok and "10.60.0.0/16" in r.reason


def test_preflight_unknown_group_raises():
    asg = MagicMock()
    asg.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": []}
    with pytest.raises(ValueError, match="not found"):
        core.preflight(MagicMock(), asg, "nope", 80)


def test_instance_status_reads_nested_response():
    ec2 = MagicMock()
    ec2.describe_application_status.return_value = {"ApplicationStatuses": {"Instances": [
        {"InstanceId": "i-1", "ApplicationStatus": {"Status": "impaired", "Details": [
            {"ApplicationStatusCheckId": "asc-1", "Status": "impaired",
             "Reason": {"Code": "ResponseCodeMismatch", "StatusCode": 500}}]}}]}}
    [row] = core.instance_status(ec2, ["i-1"])
    assert row == {"InstanceId": "i-1", "Status": "impaired", "CheckId": "asc-1", "CheckStatus": "impaired",
                   "Reason": "ResponseCodeMismatch", "HttpCode": 500}


def _eni_client(counts):
    ec2 = MagicMock()
    eni = {"Operator": {"Principal": core.MANAGED_ENI_PRINCIPAL}}
    ec2.describe_network_interfaces.side_effect = [{"NetworkInterfaces": [eni] * n} for n in counts]
    return ec2


class _Clock:
    """Fake clock that only moves when the code sleeps."""
    def __init__(self):
        self.t = 0

    def sleep(self, s):
        self.t += s

    def now(self):
        return self.t


def test_wait_eni_returns_when_released():
    c = _Clock()
    ok, waited = core.wait_for_eni_release(_eni_client([2, 1, 0]), "vpc-1", 3600, 300, sleep=c.sleep, now=c.now)
    assert ok and waited == 600


def test_wait_eni_times_out():
    c = _Clock()
    ok, waited = core.wait_for_eni_release(_eni_client([2] * 10), "vpc-1", 600, 300, sleep=c.sleep, now=c.now)
    assert not ok and waited == 600


def test_managed_enis_ignores_regular_interfaces():
    ec2 = MagicMock()
    ec2.describe_network_interfaces.return_value = {"NetworkInterfaces": [
        {"Description": "ec2-asc-eni-abc"}, {"Description": "Primary network interface"},
        {"Operator": {"Principal": core.MANAGED_ENI_PRINCIPAL}}]}
    assert len(core.managed_enis(ec2, "vpc-1")) == 2
    kwargs = ec2.describe_network_interfaces.call_args.kwargs
    assert kwargs["IncludeManagedResources"] is True
