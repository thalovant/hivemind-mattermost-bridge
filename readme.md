Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Bot-token client, settings from the environment and secret files, channel allow-list, DM policy, mention gating, per-person sessions, health endpoints, managed image.

# HiveMind Mattermost Bridge

This bridges a Mattermost bot account to a HiveMind node. A HiveMind bridge
is a satellite whose input and output are a chat platform instead of a
microphone: posts addressed to the bot become HiveMind utterances, and the
hub's spoken replies are posted back in the same channel, and in the same
thread when the question was asked in one.

![](./bridge.png)

New here? [docs/setup.md](docs/setup.md) walks through all of this from
scratch, including the `allow-msg` whitelist step below.

## A bot account and its token

On the Mattermost server (System Console → Integrations → Bot Accounts, with
bot account creation enabled), create a bot account and an access token for
it. Add the bot to the team and to every channel it should answer in. The
token is a password: anyone who has it can post as the bot. Do not commit it,
paste it into an issue, or put it on a command line.

The bridge needs the server's base URL (`https://chat.example.com`) and that
token. It reads its own `@username` and id from the token.

## Registering the bridge on the hub

Every HiveMind client needs credentials and, separately, permission to send
the message types it uses. On the machine running `hivemind-core`:

```bash
hivemind-core add-client
hivemind-core allow-msg recognizer_loop:utterance <client_id>
hivemind-core allow-msg speak <client_id>
```

`add-client` prints an access key, a password and the client id. Skipping
`allow-msg` is the most common reason a bridge "connects fine" but nothing
ever happens: the hub silently drops every message until the client is
whitelisted.

## Running the bridge

```bash
pip install .
export MATTERMOST_URL=https://chat.example.com
export MATTERMOST_TOKEN_FILE=/run/secrets/mattermost-token
export HIVEMIND_ACCESS_KEY_FILE=/run/secrets/hivemind-key
export HIVEMIND_PASSWORD_FILE=/run/secrets/hivemind-password
export HIVEMIND_HOST=ws://127.0.0.1
hivemind-mattermost-bridge
```

What it answers:

- **Channels.** With `MATTERMOST_ALLOWED_CHANNELS` (channel ids, the 26
  characters in the channel's "View Info"), only those channels are answered.
  Without it, any channel the bot is a member of.
- **Mentions.** Outside a one-to-one DM, only posts that mention the bot
  (`@hivebot what time is it`) or reply in a thread the bot started. The
  mention is removed, so the hub is asked `what time is it`.
  `MATTERMOST_REQUIRE_MENTION=false` forwards every post the bot can read.
- **Direct messages.** One-to-one and group DMs follow `MATTERMOST_DM_POLICY`:
  `auto` (the default) refuses them when channels are listed and accepts them
  otherwise; `allow` and `deny` say so. A group DM needs the mention, like a
  channel.
- **Never** posts by the bot itself, by other bots, webhooks or plugins (two
  bots would answer each other forever), or system posts.

Replies go to the question's channel; a question asked in a thread is
answered in that thread. Outside a one-to-one DM the reply starts with the
asker's `@username`.

Each person has their own conversation on the hub in each channel
(`MATTERMOST_SESSION_SCOPE=person`, the default; session id
`mattermost-<channel id>-<user id>`). `MATTERMOST_SESSION_SCOPE=chat` makes
each channel one conversation.

`BRIDGE_HEALTH_PORT` serves `/healthz` (200 while the process runs) and
`/readyz` (200 only while the HiveMind session is up and the Mattermost
websocket is open and logged in; 503 otherwise).

When the server refuses the token (HTTP 401, at start or later: revoked,
bot disabled), the bridge writes `token_rejected` to `/dev/termination-log`
and exits with status 78. Restarting cannot fix a token. A revoked token
does not close the websocket (the server just stops sending on it), so the
bridge also checks the token every 60 seconds.

## Every setting

Every flag falls back to an environment variable, so the bridge can run with
no secret on its command line, where any other process on the host could
read it. A flag wins over its variable, and the variable over its file.

| Flag | Variable | Default |
| --- | --- | --- |
| `--url` | `MATTERMOST_URL` (`https://` base URL; a bare host means HTTPS) | required |
| `--token` | `MATTERMOST_TOKEN` or `MATTERMOST_TOKEN_FILE` | required |
| `--allowed-channel` (repeatable) | `MATTERMOST_ALLOWED_CHANNELS` (ids, comma or space separated) | any channel |
| `--dm-policy` | `MATTERMOST_DM_POLICY` (`auto`, `allow`, `deny`) | `auto` |
| `--no-require-mention` | `MATTERMOST_REQUIRE_MENTION` (`true`/`false`) | `true` |
| `--session-scope` | `MATTERMOST_SESSION_SCOPE` (`person`/`chat`) | `person` |
| `--health-port` | `BRIDGE_HEALTH_PORT` | off |
| `--access-key` | `HIVEMIND_ACCESS_KEY` or `HIVEMIND_ACCESS_KEY_FILE` | identity file |
| `--password` | `HIVEMIND_PASSWORD` or `HIVEMIND_PASSWORD_FILE` | identity file |
| `--host` | `HIVEMIND_HOST` (`ws://` added if missing) | identity file |
| `--port` | `HIVEMIND_PORT` | `5678` |
| `--site-id` | `HIVEMIND_SITE_ID` | `mattermost` |
| `--self-signed` | `HIVEMIND_SELF_SIGNED` (`true`/`false`) | `false` |
| `--lang` | `HIVEMIND_LANG` | `en-us` |

A malformed value (a channel name instead of an id, `ture` for a boolean, an
empty channel list) is a usage error, never a silent default. The server's
certificate is verified against certifi's bundle, or `REQUESTS_CA_BUNDLE` /
`SSL_CERT_FILE` for a private CA.

## Docker

```bash
docker build -t hivemind-mattermost-bridge .
docker run --rm --read-only --tmpfs /tmp \
  -v bridge-identity:/home/bridge/.config/hivemind \
  -e MATTERMOST_URL=https://chat.example.com \
  -e MATTERMOST_TOKEN=... \
  -e HIVEMIND_ACCESS_KEY=... -e HIVEMIND_PASSWORD=... \
  -e HIVEMIND_HOST=ws://hivemind-core \
  hivemind-mattermost-bridge
```

The image runs as `bridge` (uid 10001) with `HOME=/home/bridge`, and its
entrypoint takes no arguments. It writes only `~/.config/hivemind` and
`/tmp`, so the root filesystem can be read-only. `~/.config/hivemind` holds
the HiveMind identity and the Noise static key
(`unnamed-node_noise.key`); keep it across restarts (a volume, or the key
mounted there), because the hub pins the key and refuses a new one.

## Testing

```bash
pip install -e ".[test,e2e]"
pytest tests/
tests/integration/run.sh        # the image against a real Mattermost server
```

`tests/` runs the bridge against a local Mattermost stand-in over real
sockets; `tests/e2e` adds a real loopback HiveMind hub;
`tests/integration` runs the built image against a real Mattermost server
(`mattermost/mattermost-preview`) and a real hub. See
[tests/integration/README.md](tests/integration/README.md).
