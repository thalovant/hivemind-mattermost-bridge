FROM python:3.14-slim

WORKDIR /app
COPY . /app

# force the current hivemind-bus-client alpha rather than whatever a stale
# base layer might already have cached, since this bridge depends on the
# bounded handshake and the current identity/Noise API
RUN pip install --no-cache-dir --upgrade "hivemind-bus-client>=1.0.16a1" \
    && pip install --no-cache-dir . \
    && rm -rf /app

# The bridge reads its settings from the environment (MATTERMOST_URL,
# MATTERMOST_TOKEN, HIVEMIND_ACCESS_KEY, HIVEMIND_PASSWORD, HIVEMIND_HOST,
# ...), or a secret from the file named by <VARIABLE>_FILE, so no secret is
# ever passed on the command line where other processes can read it.
# Nothing secret is baked into the image.
ENV HOME=/home/bridge \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HIVEMIND_HOST=ws://127.0.0.1 \
    HIVEMIND_PORT=5678 \
    HIVEMIND_SITE_ID=mattermost \
    HIVEMIND_LANG=en-us

# An unprivileged user. Its home holds the HiveMind identity
# (~/.config/hivemind, with the Noise static key as unnamed-node_noise.key),
# which must persist across restarts: mount a volume or the key there, or the
# hub sees a new identity key on every start. Nothing else is written, so the
# root filesystem can be read-only with only that directory and /tmp writable.
RUN useradd --create-home --uid 10001 --user-group bridge \
    && mkdir -p /home/bridge/.config/hivemind \
    && chown -R bridge:bridge /home/bridge
USER bridge
WORKDIR /home/bridge

ENTRYPOINT ["hivemind-mattermost-bridge"]
