from __future__ import annotations

import re
import socket
import uuid
from dataclasses import dataclass
from typing import Iterable

from media_config import LINEAR_FORMAT_RATES, MediaTransportConfig, validate_connection_id


_DIAL_ALLOWED = re.compile(r"^[0-9+*#]+$")


def build_websocket_media_dial_data(
    connection_id: str,
    media_format: str,
    call_uuid: str,
    control_format: str = "plain-text",
) -> str:
    """Build a safe chan_websocket Dial() target for one bridge call."""
    connection_id = validate_connection_id(connection_id)
    media_format = str(media_format or "").strip().lower()
    if media_format not in LINEAR_FORMAT_RATES:
        raise ValueError("WebSocket media format must be a supported signed-linear PCM format")
    try:
        call_uuid = str(uuid.UUID(str(call_uuid or "").strip()))
    except (ValueError, AttributeError) as exc:
        raise ValueError("call UUID is invalid") from exc
    options = f"c({media_format})"
    # Plain text is the compatibility default on chan_websocket 20.16+/21.11+/
    # 22.6+/23+. JSON control was added later, so request it only explicitly.
    if str(control_format or "").strip().lower() == "json":
        options += "f(json)"
    options += f"v(call_uuid={call_uuid})"
    return f"WebSocket/{connection_id}/{options}"


@dataclass
class AsteriskAMI:
    host: str = ""
    port: int = 5038
    username: str = ""
    secret: str = ""
    timeout: float = 5.0

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username and self.secret)

    def _read_response(self, sock: socket.socket) -> str:
        data = bytearray()
        while b"\r\n\r\n" not in data and len(data) < 262144:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        return data.decode("utf-8", errors="replace")

    def _login(self, sock: socket.socket) -> tuple[bool, str]:
        try:
            sock.recv(4096)
        except socket.timeout:
            pass
        login = (
            "Action: Login\r\n"
            f"Username: {self.username}\r\n"
            f"Secret: {self.secret}\r\n"
            "Events: off\r\n\r\n"
        )
        sock.sendall(login.encode())
        response = self._read_response(sock)
        return ("Response: Success" in response), response

    def _action(self, fields: Iterable[tuple[str, str]]) -> tuple[bool, str]:
        if not self.configured:
            return False, "AMI not configured"
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                ok, _ = self._login(sock)
                if not ok:
                    return False, "AMI login rejected"
                payload = "".join(f"{k}: {v}\r\n" for k, v in fields) + "\r\n"
                sock.sendall(payload.encode())
                response = self._read_response(sock)
                try:
                    sock.sendall(b"Action: Logoff\r\n\r\n")
                except OSError:
                    pass
                success = "Response: Success" in response
                message = ""
                for line in response.splitlines():
                    if line.lower().startswith("message:"):
                        message = line.split(":", 1)[1].strip()
                        break
                return success, message or response.strip()[:500]
        except Exception as exc:
            return False, str(exc)

    def _status_events(self, variables: str = "") -> list[dict[str, object]]:
        if not self.configured:
            return []
        action_id = f"status-{uuid.uuid4()}"
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                ok, _ = self._login(sock)
                if not ok:
                    return []
                payload = ["Action: Status", f"ActionID: {action_id}"]
                if variables:
                    payload.append(f"Variables: {variables}")
                sock.sendall(("\r\n".join(payload) + "\r\n\r\n").encode())
                data = bytearray()
                terminator = b"Event: StatusComplete"
                while len(data) < 2 * 1024 * 1024:
                    try:
                        chunk = sock.recv(65536)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    data.extend(chunk)
                    if terminator in data:
                        pos = data.find(terminator)
                        if b"\r\n\r\n" in data[pos:]:
                            break
                events: list[dict[str, object]] = []
                for block in data.decode("utf-8", errors="replace").split("\r\n\r\n"):
                    if not block.strip():
                        continue
                    fields: dict[str, object] = {}
                    variables_out: list[str] = []
                    for line in block.splitlines():
                        if ":" not in line:
                            continue
                        key, value = line.split(":", 1)
                        key = key.strip(); value = value.strip()
                        if key.lower() == "variable":
                            variables_out.append(value)
                        else:
                            fields[key] = value
                    if fields.get("Event") == "Status":
                        fields["Variables"] = variables_out
                        events.append(fields)
                return events
        except Exception:
            return []

    def cancel_originate(self, call_uuid: str, number: str = "", context: str = "from-internal") -> tuple[bool, str, int]:
        call_uuid = str(call_uuid or "").strip()
        if not call_uuid:
            return False, "call UUID is required", 0
        events = self._status_events("DISCORD_CALL_UUID")
        channels: list[str] = []
        for event in events:
            if any(v == f"DISCORD_CALL_UUID={call_uuid}" for v in (event.get("Variables") or [])):
                ch = str(event.get("Channel", ""))
                if ch:
                    channels.append(ch)
        if not channels and number:
            try:
                normalized = self.normalize_number(number)
            except ValueError:
                normalized = ""
            if normalized:
                prefix = f"Local/{normalized}@{context}-"
                candidates = [
                    str(e.get("Channel", "")) for e in events
                    if str(e.get("Channel", "")).startswith(prefix)
                ]
                if 0 < len(candidates) <= 2:
                    channels = candidates
        channels = list(dict.fromkeys(channels))
        if not channels:
            return False, "No matching ringing Asterisk channel found", 0
        hung = 0; details: list[str] = []
        for channel in channels:
            ok, detail = self._action((("Action", "Hangup"), ("Channel", channel), ("Cause", "16")))
            if ok:
                hung += 1
            elif detail:
                details.append(detail)
        if hung:
            return True, f"Cancelled {hung} Asterisk channel(s)", hung
        return False, "; ".join(details) or "Asterisk refused the hangup", 0

    def channels_for_call(self, call_uuid: str) -> list[dict[str, object]]:
        call_uuid = str(call_uuid or "").strip()
        if not call_uuid:
            return []
        events = self._status_events("DISCORD_CALL_UUID,BRIDGE_UUID")
        wanted = {f"DISCORD_CALL_UUID={call_uuid}", f"BRIDGE_UUID={call_uuid}"}
        return [e for e in events if any(v in wanted for v in (e.get("Variables") or []))]

    def _dtmf_channels_for_call(self, call_uuid: str) -> list[str]:
        tagged = self.channels_for_call(call_uuid)
        if not tagged:
            return []
        linked_ids = {
            str(e.get("Linkedid") or e.get("Uniqueid") or "").strip()
            for e in tagged if str(e.get("Linkedid") or e.get("Uniqueid") or "").strip()
        }
        all_events = self._status_events() if linked_ids else []
        related = [e for e in all_events if str(e.get("Linkedid") or "").strip() in linked_ids]
        by_channel: dict[str, dict[str, object]] = {}
        for event in [*related, *tagged]:
            channel = str(event.get("Channel") or "").strip()
            if channel:
                by_channel[channel] = event

        def score(item: tuple[str, dict[str, object]]) -> tuple[int, str]:
            channel, event = item
            upper = channel.upper(); state = str(event.get("ChannelStateDesc") or "").lower(); value = 0
            if state == "up": value += 100
            if upper.startswith("PJSIP/"): value += 80
            elif upper.startswith("SIP/") or upper.startswith("DAHDI/"): value += 70
            elif not upper.startswith("LOCAL/"): value += 40
            if upper.startswith("LOCAL/") and not channel.endswith(";2"): value += 25
            if channel.endswith(";2"): value -= 10
            return value, channel

        return [channel for channel, _ in sorted(by_channel.items(), key=score, reverse=True)]

    def play_dtmf(self, call_uuid: str, digit: str, duration_ms: int = 120) -> tuple[bool, str, str]:
        call_uuid = str(call_uuid or "").strip()
        if not call_uuid:
            raise ValueError("call UUID is required")
        digit = str(digit or "")[:1].upper()
        if digit not in "0123456789*#ABCD":
            raise ValueError("invalid DTMF digit")
        try:
            duration_ms = max(40, min(5000, int(duration_ms)))
        except (TypeError, ValueError):
            duration_ms = 120
        channels = self._dtmf_channels_for_call(call_uuid)
        if not channels:
            return False, "No live Asterisk channel found for this call", ""
        errors: list[str] = []
        for channel in channels:
            ok, detail = self._action((
                ("Action", "PlayDTMF"), ("Channel", channel),
                ("Digit", digit), ("Duration", str(duration_ms)),
            ))
            if ok:
                return True, detail or "DTMF successfully queued", channel
            if detail:
                errors.append(f"{channel}: {detail}")
        return False, "; ".join(errors[:3]) or "Asterisk refused DTMF", ""

    def blind_transfer(self, call_uuid: str, target: str, context: str = "from-internal") -> tuple[bool, str]:
        target = self.normalize_number(target)
        channels = [str(e.get("Channel", "")) for e in self.channels_for_call(call_uuid) if e.get("Channel")]
        if not channels:
            return False, "No Asterisk channel found for this call"
        preferred = next((c for c in channels if not c.endswith(";2")), channels[0])
        return self._action((
            ("Action", "Redirect"), ("Channel", preferred),
            ("Context", context), ("Exten", target), ("Priority", "1"),
        ))

    def command(self, command: str) -> tuple[bool, str]:
        command = str(command or "").strip()
        allowed = {
            "pjsip show registrations",
            "pjsip show endpoints",
            "core show uptime",
            "core show channels count",
            "core show version",
            "module show like chan_websocket",
        }
        if command not in allowed:
            return False, "command is not allow-listed"
        return self._action((("Action", "Command"), ("Command", command)))

    def ping(self) -> tuple[bool, str]:
        return self._action((("Action", "Ping"),))

    @staticmethod
    def normalize_number(number: str) -> str:
        number = re.sub(r"[\s().-]", "", number.strip())
        if not number or not _DIAL_ALLOWED.fullmatch(number):
            raise ValueError("number may only contain digits, +, *, and #")
        if len(number) > 32:
            raise ValueError("number is too long")
        if number.startswith("+1") and number[2:].isdigit() and len(number) == 12:
            number = number[1:]
        elif number.isdigit() and len(number) == 10:
            number = "1" + number
        return number

    @staticmethod
    def normalize_caller_id(caller_id: str) -> str:
        caller_id = caller_id.strip()
        if not caller_id:
            return ""
        if "\r" in caller_id or "\n" in caller_id:
            raise ValueError("caller ID contains invalid characters")
        if len(caller_id) > 80:
            raise ValueError("caller ID is too long")
        return caller_id

    def _originate_fields(
        self,
        number: str,
        context: str,
        timeout_ms: int,
        caller_id: str,
        call_uuid: str,
        *,
        application: str,
        data: str,
    ) -> list[tuple[str, str]]:
        fields = [
            ("Action", "Originate"),
            ("Channel", f"Local/{number}@{context}/n"),
            ("Variable", f"DISCORD_CALL_UUID={call_uuid}"),
            ("Application", application),
            ("Data", data),
            ("Timeout", str(timeout_ms)),
            ("Async", "true"),
        ]
        if caller_id:
            fields.append(("CallerID", caller_id))
        return fields

    def originate_to_websocket_media(
        self,
        number: str,
        connection_id: str,
        media_format: str = "slin16",
        context: str = "from-internal",
        timeout_ms: int = 45000,
        caller_id: str = "",
        call_uuid: str = "",
        control_format: str = "plain-text",
    ) -> tuple[bool, str, str]:
        """Originate a call whose answered Local leg is bridged by chan_websocket."""
        if not self.configured:
            return False, "AMI not configured", ""
        number = self.normalize_number(number)
        caller_id = self.normalize_caller_id(caller_id)
        call_uuid = call_uuid.strip() or str(uuid.uuid4())
        dial_data = build_websocket_media_dial_data(
            connection_id, media_format, call_uuid, control_format
        )
        fields = self._originate_fields(
            number, context, timeout_ms, caller_id, call_uuid,
            application="Dial", data=dial_data,
        )
        ok, detail = self._action(fields)
        return ok, detail, call_uuid

    def originate_media(
        self,
        number: str,
        audiosocket_host: str,
        audiosocket_port: int,
        context: str = "from-internal",
        timeout_ms: int = 45000,
        caller_id: str = "",
        call_uuid: str = "",
    ) -> tuple[bool, str, str]:
        """Use the configured first-class PBX media transport for an outbound call."""
        cfg = MediaTransportConfig.from_env()
        if cfg.use_websocket_for_outbound:
            cfg.require_websocket_ready()
            return self.originate_to_websocket_media(
                number,
                cfg.asterisk_connection_id,
                cfg.websocket_format,
                context,
                timeout_ms,
                caller_id,
                call_uuid,
                cfg.websocket_control_format,
            )
        return self._originate_legacy_audiosocket(
            number, audiosocket_host, audiosocket_port, context,
            timeout_ms, caller_id, call_uuid,
        )

    def _originate_legacy_audiosocket(
        self,
        number: str,
        audiosocket_host: str,
        audiosocket_port: int,
        context: str = "from-internal",
        timeout_ms: int = 45000,
        caller_id: str = "",
        call_uuid: str = "",
    ) -> tuple[bool, str, str]:
        if not self.configured:
            return False, "AMI not configured", ""
        number = self.normalize_number(number)
        if not audiosocket_host:
            return False, "AUDIOSOCKET_ADVERTISE_HOST is not configured", ""
        caller_id = self.normalize_caller_id(caller_id)
        call_uuid = call_uuid.strip() or str(uuid.uuid4())
        try:
            call_uuid = str(uuid.UUID(call_uuid))
        except ValueError as exc:
            raise ValueError("call UUID is invalid") from exc
        fields = self._originate_fields(
            number, context, timeout_ms, caller_id, call_uuid,
            application="AudioSocket",
            data=f"{call_uuid},{audiosocket_host}:{audiosocket_port}",
        )
        ok, detail = self._action(fields)
        return ok, detail, call_uuid

    def originate_to_audiosocket(
        self,
        number: str,
        audiosocket_host: str,
        audiosocket_port: int,
        context: str = "from-internal",
        timeout_ms: int = 45000,
        caller_id: str = "",
        call_uuid: str = "",
    ) -> tuple[bool, str, str]:
        """Compatibility entry point used by the existing operator layer.

        v3.4 routes this call through the canonical configured media transport.
        Existing installs with no WebSocket media credentials remain on the
        legacy AudioSocket path; configured installs originate true wideband
        chan_websocket media without changing the operator API.
        """
        return self.originate_media(
            number, audiosocket_host, audiosocket_port, context,
            timeout_ms, caller_id, call_uuid,
        )
