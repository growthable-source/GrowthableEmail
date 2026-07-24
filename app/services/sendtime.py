"""Ideal-local-time resolution for timed sends.

Each contact resolves to an IANA timezone: an explicit GHL timezone wins, then
the contact's country maps to a representative zone, and contacts with neither
are assumed to be in the US (central time as the coast compromise). Sends are
scheduled for the next occurrence of the ideal local hour in that zone.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/Chicago"  # no country on the contact → assume US

COUNTRY_TZ = {
    "US": "America/Chicago", "CA": "America/Toronto", "MX": "America/Mexico_City",
    "BR": "America/Sao_Paulo", "AR": "America/Argentina/Buenos_Aires",
    "CO": "America/Bogota", "CL": "America/Santiago",
    "GB": "Europe/London", "UK": "Europe/London", "IE": "Europe/Dublin",
    "FR": "Europe/Paris", "DE": "Europe/Berlin", "ES": "Europe/Madrid",
    "IT": "Europe/Rome", "NL": "Europe/Amsterdam", "BE": "Europe/Brussels",
    "PT": "Europe/Lisbon", "CH": "Europe/Zurich", "AT": "Europe/Vienna",
    "SE": "Europe/Stockholm", "NO": "Europe/Oslo", "DK": "Europe/Copenhagen",
    "FI": "Europe/Helsinki", "PL": "Europe/Warsaw", "GR": "Europe/Athens",
    "ZA": "Africa/Johannesburg", "NG": "Africa/Lagos", "EG": "Africa/Cairo",
    "KE": "Africa/Nairobi",
    "AE": "Asia/Dubai", "SA": "Asia/Riyadh", "IL": "Asia/Jerusalem",
    "TR": "Europe/Istanbul", "IN": "Asia/Kolkata", "PK": "Asia/Karachi",
    "BD": "Asia/Dhaka", "TH": "Asia/Bangkok", "VN": "Asia/Ho_Chi_Minh",
    "ID": "Asia/Jakarta", "MY": "Asia/Kuala_Lumpur", "SG": "Asia/Singapore",
    "PH": "Asia/Manila", "HK": "Asia/Hong_Kong", "CN": "Asia/Shanghai",
    "TW": "Asia/Taipei", "KR": "Asia/Seoul", "JP": "Asia/Tokyo",
    "AU": "Australia/Sydney", "NZ": "Pacific/Auckland", "FJ": "Pacific/Fiji",
}

# GHL sometimes stores the full country name rather than the ISO code
COUNTRY_NAME_TZ = {
    "united states": "America/Chicago", "united states of america": "America/Chicago",
    "usa": "America/Chicago", "canada": "America/Toronto",
    "united kingdom": "Europe/London", "great britain": "Europe/London",
    "england": "Europe/London", "ireland": "Europe/Dublin",
    "australia": "Australia/Sydney", "new zealand": "Pacific/Auckland",
    "india": "Asia/Kolkata", "philippines": "Asia/Manila",
    "south africa": "Africa/Johannesburg", "singapore": "Asia/Singapore",
    "germany": "Europe/Berlin", "france": "Europe/Paris", "spain": "Europe/Madrid",
    "netherlands": "Europe/Amsterdam", "brazil": "America/Sao_Paulo",
    "mexico": "America/Mexico_City", "japan": "Asia/Tokyo",
    "united arab emirates": "Asia/Dubai",
}


def resolve_timezone(country: str = "", tz: str = "") -> str:
    """Contact timezone if valid, else country's representative zone, else US."""
    tz = (tz or "").strip()
    if tz:
        try:
            ZoneInfo(tz)
            return tz
        except Exception:
            pass
    country = (country or "").strip()
    return (COUNTRY_TZ.get(country.upper())
            or COUNTRY_NAME_TZ.get(country.lower())
            or DEFAULT_TZ)


def next_ideal_time(tz_name: str, after: datetime, hour: int = 10) -> datetime:
    """Next occurrence of `hour` o'clock local time in tz_name, at or after `after`.
    Returns an aware UTC datetime."""
    local = after.astimezone(ZoneInfo(tz_name))
    candidate = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate < local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)
