Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Thalovant fork set up: bot-token client, managed image, CI with signed images and a real-server integration run, docs set.

# Quick facts: hivemind-mattermost-bridge

| Key | Value |
| --- | --- |
| Package (distribution) | `HiveMind-mattermost-bridge` |
| Import name | `mattermost_bridge` |
| Version | `0.3.0` (`mattermost_bridge/version.py`) |
| Console entry point | `hivemind-mattermost-bridge` -> `mattermost_bridge.__main__:main` |
| Main class | `mattermost_bridge.HiveMindMattermostBridge` |
| Mattermost client | `mattermost_bridge.mattermost.MattermostAPI` (REST v4, bot token), websocket in `HiveMindMattermostBridge._listen` |
| Health server | `mattermost_bridge.health.HealthServer` (`/healthz`, `/readyz`) |
| Image | `ghcr.io/thalovant/hivemind-mattermost-bridge` (`:sha-<commit>` and `:latest`, signed by digest with cosign keyless) |
| Image user | `bridge`, uid 10001, `HOME=/home/bridge`; identity and Noise key in `/home/bridge/.config/hivemind` (`unnamed-node_noise.key`) |
| Writable paths | `/home/bridge/.config/hivemind`, `/tmp` (root filesystem read-only) |
| Exit statuses | 0 stopped, 1 hub refused/handshake timed out, 2 bad setting, 78 token refused (`token_rejected` in `/dev/termination-log`) |
| Default branch | `main` (upstream: `JarbasHiveMind/HiveMind_mattermost_bridge`, branch `dev`) |
| CI | `.github/workflows/ci.yml`: tests on Python 3.12 and 3.14, ruff `F,E9`, image smoke test, real-server integration, publish on `main` |
| Tests | `tests/`: 123 tests including 2 e2e with a real hub (`python -m pytest tests -q`); `tests/integration/run.sh`: 21 checks against a real Mattermost server |
| Token check | `GET /api/v4/users/me` every 60 s (`TOKEN_CHECK_INTERVAL`): a revoked token does not close the websocket |
| HiveMind message types | sends `recognizer_loop:utterance`; handles `speak`, `hive.complete_intent_failure` |
| Mattermost API used | `GET /api/v4/users/me`, `GET /api/v4/posts/{id}`, `POST /api/v4/posts`, websocket `/api/v4/websocket` (`authentication_challenge`, `hello`, `posted`) |
| Runtime dependencies | `hivemind-bus-client>=1.0.16a1`, `ovos-utils`, `ovos-bus-client`, `websockets>=13.0`, `requests` |
| License | Apache-2.0 |

## Settings

| Flag | Environment variable | Default |
| --- | --- | --- |
| `--url` | `MATTERMOST_URL` | required |
| `--token` | `MATTERMOST_TOKEN` (or `MATTERMOST_TOKEN_FILE`) | required |
| `--allowed-channel` | `MATTERMOST_ALLOWED_CHANNELS` (26-character ids, comma/space separated) | any channel |
| `--dm-policy` | `MATTERMOST_DM_POLICY` (`auto`: deny when channels are listed; `allow`; `deny`) | `auto` |
| `--no-require-mention` | `MATTERMOST_REQUIRE_MENTION` | `true` |
| `--session-scope` | `MATTERMOST_SESSION_SCOPE` (`person` or `chat`) | `person` |
| `--health-port` | `BRIDGE_HEALTH_PORT` | off |
| `--access-key` (`--key`) | `HIVEMIND_ACCESS_KEY` (or `_FILE`) | identity file |
| `--password` | `HIVEMIND_PASSWORD` (or `_FILE`) | identity file |
| `--host`, `--port` | `HIVEMIND_HOST`, `HIVEMIND_PORT` | identity file, `5678` |
| `--site-id`, `--lang`, `--self-signed` | `HIVEMIND_SITE_ID`, `HIVEMIND_LANG`, `HIVEMIND_SELF_SIGNED` | `mattermost`, `en-us`, `false` |

Session id: `mattermost-<channel id>-<user id>` (`person`) or `mattermost-<channel id>` (`chat`).
