"""Pinned CC0 recordings for the effects synthesis cannot fake (KRI-173).

Only CC0 sources, each pinned by SHA-256 so a rebuild is byte-for-byte the same
input or fails loudly. Downloads are cached outside the repo (audio binaries
never enter git); set ``NOVA_SFX_CACHE`` to relocate the cache.

* Kenney.nl packs (CC0, license text inside each zip).
* Freesound previews filtered to "Creative Commons 0"; each sound page was
  checked for ``creativecommons.org/publicdomain/zero/1.0`` when pinned
  (2026-09-23). Previews are the 128 kbps "hq" MP3s Freesound serves publicly.
"""

# ruff: noqa: E501 - one pinned download per line.

from __future__ import annotations

import hashlib
import os
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from scripts.sfx_library.master import decode


@dataclass(frozen=True)
class KenneyPack:
    url: str
    sha256: str


@dataclass(frozen=True)
class FreesoundPin:
    user: str
    preview: str  # path under https://cdn.freesound.org/previews/
    sha256: str


KENNEY_PACKS: dict[str, KenneyPack] = {
    "interface-sounds": KenneyPack(
        "https://kenney.nl/media/pages/assets/interface-sounds/fa43c1dd4d-1677589452/kenney_interface-sounds.zip",
        "f2193d072726d6758a5f7871b2dcc54dcce0d5c35c6f0a62f92549b327c81232",
    ),
    "impact-sounds": KenneyPack(
        "https://kenney.nl/media/pages/assets/impact-sounds/87b4ddecda-1677589768/kenney_impact-sounds.zip",
        "029d734af1582474edf3a694d1b0cebc97c1c152f2f39fa34d4c2bafc5de77f8",
    ),
    "rpg-audio": KenneyPack(
        "https://kenney.nl/media/pages/assets/rpg-audio/8e99002d76-1677590336/kenney_rpg-audio.zip",
        "6dbeaf8544da958d8f2adcb4a4a4b76c1ade34a05f8ab9edccd327da7375f38b",
    ),
    "ui-audio": KenneyPack(
        "https://kenney.nl/media/pages/assets/ui-audio/490d233f68-1677590494/kenney_ui-audio.zip",
        "946fc23a63d535d693eb31b2eabb80c8c28d6351e2186b344ceb71b2cb1d5eb6",
    ),
}

FREESOUND: dict[int, FreesoundPin] = {
    43404: FreesoundPin("simkiott", "43/43404_465423-hq.mp3", "ee71af1d2da7d46916a5ab06a9a5d5ec7ec307bb7deb98f556853d000c7fa0ad"),
    71853: FreesoundPin("ludvique", "71/71853_1062668-hq.mp3", "60780faa2d4dd51f616b1c026742c5af35c0b21081636266183065777dce2ff2"),
    77305: FreesoundPin("bigjoedrummer", "77/77305_1105584-hq.mp3", "2f3d571bccfaf26cbdf2d08ebb3a18fdbdde21315042d9fa9d819d1d99721fa3"),
    113698: FreesoundPin("huubjeroen", "113/113698_190760-hq.mp3", "0abbf6108b66c93ec29c594e9a98c11d992e4d3e1cd4519de6a4ab70a0655a36"),
    124996: FreesoundPin("phmiller42", "124/124996_687791-hq.mp3", "6648ca3ec87a6d4ccaafc139bf1c7b6a3629c1888101630c4003428df152ce3b"),
    201211: FreesoundPin("Scheffler", "201/201211_78957-hq.mp3", "054cdfa4c1a43237c1773097dec2d487cdb9680e08ed009ae020ff74f547731a"),
    209578: FreesoundPin("Zott820", "209/209578_2558531-hq.mp3", "7459185c471d0b8ce87f8616f6f48f6e372be9c89f1ae86c6982618ab74e96d3"),
    218318: FreesoundPin("SpliceSound", "218/218318_1480854-hq.mp3", "7392e39a07d47ffbbf174677e7b82ab91fe936dbcf17a9c8d947ca763d1ad0b3"),
    221528: FreesoundPin("unfa", "221/221528_1038806-hq.mp3", "09feba0b93b4be57e9601fc55f72a77ac69735dbd480af941cdc49868bb76e97"),
    264376: FreesoundPin("HowardV", "264/264376_1654262-hq.mp3", "f0ce488c955396ca2f464f5e801ba9af69afc4db5db6e4875b7047c9fe27a741"),
    264378: FreesoundPin("HowardV", "264/264378_1654262-hq.mp3", "7c091f19f3a5c9774de3c18ac82e4de59d29ac451a34795e52f5d78f15d710af"),
    264499: FreesoundPin("noah0189", "264/264499_3890365-hq.mp3", "2602cb507d42d79b0d62873bb58e330873a03b5c544922b5cf13850d118858e1"),
    271010: FreesoundPin("Kodack", "271/271010_2276808-hq.mp3", "5c8533ff2afd167e421f9b99ac6c519eefe74f35c5cb433631204d3f4f909f58"),
    274516: FreesoundPin("stomachache", "274/274516_177850-hq.mp3", "eea0213d5ccb6262c5c752a4f7c7241365b036b1ba1c9c05383776a5de586cc0"),
    318687: FreesoundPin("ramsamba", "318/318687_1147663-hq.mp3", "8f669d70aa26c82cf6de344c8d4ebe8f8eff39cc62fffaa92c9e98222e2e174c"),
    346689: FreesoundPin("deleted_user_2104797", "346/346689_2104797-hq.mp3", "7c3375d4282dc0358ad440aba89434c44a2299d5555c22d77f33f63d07750e05"),
    371562: FreesoundPin("Kinoton", "371/371562_2247456-hq.mp3", "9b8129273534e3199a48360c462b3a33a903f2328051b2cda6689b698475a53f"),
    380138: FreesoundPin("yottasounds", "380/380138_3249786-hq.mp3", "3ef4947d89daadd9d2649f1c6350e50dcafceaa36a33b45a5a3c1bb44bb26cf9"),
    383903: FreesoundPin("deleted_user_7146007", "383/383903_7146007-hq.mp3", "b4f81f2d9031e59a3918f1cc24464cfea40e5ff3cba07cfc320ab6897f92b601"),
    478414: FreesoundPin("thaighaudio", "478/478414_2205380-hq.mp3", "c78c86d3049326fe84174bb296042dd629970f2fc8e26a9df2868d654674fb2c"),
    494362: FreesoundPin("Sandermotions", "494/494362_1402315-hq.mp3", "1c75d7dcb4833427e7a403275f9dda815c3e8e3a19f723e892d2bf46dd882768"),
    518048: FreesoundPin("Hann90", "518/518048_3663300-hq.mp3", "51ac2dec09693da63c5eec6232b053a176e9664f1f5f72975e48b8618ccce71a"),
    520684: FreesoundPin("Tonik1105", "520/520684_10067806-hq.mp3", "63ab3eb744668c6f95e97f7903dcd947283bd425ca92406821a6a4b3002002ca"),
    527386: FreesoundPin("Danilckaster", "527/527386_5833642-hq.mp3", "348d2798e7053f6f207d58b7c9ada40542d7860a462f6b7bab4f4594d6cd3152"),
    555042: FreesoundPin("bittermelonheart", "555/555042_11910076-hq.mp3", "7b5b6bd8f7567832c98f030e94783dba04108d74fef42fb24d5b2bbf1abb3949"),
    588617: FreesoundPin("Urkki69", "588/588617_12244617-hq.mp3", "c7e6cb9b820aa69d7b5c971c3604a36b31f7efe43eea43a96c9fc805e3db82cf"),
    651646: FreesoundPin("Krizin", "651/651646_5315864-hq.mp3", "417e0bcc7d22a6e3c2906ad53df5b373fcd950920834950c1cfa586b16249e8d"),
    679970: FreesoundPin("Hajisounds", "679/679970_13632374-hq.mp3", "ba8f7be0d39c9f1d4326e8ca7a9742f0ff95cf5740896dba97a2680be1be8bac"),
    752707: FreesoundPin("Nox_Sound", "752/752707_9250976-hq.mp3", "a6dcea75f5195585971e99f0e0b1ffd68387bc14b26be53dcab7ac30294965a1"),
    777709: FreesoundPin("Sadiquecat", "777/777709_5287430-hq.mp3", "53f6045ba1a80ee6c8378f4d2a4bfb35182c40f882342b1f1921fe71afe0c08d"),
}  # fmt: skip


def cache_dir() -> Path:
    root = Path(os.environ.get("NOVA_SFX_CACHE") or Path.home() / ".cache" / "nova-sfx-library")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fetch(url: str, dest: Path, sha256: str) -> Path:
    if dest.exists() and _sha256(dest) == sha256:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    # curl, not urllib: Freesound's edge rejects urllib's TLS fingerprint.
    subprocess.run(
        ["curl", "-fsSL", "--retry", "3", "-A", "Mozilla/5.0", "-o", str(partial), url],
        check=True,
        timeout=180,
    )
    actual = _sha256(partial)
    if actual != sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"{url} changed upstream: sha256 {actual} != pinned {sha256}")
    partial.replace(dest)
    return dest


def kenney_url(pack: str) -> str:
    return f"https://kenney.nl/assets/{pack}"


def freesound_page(sound_id: int) -> str:
    return f"https://freesound.org/people/{FREESOUND[sound_id].user}/sounds/{sound_id}/"


def kenney_file(pack: str, member: str) -> Path:
    spec = KENNEY_PACKS[pack]
    archive = _fetch(spec.url, cache_dir() / "kenney" / f"kenney_{pack}.zip", spec.sha256)
    dest = cache_dir() / "kenney" / pack / member
    if not dest.exists():
        with zipfile.ZipFile(archive) as bundle:
            matches = [name for name in bundle.namelist() if name.rsplit("/", 1)[-1] == member]
            if len(matches) != 1:
                raise KeyError(f"{member!r} not found exactly once in Kenney {pack}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(bundle.read(matches[0]))
    return dest


def freesound_file(sound_id: int) -> Path:
    pin = FREESOUND[sound_id]
    url = f"https://cdn.freesound.org/previews/{pin.preview}"
    return _fetch(url, cache_dir() / "freesound" / f"{sound_id}.mp3", pin.sha256)


def load_kenney(pack: str, member: str) -> np.ndarray:
    return decode(kenney_file(pack, member))


def load_freesound(sound_id: int) -> np.ndarray:
    return decode(freesound_file(sound_id))
