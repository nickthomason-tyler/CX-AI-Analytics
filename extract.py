#!/usr/bin/env python3
"""Phase 0 extractor: Foundry logs/analytics -> local SQLite warehouse.

    python3 extract.py dims                      # projects/agents/teams/workflows + ownership tags
    python3 extract.py retag                     # re-apply product_taxonomy.json only (seconds, no API)
    python3 extract.py facts --since 2026-01-01  # backfill agent + team runs (resumable)
    python3 extract.py facts --incremental       # top up from the last day already ingested
    python3 extract.py workflows                 # visual-workflow executions only (fan-out)
    python3 extract.py feedback --since 2026-01-01  # thumbs up/down turns + comments
    python3 extract.py status                    # what's loaded, and validation counts

Ownership (Tyler portfolio > product, per the Product Architecture Guide) lives on
the dimension tables only; facts reach it through views, so re-tagging never
requires re-extraction. The log rows carry no project or product field at all, so
ownership is a join -- which is why extraction is tenant-wide and filtering happens
in the dashboard, not here.
"""

import argparse
import calendar
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from foundry_client import FoundryClient, FoundryError

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "foundry_analytics.db")
SCHEMA_PATH = os.path.join(HERE, "schema.sql")
TAXONOMY_PATH = os.path.join(HERE, "product_taxonomy.json")
DIM_TABLES = ("dim_project", "dim_agent", "dim_team", "dim_workflow")

PAGE_SIZE = 1000
EARLIEST_PROBE_YEAR = 2025


# ── helpers ──────────────────────────────────────────────────────────────────

def norm_key(value):
    """Foundry emits ids both bare and 'agent-'/'team-' prefixed."""
    if not value:
        return None
    return re.sub(r"^(agent|team|workflow)-", "", str(value))


def dig(obj, *path, default=None):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def day_of(timestamp):
    if not timestamp or not isinstance(timestamp, str):
        return None
    return timestamp[:10]


def months_between(start, end):
    cur = dt.date(start.year, start.month, 1)
    stop = dt.date(end.year, end.month, 1)
    while cur <= stop:
        yield cur
        cur = dt.date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)


def strip_html_entities(text):
    if not isinstance(text, str):
        return text
    return (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))


# ── ownership tagging ────────────────────────────────────────────────────────

class Tag:
    """Ownership assignment for one Foundry object."""

    __slots__ = ("portfolio", "product", "is_focus", "rule")

    def __init__(self, portfolio, product, is_focus, rule):
        self.portfolio = portfolio
        self.product = product
        self.is_focus = 1 if is_focus else 0
        self.rule = rule

    def as_columns(self):
        return {"portfolio": self.portfolio, "product": self.product,
                "is_focus": self.is_focus, "tag_rule": self.rule}


class Tagger:
    """Maps Foundry projects/agents onto the Tyler Product Architecture.

    Rules assign a *product*; the portfolio is looked up from the canonical
    portfolio->products table so the two can never contradict each other.
    """

    def __init__(self, path=TAXONOMY_PATH):
        with open(path) as fh:
            cfg = json.load(fh)
        self.rules = [r for r in cfg.get("rules", []) if "product" in r]
        self.portfolios = cfg.get("portfolios", {})
        self.focus = set(cfg.get("focus_products", []))
        self.default_product = cfg.get("default_product", "Unattributed")

        # product -> portfolio. A product listed under two portfolios (e.g.
        # Enterprise Environmental Health is in both Civic and Health & Human
        # Services) resolves to the first listed; recorded so it can be reported.
        self.product_to_portfolio = {}
        self.ambiguous = {}
        for portfolio, products in self.portfolios.items():
            for product in products:
                if product in self.product_to_portfolio:
                    self.ambiguous.setdefault(product, [self.product_to_portfolio[product]])
                    self.ambiguous[product].append(portfolio)
                else:
                    self.product_to_portfolio[product] = portfolio

        unknown = {r["product"] for r in self.rules} - set(self.product_to_portfolio)
        if unknown:
            raise FoundryError(
                "product_taxonomy.json has rules naming products that appear in no "
                f"portfolio: {sorted(unknown)}")

    def portfolio_of(self, product):
        return self.product_to_portfolio.get(product, "Non-Product")

    def tag(self, project_id=None, project_name=None, agent_name=None, tenant_name=None):
        project_name = strip_html_entities(project_name or "")
        agent_name = strip_html_entities(agent_name or "")
        tenant_name = strip_html_entities(tenant_name or "")

        for idx, rule in enumerate(self.rules):
            checks = []
            if "project_ids" in rule:
                checks.append(project_id in rule["project_ids"])
            if "project_regex" in rule:
                checks.append(bool(re.search(rule["project_regex"], project_name, re.I)))
            if "agent_regex" in rule:
                checks.append(bool(re.search(rule["agent_regex"], agent_name, re.I)))
            if "tenant_regex" in rule:
                checks.append(bool(re.search(rule["tenant_regex"], tenant_name, re.I)))
            if checks and all(checks):
                product = rule["product"]
                matched_on = ",".join(
                    field for field in
                    ("project_ids", "project_regex", "agent_regex", "tenant_regex")
                    if field in rule)
                return Tag(self.portfolio_of(product), product,
                           product in self.focus, f"rule[{idx}]:{matched_on}")

        return Tag(self.portfolio_of(self.default_product), self.default_product,
                   self.default_product in self.focus, "default")


# ── database ─────────────────────────────────────────────────────────────────

def connect(db_path=DB_PATH, rebuild_dims=False):
    conn = sqlite3.connect(db_path, timeout=120)
    conn.row_factory = sqlite3.Row
    if rebuild_dims:
        # Dimensions are 100% derived from the API, so dropping them is safe and
        # avoids ALTER-TABLE migrations when the taxonomy columns change.
        # Facts are never touched here.
        for table in DIM_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
    with open(SCHEMA_PATH) as fh:
        conn.executescript(fh.read())
    migrate(conn)
    return conn


# Columns added to fact tables after a warehouse already existed. CREATE TABLE IF
# NOT EXISTS silently does nothing on an existing table, so new columns need an
# explicit ALTER -- otherwise the schema file and the live database drift apart and
# only the queries fail, well downstream of the cause.
MIGRATIONS = (
    ("fact_feedback_turn", "severity", "TEXT"),
    ("fact_feedback_turn", "fix_type", "TEXT"),
    ("dim_agent", "tenant_name", "TEXT"),
)


def migrate(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table, column, coltype in MIGRATIONS:
        if table not in tables:
            continue  # freshly created from schema.sql; already has the column
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            print(f"  migrated: {table}.{column} added")
    conn.commit()


def upsert(conn, table, rows):
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = (f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
           f"VALUES ({','.join('?' for _ in cols)})")
    conn.executemany(sql, [[r.get(c) for c in cols] for r in rows])
    return len(rows)


# ── dims ─────────────────────────────────────────────────────────────────────

def build_dims(conn, client, workers=4):
    tagger = Tagger()

    print("Fetching project list ...")
    # /foundry/v1/projects caps limit at 500, so page it rather than asking for all.
    projects = list(client.paginate("/foundry/v1/projects", "projects", page_size=500))
    print(f"  {len(projects)} projects")

    project_rows, by_id = [], {}
    for p in projects:
        pid = p.get("id")
        name = strip_html_entities(p.get("name"))
        tag = tagger.tag(project_id=pid, project_name=name)
        counts = p.get("counts") or {}
        by_id[pid] = {"name": name, "tag": tag, "counts": counts}
        owner = p.get("owner") or {}
        project_rows.append({
            "project_id": pid, "name": name,
            "description": strip_html_entities(p.get("description")),
            "tenant_id": p.get("tenantId"), "visibility": p.get("visibility"),
            "owner_email": owner.get("email") if isinstance(owner, dict) else str(owner or ""),
            "member_count": len(p.get("members") or []),
            "agents_count": counts.get("agents"), "workflows_count": counts.get("workflows"),
            "teams_count": counts.get("teams"), "evals_count": counts.get("evaluations"),
            "mcp_count": counts.get("mcpServers"),
            "created_at": p.get("createdAt"), "updated_at": p.get("updatedAt"),
            **tag.as_columns(),
        })
    upsert(conn, "dim_project", project_rows)
    conn.commit()

    # Only hit the child endpoints where the counts say something exists.
    def fetch_children(pid, kind):
        try:
            return pid, kind, client.get(f"/foundry/v1/projects/{pid}/{kind}")
        except FoundryError as exc:
            return pid, kind, exc

    jobs = []
    for pid, meta in by_id.items():
        counts = meta["counts"]
        for kind, key in (("agents", "agents"), ("teams", "teams"), ("workflows", "workflows")):
            if (counts.get(key) or 0) > 0:
                jobs.append((pid, kind))
    print(f"Fetching children for {len(jobs)} project/kind pairs ...")

    agent_rows, team_rows, wf_rows, errors = [], [], [], 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_children, pid, kind) for pid, kind in jobs]
        for fut in as_completed(futures):
            pid, kind, payload = fut.result()
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(jobs)}")
            if isinstance(payload, Exception):
                errors += 1
                continue
            rows = payload if isinstance(payload, list) else next(
                (v for v in payload.values() if isinstance(v, list)), []) if isinstance(payload, dict) else []
            meta = by_id[pid]
            pname, ptag = meta["name"], meta["tag"]

            for r in rows:
                if kind == "agents":
                    name = strip_html_entities(r.get("name"))
                    tname = strip_html_entities(
                        r.get("tenantName") or r.get("tenant_name")) or None
                    # An EPL-named agent inside someone's personal project should
                    # still count as EPL, so re-tag with the agent name in hand.
                    tag = tagger.tag(project_id=pid, project_name=pname, agent_name=name,
                                     tenant_name=tname)
                    agent_rows.append({
                        "agent_key": norm_key(r.get("id")), "agent_id": r.get("id"),
                        "name": name, "description": strip_html_entities(r.get("description")),
                        "model": dig(r, "model", "name") or (
                            r.get("model") if isinstance(r.get("model"), str) else None),
                        "version": r.get("version"), "tenant_id": r.get("tenant_id"),
                        "tenant_name": tname,
                        "project_id": pid, "project_name": pname,
                        **tag.as_columns(),
                    })
                elif kind == "teams":
                    team_rows.append({
                        "team_key": norm_key(r.get("id")), "team_id": r.get("id"),
                        "name": strip_html_entities(r.get("name")),
                        "description": strip_html_entities(r.get("description")),
                        "project_id": pid, "project_name": pname,
                        "portfolio": ptag.portfolio, "product": ptag.product,
                        "is_focus": ptag.is_focus,
                        "agent_ids": json.dumps(r.get("agent_ids") or r.get("agentIds") or []),
                    })
                else:
                    wid = r.get("workflowId") or r.get("id")
                    wf_rows.append({
                        "workflow_key": norm_key(wid), "workflow_id": wid,
                        "name": strip_html_entities(r.get("name")),
                        "description": strip_html_entities(r.get("description")),
                        "project_id": pid, "project_name": pname,
                        "portfolio": ptag.portfolio, "product": ptag.product,
                        "is_focus": ptag.is_focus,
                        "node_count": len(r.get("nodes") or []),
                        "is_active": 1 if r.get("isActive") else 0,
                        "created_by": r.get("createdBy"), "user_name": r.get("userName"),
                        "created_at": r.get("createdAt"), "updated_at": r.get("updatedAt"),
                    })

    upsert(conn, "dim_agent", agent_rows)
    upsert(conn, "dim_team", team_rows)
    upsert(conn, "dim_workflow", wf_rows)
    conn.commit()
    print(f"  agents={len(agent_rows)} teams={len(team_rows)} workflows={len(wf_rows)} "
          f"errors={errors}")

    # Fallback dimension: agents visible in transcripts but not in any project we
    # can enumerate. Without this they'd vanish from the leaderboard entirely.
    print("Backfilling agents from /api/transcripts/agents ...")
    try:
        tagents = client.get("/api/transcripts/agents")
    except FoundryError as exc:
        print(f"  skipped: {exc}")
        tagents = []
    known = {r["agent_key"] for r in agent_rows}
    extra = []
    for r in tagents or []:
        key = norm_key(r.get("id"))
        if not key or key in known:
            continue
        known.add(key)
        name = strip_html_entities(r.get("name"))
        subtitle = strip_html_entities(r.get("subtitle") or "")
        # subtitle is "<project> - v1.2.3", or bare "v1.2.3" when the agent has no
        # project. Treat the version-only case as unknown, not as a project named "v1.0.0".
        pname = re.sub(r"\s*-\s*v[\d.]+$", "", subtitle).strip() or None
        if pname and re.fullmatch(r"v[\d.]+", pname):
            pname = None
        tname = strip_html_entities(r.get("tenantName"))
        tag = tagger.tag(project_name=pname, agent_name=name, tenant_name=tname)
        cols = tag.as_columns()
        cols["tag_rule"] += "+transcripts"
        extra.append({
            "agent_key": key, "agent_id": r.get("id"), "name": name,
            "description": None, "model": None, "version": None, "tenant_id": None,
            "tenant_name": tname or None,
            "project_id": None, "project_name": pname,
            **cols,
        })
    upsert(conn, "dim_agent", extra)
    conn.commit()
    print(f"  +{len(extra)} agents from transcripts (no project association)")

    # Last resort: agents that only exist in the fact rows.
    print("Seeding agents seen only in logs ...")
    seed_agents_from_facts(conn, tagger)

    report_tagging(conn)


def seed_agents_from_facts(conn, tagger):
    """Create dim_agent rows for agents that appear in the logs but in no project.

    ~230 agents (5% of runs) are like this: deleted since, or living in a tenant
    this key cannot enumerate. Without a dim row the LEFT JOIN drops them to
    'Unattributed' no matter how good the rules are, so seed them from the name
    recorded on the fact rows and tag on that.
    """
    orphans = conn.execute("""
        SELECT f.agent_key, f.agent_id_raw, f.agent_name, f.tenant_name, COUNT(*) runs
        FROM fact_agent_run f
        LEFT JOIN dim_agent a ON a.agent_key = f.agent_key
        WHERE a.agent_key IS NULL AND f.agent_key IS NOT NULL
        GROUP BY f.agent_key
    """).fetchall()

    rows = []
    for r in orphans:
        tag = tagger.tag(project_name=None, agent_name=r["agent_name"],
                         tenant_name=r["tenant_name"])
        cols = tag.as_columns()
        cols["tag_rule"] += "+from_facts"
        rows.append({
            "agent_key": r["agent_key"], "agent_id": r["agent_id_raw"],
            "name": r["agent_name"], "description": None, "model": None,
            "version": None, "tenant_id": None,
            "tenant_name": r["tenant_name"],
            "project_id": None, "project_name": None,
            **cols,
        })
    upsert(conn, "dim_agent", rows)
    conn.commit()
    runs = sum(r["runs"] for r in orphans)
    print(f"  seeded {len(rows)} agents seen only in logs ({runs:,} runs recovered)")
    return len(rows)


def retag(conn):
    """Re-apply product_taxonomy.json without touching the API."""
    tagger = Tagger()
    seed_agents_from_facts(conn, tagger)
    projects = {r["project_id"]: r["name"] for r in conn.execute(
        "SELECT project_id, name FROM dim_project")}

    for pid, name in projects.items():
        t = tagger.tag(project_id=pid, project_name=name)
        conn.execute("UPDATE dim_project SET portfolio=?, product=?, is_focus=?, tag_rule=? "
                     "WHERE project_id=?", (t.portfolio, t.product, t.is_focus, t.rule, pid))

    # tenant_regex rules (School ERP Pro's 'SEPRO', Enterprise ERP's) can only fire
    # when the tenant name is in hand, and ~1,400 agents reach a product ONLY via
    # tenant: they have no project and, if they never ran, no fact rows either.
    # dim_agent.tenant_name is the durable copy; the facts are the fallback for rows
    # written before that column existed. Without this, every retag silently undid
    # the tenant-based tags build_dims had assigned -- 19 agents (11 Enterprise ERP,
    # 6 School ERP Pro) fell to Unattributed and the loss was invisible in status.
    tenants = {r["agent_key"]: r["tenant_name"] for r in conn.execute("""
        SELECT agent_key, tenant_name, COUNT(*) n
        FROM fact_agent_run
        WHERE agent_key IS NOT NULL AND tenant_name IS NOT NULL
        GROUP BY agent_key, tenant_name
        ORDER BY agent_key, n
    """)}  # ORDER BY n ascending: the last row per agent_key wins, i.e. its busiest tenant

    for row in conn.execute(
            "SELECT agent_key, name, project_id, project_name, tenant_name "
            "FROM dim_agent").fetchall():
        t = tagger.tag(project_id=row["project_id"], project_name=row["project_name"],
                       agent_name=row["name"],
                       tenant_name=row["tenant_name"] or tenants.get(row["agent_key"]))
        rule = t.rule + ("+from_facts" if row["project_id"] is None else "")
        conn.execute("UPDATE dim_agent SET portfolio=?, product=?, is_focus=?, tag_rule=? "
                     "WHERE agent_key=?",
                     (t.portfolio, t.product, t.is_focus, rule, row["agent_key"]))

    for row in conn.execute(
            "SELECT team_key, name, project_id, project_name FROM dim_team").fetchall():
        t = tagger.tag(project_id=row["project_id"], project_name=row["project_name"],
                       agent_name=row["name"])
        conn.execute("UPDATE dim_team SET portfolio=?, product=?, is_focus=? WHERE team_key=?",
                     (t.portfolio, t.product, t.is_focus, row["team_key"]))

    for row in conn.execute(
            "SELECT workflow_key, name, project_id, project_name FROM dim_workflow").fetchall():
        t = tagger.tag(project_id=row["project_id"], project_name=row["project_name"],
                       agent_name=row["name"])
        conn.execute("UPDATE dim_workflow SET portfolio=?, product=?, is_focus=? "
                     "WHERE workflow_key=?",
                     (t.portfolio, t.product, t.is_focus, row["workflow_key"]))

    conn.commit()
    report_tagging(conn)


def report_tagging(conn):
    tagger = Tagger()
    if tagger.ambiguous:
        print("\nNote: product(s) listed under multiple portfolios; first listed wins:")
        for product, portfolios in tagger.ambiguous.items():
            print(f"  {product}: {' / '.join(portfolios)} -> {tagger.portfolio_of(product)}")

    print("\nOwnership tagging — agents per portfolio / product:")
    for row in conn.execute(
            "SELECT portfolio, product, is_focus, COUNT(*) n FROM dim_agent "
            "GROUP BY portfolio, product ORDER BY portfolio, n DESC"):
        star = "*" if row["is_focus"] else " "
        print(f"  {star} {row['n']:5d}  {row['portfolio']:24s} {row['product']}")
    print("  (* = in focus_products)")


# ── facts ────────────────────────────────────────────────────────────────────

def parse_agent_run(row):
    meta = row.get("metadata") or {}
    tokens = row.get("tokenUsage") or {}
    timing = row.get("timing") or {}
    tool_calls = row.get("toolCalls") or []

    tool_ids = []
    for tc in tool_calls:
        tid = dig(tc, "attributes", "toolId")
        if not tid:
            m = re.search(r"tool:\s*'([^']+)'", str(tc.get("name") or ""))
            tid = m.group(1) if m else tc.get("name")
        if tid and tid not in tool_ids:
            tool_ids.append(tid)

    started = timing.get("startedAt")
    return {
        "run_pk": row.get("id"), "run_id": row.get("runId"),
        "thread_id": row.get("threadId"), "conversation_id": meta.get("conversationId"),
        "agent_key": norm_key(dig(row, "agent", "id")),
        "agent_id_raw": dig(row, "agent", "id"),
        "agent_name": strip_html_entities(dig(row, "agent", "name")),
        "model_name": dig(row, "model", "name"), "model_provider": dig(row, "model", "provider"),
        "temperature": as_float(dig(row, "model", "parameters", "temperature")),
        "finish_reason": dig(row, "model", "finishReason"),
        "input_tokens": as_int(tokens.get("inputTokens")),
        "output_tokens": as_int(tokens.get("outputTokens")),
        "total_tokens": as_int(tokens.get("totalTokens")),
        "reasoning_tokens": as_int(tokens.get("reasoningTokens")),
        "cached_tokens": as_int(tokens.get("cachedInputTokens")),
        "token_cost": as_float(meta.get("tokenCost")),
        "status": meta.get("status"), "error_type_id": meta.get("errorTypeId"),
        "error_message": (meta.get("errorMessage") or "")[:2000] or None,
        "user_id": meta.get("userId"), "user_name": meta.get("userName"),
        "tenant_id": meta.get("tenantId"),
        "tenant_name": strip_html_entities(meta.get("tenantDisplayName")),
        "started_at": started, "ended_at": timing.get("endedAt"),
        "duration_ms": as_int(timing.get("totalDurationMs")),
        "tool_ms": as_int(timing.get("toolCallsDurationMs")),
        "tool_call_count": len(tool_calls),
        "tool_names": json.dumps(tool_ids),
        "run_day": day_of(started),
    }


def parse_tool_calls(row, parsed):
    out = []
    for tc in row.get("toolCalls") or []:
        tid = dig(tc, "attributes", "toolId")
        if not tid:
            m = re.search(r"tool:\s*'([^']+)'", str(tc.get("name") or ""))
            tid = m.group(1) if m else None
        out.append({
            "tool_call_pk": tc.get("id"), "run_pk": parsed["run_pk"],
            "agent_key": parsed["agent_key"], "tool_id": tid,
            "tool_name": tc.get("name"), "span_type": tc.get("span_type"),
            "status_code": tc.get("status_code"),
            "duration_ms": as_int(tc.get("duration_ms")),
            "started_at": tc.get("start_time"),
            "run_day": day_of(tc.get("start_time")) or parsed["run_day"],
        })
    return [r for r in out if r["tool_call_pk"]]


def parse_team_run(row):
    meta = row.get("metadata") or {}
    tokens = row.get("tokenUsage") or {}
    timing = row.get("timing") or {}
    started = timing.get("startedAt")
    return {
        "run_pk": row.get("id"), "run_id": row.get("runId"),
        "thread_id": row.get("threadId"),
        "conversation_id": row.get("conversationId") or meta.get("conversationId"),
        "team_key": norm_key(dig(row, "team", "id")), "team_id_raw": dig(row, "team", "id"),
        "team_name": strip_html_entities(dig(row, "team", "name")),
        "strategy": row.get("strategy") if isinstance(row.get("strategy"), str)
                    else dig(row, "strategy", "type"),
        "model_name": dig(row, "model", "name"),
        "input_tokens": as_int(tokens.get("inputTokens")),
        "output_tokens": as_int(tokens.get("outputTokens")),
        "total_tokens": as_int(tokens.get("totalTokens")),
        "token_cost": as_float(meta.get("tokenCost")),
        "status": meta.get("status"),
        "error_message": (meta.get("errorMessage") or "")[:2000] or None,
        "user_id": meta.get("userId"), "user_name": meta.get("userName"),
        "tenant_id": meta.get("tenantId"),
        "tenant_name": strip_html_entities(meta.get("tenantDisplayName")),
        "feedback_type": row.get("feedbackType"),
        "feedback_comment": (row.get("feedbackComment") or "")[:2000] or None,
        "started_at": started, "ended_at": timing.get("endedAt"),
        "duration_ms": as_int(timing.get("totalDurationMs")),
        "span_count": len(row.get("spans") or []),
        "run_day": day_of(started),
    }


def parse_workflow_run(row, wf_name):
    created = row.get("createdAt")
    completed = row.get("completedAt")
    dur = None
    if created and completed:
        try:
            a = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
            b = dt.datetime.fromisoformat(completed.replace("Z", "+00:00"))
            dur = int((b - a).total_seconds() * 1000)
        except ValueError:
            dur = None
    step = row.get("suspendedStep") or {}
    if not isinstance(step, dict):
        step = {}
    err = row.get("error")
    if isinstance(err, dict):
        err = err.get("message") or json.dumps(err)
    return {
        "run_pk": row.get("id"),
        "workflow_key": norm_key(row.get("workflowId")),
        "workflow_id_raw": row.get("workflowId"),
        "workflow_name": strip_html_entities(wf_name),
        "status": row.get("status"),
        "created_at": created, "completed_at": completed,
        "suspended_at": row.get("suspendedAt"), "resumed_at": row.get("resumedAt"),
        "resumed_by": row.get("resumedBy"),
        "error_message": (str(err)[:2000] if err else None),
        "suspended_node": step.get("nodeId"),
        "suspended_step": step.get("stepName"),
        "suspended_reason": (step.get("reason") or "")[:1000] or None,
        "duration_ms": dur,
        "run_day": day_of(created),
    }


def ingest_workflows(conn, client, workers=6):
    """Fan out over dim_workflow -- there is no aggregate workflow-run endpoint.

    One request per definition, so cost scales with definitions (not executions).
    Unreachable definitions are counted and reported rather than silently skipped.
    """
    wfs = conn.execute(
        "SELECT workflow_id, workflow_key, name FROM dim_workflow "
        "WHERE workflow_id IS NOT NULL").fetchall()
    print(f"Fanning out over {len(wfs)} visual-workflow definitions ...")

    def fetch(row):
        try:
            payload = client.get(f"/api/visual-workflows/{row['workflow_id']}/history")
            return row, payload, None
        except FoundryError as exc:
            return row, None, exc

    rows, unreachable, with_runs, done = [], 0, 0, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, r) for r in wfs]
        for fut in as_completed(futures):
            row, payload, err = fut.result()
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(wfs)} · {len(rows)} executions so far")
            if err is not None:
                unreachable += 1
                continue
            execs = (payload or {}).get("executions") or []
            name = (payload or {}).get("workflowName") or row["name"]
            if execs:
                with_runs += 1
            for e in execs:
                parsed = parse_workflow_run(e, name)
                if parsed["run_pk"]:
                    rows.append(parsed)

    upsert(conn, "fact_workflow_run", rows)
    conn.execute(
        "INSERT OR REPLACE INTO ingest_log (source,day,api_total,rows_written,fetched_at) "
        "VALUES ('workflow_runs','ALL',?,?,?)",
        (len(rows), len(rows), dt.datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    print(f"  workflow executions written: {len(rows):,} "
          f"across {with_runs} of {len(wfs)} definitions")
    if unreachable:
        print(f"  {unreachable} definitions returned an error (likely deleted) "
              f"-- counted, not silently dropped")
    return len(rows)


# ── conversation feedback ────────────────────────────────────────────────────

# /api/transcripts/conversation_ids silently ignores limit and offset and truncates
# every response at this many rows. Discovery therefore pages by date window.
FEEDBACK_PAGE_CAP = 200

# Positives are pulled too: without them "negative share of rated turns" has no
# denominator and every agent's score is just its traffic volume in disguise.
FEEDBACK_RATINGS = ("negative", "positive")

FEEDBACK_TURN_COLS = (
    "turn_pk", "conversation_id", "turn_index", "rating", "comment",
    "question", "response", "question_chars", "response_chars",
    "agent_key", "agent_name", "conv_day",
)

# Kept generous enough to judge a turn on, while stopping one runaway response from
# bloating the table. The untruncated text is always in raw_transcript.
MAX_COMMENT_CHARS = 4000
MAX_QUESTION_CHARS = 4000
MAX_RESPONSE_CHARS = 8000


def discover_feedback_conversations(client, rating, since, until):
    """Conversation ids that received `rating` feedback anywhere in [since, until].

    Because limit/offset are ignored and responses truncate at FEEDBACK_PAGE_CAP,
    the only way to page this endpoint is by date window: request a window, and if
    it comes back exactly full, split it and request each half. A single day that
    saturates cannot be split any further -- those are collected and reported, not
    passed over silently, because they are the one case that undercounts.
    """
    found, saturated, requests = {}, [], 0
    stack = [(since, until)]
    while stack:
        lo, hi = stack.pop()
        rows = client.get("/api/transcripts/conversation_ids", {
            "feedback": rating,
            "startDate": lo.isoformat(),
            "endDate": hi.isoformat(),
        }) or []
        requests += 1

        if len(rows) >= FEEDBACK_PAGE_CAP and lo < hi:
            mid = lo + (hi - lo) // 2
            # Push the later half first so the walk advances chronologically.
            stack.append((mid + dt.timedelta(days=1), hi))
            stack.append((lo, mid))
            continue
        if len(rows) >= FEEDBACK_PAGE_CAP:
            saturated.append(lo.isoformat())

        for row in rows:
            cid = row.get("conversationId")
            if cid:
                found[cid] = row
        # Explicit flush: stdout is block-buffered when redirected to a log, and a
        # throttled walk can otherwise sit silent for many minutes looking hung.
        print(f"    {rating} {lo}..{hi}: {len(rows)} rows "
              f"({len(found):,} found so far)", flush=True)

    print(f"  {rating}: {len(found):,} conversations ({requests} windows)", flush=True)
    if saturated:
        print(f"    WARNING: {len(saturated)} single day(s) returned the full "
              f"{FEEDBACK_PAGE_CAP}-row cap and cannot be split further, so their "
              f"feedback is undercounted: {', '.join(saturated[:8])}"
              + (" ..." if len(saturated) > 8 else ""))
    return found


def parse_transcript(conversation_id, payload, discovery_row):
    """Flatten /api/transcripts/{id} into (conversation summary, rated turns).

    Only rated turns become rows: NO_ACTION is the overwhelming majority and carries
    no signal. turn_count keeps the denominator so "3 of 13 turns were downvoted"
    stays answerable.
    """
    agent_id_raw = (discovery_row or {}).get("agentId")
    conv_date = (discovery_row or {}).get("conversationDate")
    agent_name = strip_html_entities((discovery_row or {}).get("agentName"))
    conv_day = day_of(conv_date)

    turns, index = [], 0
    # The endpoint returns a list of conversation objects. In practice it is one,
    # but iterate anyway and keep a single running turn index across them so
    # turn_pk stays unique.
    for conv in (payload if isinstance(payload, list) else []):
        if not isinstance(conv, dict):
            continue
        for turn in (conv.get("conversation") or []):
            if not isinstance(turn, dict):
                continue
            index += 1
            rating = (turn.get("feedback") or "NO_ACTION").upper()
            if rating in ("NO_ACTION", ""):
                continue
            question = strip_html_entities(turn.get("question") or "")
            response = strip_html_entities(turn.get("response") or "")
            comment = strip_html_entities(turn.get("thumbsDownTextFeedback") or "").strip()
            turns.append({
                "turn_pk": f"{conversation_id}:{index}",
                "conversation_id": conversation_id,
                "turn_index": index,
                "rating": rating,
                "comment": comment[:MAX_COMMENT_CHARS] or None,
                "question": question[:MAX_QUESTION_CHARS],
                "response": response[:MAX_RESPONSE_CHARS],
                # Lengths of the *full* text, so truncation never distorts them.
                "question_chars": len(question),
                "response_chars": len(response),
                "agent_key": norm_key(agent_id_raw),
                "agent_name": agent_name,
                "conv_day": conv_day,
            })

    negative = sum(1 for t in turns if t["rating"] == "THUMBS_DOWN")
    positive = sum(1 for t in turns if t["rating"] == "THUMBS_UP")
    summary = {
        "conversation_id": conversation_id,
        "conversation_date": conv_date,
        "agent_key": norm_key(agent_id_raw),
        "agent_id_raw": agent_id_raw,
        "agent_name": agent_name,
        "turn_count": index,
        "rated_turns": len(turns),
        "negative_turns": negative,
        "positive_turns": positive,
        "commented_turns": sum(1 for t in turns if t["comment"]),
        "discovered_by": (discovery_row or {}).get("_discovered_by"),
        "conv_day": conv_day,
    }
    return summary, turns


def upsert_feedback_turns(conn, rows):
    """Upsert that deliberately leaves the label* columns alone: labelling costs
    model calls, so re-extracting a conversation must not throw that work away."""
    if not rows:
        return 0
    cols = list(FEEDBACK_TURN_COLS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "turn_pk")
    sql = (f"INSERT INTO fact_feedback_turn ({', '.join(cols)}) "
           f"VALUES ({', '.join('?' for _ in cols)}) "
           f"ON CONFLICT(turn_pk) DO UPDATE SET {updates}")
    conn.executemany(sql, [[r.get(c) for c in cols] for r in rows])
    return len(rows)


def ingest_feedback(conn, client, since, until, workers=6, redo=False,
                    refresh_days=14, ratings=FEEDBACK_RATINGS):
    """Discover feedback-bearing conversations, then fetch each transcript.

    Two phases because the discovery endpoint returns no comment text -- it only
    names conversations -- so the ratings and comments themselves cost one
    transcript request each.
    """
    start = dt.date.fromisoformat(since)
    end = dt.date.fromisoformat(until)

    print(f"Discovering feedback conversations {start} .. {end} ...")
    discovered = {}
    for rating in ratings:
        for cid, row in discover_feedback_conversations(client, rating, start, end).items():
            existing = discovered.get(cid)
            if existing:
                # A conversation with both a thumbs-up and a thumbs-down turn shows
                # up under both ratings; record that rather than letting the second
                # pass overwrite the first.
                if existing.get("_discovered_by") != rating:
                    existing["_discovered_by"] = "both"
            else:
                row["_discovered_by"] = rating
                discovered[cid] = row
    print(f"  {len(discovered):,} distinct conversations with feedback")
    if not discovered:
        return 0

    # Transcripts already stored are skipped, except inside the refresh window:
    # a recent conversation can still collect new feedback after it was first
    # fetched, and re-fetching is idempotent.
    cutoff = (dt.date.today() - dt.timedelta(days=refresh_days)).isoformat()
    have = {r["conversation_id"] for r in conn.execute(
        "SELECT conversation_id FROM raw_transcript")} if not redo else set()
    todo = [(cid, row) for cid, row in discovered.items()
            if cid not in have or (day_of(row.get("conversationDate")) or "") >= cutoff]
    skipped = len(discovered) - len(todo)
    print(f"Fetching {len(todo):,} transcripts "
          f"({skipped:,} already stored and older than {cutoff})")

    def fetch(item):
        cid, row = item
        try:
            return cid, row, client.get(f"/api/transcripts/{cid}"), None
        except FoundryError as exc:
            return cid, row, None, exc

    convs, turns, raws, done = [], [], [], 0
    failed = {}
    written_turns = negative = commented = 0
    fetched_at = dt.datetime.now().isoformat(timespec="seconds")

    def flush():
        """Commit in batches. This walk is thousands of requests long against a
        throttling API, so a single commit at the end would mean an interruption
        (or a hung connection) threw away the whole run."""
        nonlocal convs, turns, raws, written_turns, negative, commented
        if not convs and not turns and not raws:
            return
        negative += sum(1 for t in turns if t["rating"] == "THUMBS_DOWN")
        commented += sum(1 for t in turns
                         if t["rating"] == "THUMBS_DOWN" and t["comment"])
        written_turns += len(turns)
        upsert(conn, "dim_conversation", convs)
        upsert_feedback_turns(conn, turns)
        upsert(conn, "raw_transcript", raws)
        conn.commit()
        convs, turns, raws = [], [], []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, item) for item in todo]
        for fut in as_completed(futures):
            cid, row, payload, err = fut.result()
            done += 1
            if done % 50 == 0:
                flush()
                print(f"  {done}/{len(todo)} · {written_turns:,} rated turns so far",
                      flush=True)
            if err is not None:
                # Track *why*, separately. A 404 is a deleted conversation and is
                # fine; a 429 or 5xx means real feedback is missing from the
                # warehouse, which is a different and much more misleading thing.
                failed.setdefault(getattr(err, "status", None) or "network", []).append(cid)
                continue
            summary, parsed = parse_transcript(cid, payload, row)
            convs.append(summary)
            turns.extend(parsed)
            raws.append({"conversation_id": cid,
                         "payload": json.dumps(payload, ensure_ascii=False),
                         "fetched_at": fetched_at})

    flush()
    turns_total = written_turns
    conn.execute(
        "INSERT OR REPLACE INTO ingest_log (source,day,api_total,rows_written,fetched_at) "
        "VALUES ('feedback','ALL',?,?,?)",
        (len(discovered), turns_total, fetched_at))
    conn.commit()

    print(f"  rated turns written: {turns_total:,} "
          f"({negative:,} negative, {commented:,} of those with a comment)")
    for status, cids in sorted(failed.items(), key=lambda kv: str(kv[0])):
        if status == 404:
            print(f"  {len(cids)} transcripts gone (404) -- deleted conversations")
        else:
            print(f"  WARNING: {len(cids)} transcripts failed with {status} -- their "
                  f"feedback is MISSING from the warehouse, not absent from Foundry. "
                  f"Re-run 'extract.py feedback' to fill them in.")
    return turns_total


def day_total(client, path, day):
    payload = client.get(path, {"limit": 1, "startDate": day, "endDate": day})
    return payload.get("total") or 0


def fetch_run_page(client, day, offset, limit):
    """One page of agent runs, halving the page on a server-side 5xx.

    Foundry answers HTTP 500 {"error":"Invalid string length"} when a page happens
    to span an unusually fat run payload — V8 blowing its max string length while
    serializing the response. It is deterministic, so the client's retry budget
    never clears it and one fat run stalls the whole nightly refresh at that day
    forever. Halving the page steps around the fat row; only a page of one that
    still fails is a genuinely unfetchable run, and that is skipped loudly so the
    warehouse keeps advancing.

    Returns (rows, skipped, api_total).
    """
    try:
        payload = client.get("/api/agent-logs", {
            "limit": limit, "offset": offset,
            "startDate": day, "endDate": day,
        })
        return (payload.get("logs") or []), 0, payload.get("total")
    except FoundryError as exc:
        if (exc.status or 0) < 500:
            raise
        if limit <= 1:
            print(f"  WARNING: {day} offset {offset} is unfetchable ({exc.status}); "
                  f"skipping 1 run. That run is MISSING from the warehouse.")
            return [], 1, None
        half = max(1, limit // 2)
        rows, skipped, api_total, taken = [], 0, None, 0
        while taken < limit:
            step = min(half, limit - taken)
            sub_rows, sub_skipped, sub_total = fetch_run_page(
                client, day, offset + taken, step)
            if api_total is None:
                api_total = sub_total
            rows.extend(sub_rows)
            skipped += sub_skipped
            taken += step
            # Short page with nothing skipped means we ran off the end of the day.
            if len(sub_rows) < step and not sub_skipped:
                break
        return rows, skipped, api_total


def ingest_day(conn, client, day, want_tool_calls):
    """Pull one day of agent runs. Idempotent — safe to re-run."""
    offset, written, api_total, skipped_total = 0, 0, None, 0
    while True:
        rows, skipped, total = fetch_run_page(client, day, offset, PAGE_SIZE)
        api_total = total if api_total is None else api_total
        if not rows and not skipped:
            break

        parsed = [parse_agent_run(r) for r in rows]
        parsed = [p for p in parsed if p["run_pk"]]
        written += upsert(conn, "fact_agent_run", parsed)
        if want_tool_calls:
            tcs = []
            for raw, p in zip(rows, parsed):
                tcs.extend(parse_tool_calls(raw, p))
            upsert(conn, "fact_tool_call", tcs)

        offset += len(rows) + skipped
        skipped_total += skipped
        if api_total is not None and offset >= api_total:
            break

    if skipped_total:
        print(f"  WARNING: {day}: {skipped_total} run(s) skipped after repeated "
              f"HTTP 500s. Re-run 'extract.py facts --redo --since {day} "
              f"--until {day} --skip-teams --skip-workflows' to retry them.")
    conn.execute(
        "INSERT OR REPLACE INTO ingest_log (source,day,api_total,rows_written,fetched_at) "
        "VALUES ('agent_runs',?,?,?,?)",
        (day, api_total, written, dt.datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return written, api_total


def ingest_teams(conn, client):
    print("Ingesting team runs (single pass — the table is small) ...")
    total_written = 0
    offset = 0
    while True:
        payload = client.get("/api/team-logs", {"limit": PAGE_SIZE, "offset": offset})
        rows = payload.get("logs") or []
        total = payload.get("total")
        if not rows:
            break
        parsed = [p for p in (parse_team_run(r) for r in rows) if p["run_pk"]]
        total_written += upsert(conn, "fact_team_run", parsed)
        conn.commit()
        offset += len(rows)
        print(f"  {offset}/{total}")
        if total is not None and offset >= total:
            break
    conn.execute(
        "INSERT OR REPLACE INTO ingest_log (source,day,api_total,rows_written,fetched_at) "
        "VALUES ('team_runs','ALL',?,?,?)",
        (total_written, total_written, dt.datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    print(f"  team runs written: {total_written}")


def build_facts(conn, client, since, until, want_tool_calls, redo=False,
                skip_teams=False, skip_workflows=False, refetch_days=()):
    start = dt.date.fromisoformat(since)
    end = dt.date.fromisoformat(until)

    done_days = {r["day"] for r in conn.execute(
        "SELECT day FROM ingest_log WHERE source='agent_runs'")} if not redo else set()
    # The last ingested day was almost certainly partial when it was first pulled,
    # so an incremental run must be allowed to re-fetch it. Without this the resume
    # filter skips it and the warehouse silently stops advancing. Re-ingest is
    # idempotent (primary key on the log id), so re-fetching costs nothing but time.
    done_days -= set(refetch_days)

    # Probe month-by-month so empty stretches cost one request instead of ~30.
    print(f"Probing months {start} .. {end} for traffic ...")
    days = []
    for month_start in months_between(start, end):
        last = calendar.monthrange(month_start.year, month_start.month)[1]
        month_end = min(dt.date(month_start.year, month_start.month, last), end)
        m_from = max(month_start, start)
        payload = client.get("/api/agent-logs", {
            "limit": 1, "startDate": m_from.isoformat(), "endDate": month_end.isoformat()})
        total = payload.get("total") or 0
        print(f"  {month_start:%Y-%m}: {total:,} runs")
        if total == 0:
            continue
        cur = m_from
        while cur <= month_end:
            days.append(cur.isoformat())
            cur += dt.timedelta(days=1)

    pending = [d for d in days if d not in done_days]
    print(f"\n{len(days)} days with traffic, {len(pending)} still to ingest.")

    t0 = time.time()
    grand = 0
    for i, day in enumerate(pending, 1):
        written, api_total = ingest_day(conn, client, day, want_tool_calls)
        grand += written
        rate = i / max(time.time() - t0, 1e-9)
        eta = (len(pending) - i) / rate if rate else 0
        print(f"  [{i}/{len(pending)}] {day}: {written:,} rows "
              f"(api total {api_total:,}) | eta {eta/60:.1f} min")

    print(f"\nagent runs written this pass: {grand:,}")
    if not skip_teams:
        ingest_teams(conn, client)
    if not skip_workflows:
        ingest_workflows(conn, client)


def last_ingested_day(conn):
    row = conn.execute(
        "SELECT MAX(day) d FROM ingest_log WHERE source='agent_runs' AND day<>'ALL'").fetchone()
    return row["d"] if row and row["d"] else None


# ── status ───────────────────────────────────────────────────────────────────

def status(conn, client=None):
    def scalar(sql):
        row = conn.execute(sql).fetchone()
        return row[0] if row else None

    print("=== warehouse ===")
    for table in ("dim_project", "dim_agent", "dim_team", "dim_workflow",
                  "fact_agent_run", "fact_team_run", "fact_workflow_run",
                  "fact_tool_call", "dim_conversation", "fact_feedback_turn"):
        print(f"  {table:18s} {scalar(f'SELECT COUNT(*) FROM {table}'):>10,}")

    lo = scalar("SELECT MIN(run_day) FROM fact_agent_run")
    hi = scalar("SELECT MAX(run_day) FROM fact_agent_run")
    print(f"  agent run days     {lo} .. {hi}")

    neg = scalar("SELECT COUNT(*) FROM fact_feedback_turn WHERE rating='THUMBS_DOWN'")
    if neg:
        commented = scalar("SELECT COUNT(*) FROM fact_feedback_turn "
                           "WHERE rating='THUMBS_DOWN' AND comment IS NOT NULL")
        labelled = scalar("SELECT COUNT(*) FROM fact_feedback_turn "
                          "WHERE rating='THUMBS_DOWN' AND comment IS NOT NULL "
                          "AND label IS NOT NULL")
        flo = scalar("SELECT MIN(conv_day) FROM fact_feedback_turn")
        fhi = scalar("SELECT MAX(conv_day) FROM fact_feedback_turn")
        print(f"  feedback days      {flo} .. {fhi}")
        print(f"  thumbs-down        {neg:,} ({commented:,} with a comment, "
              f"{labelled:,} classified)")
        if commented > labelled:
            print(f"  -> {commented - labelled:,} complaints not yet classified: "
                  f"run 'python3 label_feedback.py'")

    if client:
        print("\n=== validation vs live API ===")
        api = client.get("/api/agent-logs", {"limit": 1}).get("total")
        local = scalar("SELECT COUNT(*) FROM fact_agent_run")
        print(f"  agent-logs total : api={api:,}  local={local:,}  delta={api - local:,}")
        failed_api = client.get("/api/agent-logs", {"limit": 1, "status": "Failed"}).get("total")
        failed_local = scalar("SELECT COUNT(*) FROM fact_agent_run WHERE status='Failed'")
        print(f"  failed runs      : api={failed_api:,}  local={failed_local:,}")

    print("\n=== relativity: runs and cost by product (all time) ===")
    rows = conn.execute(
        "SELECT portfolio, product, is_focus, runs, pct_of_runs, cost, pct_of_cost, "
        "unique_users, cost_per_run, failure_pct FROM v_product_share "
        "ORDER BY runs DESC").fetchall()
    if not rows:
        print("  (no facts loaded yet)")
    else:
        print(f"  {'':1} {'runs':>9} {'%':>6} {'cost':>11} {'%':>6} {'users':>6} "
              f"{'$/run':>8} {'fail%':>6}  portfolio / product")
        for r in rows:
            star = "*" if r["is_focus"] else " "
            print(f"  {star} {r['runs']:>9,} {r['pct_of_runs'] or 0:>6.2f} "
                  f"${r['cost'] or 0:>10,.2f} {r['pct_of_cost'] or 0:>6.2f} "
                  f"{r['unique_users']:>6,} {r['cost_per_run'] or 0:>8.4f} "
                  f"{r['failure_pct'] or 0:>6.2f}  {r['portfolio']} / {r['product']}")

    wf = conn.execute("""SELECT COUNT(*) execs,
                                COUNT(DISTINCT workflow_key) ran
                         FROM fact_workflow_run""").fetchone()
    wfd = conn.execute("SELECT COUNT(*) n FROM dim_workflow").fetchone()["n"]
    if wf and wfd:
        print(f"\n=== visual workflows ===")
        print(f"  {wf['execs']:,} executions across {wf['ran']} of {wfd} definitions "
              f"({100*wf['ran']/wfd:.1f}% ever ran)")
        for r in conn.execute("""SELECT status, COUNT(*) n FROM fact_workflow_run
                                 GROUP BY status ORDER BY n DESC"""):
            print(f"    {r['n']:>5,}  {r['status']}")

    gaps = conn.execute(
        "SELECT SUM(runs) n FROM v_tagging_gaps").fetchone()
    if gaps and gaps["n"]:
        print(f"\n  {gaps['n']:,} runs are still Unattributed "
              f"-- see: SELECT * FROM v_tagging_gaps LIMIT 20;")


# ── cli ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command",
                    choices=["dims", "retag", "facts", "workflows", "feedback",
                             "status"])
    ap.add_argument("--since", default=f"{EARLIEST_PROBE_YEAR}-01-01")
    ap.add_argument("--until", default=dt.date.today().isoformat())
    ap.add_argument("--incremental", action="store_true",
                    help="start from the last successfully ingested day")
    ap.add_argument("--redo", action="store_true", help="ignore ingest_log and refetch")
    ap.add_argument("--tool-calls", action="store_true",
                    help="also populate fact_tool_call (bigger, slower)")
    ap.add_argument("--skip-teams", action="store_true")
    ap.add_argument("--skip-workflows", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--refresh-days", type=int, default=14,
                    help="feedback: re-fetch stored transcripts this recent, since a "
                         "conversation can collect new ratings after its first pull")
    ap.add_argument("--negative-only", action="store_true",
                    help="feedback: skip the positive pass (halves the fetches, but "
                         "negative-share-of-rated-turns loses its denominator)")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    # `dims` always rebuilds the dimension tables so taxonomy column changes land.
    conn = connect(args.db, rebuild_dims=(args.command == "dims"))

    if args.command == "retag":
        retag(conn)
        return

    client = FoundryClient()
    print(f"Foundry: {client.base_url}  tenant={client.tenant_slug or '(none)'}\n")

    if args.command == "dims":
        build_dims(conn, client, workers=args.workers)
    elif args.command == "facts":
        since, refetch = args.since, ()
        if args.incremental:
            last = last_ingested_day(conn)
            if last:
                # Re-do the last day: it was partial when first pulled.
                since, refetch = last, (last,)
                print(f"Incremental: resuming from {since} (re-fetching {last})")
        build_facts(conn, client, since, args.until, args.tool_calls,
                    redo=args.redo, skip_teams=args.skip_teams,
                    skip_workflows=args.skip_workflows, refetch_days=refetch)
        status(conn, client)
    elif args.command == "workflows":
        ingest_workflows(conn, client, workers=args.workers)
        status(conn, client)
    elif args.command == "feedback":
        ingest_feedback(
            conn, client, args.since, args.until,
            workers=max(args.workers, 6), redo=args.redo,
            refresh_days=args.refresh_days,
            ratings=("negative",) if args.negative_only else FEEDBACK_RATINGS)
        status(conn, client)
    elif args.command == "status":
        status(conn, client)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted — rerun with --incremental to resume", file=sys.stderr)
        sys.exit(130)
    except FoundryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
