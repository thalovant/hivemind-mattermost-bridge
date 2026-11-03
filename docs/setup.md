Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Rewritten for bot accounts and access tokens instead of an email login.

# Setup walkthrough

A from-scratch guide for someone who has never made a Mattermost bot or
used HiveMind before.

## What you are building

```
Mattermost user  ⇄  Mattermost server  ⇄  hivemind-mattermost-bridge  ⇄  HiveMind hub  ⇄  OVOS skills
```

The bridge is a HiveMind satellite whose "microphone" is a Mattermost bot
account. A post addressed to the bot becomes a `recognizer_loop:utterance`
on your hub, and the hub's reply is posted back in the same channel (and
the same thread).

## 1. Create the bot account and its token

As a system admin on the Mattermost server:

1. System Console → Integrations → Bot Accounts: turn on **Enable Bot
   Account Creation**. Integrations → Integration Management: turn on
   **Enable Personal Access Tokens**.
2. Main menu → Integrations → Bot Accounts → **Add Bot Account**. Pick a
   username, for example `hivebot`. Mattermost shows the bot's first access
   token once: copy it. More tokens can be made later on the same page.
3. Add the bot to the team, and to every channel it should answer in
   (channel menu → Add Members). A bot only sees channels it is a member of.
4. For each of those channels, note the channel id: channel name → View Info
   (26 characters, like `6ru4o9giafff9fdm56kb5cqmje`).

Treat the token like a password. Pass it in a file or an environment
variable, never on a command line.

Or with the API, as an admin (`$ADMIN` is an admin's token):

```bash
curl -s -H "Authorization: Bearer $ADMIN" -d '{"username":"hivebot","display_name":"HiveBot"}' \
  https://chat.example.com/api/v4/bots                       # -> user_id
curl -s -H "Authorization: Bearer $ADMIN" -d '{"description":"bridge"}' \
  https://chat.example.com/api/v4/users/<user_id>/tokens     # -> token
```

## 2. Register the bridge on the hub

On the machine running `hivemind-core`:

```bash
hivemind-core add-client                                   # access key, password, client id
hivemind-core allow-msg recognizer_loop:utterance <client_id>
hivemind-core allow-msg speak <client_id>
```

Without `allow-msg` the bridge connects, forwards, and the hub silently
drops everything.

## 3. Run the bridge

```bash
pip install .
export MATTERMOST_URL=https://chat.example.com
export MATTERMOST_TOKEN_FILE=$HOME/.secrets/hivebot-token
export MATTERMOST_ALLOWED_CHANNELS=6ru4o9giafff9fdm56kb5cqmje
export HIVEMIND_HOST=ws://hub.example.com HIVEMIND_PORT=5678
export HIVEMIND_ACCESS_KEY=<key> HIVEMIND_PASSWORD_FILE=$HOME/.secrets/hivemind-password
export BRIDGE_HEALTH_PORT=8080
hivemind-mattermost-bridge
```

The log says `== connected to HiveMind`, `== logged in to Mattermost as
@hivebot` and `== listening to Mattermost`; `curl localhost:8080/readyz`
answers `ready`.

## 4. Verify the round trip

In an allowed channel, post `@hivebot what time is it?`. The bot answers
`@you It is ...`. Reply in the thread under its answer without mentioning
it: it answers in the thread. A post without the mention, a post in another
channel and, with channels listed, a DM get no answer.

If nothing comes back but the bridge is ready, check the `allow-msg` step
first.

## Troubleshooting

**The bridge exits with status 78 and `token_rejected`**: the server refused
the token (wrong, revoked, or the bot was disabled). Make a new token.

**`/readyz` stays 503**: either the hub session is down (the hub's address,
credentials, `allow-msg`), or the websocket cannot log in. The log names
which side is retrying.

**The hub refuses the bridge after its identity directory was wiped**: the
hub pinned the bridge's Noise key on first contact. Keep
`~/.config/hivemind` (or at least `unnamed-node_noise.key`) across restarts,
or clear the pin on the hub (`hivemind-core reset-noise-pin <client>`).

**A private CA**: set `REQUESTS_CA_BUNDLE` (or `SSL_CERT_FILE`) to the CA
file; it is used for both the REST calls and the websocket.
