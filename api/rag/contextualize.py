"""Query contextualization: rewrite a follow-up into a standalone search query.

A follow-up like "what about a monthly tenant?" is meaningless to embed on its
own -- the retriever can't see the chat history. So BEFORE retrieval we rewrite
the latest visitor turn into a standalone query using the prior turns.

    history + "what about a monthly tenant?"  --rewrite-->
        "how much notice must a landlord give a monthly tenant in Lagos?"
"""

import pathlib
import sys

import anthropic
from dotenv import load_dotenv

HERE = pathlib.Path(__file__).resolve().parent
load_dotenv(HERE.parent / ".env")
sys.path.insert(0, str(HERE))

MODEL = "claude-haiku-4-5"

REWRITE_SYSTEM = (
    "You rewrite a user's latest message into a standalone search query for a "
    "document-retrieval system. Resolve references (pronouns, 'that', 'what about X', "
    "names or topics mentioned earlier) using the chat history. Capture the user's CURRENT "
    "intent: if the latest message switches topic, base the query on the NEW topic and "
    "drop the previous one. If the latest message is already a complete standalone "
    "question, return it unchanged. Output ONLY the rewritten query text -- no preamble, "
    "no quotes, no explanation."
)


def contextualize(client, history, latest, meter=None):
    """history = [(role, text), ...] prior turns; latest = newest user message.

    meter, if given, is called with the USD cost of the rewrite call.
    """
    if not history:
        return latest                                         # nothing to resolve
    convo = "\n".join(f"{role}: {text}" for role, text in history)
    user = f"Chat history:\n{convo}\n\nLatest user message: {latest}\n\nStandalone query:"
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=128, system=REWRITE_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        if meter:
            from chat import cost_of                          # avoid a circular import
            meter(cost_of(resp.usage))
        rewritten = "".join(b.text for b in resp.content if b.type == "text").strip()
        return rewritten or latest                            # never return empty
    except anthropic.APIError as e:
        # Degrade gracefully: a failed rewrite must not break the chat. Fall back
        # to the raw query -- still retrieves fine for content-bearing follow-ups.
        print(f"  (rewrite failed: {type(e).__name__}; using raw query)", file=sys.stderr)
        return latest
