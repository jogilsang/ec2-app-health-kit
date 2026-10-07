"""Behaviour of the custom resource embedded in template.yaml (loaded from the template itself)."""
import json
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from botocore.exceptions import ClientError


class _CfnLoader(yaml.SafeLoader):
    pass


_CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


@pytest.fixture(scope="module")
def mod():
    tpl = yaml.load((Path(__file__).parents[1] / "template.yaml").read_text(), Loader=_CfnLoader)
    code = tpl["Resources"]["AssociatorFunction"]["Properties"]["Code"]["ZipFile"]
    m = types.ModuleType("associator")
    exec(compile(code, "associator", "exec"), m.__dict__)
    return m


def _event(request, values, old_values=None, check="asc-1", physical=None):
    e = {"RequestType": request, "ResourceProperties": {"CheckId": check, "TagKey": "aws:autoscaling:groupName",
                                                         "TagValues": values}}
    if old_values is not None:
        e["OldResourceProperties"] = {"CheckId": check, "TagKey": "aws:autoscaling:groupName", "TagValues": old_values}
    if physical:
        e["PhysicalResourceId"] = physical
    return e


def _ec2():
    ec2 = MagicMock()
    ec2.associate_application_status_check.return_value = {"UnsuccessfulResults": []}
    ec2.disassociate_application_status_check.return_value = {"UnsuccessfulResults": []}
    return ec2


def _pairs(*vals):
    return [{"Key": "aws:autoscaling:groupName", "Value": v} for v in vals]


def test_create_associates_all_values_and_dedupes(mod):
    ec2 = _ec2()
    pid = mod.apply(lambda: ec2, _event("Create", ["web", " api ", "web", ""]))
    assert pid == "asc-1|aws:autoscaling:groupName"
    ec2.associate_application_status_check.assert_called_once_with(
        ApplicationStatusCheckId="asc-1", TargetTagAssociations=_pairs("web", "api"))


def test_update_only_touches_the_difference(mod):
    ec2 = _ec2()
    mod.apply(lambda: ec2, _event("Update", ["web", "batch"], old_values=["web", "api"], physical="asc-1|aws:autoscaling:groupName"))
    ec2.disassociate_application_status_check.assert_called_once_with(
        ApplicationStatusCheckId="asc-1", TargetTagAssociations=_pairs("api"))
    ec2.associate_application_status_check.assert_called_once_with(
        ApplicationStatusCheckId="asc-1", TargetTagAssociations=_pairs("batch"))


def test_delete_after_failed_create_never_builds_a_client(mod):
    # Regression: the rollback Delete used to fail too, leaving the stack in ROLLBACK_FAILED.
    def no_client():
        raise AssertionError("client must not be created")
    pid = mod.apply(no_client, _event("Delete", ["web"], physical="2026/10/07/[$LATEST]abc"))
    assert pid == "2026/10/07/[$LATEST]abc"


def test_delete_ignores_already_removed_association(mod):
    ec2 = MagicMock()
    ec2.disassociate_application_status_check.side_effect = ClientError(
        {"Error": {"Code": "InvalidApplicationStatusCheckId.NotFound", "Message": "gone"}}, "Disassociate")
    assert mod.apply(lambda: ec2, _event("Delete", ["web"], physical="asc-1|aws:autoscaling:groupName"))


def test_more_than_50_values_is_rejected(mod):
    with pytest.raises(ValueError, match="At most 50"):
        mod.apply(lambda: MagicMock(), _event("Create", [f"g{i}" for i in range(51)]))


def test_handler_reports_failure_instead_of_hanging(mod):
    sent = {}

    def fake_urlopen(req, timeout):
        sent.update(json.loads(req.data))

    ec2 = MagicMock()
    ec2.associate_application_status_check.side_effect = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}}, "Associate")
    event = {**_event("Create", ["web"]), "ResponseURL": "https://example/cb", "StackId": "s",
             "RequestId": "r", "LogicalResourceId": "Association"}
    ctx = types.SimpleNamespace(log_stream_name="stream")
    with patch.object(mod.boto3, "client", return_value=ec2), patch.object(mod.urllib.request, "urlopen", fake_urlopen):
        mod.handler(event, ctx)
    assert sent["Status"] == "FAILED"
    assert "UnauthorizedOperation" in sent["Reason"]
    assert sent["PhysicalResourceId"] == "stream"


def test_unsuccessful_results_fail_the_create(mod):
    ec2 = MagicMock()
    ec2.associate_application_status_check.return_value = {
        "UnsuccessfulResults": [{"AssociationValue": "web", "Reason": "LimitExceeded"}]}
    with pytest.raises(RuntimeError, match="LimitExceeded"):
        mod.apply(lambda: ec2, _event("Create", ["web"]))


def test_query_api_fallback_signs_and_serializes(mod):
    captured = {}

    class Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return (b'<AssociateApplicationStatusCheckResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
                    b'<successfulResultSet><item><associationValue>web</associationValue></item></successfulResultSet>'
                    b'<unsuccessfulResultSet/></AssociateApplicationStatusCheckResponse>')

    def fake_urlopen(req, timeout):
        captured["body"] = req.data.decode()
        captured["auth"] = req.headers.get("Authorization", "")
        return Resp()

    session = MagicMock()
    session.get_credentials.return_value.get_frozen_credentials.return_value = \
        types.SimpleNamespace(access_key="AKIDEXAMPLE", secret_key="secret", token=None)
    client = mod.QueryEc2(session, "ap-northeast-2", "https://ec2.ap-northeast-2.amazonaws.com")
    with patch.object(mod.urllib.request, "urlopen", fake_urlopen):
        resp = client.associate_application_status_check("asc-1", _pairs("web", "api"))
    assert resp == {"UnsuccessfulResults": []}
    assert "Action=AssociateApplicationStatusCheck" in captured["body"]
    assert "TargetTagAssociation.2.Value=api" in captured["body"]
    assert captured["auth"].startswith("AWS4-HMAC-SHA256")


def test_alarm_queries_the_metric_name_that_ec2_actually_publishes():
    tpl = (Path(__file__).parents[1] / "template.yaml").read_text()
    # Observed name: StatusCheckFailed_Application_<check id>_<Name tag>; the Name tag is the stack name.
    assert "StatusCheckFailed_Application_${Check.ApplicationStatusCheckId}_${AWS::StackName}" in tpl
    assert "Value: !Ref AWS::StackName" in tpl
