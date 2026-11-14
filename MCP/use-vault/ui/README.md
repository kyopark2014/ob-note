# use-vault UI (MCP App)

`src/vault-viewer.{js,css}` is the interactive view for the read tools of this server
(`vault_search`, `vault_read`, `vault_list`, `vault_tree`, `vault_graph`, `vault_backlinks`),
served as the `ui://vault/viewer.html` resource.

    python3 MCP/use-vault/ui/build.py     # -> ui/dist/vault-viewer.html (commit the output)

`src/kit.{js,css}` and `vendor/` are copied from `agentic-work/application/mcp_app_views`
(shared MCP Apps UI kit + the official `@modelcontextprotocol/ext-apps` browser client).
Design follows Claude's MCP Apps guidelines: inline carousel/card, fullscreen explorer,
text entry stays in chat, host style tokens, no menus.
