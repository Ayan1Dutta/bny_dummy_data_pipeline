pipeline {
    agent any

    options {
        timestamps()
        disableConcurrentBuilds()
        timeout(time: 45, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '30', artifactNumToKeepStr: '10'))
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
                    trivy image --format json --output evidence/02_security_results/container.json \
                      --severity HIGH,CRITICAL "$IMAGE_REPO:$IMAGE_TAG"
                    trivy image --exit-code 1 --severity CRITICAL --ignore-unfixed "$IMAGE_REPO:$IMAGE_TAG"
                '''
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
