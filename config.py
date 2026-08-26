import os
from dataclasses import dataclass


def _int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _str_list(name: str) -> list[str]:
    raw = os.getenv(name, "").strip()
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else []


def _int_set(name: str) -> set[int]:
    out: set[int] = set()
    for item in _str_list(name):
        try:
            out.add(int(item))
        except ValueError:
            pass
    return out


@dataclass
class Config:
    version: str
    data_dir: str
    discord_token: str
    discord_client_id: str
    discord_client_secret: str
    guild_id: int
    voice_channel_id: int
    text_channel_id: int
    bot_owner_ids: set[int]
    pbx_role_ids: set[int]
    auto_join_on_start: bool
    leave_voice_after_call_seconds: float

    audiosocket_bind: str
    audiosocket_port: int
    audiosocket_advertise_host: str
    pbx_to_discord_gain: float
    discord_to_pbx_gain: float
    inbound_chime_enabled: bool
    inbound_chime_file: str
    inbound_chime_gain: float

    ami_host: str
    ami_port: int
    ami_user: str
    ami_secret: str
    ami_timeout: float
    ami_dial_context: str
    ami_dial_timeout_ms: int
    ami_caller_id: str
    ami_caller_id_options: list[str]
    allow_custom_caller_id: bool
    max_simultaneous_calls: int
    contacts_file: str
    pbx_ingress_token: str

    web_bind: str
    web_port: int
    web_auth_mode: str
    web_username: str
    web_password: str
    public_base_url: str
    trusted_proxy: bool
    github_repo: str

    log_level: str
    test_audio: str

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = os.getenv("DATA_DIR", "/app/data").strip() or "/app/data"
        roles = _int_set("PBX_ROLE_IDS") | _int_set("ALLOWED_ROLE_IDS")
        web_auth_mode = os.getenv("WEB_AUTH_MODE", "discord").strip().lower() or "discord"
        if web_auth_mode not in {"discord", "basic", "none", "hybrid"}:
            web_auth_mode = "discord"
        return cls(
            version="3.3.6",
            data_dir=data_dir,
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            discord_client_id=os.getenv("DISCORD_CLIENT_ID", "").strip(),
            discord_client_secret=os.getenv("DISCORD_CLIENT_SECRET", "").strip(),
            guild_id=_int("DISCORD_GUILD_ID"),
            voice_channel_id=_int("DISCORD_VOICE_CHANNEL_ID"),
            text_channel_id=_int("DISCORD_TEXT_CHANNEL_ID"),
            bot_owner_ids=_int_set("BOT_OWNER_IDS"),
            pbx_role_ids=roles,
            auto_join_on_start=_bool("AUTO_JOIN_ON_START", False),
            leave_voice_after_call_seconds=max(0.0, _float("LEAVE_VOICE_AFTER_CALL_SECONDS", 20.0)),

            audiosocket_bind=os.getenv("AUDIOSOCKET_BIND", "0.0.0.0").strip() or "0.0.0.0",
            audiosocket_port=_int("AUDIOSOCKET_PORT", 9092),
            audiosocket_advertise_host=os.getenv("AUDIOSOCKET_ADVERTISE_HOST", "").strip(),
            pbx_to_discord_gain=_float("PBX_TO_DISCORD_GAIN", 1.0),
            discord_to_pbx_gain=_float("DISCORD_TO_PBX_GAIN", 1.0),
            inbound_chime_enabled=_bool("INBOUND_CHIME_ENABLED", True),
            inbound_chime_file=os.getenv("INBOUND_CHIME_FILE", "/app/assets/retro-mail-chime.wav").strip() or "/app/assets/retro-mail-chime.wav",
            inbound_chime_gain=_float("INBOUND_CHIME_GAIN", 0.85),

            ami_host=os.getenv("ASTERISK_AMI_HOST", "").strip(),
            ami_port=_int("ASTERISK_AMI_PORT", 5038),
            ami_user=os.getenv("ASTERISK_AMI_USER", "").strip(),
            ami_secret=os.getenv("ASTERISK_AMI_SECRET", "").strip(),
            ami_timeout=_float("ASTERISK_AMI_TIMEOUT", 5.0),
            ami_dial_context=os.getenv("ASTERISK_DIAL_CONTEXT", "from-internal").strip() or "from-internal",
            ami_dial_timeout_ms=_int("ASTERISK_DIAL_TIMEOUT_MS", 45000),
            ami_caller_id=os.getenv("ASTERISK_CALLER_ID", "").strip(),
            ami_caller_id_options=_str_list("ASTERISK_CALLER_ID_OPTIONS"),
            allow_custom_caller_id=_bool("ALLOW_CUSTOM_CALLER_ID", False),
            max_simultaneous_calls=max(1, _int("MAX_SIMULTANEOUS_CALLS", 15)),
            contacts_file=os.getenv("CONTACTS_FILE", f"{data_dir}/contacts.json").strip() or f"{data_dir}/contacts.json",
            pbx_ingress_token=os.getenv("PBX_INGRESS_TOKEN", "").strip(),

            web_bind=os.getenv("WEB_BIND", "0.0.0.0").strip() or "0.0.0.0",
            web_port=_int("WEB_PORT", 8088),
            web_auth_mode=web_auth_mode,
            web_username=os.getenv("WEB_USERNAME", "pbx").strip() or "pbx",
            web_password=os.getenv("WEB_PASSWORD", "").strip(),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            trusted_proxy=_bool("TRUST_PROXY_HEADERS", True),
            github_repo=os.getenv("GITHUB_REPO", "p5zggfdnjx-design/discordpbx").strip() or "p5zggfdnjx-design/discordpbx",

            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            test_audio=os.getenv("TEST_AUDIO", "/app/assets/test.mp3").strip(),
        )
