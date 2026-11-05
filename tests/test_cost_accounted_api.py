"""Contracts retained when merging dev into the cost-accounting consumer."""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.grpc_node_client import GrpcNodeClient, _to_dict, common
from src.transfer_reports import ReportUnavailable, transfer_results
import CasperMessage_pb2 as models
import DeployServiceV1_pb2 as service
import RhoTypes_pb2 as rho
from test_transfer_reports import payment


PUBLIC_KEY = bytes.fromhex("04" + "11" * 64)
DEPLOY_ID = bytes.fromhex("12" * 32)
LEGACY_SIG = bytes.fromhex("ab" * 65)


@pytest.mark.parametrize(
    "field,number",
    [
        ("cosigners", 14),
        ("authorityPresentations", 18),
        ("deployId", 19),
        ("authorizationV61", 20),
        ("fundingIntent", 21),
        ("parameters", 22),
    ],
)
def test_cost_node_wire_tags_are_not_mixed_with_classic_parameters(field, number):
    assert models.DeployDataProto.DESCRIPTOR.fields_by_name[field].number == number


def test_dev_reporting_and_deploy_parameter_definitions_are_retained():
    assert common.DeployInfo.DESCRIPTOR.fields_by_name["parameters"].number == 15
    assert common.SingleReport.DESCRIPTOR.fields_by_name["phase"].number == 2
    assert common.ReportPhase.Name(common.REPORT_PHASE_USER) == "REPORT_PHASE_USER"
    assert (
        common.DeployInfoWithEventData.DESCRIPTOR.fields_by_name["offered"].number == 3
    )


def test_unset_and_nested_recursive_signatures_serialize_with_presence():
    empty = _to_dict(models.SigCompound())
    assert all(value is None for value in empty.values())
    signature = models.SigCompound(
        whynot=models.SigCompound(atom=models.SigAtom(pk=PUBLIC_KEY))
    )
    value = _to_dict(signature)
    assert value["tensor"] is None
    assert value["whynot"]["atom"]["pk"] == base64.b64encode(PUBLIC_KEY).decode()
    quoted = _to_dict(rho.CostSignature(quote=rho.Par()))
    assert quoted["compound"] is None
    assert quoted["quote"]["costSignedTerms"] == []


async def pending(deploy, rejected=False):
    response = service.PendingDeploysResponse(
        payload=common.PendingDeploysResponsePayload(
            deploys=[common.PendingDeployInfo(deploy=deploy, isRejected=rejected)],
            totalAvailable=3,
        )
    )
    response = service.PendingDeploysResponse.FromString(response.SerializeToString())
    client = GrpcNodeClient(node_host="127.0.0.1")
    client.stub = SimpleNamespace(getPendingDeploys=AsyncMock(return_value=response))
    try:
        result = await client.get_pending_deploys()
        assert result["totalAvailable"] == 3
        assert result["deploys"][0]["isRejected"] == rejected
        return result["deploys"][0]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_legacy_pending_identity_is_not_erased(rejected):
    deploy = models.DeployDataProto(
        deployer=PUBLIC_KEY,
        sig=LEGACY_SIG,
        term="Nil",
        timestamp=42,
        phloPrice=1,
        phloLimit=1000000,
    )
    value = await pending(deploy, rejected)
    assert value["sig"] == LEGACY_SIG.hex()
    assert value["deployer"] == PUBLIC_KEY.hex()
    assert value["term"] == "Nil"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_legacy_fields", [False, True])
async def test_offered_pending_uses_deploy_id_and_authorization(with_legacy_fields):
    deploy = models.DeployDataProto(deployId=DEPLOY_ID, term="Nil", timestamp=42)
    deploy.authorizationV61.formatVersion = 1
    deploy.authorizationV61.policy.allOf.members.add(publicKey=PUBLIC_KEY)
    if with_legacy_fields:
        deploy.sig = LEGACY_SIG
        deploy.deployer = b"other"
    value = await pending(deploy)
    assert value["sig"] == DEPLOY_ID.hex()
    assert value["deployer"] == PUBLIC_KEY.hex()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["absent", "empty_all_of", "threshold"])
async def test_offer_without_all_of_member_keeps_id_without_guessing_a_deployer(policy):
    deploy = models.DeployDataProto(deployId=DEPLOY_ID)
    if policy == "empty_all_of":
        deploy.authorizationV61.policy.allOf.SetInParent()
    elif policy == "threshold":
        deploy.authorizationV61.policy.threshold.minimum = 1
        deploy.authorizationV61.policy.threshold.members.add(publicKey=PUBLIC_KEY)
    value = await pending(deploy)
    assert value["sig"] == DEPLOY_ID.hex()
    assert value["deployer"] == ""


@pytest.mark.parametrize("success", [False, True])
def test_offered_user_traces_still_drive_native_movements(success):
    deploy_report = {
        "deployInfo": {"sig": DEPLOY_ID.hex(), "errored": False},
        "report": [
            {
                "phase": "REPORT_PHASE_USER",
                "events": payment(100000001, success=success),
            }
        ],
        "offered": {"nativeCostEvidence": "AA==", "receipt": {"phloUsed": 1586}},
    }
    movements = transfer_results(deploy_report)
    assert [movement["amount"] for movement in movements] == (
        [100000001] if success else []
    )


def test_offered_receipt_does_not_replace_missing_execution_events():
    deploy_report = {
        "deployInfo": {"sig": DEPLOY_ID.hex(), "errored": False},
        "report": [],
        "offered": {"nativeCostEvidence": "AA==", "receipt": {"revSpent": "100000001"}},
    }
    with pytest.raises(ReportUnavailable, match="phase-marked"):
        transfer_results(deploy_report)
