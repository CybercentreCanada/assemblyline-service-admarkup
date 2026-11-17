#!/bin/bash
set -euo pipefail

docker build \
    --pull \
    -t assemblyline-service-admarkup:pytest \
    -f ./Dockerfile \
    .

docker run \
    -t \
    --rm \
    -v "$PWD/tests:/opt/al_service/tests:ro" \
    --entrypoint bash \
    assemblyline-service-admarkup:pytest \
    -c "pip install --user -r tests/requirements.txt && pytest -p no:cacheprovider --durations=10 -rsx -vv -x"
