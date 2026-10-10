"""Wait for the migration's actual exit code, then for ECS service stability. No eval or shell interpolation."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import boto3


def finish(outputs: dict, ecs) -> None:
    value = lambda name: outputs[name]["value"]
    response = ecs.run_task(
        cluster=value("ecs_cluster"), launchType="FARGATE", taskDefinition=value("migration_task_definition"),
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": value("private_subnet_ids"), "securityGroups": [value("task_security_group_id")],
            "assignPublicIp": "DISABLED",
        }},
    )
    if response.get("failures") or len(response.get("tasks", [])) != 1:
        raise RuntimeError("migration task could not be started")
    task = response["tasks"][0]["taskArn"]
    ecs.get_waiter("tasks_stopped").wait(cluster=value("ecs_cluster"), tasks=[task],
                                        WaiterConfig={"Delay": 10, "MaxAttempts": 120})
    stopped = ecs.describe_tasks(cluster=value("ecs_cluster"), tasks=[task])
    tasks = stopped.get("tasks", [])
    if stopped.get("failures") or len(tasks) != 1:
        raise RuntimeError("migration task could not be inspected")
    containers = tasks[0].get("containers", [])
    if len(containers) != 1 or containers[0].get("exitCode") != 0:
        raise RuntimeError("migration failed; inspect the secured migration log group")
    services = value("service_names")
    if services:
        ecs.get_waiter("services_stable").wait(cluster=value("ecs_cluster"), services=services,
                                               WaiterConfig={"Delay": 15, "MaxAttempts": 80})
        state = ecs.describe_services(cluster=value("ecs_cluster"), services=services)
        actual = {s["serviceName"]: s["taskDefinition"] for s in state.get("services", [])}
        if state.get("failures") or actual != value("service_task_definitions"):
            raise RuntimeError("service deployment rolled back or is not running the expected task definition")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--terraform-dir", type=Path, required=True)
    parser.add_argument("--terraform-bin", default="terraform")
    args = parser.parse_args()
    result = subprocess.run([args.terraform_bin, f"-chdir={args.terraform_dir.resolve()}", "output", "-json"],
                            capture_output=True, text=True, check=True)
    finish(json.loads(result.stdout), boto3.client("ecs"))
    print("migration succeeded and ECS services are stable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
