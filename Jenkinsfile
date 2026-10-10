// Optional Jenkins equivalent. AWS deployment stays in the separately approved OIDC workflow.
pipeline {
    agent { label 'linux-docker' }
    options { disableConcurrentBuilds(); timestamps() }
    environment { AWS_EC2_METADATA_DISABLED = 'true' }
    stages {
        stage('Dependencies') {
            steps { sh 'python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt' }
        }
        stage('Checks') {
            steps {
                sh '.venv/bin/ruff check claimlens scripts tests && .venv/bin/mypy'
                sh '.venv/bin/pytest --cov=claimlens --cov-report=xml'
            }
        }
        stage('Offline evaluation') {
            steps {
                sh '.venv/bin/python -m claimlens gen-data --out data/ci --count 60 --seed 3 --difficulty hard'
                sh '.venv/bin/python -m claimlens eval --data data/ci --config "label=ci,llm=mock:careful" --min-f1 0.90 --min-auto-accept-precision 0.99 --min-error-catch-rate 0.95'
            }
        }
        stage('Infrastructure') {
            steps {
                sh 'terraform -chdir=infra/terraform fmt -check -recursive'
                sh 'terraform -chdir=infra/terraform init -backend=false -input=false'
                sh 'terraform -chdir=infra/terraform validate'
            }
        }
        stage('Build and scan') {
            steps {
                sh 'docker build --platform linux/amd64 -t claimlens:$GIT_COMMIT .'
                sh 'docker save claimlens:$GIT_COMMIT -o image.tar'
                sh 'docker run --rm -v "$WORKSPACE:/scan:ro" aquasec/trivy:0.65.0 image --input /scan/image.tar --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1'
            }
        }
    }
    post { always { archiveArtifacts artifacts: 'coverage.xml,results/eval/*', allowEmptyArchive: true } }
}
