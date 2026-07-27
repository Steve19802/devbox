#!/bin/bash

# Get the directory where envsetup.sh is located, then go up one level to the project root
PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# Define the 3-layer compose files as a single variable for easy reuse
DEVBOX_COMPOSE_ARGS="-f $PROJECT_ROOT/docker-compose.yml -f $PROJECT_ROOT/docker-compose.dev.yml -f $PROJECT_ROOT/build/docker-compose.mcp.yml"

# Dynamically fetch the host's actual UID/GID to prevent permission issues
LOCAL_UID=$(id -u 2>/dev/null || echo 1000)
LOCAL_GID=$(id -g 2>/dev/null || echo 1000)

# Internal helper: runs a Python script from build/scripts/ inside a Docker container
function _devbox_run_python() {
  local script=$1
  shift
  # Smart TTY detection: use -it when user is interactive, -i for CI/pipes
  local tty_flag="-i"
  if [ -t 0 ]; then
    tty_flag="-it"
  fi
  docker run --rm $tty_flag \
    -u "$LOCAL_UID:$LOCAL_GID" \
    -v "$PROJECT_ROOT:/workspace" \
    -w /workspace \
    -e LOCAL_UID="$LOCAL_UID" \
    -e LOCAL_GID="$LOCAL_GID" \
    python:3.11-slim \
    python "/workspace/build/scripts/$script" "$@"
}

function devbox-ai-setup() {
  echo "🤖 Configuring AI toolchain..."
  _devbox_run_python ai_setup.py "$@"
}
# --- Docker Compose Helpers (V2) ---

function devbox-up() {
  echo "🚀 Starting DevBox..."
  # The "$@" allows you to pass flags like --remove-orphans or --force-recreate
  docker compose $DEVBOX_COMPOSE_ARGS up -d "$@"
}

function devbox-build() {
  echo "🧱 Building DevBox infrastructure..."
  # The "$@" passes any extra flags directly to the build command
  docker compose $DEVBOX_COMPOSE_ARGS build "$@"
}

function devbox-down() {
  echo "🛑 Stopping development environment..."
  docker compose $DEVBOX_COMPOSE_ARGS down "$@"
}

function devbox-logs() {
  # If no argument is passed, show all logs. Otherwise, show logs for the specific service.
  if [ -z "$1" ]; then
    echo "📜 Tailing logs for all services (Press CTRL+C to exit)..."
    docker compose $DEVBOX_COMPOSE_ARGS logs -f
  else
    echo "📜 Tailing logs for $1 (Press CTRL+C to exit)..."
    docker compose $DEVBOX_COMPOSE_ARGS logs -f "$1"
  fi
}

# --- OpenCode / CodeBox Helper ---

function devbox-ai() {
  # Dynamically grab the current directory name and append _default
  local dir_name=$(basename "$PWD")
  local network_name="${dir_name}_default"

  echo "🤖 Starting OpenCode (CodeBox)..."
  echo "🔗 Attaching to network: $network_name"

  # IMPORTANT: Update this path to point to where your actual codebox script lives!
  PROJECT_NETWORK="$network_name" codebox
}

function _get_services() {
  # Dynamically reads all compose layers and returns the list of services (excluding our mcp infrastructure)
  docker compose $DEVBOX_COMPOSE_ARGS config --services 2>/dev/null | grep -v 'mcp'
}

function devbox-run() {
  local service=$1
  shift # Remove the first argument so the rest is just the command

  if [ -z "$service" ] || [ -z "$1" ]; then
    echo "❌ Usage: devbox-run <service> <command>"
    echo "💡 Available services: $(_get_services | paste -sd ', ' -)"
    return 1
  fi

  echo "🚀 Running '$*' in '$service'..."
  docker compose $DEVBOX_COMPOSE_ARGS exec "$service" "$@"
}

function devbox-edit() {
  local service=$1

  if [ -z "$service" ]; then
    echo "❌ Usage: devbox-edit <service>"
    echo "💡 Available services: $(_get_services | paste -sd ', ' -)"
    return 1
  fi

  echo "💻 Launching IDE in '$service'..."
  docker compose $DEVBOX_COMPOSE_ARGS exec "$service" nvim .
}

function devbox-init() {
  echo "🏗️ Scaffolding new DevBox project..."
  _devbox_run_python scaffold.py /workspace "$@"
}

function devbox-add-service() {
  echo "➕ Adding new service..."
  _devbox_run_python manage_service.py add "$@"
}

function devbox-remove-service() {
  local service_name=$1
  if [ -z "$service_name" ]; then
    echo "➖ Removing service..."
    _devbox_run_python manage_service.py remove
  else
    echo "➖ Removing service '$service_name'..."
    _devbox_run_python manage_service.py remove "$service_name"
  fi
}

# ==========================================
# --- 🪄 CLI Autocompletion Magic ---
# ==========================================

_devbox_service_completions() {
  # COMP_WORDS is an array of words typed so far
  # COMP_CWORD is the index of the word currently being typed
  local curr_arg=${COMP_WORDS[COMP_CWORD]}

  # Fetch available services dynamically using your existing helper!
  local services=$(_get_services)

  # compgen filters the $services list based on what the user typed ($curr_arg)
  COMPREPLY=($(compgen -W "${services}" -- "${curr_arg}"))
}

_devbox_ai_completions() {
  local curr_arg=${COMP_WORDS[COMP_CWORD]}

  # Dynamically fetch AI tools from the build/ai directory, plus the "none" option
  local tools="$(ls -1 "$PROJECT_ROOT/build/ai/" 2>/dev/null) none"

  COMPREPLY=($(compgen -W "${tools}" -- "${curr_arg}"))
}

# Bind the completion functions to your specific CLI commands
complete -F _devbox_service_completions devbox-logs devbox-run devbox-edit devbox-remove-service
complete -F _devbox_ai_completions devbox-ai-setup

echo "✅ DevBox Environment Loaded!"
echo "💡 Available commands:"

# Read this exact script, find lines starting with 'function', ignore ones
# starting with an underscore (_), and print the clean function name.
grep -oE '^function [^_][^ (]+' "${BASH_SOURCE[0]}" | awk '{print "   🚀 "$2}'

echo ""
echo "Run 'devbox-init' to scaffold your project"
