#!/bin/sh
set -eu

deploy_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v docker >/dev/null 2>&1; then
    printf '%s\n' 'Docker Engine and Docker Compose >= 2.30 are required.' >&2
    exit 1
fi
if [ ! -f "$deploy_dir/.env" ] || [ ! -f "$deploy_dir/server.env" ]; then
    printf '%s\n' 'Missing deploy/.env or deploy/server.env. See docs/deployment.md.' >&2
    exit 1
fi

# No shell sourcing of secret files. Compose alone parses its environment files.
exec docker compose --env-file "$deploy_dir/.env" -f "$deploy_dir/compose.yaml" "$@"
