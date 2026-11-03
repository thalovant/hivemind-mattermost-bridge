Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Examples for the bot-token client; the standalone echo bot is gone with MMostBot.

# Examples

## Run the bridge

```bash
MATTERMOST_URL=https://chat.example.com \
MATTERMOST_TOKEN_FILE=/run/secrets/hivebot-token \
MATTERMOST_ALLOWED_CHANNELS=6ru4o9giafff9fdm56kb5cqmje \
HIVEMIND_HOST=ws://127.0.0.1 HIVEMIND_ACCESS_KEY=... HIVEMIND_PASSWORD_FILE=/run/secrets/hm-password \
hivemind-mattermost-bridge
```

## Embed it

```python
from mattermost_bridge import HiveMindMattermostBridge

bridge = HiveMindMattermostBridge(
    url="https://chat.example.com",
    token=open("/run/secrets/hivebot-token").read().strip(),
    key="your-access-key",
    password="your-hivemind-password",
    host="ws://127.0.0.1",
    port=5678,
    allowed_channels=["6ru4o9giafff9fdm56kb5cqmje"],
    health_port=8080,
)
bridge.start()   # blocks; raises mattermost_bridge.TokenRejected on a refused token
```

## Conversations

In an allowed channel:

```
alice>  @hivebot what time is it?
hivebot> @alice It is half past three.

alice>  lunch at noon?                         (not for the bot: no answer)

   └─ alice (in the thread under the bot's answer)> and in Paris?
      hivebot> @alice It is half past nine.
```

In a thread someone else started, the bot needs the mention, and answers in
the thread:

```
bob>    planning the offsite
   └─ alice> @hivebot what's the weather on Friday?
      hivebot> @alice Sunny, 21 degrees.
```

In a one-to-one DM (when the DM policy allows them), no mention and no
`@username` prefix:

```
alice (DM)>   what time is it?
hivebot (DM)> It is half past three.
```

With `MATTERMOST_ALLOWED_CHANNELS` set and the DM policy at `auto`, that DM
gets no answer at all.
