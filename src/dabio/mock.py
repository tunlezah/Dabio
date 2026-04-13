"""Mock welle-cli server for development without SDR hardware.

Generates synthetic audio (sine wave) encoded as MP3 and serves
the same HTTP endpoints as welle-cli's built-in web server.
"""
import asyncio
import io
import json
import math
import struct
import time
from collections.abc import AsyncGenerator

from .logging_config import get_logger

log = get_logger("mock")

SAMPLE_RATE = 48000
CHANNELS = 2
MOCK_BITRATE = 128

# Fake station data for Australian DAB+
MOCK_STATIONS = [
    {"sid": "D201", "label": "ABC Canberra",      "ensemble": "ABC/SBS National", "eid": "E001", "block": "9C", "codec": "DAB+", "bitrate": 64, "protection": "EEP 3-A", "snr": 22.0},
    {"sid": "D202", "label": "Triple J",          "ensemble": "ABC/SBS National", "eid": "E001", "block": "9C", "codec": "DAB+", "bitrate": 80, "protection": "EEP 3-A", "snr": 21.5},
    {"sid": "D203", "label": "ABC News Radio",    "ensemble": "ABC/SBS National", "eid": "E001", "block": "9C", "codec": "DAB+", "bitrate": 48, "protection": "EEP 3-A", "snr": 20.0},
    {"sid": "D204", "label": "ABC Classic",       "ensemble": "ABC/SBS National", "eid": "E001", "block": "9C", "codec": "DAB+", "bitrate": 96, "protection": "EEP 3-A", "snr": 19.8},
    {"sid": "D205", "label": "Triple J Unearthed","ensemble": "ABC/SBS National", "eid": "E001", "block": "9C", "codec": "DAB+", "bitrate": 48, "protection": "EEP 3-A", "snr": 17.2},
    {"sid": "D206", "label": "SBS Radio 1",       "ensemble": "ABC/SBS National", "eid": "E001", "block": "9C", "codec": "DAB+", "bitrate": 64, "protection": "EEP 3-A", "snr": 12.0},
    {"sid": "D207", "label": "SBS Chill",         "ensemble": "ABC/SBS National", "eid": "E001", "block": "9C", "codec": "DAB+", "bitrate": 32, "protection": "EEP 3-A", "snr": 8.5},
    {"sid": "D208", "label": "SBS PopAsia",       "ensemble": "ABC/SBS National", "eid": "E001", "block": "9C", "codec": "DAB+", "bitrate": 32, "protection": "EEP 3-A", "snr": 4.0},
    {"sid": "DA01", "label": "Mix 106.3",         "ensemble": "Canberra DAB",     "eid": "E002", "block": "8D", "codec": "DAB+", "bitrate": 64, "protection": "EEP 3-A", "snr": 25.3},
    {"sid": "DA02", "label": "HIT 104.7",         "ensemble": "Canberra DAB",     "eid": "E002", "block": "8D", "codec": "DAB+", "bitrate": 64, "protection": "EEP 3-A", "snr": 18.1},
    {"sid": "DA03", "label": "2CC",               "ensemble": "Canberra DAB",     "eid": "E002", "block": "8D", "codec": "DAB",  "bitrate": 128,"protection": "UEP 3",   "snr": 14.7},
    {"sid": "DA04", "label": "2CA",               "ensemble": "Canberra DAB",     "eid": "E002", "block": "8D", "codec": "DAB",  "bitrate": 128,"protection": "UEP 3",   "snr": 11.0},
]

MOCK_DLS_MESSAGES = [
    "Now Playing: Test Tone - Dabio Mock Mode",
    "Welcome to Dabio - DAB+ Radio Web Player",
    "Mock Mode Active - No SDR Hardware Required",
    "Testing 1, 2, 3 - Synthetic Audio Stream",
    "Dabio v1.0 - Australian DAB+ Radio",
]


def _generate_sine_pcm(frequency: float, duration_ms: int, volume: float = 0.3) -> bytes:
    """Generate stereo PCM sine wave data (16-bit, 48kHz)."""
    num_samples = int(SAMPLE_RATE * duration_ms / 1000)
    buf = io.BytesIO()
    for i in range(num_samples):
        t = i / SAMPLE_RATE
        sample = int(volume * 32767 * math.sin(2 * math.pi * frequency * t))
        # Stereo: write same sample for L and R
        buf.write(struct.pack("<hh", sample, sample))
    return buf.getvalue()


def _encode_mp3(pcm_data: bytes) -> bytes:
    """Encode PCM data to MP3 using lameenc."""
    try:
        import lameenc
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(MOCK_BITRATE)
        encoder.set_in_sample_rate(SAMPLE_RATE)
        encoder.set_channels(CHANNELS)
        encoder.set_quality(2)
        mp3_data = encoder.encode(pcm_data)
        mp3_data += encoder.flush()
        return mp3_data
    except ImportError:
        log.warning("lameenc not installed, returning raw PCM wrapped in basic header")
        return pcm_data


async def mock_mp3_stream(sid: str) -> AsyncGenerator[bytes, None]:
    """Generate an endless MP3 audio stream with a sine wave tone.
    Different SIDs get different frequencies for audible distinction."""
    # Derive frequency from SID for variety
    try:
        freq_base = int(sid, 16) % 8
    except ValueError:
        freq_base = 0
    frequency = 440.0 + (freq_base * 50)  # 440Hz to 790Hz range

    chunk_duration_ms = 200  # 200ms chunks
    log.info(f"Starting mock MP3 stream for SID {sid} at {frequency}Hz")

    while True:
        pcm = _generate_sine_pcm(frequency, chunk_duration_ms)
        mp3 = _encode_mp3(pcm)
        yield mp3
        await asyncio.sleep(chunk_duration_ms / 1000)


def get_mock_mux_json(channel: str = "9C") -> dict:
    """Return mock /mux.json data matching welle-cli's format."""
    stations_on_channel = [s for s in MOCK_STATIONS if s["block"] == channel]
    dls_idx = int(time.time() / 10) % len(MOCK_DLS_MESSAGES)

    services = []
    for s in stations_on_channel:
        services.append({
            "sid": s["sid"],
            "label": {"label": s["label"], "shortlabel": s["label"][:8]},
            "dls_label": MOCK_DLS_MESSAGES[dls_idx],
            "dls_time": int(time.time()),
            "dls_lastchange": int(time.time()) - 5,
            # welle-cli-shaped audio info: populated by the decoder for the
            # currently-tuned service. In the mock we expose it for every
            # service on the channel.
            "channels": 2,
            "samplerate": SAMPLE_RATE,
            "mode": "stereo",
            "programType": 10,
            "ptystring": "Pop Music",
            "language": 9,
            "languagestring": "English",
            "components": [
                {
                    "componentnr": 0,
                    "primary": True,
                    "caflag": False,
                    "label": s["label"],
                    "transportmode": "audio",
                    "ascty": s.get("codec", "DAB+"),
                    "subchannel": {
                        "subchid": 0,
                        "bitrate": s.get("bitrate", MOCK_BITRATE),
                        "cu": 18,
                        "sad": 0,
                        "protection": s.get("protection", "EEP 3-A"),
                    },
                },
            ],
            "frameerrors": 0,
            "rserrors": 0,
            "aacerrors": 0,
        })

    ensemble = stations_on_channel[0] if stations_on_channel else {"ensemble": "Mock", "eid": "FFFF"}
    return {
        "ensemble": {
            "label": {"label": ensemble.get("ensemble", "Mock"), "shortlabel": "Mock"},
            "id": ensemble.get("eid", "FFFF"),
        },
        "services": services,
        "demodulator": {
            "snr": 25.0,
            "frequencyCorrection": 0,
            "isFicCrcOk": True,
        },
        "utctime": {"year": 2026, "month": 4, "day": 5},
    }


def get_mock_stations_for_scan(blocks: list[str]) -> list[dict]:
    """Return mock stations for given blocks (used by scanner in mock mode)."""
    return [s for s in MOCK_STATIONS if s["block"] in blocks]
