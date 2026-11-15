#!/usr/bin/env bash
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run with sudo' >&2; exit 2; }
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/public" && pwd)
target=/opt/ku-academic-mcp/public
env_file=/opt/ku-academic-mcp/config/public.env
[[ -f "$env_file" ]] || { echo "Prepare $env_file (see public/.env.example)" >&2; exit 2; }
# Tokens never enter command arguments or config output. Do not use compose config
# without --quiet, because its resolved output contains authentication secrets.
chown root:root "$env_file"
chmod 600 "$env_file"
docker network inspect ku-academic-mcp_default >/dev/null
install -d -m 755 "$target"
install -m 644 "$source_dir/compose.yml" "$target/compose.yml"
install -m 644 "$source_dir/Caddyfile" "$target/Caddyfile"
compose=(docker compose --project-name ku-mcp-public --env-file "$env_file" -f "$target/compose.yml")
"${compose[@]}" config --quiet
"${compose[@]}" run --rm --no-deps gateway caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
"${compose[@]}" up -d
printf 'HTTPS gateway started. Verify TLS, unauthorized rejection and actual MCP before retiring the old tunnel.\n'
