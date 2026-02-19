ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Node.js 20
RUN apk add --no-cache nodejs npm

# Set up working directory
WORKDIR /app

# Copy package files and install dependencies
COPY package.json ./
RUN npm install --omit=dev

# Copy application source
COPY src/ ./src/

# Create persistent data directory for Claude CLI auth
RUN mkdir -p /data/.claude && \
    ln -sf /data/.claude /root/.claude

# Copy s6-overlay service definition (replaces CMD)
COPY rootfs /
