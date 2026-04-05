from dataclasses import dataclass, field


@dataclass
class Station:
    """A DAB+ radio station identified by ensemble + service ID."""
    service_id: str  # hex SID e.g. "D220"
    ensemble_id: str  # hex ensemble ID e.g. "1001"
    name: str
    ensemble_name: str
    block: str  # DAB channel block e.g. "9C"
    # Composite unique key
    station_id: str = ""  # auto-generated: "{ensemble_id}_{service_id}"

    def __post_init__(self):
        if not self.station_id:
            self.station_id = f"{self.ensemble_id}_{self.service_id}"


@dataclass
class StationMetadata:
    station_id: str
    dls_text: str = ""  # Dynamic Label Segment (current song/show)
    slide_url: str | None = None  # SLS slideshow image URL
    programme_type: str = ""
    bitrate: int = 0
    sample_rate: int = 0


@dataclass
class ScanResult:
    block: str
    ensemble_name: str
    ensemble_id: str
    stations: list[Station] = field(default_factory=list)
