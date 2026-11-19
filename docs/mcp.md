# Read-only MCP server

ForgeSRE can answer questions from an AI client that speaks the Model Context Protocol
(Claude, Claude Code, Cursor and others): "what is burning right now?", "explain INC-0412",
"what sits behind sw-acc-07?", "how much error budget is left?".

## Before you turn it on

**Data leaves the appliance.** The client sends tool results to the model it uses. If that
is a cloud model, incident titles, hostnames, ForgeRCA facts and notes go there. Turn this
on only when that is acceptable for your organisation. To limit what leaves, tool results
leave out evidence queries, notification bodies, people's email addresses and phone numbers,
and any secret.

Every tool only reads. There is no tool that acknowledges, edits or deletes anything.

## Turn it on

```bash
./forgesre settings set mcp.enabled true
grep MCP_TOKEN secrets/secrets.env          # install and update generate it
./forgesre settings set system.public_url https://forgesre.dc.local   # if not set yet
./forgesre doctor                           # MCP row: ok
```

The endpoint is `https://<forgesre>/mcp/` (Streamable HTTP, stateless, JSON responses). Every
request needs `Authorization: Bearer <MCP_TOKEN>`. A browser `Origin` other than
`system.public_url` is refused. While `mcp.enabled` is false, `/mcp` answers 404.

Claude Code, for example:

```bash
claude mcp add --transport http forgesre https://forgesre.dc.local/mcp/ \
  --header "Authorization: Bearer $MCP_TOKEN"
```

To revoke access, set a new token (`./forgesre settings set-secret MCP_TOKEN`) or turn
`mcp.enabled` off.

## Tools

| Tool | Answers |
|---|---|
| `noc_overview` | Counts, active and acknowledged incidents, health by asset group |
| `list_incidents` | Incident list by status (active, acknowledged, resolved, all), severity, search |
| `get_incident` | One incident: ForgeRCA facts and hypotheses, ForgeAI text, timeline, notes, what changed before, playbook, review |
| `search_assets` | Inventory by hostname, IP, type or owner; views down, alarm, no_source, no_owner |
| `asset_dependencies` | Parents up to the root and everything behind an asset |
| `on_call_now` | Name and window of the person on call |
| `slo_status` | SLO availability, error budget, burn rate |
| `checks_status` | Certificate expiry, ForgeSRE backup age, heartbeats |

Built on the official MCP Python SDK, which negotiates the protocol version with the client.
