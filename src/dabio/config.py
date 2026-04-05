import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DATA_DIR = PROJECT_ROOT / "data"
STATIONS_CACHE = DATA_DIR / "stations.json"

# Complete Australian Band III DAB+ block table
BAND_III_BLOCKS: dict[str, float] = {
    "5A": 174.928, "5B": 176.640, "5C": 178.352, "5D": 180.064,
    "6A": 181.936, "6B": 183.648, "6C": 185.360, "6D": 187.072,
    "7A": 188.928, "7B": 190.640, "7C": 192.352, "7D": 194.064,
    "8A": 195.936, "8B": 197.648, "8C": 199.360, "8D": 201.072,
    "9A": 202.928, "9B": 204.640, "9C": 206.352, "9D": 208.064,
    "10A": 209.936, "10B": 211.648, "10C": 213.360, "10D": 215.072,
    "11A": 216.928, "11B": 218.640, "11C": 220.352, "11D": 222.064,
    "12A": 223.936, "12B": 225.648, "12C": 227.360, "12D": 229.072,
    "13A": 230.784, "13B": 232.496, "13C": 234.208, "13D": 235.776,
    "13E": 237.488, "13F": 239.200,
}

AUSTRALIAN_PRIORITY_BLOCKS = ["8C", "8D", "9A", "9B", "9C", "9D"]


@dataclass
class SDRConfig:
    gain: int = -1
    driver: str = "auto"


@dataclass
class ServerConfig:
    port: int = 8800
    host: str = "0.0.0.0"


@dataclass
class WelleCliConfig:
    internal_port: int = 7979
    binary_path: str | None = None


@dataclass
class ScanningConfig:
    priority_blocks: list[str] = field(default_factory=lambda: list(AUSTRALIAN_PRIORITY_BLOCKS))
    dwell_time: int = 10
    poll_interval: int = 2
    full_scan: bool = False
    retry_empty: bool = True


@dataclass
class AppConfig:
    sdr: SDRConfig = field(default_factory=SDRConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    welle_cli: WelleCliConfig = field(default_factory=WelleCliConfig)
    scanning: ScanningConfig = field(default_factory=ScanningConfig)
    mock_mode: bool = False

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        path = path or CONFIG_PATH
        if not path.exists():
            return cls()

        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        cfg = cls()
        if "sdr" in raw:
            cfg.sdr = SDRConfig(**{k: v for k, v in raw["sdr"].items() if k in SDRConfig.__dataclass_fields__})
        if "server" in raw:
            cfg.server = ServerConfig(**{k: v for k, v in raw["server"].items() if k in ServerConfig.__dataclass_fields__})
        if "welle_cli" in raw:
            cfg.welle_cli = WelleCliConfig(**{k: v for k, v in raw["welle_cli"].items() if k in WelleCliConfig.__dataclass_fields__})
        if "scanning" in raw:
            cfg.scanning = ScanningConfig(**{k: v for k, v in raw["scanning"].items() if k in ScanningConfig.__dataclass_fields__})
        cfg.mock_mode = raw.get("mock_mode", False)
        return cfg

    def resolve_welle_cli_binary(self) -> str | None:
        if self.welle_cli.binary_path:
            p = Path(self.welle_cli.binary_path)
            if p.exists():
                return str(p)
        # Prefer source-built (v2.7) in /usr/local/bin over apt (v2.4) in /usr/bin
        for candidate in ["/usr/local/bin/welle-cli", "/usr/bin/welle-cli"]:
            if Path(candidate).exists():
                return candidate
        return shutil.which("welle-cli")
