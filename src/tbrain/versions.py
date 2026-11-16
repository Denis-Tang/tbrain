"""Release ordering shared by installation and update checks."""

from packaging.version import InvalidVersion, Version


def parse_release(text: str) -> Version | None:
    try:
        version = Version(text)
    except InvalidVersion:
        return None
    return None if version.local else version


def key(text: str) -> tuple:
    version = parse_release(text)
    return (version,) if version is not None else ()
