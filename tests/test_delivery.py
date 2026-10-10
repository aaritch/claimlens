from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from scripts.finish_deployment import finish

ROOT = Path(__file__).resolve().parents[1]


def outputs():
    return {key: {"value": value} for key, value in {
        "ecs_cluster": "cluster", "migration_task_definition": "migration:1",
        "private_subnet_ids": ["subnet-a", "subnet-b"], "task_security_group_id": "sg-task",
        "service_names": ["worker"], "service_task_definitions": {"worker": "worker:2"},
    }.items()}


def ecs():
    client = Mock()
    client.run_task.return_value = {"tasks": [{"taskArn": "task:1"}]}
    client.describe_tasks.return_value = {"tasks": [{"containers": [{"exitCode": 0}]}]}
    client.describe_services.return_value = {"services": [{"serviceName": "worker", "taskDefinition": "worker:2"}]}
    return client


def test_deployment_waits_for_migration_exit_and_service_health():
    client = ecs()
    finish(outputs(), client)
    assert client.run_task.call_args.kwargs["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"] == "DISABLED"
    assert [call.args[0] for call in client.get_waiter.call_args_list] == ["tasks_stopped", "services_stable"]


@pytest.mark.parametrize("containers", [[{"exitCode": 1}], [{}], []])
def test_migration_failure_blocks_successful_deployment(containers):
    client = ecs()
    client.describe_tasks.return_value = {"tasks": [{"containers": containers}]}
    with pytest.raises(RuntimeError, match="migration failed"):
        finish(outputs(), client)
    assert client.get_waiter.call_count == 1


def test_failed_task_launch_is_not_reported_as_a_deploy():
    client = ecs()
    client.run_task.return_value = {"tasks": [], "failures": [{"reason": "capacity"}]}
    with pytest.raises(RuntimeError, match="could not be started"):
        finish(outputs(), client)


def test_rolled_back_service_is_not_reported_as_successful():
    client = ecs()
    client.describe_services.return_value = {"services": [{"serviceName": "worker", "taskDefinition": "worker:1"}]}
    with pytest.raises(RuntimeError, match="rolled back"):
        finish(outputs(), client)


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_ci_runs_tests_quality_gate_infrastructure_validation_and_scan():
    config = workflow("ci.yml")
    assert "pull_request" in config["on"] and config["permissions"] == {"contents": "read"}
    assert set(config["jobs"]) == {"test", "infrastructure", "image"}
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    for check in ("mypy", "ruff check", "--cov", "--min-f1", "--min-auto-accept-precision", "validate", "--exit-code 1"):
        assert check in text
    assert "docker push" not in text and "id-token" not in text


def test_deploy_is_manual_main_only_approved_and_never_cancels_running_apply():
    config = workflow("deploy.yml")
    assert set(config["on"]) == {"workflow_dispatch"}
    assert config["concurrency"]["cancel-in-progress"] == "false"
    assert config["jobs"]["checks"]["if"] == "github.ref == 'refs/heads/main'"
    assert config["jobs"]["deploy"]["environment"] == "production"
    assert config["jobs"]["deploy"]["permissions"]["id-token"] == "write"
    assert config["jobs"]["deploy"]["needs"] == "plan"
    assert config["jobs"]["evaluate"]["uses"] == "./.github/workflows/evaluate.yml"
    assert config["jobs"]["plan"]["needs"] == ["checks", "evaluate"]
    assert "upload-artifact" not in (ROOT / ".github/workflows/deploy.yml").read_text()


def test_real_model_eval_is_manual_and_uses_a_separate_seed():
    config = workflow("evaluate.yml")
    assert set(config["on"]) == {"workflow_dispatch", "workflow_call"}
    assert config["jobs"]["evaluate"]["environment"] == "model-evaluation"
    text = (ROOT / ".github/workflows/evaluate.yml").read_text()
    assert "--seed 29" in text and "llm=bedrock" in text
