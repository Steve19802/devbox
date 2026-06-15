# --- STAGE 1: Production Base ---
FROM node:24-alpine AS prod
WORKDIR /app
# (In a real app, you would npm install and build here)
CMD ["npm", "start"]

# --- STAGE 2: Development / Neovim Environment ---
FROM prod AS dev

# COMBINED LAYER: Install tools, setup sudo, and clean up in ONE step.
# - 'build-base' is Alpine's lightweight equivalent of build-essential (adds gcc, g++, make).
# - 'fd' is the Alpine package name for fd-find.
# - We install neovim and lazygit directly via apk!
RUN apk add --no-cache \
  curl \
  git \
  sudo \
  ripgrep \
  fd \
  python3 \
  ca-certificates \
  build-base \
  neovim \
  lazygit \
  && mkdir -p /etc/sudoers.d \
  && echo "node ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/nopasswd \
  && chmod 0440 /etc/sudoers.d/nopasswd

# Pre-create docker volume mount points
RUN mkdir -p /home/node/.config /home/node/.local/share /home/node/.local/state && \
  chown -R node:node /home/node/.config /home/node/.local

# Enable corepack natively
RUN corepack enable && corepack prepare pnpm@latest --activate

# Switch to the built-in non-root user
USER node

# Keep the container alive for live-reloading / development
CMD ["sleep", "infinity"]
