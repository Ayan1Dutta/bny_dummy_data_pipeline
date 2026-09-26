pipeline {
    agent any

    options {
        timestamps()
        disableConcurrentBuilds()
        timeout(time: 45, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '30', artifactNumToKeepStr: '10'))
    }

    triggers {
        pollSCM('H/5 * * * *')
    }

    parameters {
        booleanParam(name: 'PUBLISH', defaultValue: false, description: 'Push the image to the registry (only when GitLab CI is unavailable)')
    }

    environment {
        REGISTRY   = 'registry.gitlab.com'
        IMAGE_REPO = 'registry.gitlab.com/bny/settlement-platform/settlement-api'
        VENV       = "${WORKSPACE}/.venv"
        PIP_CACHE_DIR = "${WORKSPACE}/.cache/pip"
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
                script {
                    env.GIT_SHA = sh(returnStdout: true, script: 'git rev-parse --short HEAD').trim()
                    env.IMAGE_TAG = "${env.GIT_SHA}"
                    currentBuild.displayName = "#${env.BUILD_NUMBER} ${env.GIT_SHA}"
                }
            }
        }

        stage('Install Dependencies') {
            steps {
                sh '''
                    python3 -m venv "$VENV"
                    . "$VENV/bin/activate"
                    pip install --quiet --upgrade pip
                    pip install --quiet -r requirements-dev.txt
                '''
            }
        }

        stage('Run Tests') {
            steps {
                sh '''
                    . "$VENV/bin/activate"
                    ruff check src tests scripts deploy incident
                    python scripts/generate_sample_data.py
                    python -m src.pipeline.run_pipeline
                    python -m scripts.run_test_gate --out evidence/01_test_results
                    python -m scripts.reconciliation_gate --out evidence/09_kpi_reconciliation/jenkins_warehouse.json
                '''
            }
            post {
                always {
                    junit allowEmptyResults: false, testResults: 'evidence/01_test_results/junit_*.xml'
                }
            }
        }

        stage('Security Checks') {
            parallel {
                stage('SAST') {
                    steps { sh '. "$VENV/bin/activate" && python -m scripts.security_gate --checks sast --out evidence/02_security_results' }
                }
                stage('Secret Scan') {
                    steps { sh '. "$VENV/bin/activate" && python -m scripts.security_gate --checks secrets --out evidence/02_security_results' }
                }
                stage('SCA') {
                    steps { sh '. "$VENV/bin/activate" && python -m scripts.security_gate --checks sca --out evidence/02_security_results' }
                }
            }
        }

        stage('Build Docker Image') {
            steps {
                sh '''
                    docker build \
                      --label "org.opencontainers.image.revision=$GIT_SHA" \
                      --label "built-by=jenkins" \
                      -t "$IMAGE_REPO:$IMAGE_TAG" .
                    mkdir -p evidence/05_docker_image
                    docker image inspect "$IMAGE_REPO:$IMAGE_TAG" > evidence/05_docker_image/jenkins_image_inspect.json
                '''
            }
        }

        stage('Container Scan') {
            steps {
                sh '''
                    export TRIVY_CACHE_DIR=/var/jenkins_home/.cache/trivy
                    export TRIVY_DB_REPOSITORY=mirror.gcr.io/aquasec/trivy-db:2,ghcr.io/aquasecurity/trivy-db:2
                    trivy image --download-db-only --timeout 30m
                    trivy image --skip-db-update --timeout 15m --format json --output evidence/02_security_results/container.json \
                      --severity HIGH,CRITICAL "$IMAGE_REPO:$IMAGE_TAG"
                    trivy image --skip-db-update --timeout 15m --exit-code 1 --severity CRITICAL --ignore-unfixed "$IMAGE_REPO:$IMAGE_TAG"
                '''
            }
        }

        stage('Smoke Test') {
            environment {
                SMOKE_NET = "smoke-${env.BUILD_TAG}"
                SMOKE_CT  = "smoke-api-${env.BUILD_TAG}"
            }
            steps {
                sh '''
                    . "$VENV/bin/activate"
                    SMOKE_API_KEY=$(python -c "import secrets; print(secrets.token_hex(16))")
                    docker network create "$SMOKE_NET"
                    docker create --name "$SMOKE_CT" --network "$SMOKE_NET" --network-alias smoke-api \
                      -e APP_ENV=dev -e API_KEY="$SMOKE_API_KEY" "$IMAGE_REPO:$IMAGE_TAG"
                    docker cp data/warehouse/settlement.duckdb "$SMOKE_CT":/app/data/warehouse/settlement.duckdb
                    docker start "$SMOKE_CT"
                    docker network connect "$SMOKE_NET" "$(hostname)"
                    for i in $(seq 1 30); do
                      curl -fs http://smoke-api:8000/health && break
                      sleep 2
                    done
                    python -m scripts.smoke_test --base-url http://smoke-api:8000 --api-key "$SMOKE_API_KEY" \
                      --out evidence/08_smoke_test_results/jenkins_smoke.json
                    python -m scripts.reconciliation_gate --api-url http://smoke-api:8000 --api-key "$SMOKE_API_KEY" \
                      --out evidence/09_kpi_reconciliation/jenkins_api.json
                '''
            }
            post {
                always {
                    sh '''
                        mkdir -p evidence/08_smoke_test_results
                        docker logs "$SMOKE_CT" > evidence/08_smoke_test_results/jenkins_container.log 2>&1 || true
                        docker network disconnect "$SMOKE_NET" "$(hostname)" || true
                        docker rm -f "$SMOKE_CT" || true
                        docker network rm "$SMOKE_NET" || true
                    '''
                }
            }
        }

        stage('Publish Artifact') {
            steps {
                script {
                    if (params.PUBLISH) {
                        withCredentials([usernamePassword(credentialsId: 'gitlab-registry-deploy-token',
                                                          usernameVariable: 'REG_USER', passwordVariable: 'REG_PASS')]) {
                            sh '''
                                echo "$REG_PASS" | docker login -u "$REG_USER" --password-stdin "$REGISTRY"
                                docker push "$IMAGE_REPO:$IMAGE_TAG"
                                docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE_REPO:$IMAGE_TAG" \
                                  > evidence/05_docker_image/image_digest.txt
                                docker logout "$REGISTRY"
                            '''
                        }
                    } else {
                        sh 'docker save "$IMAGE_REPO:$IMAGE_TAG" | gzip > "settlement-api-$IMAGE_TAG.tar.gz"'
                        archiveArtifacts artifacts: "settlement-api-${env.IMAGE_TAG}.tar.gz", fingerprint: true
                    }
                }
            }
        }
    }

    post {
        always {
            archiveArtifacts allowEmptyArchive: true, artifacts: 'evidence/**', fingerprint: true
        }
        failure {
            echo "Jenkins secondary build failed for ${env.GIT_SHA}; GitLab CI remains the release path."
        }
        cleanup {
            sh 'docker image rm "$IMAGE_REPO:$IMAGE_TAG" || true'
            cleanWs()
        }
    }
}