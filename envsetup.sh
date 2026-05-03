#!/bin/bash

# Get the directory where envsetup.sh is located, then go up one level to the project root
PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# Define the 3-layer compose files as a single variable for easy reuse
DEVBOX_COMPOSE_ARGS="-f $PROJECT_ROOT/docker-compose.yml -f $PROJECT_ROOT/docker-compose.dev.yml -f $PROJECT_ROOT/build/docker-compose.mcp.yml"

function devbox-ai-setup() {
  local target_tool=$1

  # 1. Dynamically discover available AI tools based on folders in build/ai/
  local available_tools=($(ls -1 "$PROJECT_ROOT/build/ai/" 2>/dev/null))

  # Append "none" as a valid option to opt-out
  available_tools+=("none")

  # 2. Interactive Menu (if no argument is provided)
  if [ -z "$target_tool" ]; then
    echo "🤖 DevBox AI Toolchain - Select your IDE/Agent:"
    select opt in "${available_tools[@]}"; do
      if [ -n "$opt" ]; then
        target_tool=$opt
        break
      else
        echo "❌ Invalid selection."
      fi
    done
  fi

  # 3. Graceful Exit for "none"
  if [ "$target_tool" == "none" ]; then
    echo "✅ AI toolchain compilation skipped. You are managing your own configuration."
    return 0
  fi

  # 4. Validate the selection
  if [ ! -d "$PROJECT_ROOT/build/ai/$target_tool" ]; then
    echo "❌ Unknown AI tool: $target_tool"
    return 1
  fi

  echo "🔧 Compiling AI Workspace for: $target_tool"

  local base_dir="build/ai/$target_tool"
  local proj_dir="ai/$target_tool"
  local out_dir=".$target_tool"

  # 5. Run the generic Python compiler using Environment Variables
  docker run --rm \
    -v "$PROJECT_ROOT:/workspace" \
    -w /workspace \
    -e BASE_DIR="$base_dir" \
    -e PROJ_DIR="$proj_dir" \
    -e OUT_DIR="$out_dir" \
    python:3.11-slim python /workspace/build/scripts/compile_ai.py

  echo "✅ Activated $target_tool! You can now launch your IDE."
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
  # Dynamically reads the docker-compose.yml and returns the list of services (excluding our mcp infrastructure)
  docker compose -f "$PROJECT_ROOT/docker-compose.yml" config --services 2>/dev/null | grep -v 'mcp'
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
  docker compose $DEVBOX_COMPOSE_ARGS exec "$service" sh -c "$*"
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

  # 1. Base Structure
  mkdir -p "$PROJECT_ROOT/src/frontend"
  mkdir -p "$PROJECT_ROOT/src/backend"
  mkdir -p "$PROJECT_ROOT/ai"

  # 2. Copy base Templates
  local tpl_dir="$PROJECT_ROOT/build/templates"

  if [ ! -f "$PROJECT_ROOT/.gitignore" ] && [ -f "$tpl_dir/gitignore.base" ]; then
    echo "📄 Creating .gitignore..."
    cp "$tpl_dir/gitignore.base" "$PROJECT_ROOT/.gitignore"
  fi

  if [ ! -f "$PROJECT_ROOT/docker-compose.yml" ] && [ -f "$tpl_dir/docker-compose.base.yml" ]; then
    echo "🐳 Creating base docker-compose.yml..."
    cp "$tpl_dir/docker-compose.base.yml" "$PROJECT_ROOT/docker-compose.yml"
  fi

  if [ ! -f "$PROJECT_ROOT/docker-compose.dev.yml" ] && [ -f "$tpl_dir/docker-compose.dev.base.yml" ]; then
    echo "🛠️ Creating docker-compose.dev.yml..."
    cp "$tpl_dir/docker-compose.dev.base.yml" "$PROJECT_ROOT/docker-compose.dev.yml"
  fi

  if [ ! -f "$PROJECT_ROOT/ARCHITECTURE.md" ] && [ -f "$tpl_dir/ARCHITECTURE.base.md" ]; then
    echo "📝 Creating ARCHITECTURE.md..."
    cp "$tpl_dir/ARCHITECTURE.base.md" "$PROJECT_ROOT/ARCHITECTURE.md"
  fi

  # 3. Environment Variables (.env)
  if [ ! -f "$PROJECT_ROOT/.env" ]; then
    echo "🔐 Creating .env file..."

    # Dynamically fetch the host's actual UID/GID to prevent permission issues
    LOCAL_UID=$(id -u 2>/dev/null || echo 1000)
    LOCAL_GID=$(id -g 2>/dev/null || echo 1000)

    # Notice we don't use quotes around EOF here so bash CAN evaluate the UID/GID variables
    cat <<EOF >"$PROJECT_ROOT/.env"
UID=${LOCAL_UID}
GID=${LOCAL_GID}

# Can be 'stable', 'nightly', or a specific tag like 'v0.10.0'
# it's recommended to match with the host version
NEOVIM_VERSION=v0.11.6
LAZYGIT_VERSION=0.60.0
EOF
  fi

  echo "✅ Project scaffolded successfully!"
  echo "Run 'devbox-toolchain' to set up your AI environment."
}

echo "✅ DevBox Environment Loaded!"
echo "💡 Available commands:"

# Read this exact script, find lines starting with 'function', ignore ones
# starting with an underscore (_), and print the clean function name.
grep -oE '^function [^_][^ (]+' "${BASH_SOURCE[0]}" | awk '{print "   🚀 "$2}'

echo "\nRun 'devbox-init' to scaffold your project"
