"""Spoken commentary for the line the director wrote.

The director already produces a broadcast sentence for every cut. This speaks
it, using Gemini's text-to-speech on Vertex.

Generated on demand rather than on every cut. TTS is another model call, quota
is the tightest constraint in this project, and most viewers never turn audio
on — so nothing is synthesised until a client actually asks for it. Results are
cached by line, because the same sentence never needs paying for twice.

Vertex returns raw 16-bit PCM at 24 kHz. Browsers will not play that, so it is
wrapped in a WAV header here rather than shipping a decoder to the client.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import struct
from collections import OrderedDict

from ..config import settings

log = logging.getLogger("gallery.commentary")

# Two voices, as on a real broadcast. Charon is lower and steadier — the
# play-by-play. Puck is brighter and faster — the ex-driver reacting beside
# them. A single voice reading both halves sounds like a press release.
LEAD_SPEAKER, LEAD_VOICE = "Lead", "Charon"
COLOUR_SPEAKER, COLOUR_VOICE = "Colour", "Puck"
VOICE = LEAD_VOICE
SAMPLE_RATE = 24000
MAX_CACHE = 48


def wav_header(n_bytes: int, rate: int = SAMPLE_RATE, channels: int = 1,
               bits: int = 16) -> bytes:
    """Minimal RIFF/WAVE header for raw little-endian PCM."""
    byte_rate = rate * channels * bits // 8
    block_align = channels * bits // 8
    return (
        b"RIFF" + struct.pack("<I", 36 + n_bytes) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                byte_rate, block_align, bits)
        + b"data" + struct.pack("<I", n_bytes)
    )


class Commentary:
    def __init__(self) -> None:
        self._client = None
        self._cache: "OrderedDict[str, bytes]" = OrderedDict()
        self._lock = asyncio.Lock()
        self.model = settings.tts_model
        self.generated = 0
        self.last_error: str | None = None

    @property
    def ready(self) -> bool:
        return settings.gemini_ready

    def _ensure(self):
        if self._client is not None:
            return self._client
        from google import genai
        self._client = (
            genai.Client(vertexai=True, project=settings.gcp_project,
                         location=settings.gcp_location)
            if settings.use_vertex else genai.Client(api_key=settings.api_key)
        )
        return self._client

    @staticmethod
    def key(line: str) -> str:
        return hashlib.sha1(line.strip().lower().encode()).hexdigest()[:16]

    async def speak(self, line: str, colour: str = "",
                    big: bool = False) -> bytes | None:
        """WAV bytes for the call. Two voices when there is a colour line."""
        line = (line or "").strip()
        colour = (colour or "").strip()
        if not line or not self.ready:
            return None
        k = self.key(line + "|" + colour + ("|!" if big else ""))

        async with self._lock:
            hit = self._cache.get(k)
            if hit is not None:
                self._cache.move_to_end(k)
                return hit

        try:
            from google.genai import types
            client = self._ensure()

            if colour:
                # Direction in the prompt shapes delivery — the model reads the
                # framing, not just the words. A pass that has just happened
                # should not be read at the same pitch as a gap report.
                mood = ("A move has just been completed. Call it like it is "
                        "happening, then react."
                        if big else
                        "Live race commentary, measured and specific.")
                script = (f"{mood}\n{LEAD_SPEAKER}: {line}\n"
                          f"{COLOUR_SPEAKER}: {colour}")
                speech = types.SpeechConfig(
                    multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
                        speaker_voice_configs=[
                            types.SpeakerVoiceConfig(
                                speaker=LEAD_SPEAKER,
                                voice_config=types.VoiceConfig(
                                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                        voice_name=LEAD_VOICE))),
                            types.SpeakerVoiceConfig(
                                speaker=COLOUR_SPEAKER,
                                voice_config=types.VoiceConfig(
                                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                        voice_name=COLOUR_VOICE))),
                        ]))
            else:
                script = line
                speech = types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=LEAD_VOICE)))

            resp = await client.aio.models.generate_content(
                model=self.model,
                contents=script,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"], speech_config=speech),
            )
            part = resp.candidates[0].content.parts[0]
            pcm = part.inline_data.data
        except Exception as exc:  # noqa: BLE001 — audio is never load-bearing
            self.last_error = str(exc).replace("\n", " ")[:180]
            log.warning("tts failed: %s", self.last_error)
            return None

        wav = wav_header(len(pcm)) + pcm
        async with self._lock:
            self._cache[k] = wav
            while len(self._cache) > MAX_CACHE:
                self._cache.popitem(last=False)
        self.generated += 1
        self.last_error = None
        return wav


commentary = Commentary()
