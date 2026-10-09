#!/usr/bin/env bash
# Build, push and roll out ClaimLens. Two-phase because the ECR repository must exist before an image can be pushed,
# and the ECS services need an image to start.
#
#   ./scripts/deploy.sh bootstrap   # create everything except the services (+ ECR repo to push to)
#   ./scripts/deploy.sh release     # build the image tagged with the current git SHA, push it, enable/roll the services
#   ./scripts/deploy.sh migrate     # run the database-migration task once
#
# Requires: aws CLI v2, docker, terraform (or tofu). Set TF_VARS to your tfvars file.
set -euo pipefail

TF_BIN="${TF_BIN:-terraform}"
TF_DIR="$(cd "$(dirname "$0")/../infra/terraform" && pwd)"
TF_VARS="${TF_VARS:-envs/dev.tfvars}"
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v "$TF_BIN" >/dev/null || die "$TF_BIN not found (set TF_BIN=tofu to use OpenTofu)"
command -v aws >/dev/null || die "aws CLI not found"
[ -f "$TF_DIR/$TF_VARS" ] || die "$TF_DIR/$TF_VARS not found (copy envs/dev.tfvars.example)"

tf() { "$TF_BIN" -chdir="$TF_DIR" "$@"; }
tf_out() { tf output -raw "$1"; }

git_sha() {
  # Refuse to ship uncommitted code: an image tag must identify exactly one source tree.
  if ! git -C "$APP_DIR" diff --quiet HEAD -- 2>/dev/null; then
    die "working tree has uncommitted changes; commit first so the image tag (git SHA) means something"
  fi
  git -C "$APP_DIR" rev-parse --short=12 HEAD
}

case "${1:-}" in
  bootstrap)
    log "initialising"
    tf init
    log "planning (enable_services=false)"
    tf plan -var-file="$TF_VARS" -var enable_services=false -out=tfplan.bootstrap
    read -r -p "Apply this plan? [y/N] " answer
    [ "$answer" = "y" ] || die "aborted"
    tf apply tfplan.bootstrap
    log "done. Next: ./scripts/deploy.sh release   (then ./scripts/deploy.sh migrate)"
    ;;

  release)
    TAG="$(git_sha)"
    REPO="$(tf_out ecr_repository_url)"
    REGISTRY="${REPO%%/*}"
    REGION="$(aws configure get region || true)"
    REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-${REGION:-us-east-1}}}"

    log "logging in to $REGISTRY"
    aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

    log "building $REPO:$TAG (linux/amd64: matches the Fargate task definition even on Apple Silicon)"
    docker build --platform linux/amd64 -t "$REPO:$TAG" "$APP_DIR"

    # Immutable tags: pushing an existing tag fails by design. If it already exists we reuse it.
    if aws ecr describe-images --region "$REGION" --repository-name "${REPO#*/}" --image-ids imageTag="$TAG" >/dev/null 2>&1; then
      log "image tag $TAG already in ECR; not pushing again"
    else
      log "pushing $REPO:$TAG"
      docker push "$REPO:$TAG"
    fi

    log "planning rollout of $TAG (enable_services=true)"
    tf plan -var-file="$TF_VARS" -var enable_services=true -var "image_tag=$TAG" -out=tfplan.release
    read -r -p "Apply this plan? [y/N] " answer
    [ "$answer" = "y" ] || die "aborted"
    tf apply tfplan.release
    log "released $TAG. Watch: aws ecs describe-services (circuit breaker rolls back a bad image automatically)"
    ;;

  migrate)
    log "running the database migration task"
    CMD="$(tf_out migrate_command)"
    log "$CMD"
    eval "$CMD"
    log "check the task's logs in CloudWatch (log group /<project>/<env>/migrate)"
    ;;

  *)
    sed -n '2,10p' "$0"
    exit 2
    ;;
esac
