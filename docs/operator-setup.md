Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Rewritten as the container contract: files, environment, probes, termination message; a local server for trying it.

# Running the bridge as a managed container

This is the shape the image is built for: one replica per bot, every
setting from the environment, every secret from a read-only file, a
read-only root filesystem. `tests/integration/run.sh` runs exactly this
against a real Mattermost server.

## The container

| Item | Value |
| --- | --- |
| Image | `ghcr.io/thalovant/hivemind-mattermost-bridge:sha-<commit>` (signed by digest; `:latest` follows `main`) |
| User | `bridge`, uid/gid 10001; `runAsNonRoot` works |
| Root filesystem | read-only |
| Writable | `/home/bridge/.config/hivemind` (an emptyDir is enough when the Noise key is mounted) and `/tmp` |
| Command | the entrypoint, no arguments |
| Capabilities | none needed (`drop: [ALL]`, no privilege escalation) |
| Network | outbound only: the hub's websocket and the Mattermost server (HTTPS and its websocket on the same host) |
| Replicas | one: two copies of one bot would both answer (use `Recreate`) |

## Files

| Path | Holds |
| --- | --- |
| `/home/bridge/.config/hivemind/unnamed-node_noise.key` | the bridge's Noise static key, 64 hex characters, read-only. The hub pins it on first contact, so it must be the same on every start; mount the directory first and the key file inside it, or the directory comes out root-owned. |
| `HIVEMIND_ACCESS_KEY_FILE`, `HIVEMIND_PASSWORD_FILE` | the hub credentials |
| `MATTERMOST_TOKEN_FILE` | the bot's access token |
| `/dev/termination-log` | written by the bridge: `token_rejected` when the server refuses the token |

The bridge reads secret files once, at start: a rotated token needs a
restart (roll the pod on a change of the token's digest).

## Environment

```
HOME=/home/bridge
HIVEMIND_HOST=ws://<hub service>      HIVEMIND_PORT=<websocket port>
HIVEMIND_SITE_ID=<bridge name>
HIVEMIND_ACCESS_KEY_FILE=...          HIVEMIND_PASSWORD_FILE=...
MATTERMOST_URL=https://chat.example.com
MATTERMOST_TOKEN_FILE=...
MATTERMOST_ALLOWED_CHANNELS=<channel id>[,<channel id>...]
MATTERMOST_DM_POLICY=auto             # auto | allow | deny (auto denies when channels are listed)
MATTERMOST_REQUIRE_MENTION=true       # true | false
MATTERMOST_SESSION_SCOPE=person       # person | chat
BRIDGE_HEALTH_PORT=8080
```

No variable needs to carry a secret by value. Channel ids are 26 lowercase
letters and digits; anything else (a channel name, an empty list) stops the
bridge at start with a usage error (exit status 2).

## Probes and exits

- liveness `GET /healthz` (200 while the process answers);
- readiness `GET /readyz` (200 only while the HiveMind session is live and
  the Mattermost websocket is open and logged in, 503 otherwise).

| Exit status | Meaning |
| --- | --- |
| 0 | stopped (SIGTERM, Ctrl-C) |
| 1 | the hub refused the credentials or never completed the handshake (10 bounded retries, about a minute) |
| 2 | a setting is missing or malformed (the message names the variable) |
| 78 | the Mattermost server refused the token; `/dev/termination-log` says `token_rejected` |

## On the Mattermost server

- Bot account creation and personal access tokens enabled.
- The bot is a member of the team and of each allowed channel; it sees
  nothing else (and DMs, which the DM policy decides).
- Nothing else: no webhooks, no slash commands, no admin rights.

## Trying it locally

```bash
docker run -d --name mattermost -p 127.0.0.1:8065:8065 mattermost/mattermost-preview
```

Open `http://localhost:8065`, create the first (admin) account and a team,
then follow [setup.md](setup.md) with `MATTERMOST_URL=http://localhost:8065`.
`tests/integration/real_mattermost.py` (`set_scene`) does the same through
the API.
