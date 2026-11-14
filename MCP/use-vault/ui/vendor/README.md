# vendor

`ext-apps-app-with-deps.js` is the prebuilt browser client of
[`@modelcontextprotocol/ext-apps`](https://github.com/modelcontextprotocol/ext-apps)
**1.7.5** (`dist/src/app-with-deps.js`, MIT per the npm package metadata; see
`ext-apps-LICENSE`). It is inlined into each app's HTML by `build.py` because an MCP App
runs under a strict CSP and cannot load it from a CDN.
