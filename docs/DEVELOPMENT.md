# Development Guide

Guide for maintainers setting up the project for the first time.

## Initial Git Setup

```bash
# 1. Create the repo on GitHub (e.g. mcp-baldr) via the browser

# 2. Initialize the local repo
git init

# 3. Set your identity (if not already configured globally)
git config --global --list
git config --global user.email "you@example.com"
git config --global user.name "Your Name"

# 4. Stage and commit everything
git branch -m main
git add .
git commit -m "initial commit"

# 5. Connect to GitHub and push
# Use HTTPS or SSH depending on your setup:
git remote add origin https://github.com/YOUR_USERNAME/mcp-baldr.git
# or: git remote add origin git@github.com:YOUR_USERNAME/mcp-baldr.git

git push -u origin main
```

## Dev Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Daily Workflow

```bash
# Lint
ruff check mcp_baldr/ tests/ examples/ scripts/
ruff format mcp_baldr/ tests/ examples/ scripts/

# Test
python -m pytest tests/ -v

# Quick verification
python scripts/quick_e2e.py
python scripts/verify_multi_server.py
python scripts/example_client.py gateway_config.yaml
```

## Project Structure

```
mcp-baldr/
├── .github/
│   └── workflows/
│       └── ci.yml             # GitHub Actions: lint + test on push/PR
├── mcp_baldr/
│   ├── config/
│   │   ├── models.py          # Pydantic config (servers, logging, learning, pagination)
│   │   └── loader.py          # YAML parser + validation
│   ├── protocol/
│   │   ├── uri.py             # Reversible namespaced resource URIs
│   │   └── pagination.py      # Opaque, stable cursor pagination
│   ├── transports/
│   │   └── stdio.py           # StdioTransport: subprocess bridge + crash monitoring
│   ├── routing/
│   │   └── router.py          # RequestRouter: method → handler dispatch
│   ├── registry/
│   │   ├── base_registry.py   # Generic namespaced-registry mechanics
│   │   ├── tool_registry.py   # Tool aggregation
│   │   ├── resource_registry.py  # Resource + template aggregation
│   │   ├── prompt_registry.py    # Prompt aggregation
│   │   └── capability_registry.py # Downstream caps + honest upstream aggregate
│   ├── gateway/
│   │   ├── server.py          # Gateway: MCP server loop + routing + relaying
│   │   └── subscription_manager.py # Resource-subscription tracking
│   ├── educational/
│   │   └── inspector.py       # ProtocolInspector: educational stderr traces
│   ├── learning/              # Learning Lab: events, curriculum, redaction, progress
│   └── logging_utils.py       # Structured JSON logging
├── examples/
│   ├── mock_server.py               # Tools-only mock MCP server
│   ├── full_mock_server.py          # Capability-complete mock (all protocol surfaces)
│   ├── slow_server.py               # Slow MCP server for timeout testing
│   └── learning_network_config.yaml  # Local + live search config
├── tests/                      # 16 test files (see README "Testing" section)
├── scripts/
│   ├── quick_e2e.py            # Single-server verification
│   ├── verify_multi_server.py  # Multi-server routing verification
│   ├── example_client.py      # Example MCP client connecting to the gateway
│   └── learn_traffic_init.py   # Educational traffic initiation (offline/online)
├── docs/
│   └── LEARNING.md             # Educational guide: inspection + traffic initiation
├── CHANGELOG.md                # Development milestones by phase
├── Dockerfile                  # Containerized deployment
├── gateway_config.yaml         # Default configuration (two mock servers)
├── enhanced_config.yaml        # Enhanced educational tool set config
├── LICENSE
└── pyproject.toml
```

## Release Checklist

- [ ] All tests pass: `python -m pytest tests/ -v`
- [ ] Lint passes: `ruff check mcp_baldr/ tests/ examples/ scripts/`
- [ ] Version bumped in `pyproject.toml`
- [ ] `README.md` and `DEVELOPMENT.md` are up to date
- [ ] Commit and tag: `git tag v0.x.0 && git push --tags`
