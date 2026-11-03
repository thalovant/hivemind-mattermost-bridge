Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: First suggestions for the Thalovant fork.

# Suggestions: hivemind-mattermost-bridge

## Resume the websocket after a gap

- **Problem:** AUDIT.md section 5: posts made while the websocket is down are never seen.
- **Proposal:** keep the `connection_id` from `hello` and the last `seq`, and reconnect with `?connection_id=...&sequence_number=...` so the server replays what was missed (check which server versions support the resume before relying on it).
- **Impact:** medium; a server restart no longer loses questions.

## Reply off the bus thread

- **Problem:** AUDIT.md sections 3 and 4.
- **Proposal:** hand replies to the asyncio loop (`run_coroutine_threadsafe` + `asyncio.to_thread`) and run thread lookups as tasks, so neither the bus thread nor the websocket reader waits on REST.
- **Impact:** medium.

## Split long replies

- **Problem:** AUDIT.md section 6.
- **Proposal:** learn the server's maximum post size at start (the client configuration API) and post a long reply as several posts in the same thread.
- **Impact:** low; long answers are rare.

## Per-person rate limit in the bridge

- **Problem:** AUDIT.md section 2.
- **Proposal:** a token bucket per (channel, person) in `forward_to_hivemind`, with a short "slow down" reply, configurable by flag and environment; useful where the hub has no quota of its own.
- **Impact:** medium for self-hosted use; the managed platform already limits per person on the hub.

## Localised fallback line

- **Problem:** AUDIT.md section 7.
- **Proposal:** a small per-language table keyed by `HIVEMIND_LANG`, or let the hub supply it.
- **Impact:** low.

## Reproducible images

- **Problem:** AUDIT.md section 11.
- **Proposal:** pin the base image by digest and build from a constraints file generated in CI; Renovate updates both (and the `mattermost-preview` digest in `ci.yml`).
- **Impact:** medium; makes a rollback mean something.

## Platform integration

- **Proposal:** run the image as the managed bridge workload, like the Telegram and Discord forks: the network needs `MATTERMOST_URL` beside the token, channel ids that are 26-character strings (not numbers), and `MATTERMOST_DM_POLICY` passed through when a deployment wants DMs.
- **Impact:** high; this is what the fork is for.
