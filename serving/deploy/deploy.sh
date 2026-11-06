#!/usr/bin/env bash
set -euo pipefail
sha=${1:?Usage: deploy.sh COMMIT_SHA}
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid commit SHA' >&2; exit 2; }
[[ $(id -u) == 0 ]] || { echo 'Run as root through SSM or sudo' >&2; exit 2; }
command -v docker >/dev/null
command -v curl >/dev/null
command -v flock >/dev/null
docker compose version >/dev/null
base=${KU_DEPLOY_ROOT:-/opt/ku-academic-mcp}
mkdir -p "$base/releases" "$base/config"
exec 9>"$base/deploy.lock"
flock -w 900 9
# Runtime configuration stays on the instance, outside Git and release folders.
if [[ ! -e "$base/config/runtime.env" ]]; then
    (umask 077; printf 'MODEL_MODE=local\n' > "$base/config/runtime.env")
fi
previous=$(readlink -f "$base/current" 2>/dev/null || true)
previous_image=$(docker inspect -f '{{.Image}}' ku-academic-mcp-mcp-1 2>/dev/null || true)
release="$base/releases/$sha"
if [[ ! -d "$release" ]]; then
    staging=$(mktemp -d "$base/releases/.staging-XXXXXXXX")
    curl --fail --location --retry 2 "https://github.com/kimuc2013/262RCOSE45700/archive/$sha.tar.gz" -o "$staging/source.tar.gz"
    mkdir "$staging/source"
    tar -xzf "$staging/source.tar.gz" -C "$staging/source" --strip-components=1
    mv "$staging/source" "$release"
    rm "$staging/source.tar.gz"
    rmdir "$staging"
fi
export ACADEMIC_IMAGE="ku-academic-mcp:$sha"
compose() {
    # Explicit override also pins rollback for releases predating ACADEMIC_IMAGE.
    printf 'services:\n  mcp:\n    image: %s\n' "$ACADEMIC_IMAGE" > "$base/image.override.yaml"
    docker compose --env-file "$base/config/runtime.env" -f "$release/serving/compose.yaml" -f "$base/image.override.yaml" "$@"
}
# Build before touching the running container. No shared latest image tag.
docker build -t "$ACADEMIC_IMAGE" "$release/serving"
rollback() {
    code=${1:-$?}
    trap - ERR
    echo "Deployment failed; attempting previous image rollback" >&2
    if [[ -n "$previous_image" ]]; then
        if [[ -n "$previous" && -f "$previous/serving/compose.yaml" ]]; then release="$previous"; fi
        export ACADEMIC_IMAGE="$previous_image"
        if compose up -d --no-build --force-recreate mcp; then
            if compose exec -T mcp python -m academic_mcp.smoke; then
                echo 'Previous image health check passed'
            else
                echo 'Rollback started but health check failed; inspect EC2' >&2
            fi
        else
            echo 'Rollback failed; inspect EC2' >&2
        fi
    else
        echo 'First deployment: no previous image available for rollback' >&2
    fi
    exit "$code"
}
trap 'rollback $?' ERR
# Seed samples only on an empty DB. Never replace existing production data.
revision=$(compose run --rm --no-deps mcp python -c 'import os; from academic_mcp.store import Store; print(Store(os.environ["ACADEMIC_DB"]).revision())')
if [[ "$revision" == 'empty' ]]; then
    compose run --rm --no-deps mcp python -m academic_mcp.ingest --db /data/academic.sqlite3 --samples /samples
fi
compose up -d --no-build --force-recreate mcp
healthy=false
for attempt in {1..12}; do
    if compose exec -T mcp python -m academic_mcp.smoke; then healthy=true; break; fi
    sleep 2
done
if [[ "$healthy" != true ]]; then rollback 1; fi
ln -sfn "$release" "$base/current.next"
mv -Tf "$base/current.next" "$base/current"
trap - ERR
echo "Deployed $sha; MCP is available on EC2 loopback port 8000"
