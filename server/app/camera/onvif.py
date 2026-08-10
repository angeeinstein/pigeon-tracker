"""ONVIF discovery and stream-profile helpers used by camera onboarding."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from app.logging_config import get_logger

log = get_logger(__name__)


class OnvifError(RuntimeError):
    """A camera could not be discovered, authenticated, or queried."""


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _scope_value(scopes: list[str], key: str) -> str:
    marker = f"onvif.org/{key}/"
    for scope in scopes:
        if marker in scope:
            return unquote(scope.split(marker, 1)[1]).replace("_", " ")
    return ""


def discover_onvif(timeout_s: int = 4) -> list[dict[str, Any]]:
    """Return JSON-safe devices found using local WS-Discovery multicast."""
    try:
        from onvif import ONVIFDiscovery

        raw_devices = ONVIFDiscovery(timeout=timeout_s).discover(prefer_https=False)
    except Exception as exc:
        raise OnvifError(f"camera discovery failed: {exc}") from exc

    devices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_devices:
        xaddrs = [str(item) for item in (_value(raw, "xaddrs", []) or [])]
        host = str(_value(raw, "host", ""))
        port = int(_value(raw, "port", 80) or 80)
        xaddr = xaddrs[0] if xaddrs else f"http://{host}:{port}/onvif/device_service"
        if xaddr in seen:
            continue
        seen.add(xaddr)
        scopes = [str(item) for item in (_value(raw, "scopes", []) or [])]
        devices.append(
            {
                "host": host,
                "port": port,
                "xaddr": xaddr,
                "xaddrs": xaddrs,
                "name": _scope_value(scopes, "name") or host,
                "hardware": _scope_value(scopes, "hardware"),
                "location": _scope_value(scopes, "location"),
                "types": [str(item) for item in (_value(raw, "types", []) or [])],
            }
        )
    return sorted(devices, key=lambda item: (item["name"].lower(), item["host"]))


def validate_private_device_url(url: str, schemes: set[str]) -> tuple[str, int, bool]:
    """Validate an operator-supplied camera address and confine it to the LAN."""
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in schemes or not parsed.hostname:
        expected = "/".join(sorted(schemes))
        raise OnvifError(f"expected a {expected} camera URL with a host")
    if parsed.username is not None or parsed.password is not None:
        raise OnvifError("put camera credentials in the separate username/password fields")
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(parsed.hostname, parsed.port, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError) as exc:
        raise OnvifError(f"camera host could not be resolved: {parsed.hostname}") from exc
    if not addresses or any(
        not (address.is_private or address.is_link_local) for address in addresses
    ):
        raise OnvifError("camera address must resolve only to a private LAN address")
    default_port = 443 if parsed.scheme.lower() in {"https", "rtsps"} else 80
    return parsed.hostname, parsed.port or default_port, parsed.scheme.lower() == "https"


def _clean_stream_uri(uri: str, camera_host: str) -> str:
    parsed = urlsplit(uri)
    if parsed.scheme.lower() not in {"rtsp", "rtsps"}:
        raise OnvifError("camera returned a non-RTSP stream URI")
    host = parsed.hostname
    if not host or host in {"0.0.0.0", "127.0.0.1", "localhost", "::"}:
        host = camera_host
    port = f":{parsed.port}" if parsed.port else ""
    # Strip credentials: the protected credential store supplies them at decode time.
    return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, parsed.query, parsed.fragment))


def fetch_onvif_profiles(
    xaddr: str, username: str, password: str, timeout_s: int = 10
) -> dict[str, Any]:
    """Authenticate with an ONVIF device and obtain its RTSP profiles."""
    host, port, use_https = validate_private_device_url(xaddr, {"http", "https"})
    try:
        with socket.create_connection((host, port), timeout=min(timeout_s, 3)):
            pass
    except OSError as exc:
        raise OnvifError(
            f"could not reach the ONVIF service at {host}:{port}; check its address, "
            "port and network route"
        ) from exc
    try:
        from onvif import CacheMode, ONVIFClient

        client = ONVIFClient(
            host,
            port,
            username,
            password,
            timeout=timeout_s,
            cache=CacheMode.MEM,
            use_https=use_https,
            verify_ssl=False,
        )
        info = client.devicemgmt().GetDeviceInformation()
        media = client.media()
        raw_profiles = media.GetProfiles() or []
        profiles: list[dict[str, Any]] = []
        for profile in raw_profiles:
            token = str(_value(profile, "token", "") or _value(profile, "Token", ""))
            if not token:
                continue
            result = media.GetStreamUri(
                StreamSetup={"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}},
                ProfileToken=token,
            )
            uri = str(_value(result, "Uri", ""))
            if not uri:
                continue
            encoder = _value(profile, "VideoEncoderConfiguration")
            resolution = _value(encoder, "Resolution")
            rate = _value(encoder, "RateControl")
            profiles.append(
                {
                    "token": token,
                    "name": str(_value(profile, "Name", "") or token),
                    "uri": _clean_stream_uri(uri, host),
                    "encoding": str(_value(encoder, "Encoding", "") or ""),
                    "width": int(_value(resolution, "Width", 0) or 0),
                    "height": int(_value(resolution, "Height", 0) or 0),
                    "fps": int(_value(rate, "FrameRateLimit", 0) or 0),
                }
            )
    except OnvifError:
        raise
    except Exception as exc:
        log.warning(
            "ONVIF profile query failed",
            extra={"ctx": {"host": host, "port": port, "error_type": type(exc).__name__}},
        )
        reason = str(exc).lower()
        if any(word in reason for word in ("401", "unauthorized", "not authorized", "auth")):
            message = "camera rejected the ONVIF account username or password"
        elif "timed out" in reason or "timeout" in reason:
            message = (
                f"ONVIF service at {host}:{port} timed out; check the port and camera settings"
            )
        else:
            message = (
                f"camera answered at {host}:{port}, but the ONVIF query failed; check that this "
                "is the device-service port and that ONVIF is enabled for the camera account"
            )
        raise OnvifError(message) from exc

    if not profiles:
        raise OnvifError("camera exposed no usable RTSP profiles")
    return {
        "device": {
            "manufacturer": str(_value(info, "Manufacturer", "") or ""),
            "model": str(_value(info, "Model", "") or ""),
            "firmware": str(_value(info, "FirmwareVersion", "") or ""),
            "serial_number": str(_value(info, "SerialNumber", "") or ""),
            "host": host,
            "xaddr": xaddr,
        },
        "profiles": profiles,
    }
