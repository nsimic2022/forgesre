## What and why

## How it was tested
- [ ] `pytest` passes
- [ ] for `web/` changes: `npm run typecheck && npm test && npm run build`
- [ ] new behaviour has a test

## Checklist ([AGENTS.md](../AGENTS.md))
- [ ] calls to other systems have a timeout; slow work runs as a background job
- [ ] permissions are checked on the server for every write
- [ ] install / update / doctor / `./forgesre` updated if a setting, container, port, secret or column changed
- [ ] docs updated in the same pull request
