"""Tune the confidence gate empirically for THIS corpus .

The gate refuses before generation when the top rerank score is below THRESHOLD.
A threshold is NOT transferable between corpora, so we re-measure it here: run a
batch of clearly in-scope and clearly out-of-scope questions, record the top-1
rerank score for each, and read off the gap between the two bands. Set THRESHOLD
in that gap (leaning toward the low end for law, where a false refusal is far
cheaper than a confident wrong answer -- the grounded prompt is the backstop).

    python tune_gate.py
"""

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from engine import RetrievalEngine                              # noqa: E402

# Clearly in-scope: answerable from the firm's website content.
IN_SCOPE = [
    "how much notice must a landlord give a yearly tenant in Lagos?",
    "can my landlord collect 2 years rent?",
    "how much does a consultation cost?",
    "what are your office hours?",
    "who leads the tax practice?",
    "what is the deadline for filing company tax returns?",
    "what are my rights if I am arrested?",
    "how long does a trademark registration last?",
    "do you handle data protection compliance?",
    "can a company in Nigeria have just one shareholder?",
    "what languages do your lawyers speak?",
    "how quickly will you reply to my enquiry?",
    "what does the firm do with my personal data?",
]

# Clearly out-of-scope: plausible questions the website cannot answer.
OUT_OF_SCOPE = [
    "what are the tenancy rules in Abuja?",
    "how long is the trademark opposition period?",
    "what are the grounds for divorce in Nigeria?",
    "what is the punishment for armed robbery?",
    "what is the current exchange rate of the naira to the US dollar?",
    "what is the capital of France?",
    "write me a poem about the sea",
    "who won the last Nigerian presidential election?",
]


def top_scores(engine, questions):
    out = []
    for q in questions:
        hits = engine.search(q, k=5)
        out.append((hits[0][1] if hits else 0.0, q))
    return out


def main():
    engine = RetrievalEngine()
    try:
        ins = sorted(top_scores(engine, IN_SCOPE), reverse=True)
        outs = sorted(top_scores(engine, OUT_OF_SCOPE), reverse=True)
    finally:
        engine.close()

    print("IN-SCOPE (want high scores)")
    for s, q in ins:
        print(f"  {s:0.3f}  {q}")
    print("\nOUT-OF-SCOPE (want low scores)")
    for s, q in outs:
        print(f"  {s:0.3f}  {q}")

    worst_in = min(s for s, _ in ins)
    best_out = max(s for s, _ in outs)
    print("\n" + "-" * 60)
    print(f"worst in-scope score : {worst_in:0.3f}")
    print(f"best out-of-scope    : {best_out:0.3f}")
    if worst_in > best_out:
        print(f"=> clean gap; set THRESHOLD in ({best_out:0.3f}, {worst_in:0.3f}), "
              f"e.g. {(worst_in + best_out) / 2:0.2f}")
    else:
        print("=> bands OVERLAP; no single threshold separates them. Lean low so "
              "the prompt (not the gate) handles the overlap, and rely on citations "
              "+ coverage for anything that slips through.")


if __name__ == "__main__":
    main()
