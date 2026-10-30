# Pinned version — Playwright MCP image

This file is the one place that documents the exact image this deployment has been tested with, separate from
`docker-compose.yml` so updating the tag or re-pinning the digest is a one-line, reviewable diff.

## Current pin

| Field | Value |
|---|---|
| Image | `mcr.microsoft.com/playwright/mcp` |
| Tag | `v0.0.83` |
| Digest | *(not pinned yet — see below)* |
| Checked | not yet deployed to a real Pi; tag picked from `mcr.microsoft.com/v2/playwright/mcp/tags/list` at the time this file was written |

## Why pin by digest, not just by tag

A tag (`v0.0.83`) is a mutable pointer: Microsoft Container Registry *could* repoint it (accidentally or not), and
`docker compose pull` would then fetch different bytes under the same name without anyone reviewing it. A digest
(`sha256:...`) is immutable — it names these exact bytes forever. For a container that gets public, untrusted
pages fed into it, that immutability matters more than usual.

## How to pin the digest (do this before a real deployment, not just a local try-out)

```bash
# 1. Pull the tag you intend to run, after reading that release's notes on
#    https://github.com/microsoft/playwright-mcp/releases
docker pull mcr.microsoft.com/playwright/mcp:v0.0.83

# 2. Resolve the digest docker actually pulled
docker inspect --format='{{index .RepoDigests 0}}' mcr.microsoft.com/playwright/mcp:v0.0.83
# -> mcr.microsoft.com/playwright/mcp@sha256:<...>

# 3. Put that full "name@sha256:..." string in docker-compose.yml's `image:` line (replacing the tag), and update
#    the table above with the digest and today's date.
image: mcr.microsoft.com/playwright/mcp@sha256:<...>
```

## Bumping the version later

1. Read the new release's notes for anything security-relevant (new flags, CVE fixes, changed defaults).
2. Repeat the steps above for the new tag.
3. Re-run `uv run pytest -q tests/test_browser_mcp.py` — it exercises `aecom.browser_guard`, not the real
   container, so a version bump only needs a new entry here plus a human smoke-test of the container itself.
4. Update this file's table (tag, digest, date, who checked).
