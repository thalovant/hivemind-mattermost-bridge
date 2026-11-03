Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Thalovant fork set up: bot-token client, DM policy, mention gating, managed image.

# FAQ: hivemind-mattermost-bridge

## Why does the bot ignore some posts in a channel?

Outside a one-to-one DM, `HiveMindMattermostBridge.addressed_text` forwards only posts that mention `@botname` or that reply in a thread the bot started, and removes the mention first. `MATTERMOST_REQUIRE_MENTION=false` forwards every post the bot can read. Posts in channels not in `MATTERMOST_ALLOWED_CHANNELS`, posts by bots, webhooks and plugins, and system posts are never forwarded (`handle_posted`).

## Why does the bot not answer my DM?

`MATTERMOST_DM_POLICY` decides one-to-one and group DMs. Its default, `auto`, refuses them when `MATTERMOST_ALLOWED_CHANNELS` is set, so a restricted bot cannot be reached around its channel list. Set `allow` to accept DMs as well. A group DM also needs the mention.

## Where does the reply go?

To the question's channel, and into its thread when the question was in one (`root_id` in the utterance context). Outside one-to-one DMs it starts with the asker's `@username` (`HiveMindMattermostBridge.speak`).

## Does it log email login or password, like upstream?

No. Upstream's `--mail`/`--pswd` login is gone; the bridge uses a bot access token (`MATTERMOST_TOKEN`/`_FILE`), and never logs it, nor any question or reply (`tests/test_logs.py`). The websocket library's frame logging is pinned off (`_WEBSOCKET_LOG`).

## What does exit status 78 mean?

The Mattermost server answered HTTP 401: the token is wrong, revoked, or the bot is disabled. The bridge writes `token_rejected` to `/dev/termination-log` and stops (`mattermost_bridge/__main__.py`, `main`). Network failures are retried instead.

## What does `/readyz` check?

That the HiveMind session is live (the client's `handshake_event`) and that the Mattermost websocket is open and has received `hello` (`HiveMindMattermostBridge.ready`). `/healthz` only says the process answers.

## Why must the Noise key stay the same?

The hub pins the bridge's Noise static key (`~/.config/hivemind/unnamed-node_noise.key`) on first contact and refuses a different one; `tests/integration/run.sh` saw exactly that when a run started a second container with a new key. Mount the kept key there.

## Which channel ids go in `MATTERMOST_ALLOWED_CHANNELS`?

The 26-character channel ids (channel name → View Info), not channel names. A name, an uppercase id or an empty list stops the bridge with a usage error.

## Why is the default branch `main` and not `dev`?

This is the Thalovant fork. Upstream's release workflows were removed, so nothing here publishes to PyPI or to upstream's registry; images go to `ghcr.io/thalovant/hivemind-mattermost-bridge`.
