"""CLI entry point for the HiveMind <-> Mattermost bridge.

HiveMind identity (key/password/host/port) defaults to the values stored
by ``hivemind-client set-identity``; flags override them.

Every option can also come from the environment, so a container never has
to put a secret on its command line, where any process on the host can
read it. Secrets additionally accept a ``*_FILE`` variable naming a file
that holds the value, the convention for secrets mounted into a container.
A flag wins over its environment variable, and the variable over its file.

When the Mattermost server refuses the bot's access token, the bridge
writes ``token_rejected`` to ``/dev/termination-log`` (where Kubernetes
reads a container's termination message) and exits with status 78
(``EX_CONFIG``): restarting cannot fix a token, a person has to.
"""
import argparse
import os
import signal
from typing import Mapping, Optional, Sequence

from ovos_utils.log import LOG

from mattermost_bridge import DM_POLICIES, SESSION_SCOPES, HiveMindMattermostBridge
from mattermost_bridge.mattermost import ID_PATTERN, TokenRejected, is_cleartext, normalise_url

#: option -> environment variable it falls back to.
ENVIRONMENT = {
    "url": "MATTERMOST_URL",
    "token": "MATTERMOST_TOKEN",
    "allowed_channels": "MATTERMOST_ALLOWED_CHANNELS",
    "dm_policy": "MATTERMOST_DM_POLICY",
    "require_mention": "MATTERMOST_REQUIRE_MENTION",
    "session_scope": "MATTERMOST_SESSION_SCOPE",
    "health_port": "BRIDGE_HEALTH_PORT",
    "key": "HIVEMIND_ACCESS_KEY",
    "password": "HIVEMIND_PASSWORD",
    "host": "HIVEMIND_HOST",
    "port": "HIVEMIND_PORT",
    "site_id": "HIVEMIND_SITE_ID",
    "self_signed": "HIVEMIND_SELF_SIGNED",
    "lang": "HIVEMIND_LANG",
}
#: options whose value may be read from the file named by ``<VARIABLE>_FILE``.
SECRETS = ("token", "key", "password")

#: Where Kubernetes reads a container's termination message.
TERMINATION_LOG = "/dev/termination-log"
#: The termination message for a refused Mattermost token.
TOKEN_REJECTED = "token_rejected"
#: ``EX_CONFIG`` from sysexits.h: the configuration is wrong, not the process.
TOKEN_REJECTED_EXIT_CODE = 78

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def connect_mattermost_to_hivemind(url: str, token: str, key: Optional[str] = None,
                                   password: Optional[str] = None,
                                   host: Optional[str] = None, port: int = 5678,
                                   self_signed: bool = False, lang: str = "en-us",
                                   site_id: str = "mattermost",
                                   allowed_channels: Optional[Sequence[str]] = None,
                                   dm_policy: str = "auto", require_mention: bool = True,
                                   session_scope: str = "person",
                                   health_port: Optional[int] = None) -> HiveMindMattermostBridge:
    """Build the bridge and run it; returns when it stops."""
    bridge = HiveMindMattermostBridge(
        url=url, token=token, key=key, password=password, host=host, port=port,
        self_signed=self_signed, lang=lang, site_id=site_id,
        allowed_channels=allowed_channels, dm_policy=dm_policy,
        require_mention=require_mention, session_scope=session_scope,
        health_port=health_port,
    )
    bridge.start()
    return bridge


def build_parser() -> argparse.ArgumentParser:
    """The command line: every option, with the variable it falls back to."""
    parser = argparse.ArgumentParser(
        description="Bridge a Mattermost bot account to a HiveMind node",
        epilog="Every option falls back to an environment variable "
               "(MATTERMOST_URL, MATTERMOST_TOKEN, MATTERMOST_ALLOWED_CHANNELS, "
               "MATTERMOST_DM_POLICY, MATTERMOST_REQUIRE_MENTION, "
               "MATTERMOST_SESSION_SCOPE, BRIDGE_HEALTH_PORT, "
               "HIVEMIND_ACCESS_KEY, HIVEMIND_PASSWORD, HIVEMIND_HOST, "
               "HIVEMIND_PORT, HIVEMIND_SITE_ID, HIVEMIND_SELF_SIGNED, "
               "HIVEMIND_LANG). The token, access key and password also "
               "accept <VARIABLE>_FILE naming a file that holds the value.")
    # Mattermost
    parser.add_argument("--url", default=None,
                        help="the Mattermost server's base URL, e.g. https://chat.example.com")
    parser.add_argument("--token", default=None,
                        help="the bot account's access token")
    parser.add_argument("--allowed-channel", dest="allowed_channels",
                        action="append", default=None,
                        help="Mattermost channel id (26 characters) the bridge answers in "
                             "(repeatable); default: any channel the bot is in")
    parser.add_argument("--dm-policy", choices=DM_POLICIES, default=None,
                        help="direct and group-direct messages: auto (the default) refuses "
                             "them when channels are listed and accepts them otherwise")
    parser.add_argument("--no-require-mention", dest="require_mention",
                        action="store_false", default=None,
                        help="forward every post, not only those that mention the bot "
                             "or reply in a thread it started")
    parser.add_argument("--session-scope", choices=SESSION_SCOPES, default=None,
                        help="one hub conversation per person in each channel "
                             "(default) or per channel")
    parser.add_argument("--health-port", type=int, default=None,
                        help="serve /healthz and /readyz on this port (default: off)")
    # HiveMind
    parser.add_argument("--access-key", "--key", dest="key", default=None,
                        help="HiveMind access key (default: from identity file)")
    parser.add_argument("--password", default=None,
                        help="HiveMind password (default: from identity file)")
    parser.add_argument("--host", default=None,
                        help="HiveMind host, e.g. ws://127.0.0.1 (default: from identity file)")
    parser.add_argument("--port", type=int, default=None,
                        help="HiveMind port (default: 5678)")
    parser.add_argument("--site-id", default=None,
                        help="this bridge's HiveMind site id (default: mattermost)")
    parser.add_argument("--self-signed", action="store_true", default=None,
                        help="accept self-signed SSL certificates from the hub")
    parser.add_argument("--lang", default=None, help="utterance language (default: en-us)")
    return parser


def _read_secret_file(path: str, variable: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError as error:
        raise SystemExit(f"{variable}: cannot read {path}: {error.strerror}") from None


def _from_environment(option: str, environ: Mapping[str, str]) -> Optional[str]:
    variable = ENVIRONMENT[option]
    value = environ.get(variable)
    if value:
        return value
    if option in SECRETS:
        path = environ.get(f"{variable}_FILE")
        if path:
            return _read_secret_file(path, f"{variable}_FILE")
    return None


def resolve_options(argv: Optional[Sequence[str]] = None,
                    environ: Optional[Mapping[str, str]] = None) -> dict:
    """The bridge's settings: flags first, then the environment, then defaults.

    Raises ``SystemExit`` with a usage message when the server URL or the
    token is missing, or when a setting is malformed.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ

    def pick(option: str) -> Optional[str]:
        given = getattr(args, option)
        return given if given is not None else _from_environment(option, environ)

    def boolean(option: str, default: bool) -> bool:
        variable = ENVIRONMENT[option]
        setting = str(environ.get(variable, "")).strip().lower()
        if not setting:
            return default
        if setting in _TRUE:
            return True
        if setting in _FALSE:
            return False
        parser.error(f"{variable} must be true or false")

    url = pick("url")
    if not url:
        parser.error("the Mattermost server is required: --url or MATTERMOST_URL")
    try:
        url = normalise_url(url)
    except ValueError as error:
        parser.error(f"MATTERMOST_URL: {error}")

    token = pick("token")
    if not token:
        parser.error("a bot access token is required: --token, MATTERMOST_TOKEN "
                     "or MATTERMOST_TOKEN_FILE")

    listed = args.allowed_channels
    if listed is None:
        listed = _from_environment("allowed_channels", environ)
        listed = [listed] if listed else None
    allowed_channels = None
    if listed is not None:
        allowed_channels = " ".join(listed).replace(",", " ").split()
        # A list that was given but names nothing must not mean "every channel".
        if not allowed_channels or not all(ID_PATTERN.fullmatch(item) for item in allowed_channels):
            parser.error("MATTERMOST_ALLOWED_CHANNELS must list channel ids (26 lowercase "
                         "letters and digits) separated by commas or spaces")

    dm_policy = pick("dm_policy") or "auto"
    if dm_policy not in DM_POLICIES:
        parser.error("MATTERMOST_DM_POLICY must be 'auto', 'allow' or 'deny'")

    require_mention = args.require_mention
    if require_mention is None:
        require_mention = boolean("require_mention", True)

    session_scope = pick("session_scope") or "person"
    if session_scope not in SESSION_SCOPES:
        parser.error("MATTERMOST_SESSION_SCOPE must be 'person' or 'chat'")

    health_port = pick("health_port")
    try:
        health_port = int(health_port) if health_port is not None else None
    except ValueError:
        parser.error("BRIDGE_HEALTH_PORT must be a number")
    if health_port is not None and not 0 < health_port < 65536:
        parser.error("BRIDGE_HEALTH_PORT must be between 1 and 65535")

    port = pick("port")
    try:
        port = int(port) if port is not None else 5678
    except ValueError:
        parser.error("HIVEMIND_PORT must be a number")

    self_signed = args.self_signed
    if self_signed is None:
        self_signed = boolean("self_signed", False)

    host = pick("host")
    if host and not host.startswith("ws://") and not host.startswith("wss://"):
        host = "ws://" + host

    return {
        "url": url,
        "token": token,
        "key": pick("key"),
        "password": pick("password"),
        "host": host,
        "port": port,
        "self_signed": self_signed,
        "lang": pick("lang") or "en-us",
        "site_id": pick("site_id") or "mattermost",
        "allowed_channels": allowed_channels,
        "dm_policy": dm_policy,
        "require_mention": require_mention,
        "session_scope": session_scope,
        "health_port": health_port,
    }


def write_termination_message(message: str, path: Optional[str] = None) -> None:
    """Leave ``message`` where Kubernetes reads why the container stopped.

    Outside Kubernetes the file usually cannot be written; that is not an
    error, the log and the exit status say the same.
    """
    try:
        with open(path or TERMINATION_LOG, "w", encoding="utf-8") as handle:
            handle.write(message)
    except OSError:
        pass


def stop_on_sigterm(signum: int, frame: object) -> None:
    """SIGTERM stops the bridge the way Ctrl-C does.

    In a container the bridge is PID 1, and the kernel does not deliver a
    signal to PID 1 that it has no handler for: without this, ``docker stop``
    and a Kubernetes rollout wait out the grace period and then kill it.
    """
    raise KeyboardInterrupt


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Run the bridge with settings from flags, the environment and secret files."""
    options = resolve_options(argv)
    if is_cleartext(options["url"]):
        LOG.warning("MATTERMOST_URL starts with http://: the bot's token and every "
                    "message travel unencrypted; use https://")
    signal.signal(signal.SIGTERM, stop_on_sigterm)
    LOG.info("bridge starting; press Ctrl-C to stop")
    try:
        connect_mattermost_to_hivemind(**options)
    except KeyboardInterrupt:
        LOG.info("shutting down")
    except TokenRejected:
        LOG.error("Mattermost refused the bot's access token; stopping "
                  f"(exit status {TOKEN_REJECTED_EXIT_CODE})")
        write_termination_message(TOKEN_REJECTED)
        raise SystemExit(TOKEN_REJECTED_EXIT_CODE) from None


if __name__ == '__main__':
    main()
