"""Bedrock client tests use a FAKE boto3 client: no AWS account, no cost."""
import pytest
from botocore.exceptions import ClientError

from claimlens.llm.bedrock_client import BedrockClient, LLMError
from claimlens.models import ExtractedClaim


class FakeRuntime:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.last_request = response, error, None

    def converse(self, **request):
        self.last_request = request
        if self.error:
            raise self.error
        return self.response


def ok_response(payload):
    return {"stopReason": "tool_use", "usage": {"inputTokens": 1000, "outputTokens": 200},
            "output": {"message": {"content": [{"toolUse": {"name": "record_claim", "input": payload}}]}}}


def call(client):
    return client.call_tool(system="s", user_text="u", tool_name="record_claim",
                            tool_description="d", schema_model=ExtractedClaim)


def test_forces_tool_and_parses_result():
    fake = FakeRuntime(ok_response({"provider_npi": "1234567893"}))
    result = call(BedrockClient("some-model", "us-east-1", runtime_client=fake))
    assert fake.last_request["toolConfig"]["toolChoice"] == {"tool": {"name": "record_claim"}}
    assert fake.last_request["inferenceConfig"]["temperature"] == 0.0
    assert result.data["provider_npi"] == "1234567893"
    assert result.input_tokens == 1000 and result.cost_usd > 0


def test_missing_tool_call_raises():
    fake = FakeRuntime({"usage": {}, "output": {"message": {"content": [{"text": "chatty reply"}]}}})
    with pytest.raises(LLMError):
        call(BedrockClient("m", "us-east-1", runtime_client=fake))


def test_truncated_output_raises():
    resp = ok_response({})
    resp["stopReason"] = "max_tokens"
    with pytest.raises(LLMError):
        call(BedrockClient("m", "us-east-1", runtime_client=FakeRuntime(resp)))


def test_aws_error_becomes_llmerror_without_leaking_input():
    err = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse")
    with pytest.raises(LLMError) as info:
        call(BedrockClient("m", "us-east-1", runtime_client=FakeRuntime(error=err)))
    assert "ThrottlingException" in str(info.value)


def test_no_model_configured_gives_helpful_error():
    with pytest.raises(LLMError, match="CLAIMLENS_MODEL_ID"):
        BedrockClient("", "us-east-1")
