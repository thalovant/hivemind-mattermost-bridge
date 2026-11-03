Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Review: code map names the reply sender and the new helpers.

# hivemind-mattermost-bridge

A HiveMind bridge for Mattermost: a satellite whose input and output are a
Mattermost bot account instead of a microphone. Posts addressed to the bot
become `recognizer_loop:utterance` on the hub; `speak` replies go back to the
same channel, and the same thread when the question was in one.

- How to run it, every setting and its default: [README](../readme.md)
- From nothing to a working bot: [setup.md](setup.md)
- Every setting, what is forwarded, where replies go: [configuration.md](configuration.md)
- Running it as a managed container (files, probes, termination message): [operator-setup.md](operator-setup.md)
- Conversations and embedding: [examples.md](examples.md)
- The image against a real Mattermost server: [tests/integration/README.md](../tests/integration/README.md)
- Facts for lookup: [QUICK_FACTS.md](../QUICK_FACTS.md)
- Questions: [FAQ.md](../FAQ.md)
- Known issues: [AUDIT.md](../AUDIT.md)
- Proposed work: [SUGGESTIONS.md](../SUGGESTIONS.md)
- What changed and how it was verified: [MAINTENANCE_REPORT.md](../MAINTENANCE_REPORT.md)

## Code map

| File | What it holds |
| --- | --- |
| `mattermost_bridge/__init__.py` | `HiveMindMattermostBridge`: hub connection (`connect_hivemind`), the websocket loop (`_listen`, reconnect), `handle_posted` (allow-list, DM policy), `addressed_text` (mention gating, bot threads), `session_id`, `ready`, `may_answer` (allow-list both ways), the reply path (`_queue_reply`, the `_send_replies` thread, `speak`, `_post` with the 429 retry) |
| `mattermost_bridge/mattermost.py` | `MattermostAPI` (REST v4 with the bot token), `TokenRejected`, `RateLimited`, `parse_posted`, `mention_pattern`, `defuse_reply`, `split_post`, `normalise_url`, `is_cleartext`, `websocket_host`, `ca_bundle` |
| `mattermost_bridge/__main__.py` | `resolve_options` (flags, environment, `*_FILE` secrets), `write_termination_message`, `stop_on_sigterm`, `main` |
| `mattermost_bridge/health.py` | `HealthServer`: `/healthz` and `/readyz` |
| `Dockerfile` | the image: uid 10001, `HOME=/home/bridge`, entrypoint without arguments, read-only root |
| `.github/workflows/ci.yml` | tests (3.12, 3.14), lint, image smoke test, the real-server integration run, signed publish to `ghcr.io/thalovant/hivemind-mattermost-bridge` from `main` |
| `tests/fake_mattermost.py` | a local Mattermost stand-in (REST and websocket over real sockets) the tests run against |
| `tests/e2e/` | the bridge with a real loopback hivemind-core hub |
| `tests/integration/` | the built image with a real Mattermost server and a real hub |

Related: the HiveMind hub ([HiveMind-core](https://github.com/JarbasHiveMind/HiveMind-core)),
the client library ([hivemind-bus-client](https://github.com/JarbasHiveMind/hivemind-bus-client)),
and the sibling bridge this one follows, `thalovant/hivemind-telegram-bridge`
(`hivemind_telegram_bridge/__init__.py`, same lifecycle and settings shape).
