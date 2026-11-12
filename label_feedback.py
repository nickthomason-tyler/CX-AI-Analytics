#!/usr/bin/env python3
"""Classify thumbs-down feedback into a failure taxonomy, in place, in the warehouse.

    python3 label_feedback.py                  # label every unlabelled negative turn
    python3 label_feedback.py --limit 50       # one batch, to sanity-check first
    python3 label_feedback.py --report         # summarise what is already labelled (no API)
    python3 label_feedback.py --relabel        # discard existing labels and redo
    python3 label_feedback.py --brief "ERP Pro"  # dump a product's corpus for synthesis

The judge is a dedicated Foundry agent ("Feedback Failure Classifier"), so this needs
no credential beyond the Foundry key the extractor already uses. Two hard-won facts
about /api/agents/{id}/generate shape the implementation:

  * `structuredOutput` must be wrapped -- {"structuredOutput": {"schema": {...}}}.
    Passing the bare JSON Schema returns HTTP 500.
  * the per-request `instructions` override is SILENTLY IGNORED. The agent always
    runs its stored prompt, which is why the taxonomy lives in the agent itself
    (foundry-agents/agents/feedback-failure-classifier/instructions.md) rather than
    being sent per call. Editing the taxonomy means updating that agent.

Labels are written to fact_feedback_turn and never touched by extract.py, so
re-extracting a conversation does not throw the work away.
"""

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from foundry_client import FoundryClient, FoundryError

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "foundry_analytics.db")
# Where the agent backups live. The local copy comes FIRST and is the one that
# matters for scheduled runs: the canonical backups sit in the OneDrive workspace,
# and ~/Library/CloudStorage is TCC-protected, so a launchd job cannot read them
# (the same reason the warehouse itself moved out of OneDrive on 2026-08-10).
# Keep ./agents/<name>/metadata.json in sync when an agent is recreated.
AGENTS_DIRS = (
    os.path.join(HERE, "agents"),
    os.path.join(
        os.path.expanduser("~"), "Library", "CloudStorage",
        "OneDrive-TylerTechnologies,Inc", "Documents PC", "AI_Busyness",
        "foundry-agents", "agents"),
)


def _agent_metadata(name):
    """First existing metadata.json for an agent, else the preferred path so the
    error message points at the copy the caller should create."""
    paths = [os.path.join(d, name, "metadata.json") for d in AGENTS_DIRS]
    return next((p for p in paths if os.path.exists(p)), paths[0])


CLASSIFIER_METADATA = _agent_metadata("feedback-failure-classifier")
SYNTH_METADATA = _agent_metadata("feedback-synthesis-analyst")
BRIEFS_DIR = os.path.join(HERE, "feedback-briefs")

# Must match instructions.md in the classifier agent. Kept here as a validation
# gate: a model that invents a category gets rejected rather than quietly widening
# the taxonomy and fragmenting the counts.
LABELS = (
    "kb_gap", "over_narrow_refusal", "incorrect_answer", "wrong_ownership_claim",
    "incomplete_answer", "too_generic", "missing_source_or_next_step",
    "language_handling", "tool_or_system_error", "formatting_or_tone",
    "not_a_defect", "other",
)
SEVERITIES = ("misleading", "unhelpful", "minor")
FIX_TYPES = ("knowledge_base", "instructions", "routing", "model", "none")

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(LABELS)},
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "fix_type": {"type": "string", "enum": list(FIX_TYPES)},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["label", "severity", "fix_type", "confidence", "rationale"],
}

# The response is what the user actually reacted to, so it gets the most room;
# beyond this a complaint is essentially never about the tail of the text.
MAX_RESPONSE_IN_PROMPT = 3000
MAX_CONTEXT_TURNS = 2


def resolve_agent_id(explicit=None, path=CLASSIFIER_METADATA, what="classifier"):
    if explicit:
        return explicit
    if os.path.exists(path):
        with open(path) as fh:
            agent_id = (json.load(fh) or {}).get("id")
        if agent_id:
            return agent_id
    sys.exit(
        f"No {what} agent id. Expected it in\n  {os.path.normpath(path)}\n"
        "Create the agent first (see README, 'Classifying the complaints'), or pass "
        "--agent-id.")


def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path, timeout=120)
    conn.row_factory = sqlite3.Row
    return conn


def preceding_context(conn, conversation_id, turn_index):
    """The turns just before the flagged one.

    Complaints are frequently about context the flagged turn dropped ("ignored the
    middle question", "I already told you"), so judging it in isolation misreads it.
    Pulled from raw_transcript because unrated turns are deliberately not stored as
    rows -- they are the majority and carry no signal of their own.
    """
    row = conn.execute("SELECT payload FROM raw_transcript WHERE conversation_id = ?",
                       (conversation_id,)).fetchone()
    if not row or not row["payload"]:
        return []
    try:
        payload = json.loads(row["payload"])
    except (ValueError, TypeError):
        return []

    turns, index = [], 0
    for conv in (payload if isinstance(payload, list) else []):
        if not isinstance(conv, dict):
            continue
        for turn in (conv.get("conversation") or []):
            if not isinstance(turn, dict):
                continue
            index += 1
            if index >= turn_index:
                continue
            turns.append((turn.get("question") or "").strip())
    return [q for q in turns[-MAX_CONTEXT_TURNS:] if q]


def build_prompt(conn, row):
    parts = [
        f"Assistant: {row['agent_name_current'] or row['agent_name'] or '(unknown)'}",
        f"Product: {row['product'] or '(untagged)'}",
    ]
    context = preceding_context(conn, row["conversation_id"], row["turn_index"])
    if context:
        parts.append("\nEarlier in this conversation the user asked:")
        parts.extend(f"  - {q[:300]}" for q in context)

    response = row["response"] or ""
    if len(response) > MAX_RESPONSE_IN_PROMPT:
        response = response[:MAX_RESPONSE_IN_PROMPT] + "\n[...response truncated...]"

    parts += [
        f"\nThe user's question on the turn they marked thumbs-down:\n{row['question'] or '(empty)'}",
        f"\nThe assistant's response:\n{response or '(empty)'}",
        f"\nWhat the user typed as their complaint:\n{(row['comment'] or '').strip() or '(they left no comment)'}",
        "\nClassify this failure.",
    ]
    return "\n".join(parts)


def parse_verdict(text):
    """The endpoint returns the structured object as a JSON *string* in `response`."""
    if isinstance(text, dict):
        return text
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Models occasionally wrap it in prose or a fenced block despite the schema.
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None


def validate(verdict):
    """Reject anything outside the taxonomy rather than storing a novel category."""
    if not isinstance(verdict, dict):
        return None, "unparseable response"
    label = str(verdict.get("label") or "").strip()
    severity = str(verdict.get("severity") or "").strip()
    fix_type = str(verdict.get("fix_type") or "").strip()
    if label not in LABELS:
        return None, f"label not in taxonomy: {label!r}"
    if severity not in SEVERITIES:
        return None, f"severity not in taxonomy: {severity!r}"
    if fix_type not in FIX_TYPES:
        return None, f"fix_type not in taxonomy: {fix_type!r}"
    try:
        confidence = float(verdict.get("confidence"))
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))
    return {
        "label": label,
        "severity": severity,
        "fix_type": fix_type,
        "label_confidence": confidence,
        "label_rationale": (str(verdict.get("rationale") or "").strip() or None),
    }, None


def classify(client, agent_id, prompt, model_name):
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "maxTokens": 1200,
        # Wrapped: the bare schema is a 500. See module docstring.
        "structuredOutput": {"schema": OUTPUT_SCHEMA},
    }
    try:
        payload = client.post(f"/api/agents/{agent_id}/generate", body)
    except FoundryError as exc:
        if getattr(exc, "status", None) != 500:
            raise
        # Some inputs make structured output 500 deterministically while the very
        # same prompt succeeds without it (reproduced on two turns out of ~990;
        # short questions containing record-id lists). The model still returns the
        # right object, just fenced in prose, and parse_verdict digs it out -- so
        # retry unconstrained rather than losing the row.
        del body["structuredOutput"]
        payload = client.post(f"/api/agents/{agent_id}/generate", body)

    verdict, problem = validate(parse_verdict((payload or {}).get("response")))
    if verdict is None:
        return None, problem
    verdict["labeler_model"] = model_name
    verdict["labeled_at"] = dt.datetime.now().isoformat(timespec="seconds")
    return verdict, None


def pending_turns(conn, relabel=False, limit=None, product=None,
                  include_uncommented=False):
    where = ["rating = 'THUMBS_DOWN'"]
    params = []
    if not include_uncommented:
        # A downvote with no comment can still be labelled from the transcript, but
        # commented ones are both more numerous and far more diagnostic, so they go
        # first by default.
        where.append("has_comment = 1")
    if not relabel:
        where.append("(label IS NULL OR trim(label) = '')")
    if product:
        where.append("product = ?")
        params.append(product)
    sql = (f"SELECT turn_pk, conversation_id, turn_index, question, response, comment, "
           f"agent_name, agent_name_current, product FROM v_feedback_turns "
           f"WHERE {' AND '.join(where)} ORDER BY conv_day DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def write_labels(conn, rows):
    if not rows:
        return
    conn.executemany(
        "UPDATE fact_feedback_turn SET label=?, severity=?, fix_type=?, "
        "label_confidence=?, label_rationale=?, labeler_model=?, labeled_at=? "
        "WHERE turn_pk=?",
        [(r["label"], r["severity"], r["fix_type"], r["label_confidence"],
          r["label_rationale"], r["labeler_model"], r["labeled_at"], r["turn_pk"])
         for r in rows])
    conn.commit()


def run_labelling(conn, client, agent_id, model_name, turns, workers=3,
                  flush_every=25):
    """Label with modest concurrency: Foundry throttles hard, and the client's 429
    backoff means extra workers past a few just queue up inside sleeps."""
    print(f"Labelling {len(turns):,} negative turns via agent {agent_id} "
          f"({workers} workers) ...")
    # Prompts are built here, on this thread, because they read raw_transcript and a
    # sqlite3 connection may only be used from the thread that created it. The pool
    # then does nothing but HTTP.
    prompts = [(row, build_prompt(conn, row)) for row in turns]

    done = 0
    labelled, failures = [], []
    lock = threading.Lock()

    def work(item):
        row, prompt = item
        try:
            verdict, problem = classify(client, agent_id, prompt, model_name)
        except FoundryError as exc:
            return row, None, str(exc)
        return row, verdict, problem

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, item) for item in prompts]
        for fut in as_completed(futures):
            row, verdict, problem = fut.result()
            done += 1
            if verdict is None:
                failures.append((row["turn_pk"], problem))
            else:
                verdict["turn_pk"] = row["turn_pk"]
                labelled.append(verdict)
            # Flush periodically so an interrupted run keeps its work; the next run
            # picks up only the still-unlabelled rows.
            with lock:
                if len(labelled) >= flush_every:
                    write_labels(conn, labelled)
                    labelled = []
            if done % 25 == 0 or done == len(turns):
                print(f"  {done}/{len(turns)} · {len(failures)} failed")

    write_labels(conn, labelled)
    print(f"  labelled: {len(turns) - len(failures):,}  failed: {len(failures):,}")
    if failures:
        print("  first few failures (left unlabelled, safe to re-run):")
        for turn_pk, problem in failures[:5]:
            print(f"    {turn_pk}: {problem}")
    return len(failures)


def report(conn):
    """What the corpus says, without spending a single API call."""
    total = conn.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN label IS NOT NULL AND trim(label)<>'' "
        "THEN 1 ELSE 0 END) labelled FROM v_feedback_turns "
        "WHERE rating='THUMBS_DOWN' AND has_comment=1").fetchone()
    print(f"\nCommented negative turns: {total['n']:,}  "
          f"labelled: {total['labelled'] or 0:,}")

    for title, sql in [
        ("By failure category", """
            SELECT label, COUNT(*) n,
                   ROUND(AVG(label_confidence), 2) avg_conf,
                   COUNT(DISTINCT product) products
            FROM v_feedback_turns
            WHERE rating='THUMBS_DOWN' AND label IS NOT NULL AND trim(label)<>''
            GROUP BY label ORDER BY n DESC"""),
        ("By where the fix belongs", """
            SELECT fix_type, severity, COUNT(*) n
            FROM v_feedback_turns
            WHERE rating='THUMBS_DOWN' AND fix_type IS NOT NULL AND trim(fix_type)<>''
            GROUP BY fix_type, severity ORDER BY n DESC"""),
        ("Worst product / category pairs", """
            SELECT product, label, COUNT(*) n
            FROM v_feedback_turns
            WHERE rating='THUMBS_DOWN' AND label IS NOT NULL AND trim(label)<>''
              AND label <> 'not_a_defect'
            GROUP BY product, label ORDER BY n DESC LIMIT 15"""),
    ]:
        rows = conn.execute(sql).fetchall()
        if not rows:
            continue
        print(f"\n{title}")
        for row in rows:
            cells = "  ".join(
                f"{row[k]!s:>{max(6, len(k))}}" if not isinstance(row[k], str)
                else f"{row[k]:<28}" for k in row.keys())
            print(f"  {cells}")


def brief_corpus(conn, product, limit=120):
    """Print a product's complaints for a human to read — no API call.

    Same corpus the synthesis agent is given, so what you read is what it judged.
    """
    rows = corpus_rows(conn, product=product, limit=limit)
    if not rows:
        print(f"No commented negative feedback for product {product!r}. "
              "Product names must match dim_agent.product exactly; try --report.")
        return
    print(corpus_text(rows, product))


def corpus_rows(conn, product=None, agent=None, limit=120):
    where = ["rating='THUMBS_DOWN'", "has_comment=1"]
    params = []
    if product:
        where.append("product = ?")
        params.append(product)
    if agent:
        where.append("agent_name_current = ?")
        params.append(agent)
    params.append(limit)
    return conn.execute(f"""
        SELECT agent_name_current, conv_day, label, severity, fix_type,
               question, comment
        FROM v_feedback_turns
        WHERE {' AND '.join(where)}
        ORDER BY CASE severity WHEN 'misleading' THEN 0 WHEN 'unhelpful' THEN 1
                               ELSE 2 END, conv_day DESC
        LIMIT ?""", params).fetchall()


def corpus_text(rows, scope):
    lines = [f"Negative feedback corpus for: {scope}",
             f"Total downvoted turns with a written complaint: {len(rows)}", ""]
    for i, r in enumerate(rows, 1):
        lines += [
            f"--- {i} · {r['conv_day']} · {r['agent_name_current']} · "
            f"{r['label'] or 'unclassified'} / {r['severity'] or '?'} / "
            f"fix={r['fix_type'] or '?'}",
            f"  asked: {' '.join((r['question'] or '').split())[:300]}",
            f"  complaint: {' '.join((r['comment'] or '').split())[:900]}",
        ]
    return "\n".join(lines)


def synthesize(conn, client, agent_id, scope, product=None, agent=None, limit=120):
    """Ask the synthesis agent for a narrative brief over one product's complaints."""
    rows = corpus_rows(conn, product=product, agent=agent, limit=limit)
    if not rows:
        print(f"  no commented negative feedback for {scope!r} — skipped")
        return None
    unlabelled = sum(1 for r in rows if not r["label"])
    if unlabelled:
        print(f"  note: {unlabelled} of {len(rows)} turns are unclassified — "
              f"run the labelling pass first for a sharper brief")

    payload = client.post(f"/api/agents/{agent_id}/generate", {
        "messages": [{"role": "user", "content": corpus_text(rows, scope)}],
        "temperature": 0,
        "maxTokens": 4000,
    })
    brief = (payload or {}).get("response")
    if not brief:
        print(f"  {scope}: agent returned nothing")
        return None

    os.makedirs(BRIEFS_DIR, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", scope.lower()).strip("-") or "brief"
    path = os.path.join(BRIEFS_DIR, f"{slug}.md")
    header = (f"# Negative feedback brief — {scope}\n\n"
              f"*{len(rows)} downvoted turns with written complaints. "
              f"Generated {dt.datetime.now():%Y-%m-%d} by the Feedback Synthesis "
              f"Analyst agent from the local warehouse.*\n\n")
    with open(path, "w") as fh:
        fh.write(header + brief.strip() + "\n")
    print(f"  wrote {os.path.relpath(path, HERE)}  ({len(rows)} complaints)")
    return path


def top_products(conn, n=6, min_complaints=15):
    return [r["product"] for r in conn.execute("""
        SELECT product, COUNT(*) n FROM v_feedback_turns
        WHERE rating='THUMBS_DOWN' AND has_comment=1
        GROUP BY product HAVING n >= ? ORDER BY n DESC LIMIT ?""",
        (min_complaints, n))]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--agent-id", help="classifier agent (defaults to the saved one)")
    ap.add_argument("--model", default="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                    help="recorded in labeler_model; the agent's own config decides "
                         "what actually runs")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--product", help="only this product")
    # 2 is a measured optimum, not caution: Foundry's limiter punishes width, and at 6
    # workers throughput collapsed to zero with every worker parked in backoff.
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--relabel", action="store_true",
                    help="re-judge turns that already have a label")
    ap.add_argument("--include-uncommented", action="store_true",
                    help="also judge thumbs-down turns where the user typed nothing")
    ap.add_argument("--report", action="store_true", help="summarise, no API calls")
    ap.add_argument("--brief", metavar="PRODUCT",
                    help="print that product's complaint corpus for synthesis")
    ap.add_argument("--synthesize", metavar="PRODUCT", nargs="?", const="__TOP__",
                    help="write a narrative brief to feedback-briefs/ for this product, "
                         "or for the top products if given no value")
    ap.add_argument("--synth-agent-id", help="synthesis agent (defaults to the saved one)")
    args = ap.parse_args()

    conn = connect(args.db)

    if args.report:
        report(conn)
        return
    if args.brief:
        brief_corpus(conn, args.brief)
        return
    if args.synthesize:
        agent_id = resolve_agent_id(args.synth_agent_id, SYNTH_METADATA, "synthesis")
        client = FoundryClient()
        scopes = (top_products(conn) if args.synthesize == "__TOP__"
                  else [args.synthesize])
        if not scopes:
            print("No product has enough commented negative feedback to summarise.")
            return
        print(f"Synthesising briefs for {len(scopes)} product(s) via agent {agent_id} ...")
        for scope in scopes:
            synthesize(conn, client, agent_id, scope, product=scope)
        return

    turns = pending_turns(conn, relabel=args.relabel, limit=args.limit,
                          product=args.product,
                          include_uncommented=args.include_uncommented)
    if not turns:
        print("Nothing to label. (Run extract.py feedback first, or --relabel to redo.)")
        report(conn)
        return

    agent_id = resolve_agent_id(args.agent_id)
    client = FoundryClient()
    print(f"Foundry: {client.base_url}  tenant={client.tenant_slug or '(none)'}")
    run_labelling(conn, client, agent_id, args.model, turns, workers=args.workers)
    report(conn)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted — labels already written are kept; just re-run",
              file=sys.stderr)
        sys.exit(130)
    except FoundryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
