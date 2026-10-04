"""Text-to-speech for one voice turn, one sentence at a time.

Synthesis is fired per sentence rather than per reply, so the first audio byte no
longer waits for the whole answer: the reply's first sentence can be playing
before the model has finished generating the second.

Unlike the transcriber, this module does not turn failures into a result object.
It is a stream: once a chunk has been handed over there is nothing left to return
an error alongside, because the audio has already been played. Failures therefore
propagate, and the voice session is the tool boundary that catches them and
degrades to text.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# `gpt-4o-mini-tts` streams raw PCM at 24 kHz, mono, signed 16-bit little-endian.
# Exposed so the session can resample for a device that wants another rate
# without hardcoding the provider's native one.
PCM_SAMPLE_RATE = 24000

_TERMINATORS = ".!?"
_CLOSERS = "\"')]}"
_NEWLINE = "\n"

# Abbreviations whose trailing period is not a sentence end. Dotted forms such as
# `e.g.`, `z.B.` and `p.ex.` need no entry: their period lands after a single
# letter, which the initial rule below already covers.
_ABBREVIATIONS = frozenset(
    {
        # English
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "st",
        "vs",
        "etc",
        "inc",
        "ltd",
        "jr",
        "sr",
        "approx",
        "fig",
        # German
        "ca",
        "nr",
        "bzw",
        "evtl",
        "ggf",
        "usw",
        "resp",
        # Portuguese
        "dra",
        "sra",
    }
)


class Synthesizer(Protocol):
    """Minimal text-to-speech surface the voice session depends on."""

    def synthesize(self, text: str, voice: str) -> AsyncIterator[bytes]:
        """Stream raw PCM for one sentence.

        The provider chooses the chunk boundaries, so individual chunks are
        arbitrary byte ranges and only their concatenation is meaningful PCM.
        Implementations raise on failure rather than yielding an error.
        """
        ...


def _is_abbreviation(prefix: str) -> bool:
    """Return True when the word before a period is an abbreviation or initial.

    Args:
        prefix: The text that precedes the period.

    Returns:
        True for a known abbreviation, and for a one-letter token, which is an
        initial such as `J.` in `J. Silva` far more often than a sentence end.
    """
    token = ""
    for character in reversed(prefix):
        if not character.isalpha():
            break
        token = character + token
    if not token:
        return False
    if len(token) == 1:
        return True
    return token.lower() in _ABBREVIATIONS


def _terminator_end(text: str, index: int) -> int | None:
    """Return the offset just past a sentence end at `index`, if there is one.

    Args:
        text: The text being scanned.
        index: The position to test.

    Returns:
        The offset just past the terminator and any closing punctuation, or
        `None` when `index` does not end a sentence.
    """
    character = text[index]
    if character == _NEWLINE:
        return index + 1
    if character not in _TERMINATORS:
        return None
    if character == ".":
        following = text[index + 1 : index + 2]
        # A digit means a decimal point ("21.5"); another dot means an ellipsis,
        # whose later dots get their own chance to end the sentence.
        if following and (following.isdigit() or following == "."):
            return None
        if _is_abbreviation(text[:index]):
            return None

    end = index + 1
    while end < len(text) and (text[end] in _TERMINATORS or text[end] in _CLOSERS):
        end += 1
    # A terminator must be followed by a break, otherwise a domain such as
    # `example.com` would be torn in half.
    if end < len(text) and not text[end].isspace():
        return None
    return end


def _whitespace_split(text: str, max_chars: int) -> int:
    """Return a split offset at or before the limit, never inside a word."""
    space = text[:max_chars].rfind(" ")
    if space > 0:
        return space + 1
    return max_chars


def _find_split(
    text: str, *, min_chars: int, max_chars: int, flush: bool
) -> int | None:
    """Return the offset just past the text that should be spoken next.

    Args:
        text: The buffered text to examine.
        min_chars: Shortest chunk worth its own request; a shorter sentence is
            merged with the following one instead of stalling.
        max_chars: Hard bound, so text without punctuation cannot delay audio
            indefinitely.
        flush: When True the end of the stream is known, so the whole remainder
            is drained.

    Returns:
        The split offset, or `None` when nothing is safe to speak yet.
    """
    index = 0
    while index < len(text):
        end = _terminator_end(text, index)
        if end is not None and (flush or end >= min_chars):
            return end
        if end is not None:
            index = end
            continue
        index += 1
        if index >= max_chars:
            return _whitespace_split(text, max_chars)
    if flush:
        return len(text)
    return None


def _split(
    text: str, *, min_chars: int, max_chars: int, flush: bool
) -> tuple[list[str], str]:
    """Split off every complete sentence and return the leftover buffer."""
    chunks: list[str] = []
    remaining = text
    while remaining:
        end = _find_split(
            remaining, min_chars=min_chars, max_chars=max_chars, flush=flush
        )
        if end is None:
            break
        piece = remaining[:end].strip()
        remaining = remaining[end:]
        if piece:
            chunks.append(piece)
    return chunks, remaining


class SentenceSplitter:
    """Turn streamed reply text into sentences that are worth speaking.

    Fragments below `min_chars` are merged with the sentence that follows them,
    which avoids both a wasted request for a reply that opens with `Yes.` and the
    stall a naive "wait for a long enough sentence" rule would cause.
    """

    def __init__(self, *, min_chars: int, max_chars: int) -> None:
        """Configure the split bounds.

        Args:
            min_chars: Shortest chunk worth its own synthesis request.
            max_chars: Hard bound on one request.

        Raises:
            ValueError: if min_chars is not positive, or exceeds max_chars.
        """
        if min_chars <= 0 or max_chars < min_chars:
            raise ValueError("min_chars must be positive and <= max_chars")
        self._min_chars = min_chars
        self._max_chars = max_chars
        self._buffer = ""

    def push(self, delta: str) -> list[str]:
        """Add a streamed token and return every sentence now safe to speak.

        Args:
            delta: The newly generated text, usually one token.

        Returns:
            The completed sentences, in order; empty when the reply has not
            reached a break yet.
        """
        self._buffer += delta
        chunks, self._buffer = _split(
            self._buffer,
            min_chars=self._min_chars,
            max_chars=self._max_chars,
            flush=False,
        )
        return chunks

    def flush(self) -> list[str]:
        """Return the trailing fragment once the stream has ended."""
        chunks, _ = _split(
            self._buffer,
            min_chars=self._min_chars,
            max_chars=self._max_chars,
            flush=True,
        )
        self._buffer = ""
        return chunks


class OpenAISynthesizer:
    """Synthesize speech through the OpenAI audio endpoint."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        instructions: str,
        timeout_seconds: float,
    ) -> None:
        """Create the client and remember the request parameters.

        Args:
            model: Speech model name.
            base_url: OpenAI-compatible API root, including the version segment.
            api_key: Provider credential.
            instructions: Spoken style, sent with every request.
            timeout_seconds: Per-request timeout.
        """
        self._model = model
        self._instructions = instructions
        # One retry, as in the transcriber: a late turn is better served by
        # telling the user than by spending seconds on backoff.
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=1,
        )

    async def synthesize(self, text: str, voice: str) -> AsyncIterator[bytes]:
        """Stream PCM for one sentence.

        Args:
            text: The sentence to speak.
            voice: Provider voice name, resolved from the reply language.

        Yields:
            Raw 24 kHz mono s16le PCM. Every chunk has an even byte count, so
            the concatenation is always sample-aligned.

        Raises:
            Exception: whatever the provider raised; the caller is the boundary.
        """
        request: dict[str, Any] = {
            "model": self._model,
            "voice": voice,
            "input": text,
            "instructions": self._instructions,
            "response_format": "pcm",
        }

        async with self._client.audio.speech.with_streaming_response.create(
            **request
        ) as response:
            # The transport may split the stream at any byte, so a chunk can end
            # half-way through a sample; carry that byte into the next chunk
            # instead of emitting audio the client cannot align.
            carry = b""
            async for chunk in response.iter_bytes():
                data = carry + chunk
                if len(data) % 2:
                    carry = data[-1:]
                    data = data[:-1]
                else:
                    carry = b""
                if data:
                    yield data
            if carry:
                # An odd total length means the provider truncated the audio.
                logger.debug("Dropped a stray trailing byte from %s", self._model)
