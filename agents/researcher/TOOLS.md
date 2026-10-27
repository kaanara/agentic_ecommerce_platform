---
tools: [fetch_url, submit_brief, propose_tool, list_products, list_decisions, list_registry, read_company_doc, recall_memory, remember, request_account_need]
server_tools: [web_search, web_fetch]
---
# Researcher tools
Anthropic server-side `web_search` and `web_fetch` (live mode; pages are fetched on Anthropic's side, not from our host), plus `fetch_url` as a hardened fallback. All return untrusted content. May propose new MCP servers or skills, but can't approve them.
