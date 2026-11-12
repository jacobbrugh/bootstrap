from __future__ import annotations

from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.toml_document import TOMLDocument

from bootstrap.lib import boot_config
from bootstrap.lib.errors import BootstrapError

#: Tags that pull in others, transitively: `tagImplications` in the dotfiles
#: lib/hosts/fromRegistry.nix. `private` implying `sandbox` is what keeps a
#: private VM on the sandbox tier here, as it is in dotfiles.
TAG_IMPLICATIONS: dict[str, tuple[str, ...]] = {
    "laptop": ("desktop", "wireless"),
    "private": ("sandbox",),
}

#: The port every Tang host listens on (the dotfiles `tang` tag module).
TANG_PORT = 7500


def expand_tags(tags: list[str]) -> list[str]:
    """``tags`` plus everything they imply, in first-seen order."""
    out = list(dict.fromkeys(tags))
    index = 0
    while index < len(out):
        for implied in TAG_IMPLICATIONS.get(out[index], ()):
            if implied not in out:
                out.append(implied)
        index += 1
    return out


def load(path: Path) -> TOMLDocument:
    return tomlkit.parse(path.read_text())


def save(doc: TOMLDocument, path: Path) -> None:
    path.write_text(tomlkit.dumps(doc))


def _expand_hosts(doc: TOMLDocument) -> dict[str, dict[str, Any]]:
    """Expand the registry the way the dotfiles lib/hosts/fromRegistry.nix does.

    A section with a `system` field is a host template. When `endIndex` is set
    it is indexed: it expands to base<start>..base<end> (start defaulting to 0)
    and the template name itself is NOT a host. Without `endIndex` it is a
    single literal host. A section without `system` is a partial override
    merged onto the same-named expanded host: scalar fields override-win,
    `tags` concatenate. Override sections that match no expanded host are
    ignored here — fromRegistry hard-fails the dotfiles eval on those, so a
    registry containing one is already broken upstream of bootstrap. Tags
    carry their implications, as fromRegistry's `expandTags` gives them.
    """
    templates: dict[str, dict[str, Any]] = {}
    overrides: dict[str, dict[str, Any]] = {}
    for name in doc:
        section = doc[name]
        if not isinstance(section, dict):
            continue
        plain = {str(k): v for k, v in section.items()}
        if "system" in plain:
            templates[str(name)] = plain
        else:
            overrides[str(name)] = plain

    hosts: dict[str, dict[str, Any]] = {}
    for base, template in templates.items():
        fields = {k: v for k, v in template.items() if k not in ("startIndex", "endIndex")}
        if "endIndex" in template:
            start = int(template.get("startIndex", 0))
            end = int(template["endIndex"])
            for i in range(start, end + 1):
                hosts[f"{base}{i}"] = dict(fields)
        else:
            hosts[base] = dict(fields)

    for name, override in overrides.items():
        host = hosts.get(name)
        if host is None:
            continue
        for key, value in override.items():
            if key == "tags":
                host["tags"] = [*host.get("tags", []), *value]
            else:
                host[key] = value
    for host in hosts.values():
        host["tags"] = expand_tags([str(t) for t in host.get("tags", [])])
    return hosts


def has_host(doc: TOMLDocument, hostname: str) -> bool:
    return hostname in _expand_hosts(doc)


def get_tags(doc: TOMLDocument, hostname: str) -> list[str]:
    host = _expand_hosts(doc).get(hostname)
    if host is None:
        return []
    return [str(t) for t in host.get("tags", [])]


def get_luks_unlock(doc: TOMLDocument, hostname: str) -> str:
    """How this host's LUKS root unlocks at boot.

    Mirrors the luksUnlock option in the dotfiles lib/hosts/fromRegistry.nix,
    including its default — an entry that omits the field means the fleet
    default, not "unset".
    """
    host = _expand_hosts(doc).get(hostname)
    if host is None:
        return boot_config.LUKS_UNLOCK_DEFAULT
    method = host.get("luksUnlock")
    if method is None:
        return boot_config.LUKS_UNLOCK_DEFAULT
    if str(method) not in boot_config.LUKS_UNLOCK_MODES:
        raise BootstrapError(
            f"host {hostname!r} declares luksUnlock={method!r}, which is not one "
            f"of {', '.join(boot_config.LUKS_UNLOCK_MODES)}"
        )
    return str(method)


def get_dotfiles_dir(doc: TOMLDocument, hostname: str) -> str | None:
    """The host's `dotfilesDir` when its entry pins one, else None (the fleet default)."""
    host = _expand_hosts(doc).get(hostname)
    value = host.get("dotfilesDir") if host is not None else None
    return str(value) if value is not None else None


def tang_urls(doc: TOMLDocument) -> list[str]:
    """One URL per host tagged `tang`, at the address its registry entry declares.

    The registry is where the Tang hosts and their LAN addresses already live
    (the `tang` tag, `networking.interfaces.ethernet.address`), so a disk is
    bound to whatever serves Tang today and this source carries no address.
    """
    urls: list[str] = []
    for name, host in sorted(_expand_hosts(doc).items()):
        if "tang" not in host["tags"]:
            continue
        address: Any = host
        for key in ("networking", "interfaces", "ethernet", "address"):
            address = address.get(key) if isinstance(address, dict) else None
        if not address:
            raise BootstrapError(
                f"host {name!r} is tagged tang but declares no "
                "networking.interfaces.ethernet.address to reach it at"
            )
        urls.append(f"http://{address}:{TANG_PORT}")
    return urls
