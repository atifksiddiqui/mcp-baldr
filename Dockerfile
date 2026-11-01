FROM python:3.12-slim

WORKDIR /app

# Install the gateway
COPY pyproject.toml .
COPY mcp_gateway/ mcp_gateway/
RUN pip install --no-cache-dir .

# Copy example config and scripts
COPY gateway_config.yaml .
COPY examples/ examples/

# The gateway reads from stdin and writes to stdout.
# Mount your own gateway_config.yaml to override.
ENTRYPOINT ["mcp-gateway", "gateway_config.yaml"]
