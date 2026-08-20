"""
transcription.py

Audio in, transcript out. No DB access, no extraction logic — mirrors
extraction.py's shape: a single LLM-calling function with no side effects,
so it can be tested and reused independently of how the audio arrived
(interview upload, live in-app recording, future voice-note features, etc.).

Uses Gemini's native audio understanding via the same `llm` instance
ai_engine.py already owns. No separate transcription service (Whisper or
otherwise) is needed — Gemini 2.5 Flash accepts audio directly as part of
a normal multimodal message.

IMPORTANT — browser recording compatibility:
Gemini's audio understanding endpoint only accepts wav, mp3, aiff, aac,
ogg, and flac. Browsers' MediaRecorder API (the standard way to capture
live mic audio in a web app) defaults to webm/Opus in most browsers, which
is NOT in that list — a raw webm blob sent straight to Gemini will be
rejected with a 400. transcode_if_needed() below normalizes webm (and
anything else ffmpeg recognizes but Gemini doesn't) to ogg before it ever
reaches the API, so the frontend can record however the browser wants to
and doesn't need to fight codec constraints itself.

Requires the `ffmpeg` binary on PATH (system dependency, not a pip package).

Cost/context note: Gemini bills audio input at roughly 32 tokens/second
(~1,920 tokens/minute). A 45-minute interview is ~86K tokens just for the
audio. Nothing in this module enforces a length or size limit — that's a
product decision for the upload endpoint (reject/warn above some duration),
not this module's job.
"""

import base64
import subprocess
import tempfile
import os
from langchain_core.messages import HumanMessage

# Formats Gemini's audio understanding endpoint accepts directly — anything
# else gets transcoded first.
GEMINI_SUPPORTED_MIME_TYPES = {
    "audio/wav", "audio/x-wav",
    "audio/mp3", "audio/mpeg",
    "audio/aiff", "audio/x-aiff",
    "audio/aac",
    "audio/ogg",
    "audio/flac", "audio/x-flac",
}


def transcode_if_needed(audio_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
    """
    Transcodes to Ogg/Opus via ffmpeg if the incoming mime_type isn't one
    Gemini accepts directly — this is what makes webm (the default output
    of browser MediaRecorder) work without the frontend needing to do
    anything special. Returns (possibly-transcoded bytes, resulting mime_type).

    If ffmpeg isn't installed, raises a clear error rather than silently
    forwarding an unsupported format to Gemini and getting a confusing 400
    back from a third party.
    """
    if mime_type in GEMINI_SUPPORTED_MIME_TYPES:
        return audio_bytes, mime_type

    try:
        with tempfile.NamedTemporaryFile(suffix=".input") as infile, \
             tempfile.NamedTemporaryFile(suffix=".ogg") as outfile:
            infile.write(audio_bytes)
            infile.flush()

            result = subprocess.run(
                ["ffmpeg", "-y", "-i", infile.name, "-c:a", "libopus", outfile.name],
                capture_output=True, timeout=120
            )
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg transcoding failed: {result.stderr.decode(errors='ignore')}")

            outfile.seek(0)
            return outfile.read(), "audio/ogg"

    except FileNotFoundError:
        raise RuntimeError(
            f"Received audio in an unsupported format ({mime_type}) and ffmpeg is not "
            "installed to convert it. Install ffmpeg on this server, or have the client "
            "record in a Gemini-supported format directly (wav, mp3, aac, ogg, flac)."
        )


def transcribe_audio(llm, audio_bytes: bytes, mime_type: str) -> str:
    """
    Sends raw audio bytes to Gemini for verbatim transcription.

    Deliberately asks for verbatim transcription in whatever language(s)
    were spoken — not translation, not summarization. This transcript is
    meant to be a primary source (grounding a biography, shown to the family
    for verification), so anything Gemini "cleaned up" or shortened would
    quietly reduce trust in exactly the way the product can't afford to.

    If multiple speakers are present (an interviewer prompting, other
    relatives chiming in), asks for speaker labels based on voice — not
    named identification, just Speaker 1 / Speaker 2 / etc., so downstream
    extraction has a chance at attributing statements to the right person
    where the transcript makes that inferable from context.
    """
    audio_bytes, mime_type = transcode_if_needed(audio_bytes, mime_type)
    encoded = base64.b64encode(audio_bytes).decode("utf-8")

    message = HumanMessage(content=[
        {
            "type": "text",
            "text": (
                "Transcribe this audio verbatim, in the language(s) it was spoken in. "
                "Do not translate, summarize, paraphrase, or omit anything — this transcript "
                "will be used as a primary source for a family history record, so accuracy "
                "and completeness matter more than polish. "
                "If there is more than one speaker, label turns as Speaker 1, Speaker 2, etc. "
                "based on voice changes — you don't need to know who they are."
            )
        },
        {
            "type": "media",
            "data": encoded,
            "mime_type": mime_type
        }
    ])

    response = llm.invoke([message])
    return response.content.strip()