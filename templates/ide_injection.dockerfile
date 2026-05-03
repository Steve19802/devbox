# === CONTAINERIZED IDE INJECTION ===
# Install Neovim
RUN curl -LO "https://github.com/neovim/neovim/releases/download/${NEOVIM_VERSION}/nvim-linux-x86_64.tar.gz" && \
  tar -xzf nvim-linux-x86_64.tar.gz -C /opt && \
  ln -s /opt/nvim-linux-x86_64/bin/nvim /usr/local/bin/nvim && \
  rm nvim-linux-x86_64.tar.gz

# Install Lazygit
RUN curl -LO "https://github.com/jesseduffield/lazygit/releases/download/v${LAZYGIT_VERSION}/lazygit_${LAZYGIT_VERSION}_Linux_x86_64.tar.gz" && \
  tar -xzf lazygit_${LAZYGIT_VERSION}_Linux_x86_64.tar.gz lazygit && \
  install lazygit /usr/local/bin && \
  rm lazygit_${LAZYGIT_VERSION}_Linux_x86_64.tar.gz

# Install common LSP dependencies (Ripgrep, fd, npm, python3-venv)
RUN apt-get update && apt-get install -y ripgrep fd-find python3-venv npm && \
  ln -s $(which fdfind) /usr/local/bin/fd || true && \
  rm -rf /var/lib/apt/lists/*
