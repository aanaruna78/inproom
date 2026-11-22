#!/usr/bin/env bash
# Checks local prerequisites for INPROOM development. Exits non-zero if anything required is missing.
set -u

fail=0
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
miss() { printf '  \033[31mMISSING\033[0m %s\n          fix: %s\n' "$1" "$2"; fail=1; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }

echo "INPROOM doctor - checking prerequisites"

if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then ok "docker ($(docker --version | awk '{print $3}' | tr -d ,))"
  else miss "docker daemon not reachable" "start Docker and make sure your user can run 'docker info'"; fi
  if docker compose version >/dev/null 2>&1; then ok "docker compose ($(docker compose version --short))"
  else miss "docker compose v2" "install the Docker Compose plugin: https://docs.docker.com/compose/install/"; fi
else
  miss "docker" "install Docker Engine: https://docs.docker.com/engine/install/"
fi

if command -v uv >/dev/null 2>&1; then ok "uv ($(uv --version | awk '{print $2}'))"
else miss "uv (Python toolchain)" "curl -LsSf https://astral.sh/uv/install.sh | sh"; fi

if command -v node >/dev/null 2>&1; then
  major=$(node -p 'process.versions.node.split(".")[0]')
  if [ "$major" -ge 20 ]; then ok "node ($(node --version))"
  else miss "node >= 20 (found $(node --version))" "install Node.js 22 LTS: https://nodejs.org/"; fi
else
  miss "node >= 20" "install Node.js 22 LTS: https://nodejs.org/"
fi

if command -v npm >/dev/null 2>&1; then ok "npm ($(npm --version))"
else miss "npm" "installed with Node.js: https://nodejs.org/"; fi

# Ports: in use is fine if it is our own running stack, so this only warns.
for port in "${POSTGRES_PORT:-55432}" "${KEYCLOAK_PORT:-18080}" "${OBJECTSTORE_PORT:-18333}" "${API_PORT:-8000}" "${WEB_PORT:-3000}"; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    warn "port $port is in use (ignore if the INPROOM stack is already running; otherwise change it in .env)"
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "doctor: missing prerequisites - fix the items above and re-run 'make doctor'"
  exit 1
fi
echo "doctor: all prerequisites present"
