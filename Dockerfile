# ── Build stage ─────────────────────────────────────────────────────────────
FROM python:3.10-alpine AS base

# Metadata
LABEL maintainer="jellyhook-debouncer"
LABEL description="JellyHookDebouncer: Smart webhook wrapper for Jellyfin → Home Assistant"

# Create app directory
WORKDIR /app

# Copy only the server script (no dependencies needed - uses stdlib only)
COPY server.py .

# Create non-root user
RUN addgroup -g 1000 wrapper && \
    adduser -D -u 1000 -G wrapper wrapper && \
    chown -R wrapper:wrapper /app

# Switch to non-root user
USER wrapper

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:${PORT:-8099}/health || exit 1

# Expose port (default 8099, configurable via env)
EXPOSE 8099

# Run the wrapper
CMD ["python3", "server.py"]
