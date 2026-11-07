# Development Guide

Guide for maintainers setting up the project for the first time.

## Initial Git Setup

```bash
# 1. Create the repo on GitHub (e.g. mcp-gw) via the browser

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
git remote add origin https://github.com/YOUR_USERNAME/mcp-gw.git
# or: git remote add origin git@github.com:YOUR_USERNAME/mcp-gw.git

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

## Release Checklist

- [ ] All tests pass: `python -m pytest tests/ -v`
- [ ] Lint passes: `ruff check mcp_baldr/ tests/ examples/ scripts/`
- [ ] Version bumped in `pyproject.toml`
- [ ] `README.md` and `DEVELOPMENT.md` are up to date
- [ ] Commit and tag: `git tag v0.x.0 && git push --tags`
