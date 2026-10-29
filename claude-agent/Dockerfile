ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Node.js 20
RUN apk add --no-cache nodejs npm

# Set up working directory
WORKDIR /app

# Copy package files and install dependencies
COPY package.json ./
RUN npm install --omit=dev

# Install the native Claude Code CLI for this arch's musl (Alpine) build.
# Since SDK 0.2.113 the CLI ships as a per-platform optional dependency
# instead of a bundled cli.js, and npm can't be trusted to pick the musl
# variant here. Pinned to the same exact version as the SDK.
RUN case "$(uname -m)" in \
      aarch64) CLI_ARCH=arm64 ;; \
      x86_64) CLI_ARCH=x64 ;; \
      *) echo "Unsupported architecture: $(uname -m)" && exit 1 ;; \
    esac && \
    SDK_VERSION=$(node -p "require('./package.json').dependencies['@anthropic-ai/claude-agent-sdk']") && \
    npm install --omit=dev --no-save "@anthropic-ai/claude-agent-sdk-linux-${CLI_ARCH}-musl@${SDK_VERSION}" && \
    ln -sf "/app/node_modules/@anthropic-ai/claude-agent-sdk-linux-${CLI_ARCH}-musl/claude" /usr/local/bin/claude && \
    test -x /usr/local/bin/claude

# Copy application source
COPY src/ ./src/

# Create persistent data directory for Claude CLI auth
RUN mkdir -p /data/.claude && \
    ln -sf /data/.claude /root/.claude

# Copy s6-overlay service definition (replaces CMD)
COPY rootfs /
RUN chmod +x /etc/services.d/claude-agent/run
