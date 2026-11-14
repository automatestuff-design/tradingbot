"""Optional plain-language summary of a trade request, written by Claude.

Only used when ANTHROPIC_API_KEY is set (or an `ant auth login` profile exists) and the
`anthropic` package is installed. Claude only rewords the facts it is given; the strategy's own
numbers stay on the approval card so the summary can be checked against them.
"""
import json
import os

MODEL = "claude-opus-5-5"

SYSTEM = """You summarize a proposed stock trade for the person who must approve or reject it.
You are given the facts as JSON: the order, the strategy's exact reasons, the risk plan, recent price
moves and account state. Write 2-3 short sentences in plain language:
- what the strategy saw and what the trade risks, using only the numbers provided;
- anything in the facts that argues against it (a big recent move, position limits nearly used,
  the daily loss limit close, a currency exposure).
Do not invent news, earnings dates, analyst views or any fact not in the JSON. Do not predict whether
the trade will work and do not tell the person what to do. No headings or bullet points."""

_client = None


def available():
    if os.getenv("AI_SUMMARY", "on").lower() in ("0", "off", "no", "false"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_PROFILE"))


def summarize(facts: dict, timeout: float = 20.0) -> str:
    """Blocking call; the engine runs it in a worker thread. Raises on failure."""
    global _client
    import anthropic
    if _client is None:
        _client = anthropic.Anthropic(max_retries=1)
    resp = _client.with_options(timeout=timeout).beta.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},  # re-run on a fallback model if the request is declined
        messages=[{"role": "user", "content": json.dumps(facts, default=str)}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to summarize this request")
    text = " ".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise RuntimeError("empty summary")
    return text
