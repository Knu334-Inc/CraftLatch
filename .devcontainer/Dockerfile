ARG IMAGEARCH
FROM ${IMAGEARCH:-arm64v8/}node:26-trixie-slim

ARG TZ
ENV TZ="$TZ"

# Install basic development tools and iptables/ipset
RUN apt-get update && apt-get install -y --no-install-recommends \
  locales \
  less \
  git \
  gh \
  procps \
  sudo \
  fzf \
  zsh \
  man-db \
  unzip \
  zip \
  gnupg2 \
  iptables \
  ipset \
  iproute2 \
  dnsutils \
  aggregate \
  jq \
  nano \
  vim \
  wget \
  curl \
  ca-certificates \
  bubblewrap \
  openssh-client \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# Generate the Japanese UTF-8 locale used by the shell and CLI tools.
RUN sed -i 's/^# *ja_JP.UTF-8 UTF-8/ja_JP.UTF-8 UTF-8/' /etc/locale.gen && \
  locale-gen ja_JP.UTF-8 && \
  update-locale LANG=ja_JP.UTF-8 LC_ALL=ja_JP.UTF-8

ENV LANG=ja_JP.UTF-8
ENV LANGUAGE=ja_JP:ja
ENV LC_ALL=ja_JP.UTF-8

# Ensure default node user has access to /usr/local/share
RUN chown -R node:node /usr/local/share

ARG USERNAME=node

# Persist bash history.
RUN SNIPPET="export PROMPT_COMMAND='history -a' && export HISTFILE=/commandhistory/.bash_history" \
  && mkdir /commandhistory \
  && touch /commandhistory/.bash_history \
  && chown -R $USERNAME /commandhistory

# Set `DEVCONTAINER` environment variable to help with orientation
ENV DEVCONTAINER=true

# Create workspace and config directories and set permissions
RUN mkdir -p /workspace /home/node/.claude && \
  mkdir -p /workspace /home/node/.codex && \
  mkdir -p /workspace /home/node/.agents && \
  chown -R node:node /workspace /home/node/.claude && \
  chown -R node:node /workspace /home/node/.codex && \
  chown -R node:node /workspace /home/node/.agents

WORKDIR /workspace

ARG GIT_DELTA_VERSION=0.19.2
RUN ARCH=$(dpkg --print-architecture) && \
  wget "https://github.com/dandavison/delta/releases/download/${GIT_DELTA_VERSION}/git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb" && \
  sudo dpkg -i "git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb" && \
  rm "git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb"

# Install Context7
RUN npm install -g ctx7

# Install Playwright CLI
RUN npm install -g @playwright/cli@latest && \
  npx playwright install-deps

# Install Codex
RUN npm i -g @openai/codex

# Copy and set up firewall script
COPY init-firewall.sh /usr/local/bin/
COPY init-context7.sh /usr/local/bin/
COPY init-git.sh /usr/local/bin/
COPY init-playwright-cli.sh /usr/local/bin/
COPY init-statusline.sh /usr/local/bin/
COPY statusline.sh /usr/local/lib/claude-statusline.sh
RUN chmod +x /usr/local/bin/init-firewall.sh /usr/local/bin/init-context7.sh /usr/local/bin/init-git.sh /usr/local/bin/init-playwright-cli.sh /usr/local/bin/init-statusline.sh /usr/local/lib/claude-statusline.sh && \
  echo "node ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" > /etc/sudoers.d/node-firewall && \
  chmod 0440 /etc/sudoers.d/node-firewall

# Set up non-root user
USER node

# Set the default shell to zsh rather than sh
ENV SHELL=/bin/zsh

# Set the default editor and visual
ENV EDITOR=vim
ENV VISUAL=vim

# Default powerline10k theme
ARG ZSH_IN_DOCKER_VERSION=1.2.0
RUN sh -c "$(wget -O- https://github.com/deluan/zsh-in-docker/releases/download/v${ZSH_IN_DOCKER_VERSION}/zsh-in-docker.sh)" -- \
  -p git \
  -p fzf \
  -a "source <(fzf --zsh)" \
  -a "export PROMPT_COMMAND='history -a' && export HISTFILE=/commandhistory/.bash_history" \
  -x

RUN sed -i \
  -e "s/^export LANG=.*/export LANG='ja_JP.UTF-8'/" \
  -e "s/^export LANGUAGE=.*/export LANGUAGE='ja_JP:ja'/" \
  -e "s/^export LC_ALL=.*/export LC_ALL='ja_JP.UTF-8'/" \
  ~/.zshrc

# Install Claude
RUN curl -fsSL https://claude.ai/install.sh | zsh && \
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc

RUN echo 'alias codex="codex --no-daemon"' >> ~/.zshrc

# Install Chromium for Playwright
RUN playwright-cli install-browser chromium

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
