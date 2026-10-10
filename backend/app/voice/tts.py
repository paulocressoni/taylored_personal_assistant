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
# Clause-level breaks the eager FIRST chunk may use. A sentence terminator is
# still preferred; these only let the opening fragment reach the speaker before
# the sentence that contains it is complete. A hyphen is deliberately absent: it
# also sits inside words ("well-known", "e-mail"), where a stop would tear one.
_CLAUSE_BREAKS = ",;:—"

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


def _clause_break_end(text: str, index: int) -> int | None:
    """Return the offset just past a clause break at `index`, if there is one.

    Args:
        text: The text being scanned.
        index: The position to test.

    Returns:
        The offset just past the clause punctuation, or `None` when `index` is
        not a clause break or the punctuation belongs to a number instead.
    """
    if text[index] not in _CLAUSE_BREAKS:
        return None
    # A comma or colon between digits is part of a number or a clock time
    # ("1,000", "10:30"), not a place to pause.
    if (
        text[index] in ",:"
        and text[index - 1 : index].isdigit()
        and text[index + 1 : index + 2].isdigit()
    ):
        return None
    return index + 1


def _find_split(
    text: str,
    *,
    min_chars: int,
    max_chars: int,
    first_chunk_min_chars: int | None = None,
    first_chunk_max_chars: int | None = None,
    flush: bool,
) -> int | None:
    """Return the offset just past the text that should be spoken next.

    Args:
        text: The buffered text to examine.
        min_chars: Shortest chunk worth its own request; a shorter sentence is
            merged with the following one instead of stalling.
        max_chars: Hard bound, so text without punctuation cannot delay audio
            indefinitely.
        first_chunk_min_chars: When set, the reply's FIRST chunk may also be cut
            at a clause break this many characters in. `None` disables that and
            leaves every chunk on the terminator rule below.
        first_chunk_max_chars: Word-boundary bound for the eager first chunk when
            no clause break arrives; `None` reuses `max_chars`.
        flush: When True the end of the stream is known, so the whole remainder
            is drained.

    Returns:
        The split offset, or `None` when nothing is safe to speak yet.
    """
    eager = first_chunk_min_chars is not None
    first_bound = max_chars
    if eager and first_chunk_max_chars is not None:
        first_bound = min(first_chunk_max_chars, max_chars)
    index = 0
    while index < len(text):
        end = _terminator_end(text, index)
        if end is not None and (flush or end >= min_chars):
            return end
        if end is not None:
            index = end
            continue
        if first_chunk_min_chars is not None:
            clause = _clause_break_end(text, index)
            if clause is not None and clause >= first_chunk_min_chars:
                return clause
        index += 1
        if index >= first_bound:
            return _whitespace_split(text, first_bound)
    if flush:
        return len(text)
    return None


def _split(
    text: str,
    *,
    min_chars: int,
    max_chars: int,
    first_chunk_min_chars: int | None = None,
    first_chunk_max_chars: int | None = None,
    first: bool,
    flush: bool,
) -> tuple[list[str], str]:
    """Split off every complete sentence and return the leftover buffer.

    Args:
        text: The buffered text to split.
        min_chars: Shortest chunk worth its own request, after the first one.
        max_chars: Hard bound on one request, after the first one.
        first_chunk_min_chars: Eager bound for the reply's first chunk; `None`
            disables it.
        first_chunk_max_chars: Word-boundary bound for the eager first chunk.
        first: Whether nothing has been emitted yet, so the next chunk is the
            reply's first.
        flush: Whether the stream has ended.

    Returns:
        The completed chunks, and the text that is not yet safe to speak.
    """
    chunks: list[str] = []
    remaining = text
    eager = first
    while remaining:
        end = _find_split(
            remaining,
            min_chars=min_chars,
            max_chars=max_chars,
            first_chunk_min_chars=first_chunk_min_chars if eager else None,
            first_chunk_max_chars=first_chunk_max_chars if eager else None,
            flush=flush,
        )
        if end is None:
            break
        piece = remaining[:end].strip()
        remaining = remaining[end:]
        if piece:
            chunks.append(piece)
            eager = False
    return chunks, remaining


class SentenceSplitter:
    """Turn streamed reply text into sentences that are worth speaking.

    Fragments below `min_chars` are merged with the sentence that follows them,
    which avoids both a wasted request for a reply that opens with `Yes.` and the
    stall a naive "wait for a long enough sentence" rule would cause.

    The reply's FIRST chunk is the one exception. A whole reply that is a single
    sentence cannot be pipelined at all — nothing is speakable until its final
    terminator — so the opening chunk may also be cut at a clause break, or, when
    none arrives, at a word boundary. Every later chunk keeps the sentence rule:
    by then audio is already playing, so cutting mid-sentence would cost prosody
    and an extra request without saving the caller any waiting.
    """

    def __init__(
        self,
        *,
        min_chars: int,
        max_chars: int,
        first_chunk_min_chars: int | None = None,
        first_chunk_max_chars: int | None = None,
    ) -> None:
        """Configure the split bounds.

        Args:
            min_chars: Shortest chunk worth its own synthesis request.
            max_chars: Hard bound on one request.
            first_chunk_min_chars: Characters buffered before the reply's first
                chunk may be cut at a clause break. `None` keeps every chunk on
                the sentence rule, which is the behaviour without the eager first
                chunk.
            first_chunk_max_chars: Word-boundary bound for the eager first chunk
                when no clause break arrives; `None` reuses `max_chars`.

        Raises:
            ValueError: if `min_chars` is not positive or exceeds `max_chars`, or
                if the eager first-chunk bounds are not positive and ordered.
        """
        if min_chars <= 0 or max_chars < min_chars:
            raise ValueError("min_chars must be positive and <= max_chars")
        if first_chunk_min_chars is not None and first_chunk_min_chars <= 0:
            raise ValueError("first_chunk_min_chars must be positive")
        if (
            first_chunk_min_chars is not None
            and first_chunk_max_chars is not None
            and first_chunk_max_chars < first_chunk_min_chars
        ):
            raise ValueError(
                "first_chunk_max_chars must be at or above first_chunk_min_chars"
            )
        self._min_chars = min_chars
        self._max_chars = max_chars
        self._first_chunk_min_chars = first_chunk_min_chars
        self._first_chunk_max_chars = first_chunk_max_chars
        self._buffer = ""
        # Flips on the first emitted chunk, which is what makes the eager cut a
        # first-chunk rule and not a global one.
        self._emitted = False

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
            first_chunk_min_chars=self._first_chunk_min_chars,
            first_chunk_max_chars=self._first_chunk_max_chars,
            first=not self._emitted,
            flush=False,
        )
        if chunks:
            self._emitted = True
        return chunks

    def flush(self) -> list[str]:
        """Return the trailing fragment once the stream has ended."""
        chunks, _ = _split(
            self._buffer,
            min_chars=self._min_chars,
            max_chars=self._max_chars,
            first_chunk_min_chars=self._first_chunk_min_chars,
            first_chunk_max_chars=self._first_chunk_max_chars,
            first=not self._emitted,
            flush=True,
        )
        self._buffer = ""
        if chunks:
            self._emitted = True
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
