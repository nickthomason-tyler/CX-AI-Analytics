#!/usr/bin/env python3
"""Phase 1: render the warehouse into a single self-contained dashboard.html.

    python3 build_dashboard.py [--db foundry_analytics.db] [--out dashboard.html]

No server and no dependencies: aggregates are embedded as JSON and all filtering
happens client-side, so the file can be mailed or dropped in SharePoint and still
work. Distinct-user and distinct-agent counts are exact under every filter
combination because the presence tuples (user x day x product) are embedded
rather than pre-summed -- distinct counts are not additive, so summing
per-product user counts would overstate them.
"""

import argparse
import datetime as dt
import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "foundry_analytics.db")
OUT_PATH = os.path.join(HERE, "dashboard.html")

# Categorical slots 1-6 from the validated reference palette (light / dark).
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"]
MAX_LINE_SERIES = 6  # 5 named portfolios + "Other"

# Per-complaint text embedded in the feedback tab. The comment is the payload and is
# kept effectively whole; the question and response are context, so they are trimmed
# to hold the file mailable (~1,300 negative turns x these limits ~= 2 MB).
FB_COMMENT_CHARS = 1500
FB_QUESTION_CHARS = 400
FB_RESPONSE_CHARS = 700

# Display order for failure categories: worst-to-mildest, with the non-defects last,
# so the chart reads top-down as "what to fix first". Must stay in sync with
# label_feedback.py's LABELS.
FB_LABELS = (
    "incorrect_answer", "wrong_ownership_claim", "kb_gap", "over_narrow_refusal",
    "incomplete_answer", "too_generic", "missing_source_or_next_step",
    "language_handling", "tool_or_system_error", "formatting_or_tone",
    "other", "not_a_defect",
)


# ── EPL solutions tab ────────────────────────────────────────────────────────
# Which EPL deployments are *shipped*, and to whom. This is curated knowledge that
# exists nowhere in the API: Foundry records a tenant name and nothing about whether
# that tenant is a customer, a preview or a sandbox. Confirmed 2026-08-10.
#
# Two namings are deliberately counter-intuitive and were verified, not assumed:
# '(Test)' and '(Train)' are the environments those customers actually work in, so
# they are real customer usage -- not probing. 'City of AI, ME' is a private preview
# where users from many clients shared one environment, which is why its user count
# is the best multi-client evidence available.
#
# Encinitas runs BOTH environments and both are listed, because the customer cut over
# from '(Test)' to production on 2026-08-14 -- the last (Test) run and the first
# production run are the same day. Dropping either one breaks the adoption curve at
# exactly the cutover: (Test) alone reads as a customer that went dormant, production
# alone as one that only started last week. Note the production tenant is
# MIXED-PRODUCT (its 'Resident AI Assistant', 440 runs, is Tyler One, not EPL) --
# safe here only because every query in this tab scopes on product = EPL_PRODUCT as
# well as tenant_name. Same trap as 'City of AI, ME'; do not relax that scope.
#
# Everything NOT listed here is disclosed in the tab rather than dropped silently --
# 'Tyler Technologies' alone is 4,380 runs of dev sandbox, larger than every shipped
# deployment combined, and omitting it without saying so would look like hiding it.
EPL_DEPLOYMENTS = {
    "EPL CX":                       "Support",
    "City of AI, ME":               "Private preview",
    "City of AI, ME - EPL":         "Private preview",
    "City of Encinitas, CA (Test)": "Customer",
    "City of Encinitas, CA":        "Customer",
    "Coral Gables, FL (Train)":     "Customer",
    "Coral Gables, FL":             "Customer",
    "City of Boulder":              "Customer",
    "City of Roswell":              "Customer",
    "City of Johns Creek":          "Customer",
}
EPL_TYPE_ORDER = ("Support", "Private preview", "Customer")
EPL_PRODUCT = "Enterprise Permitting & Licensing"
# Monday-anchored weeks. NOT sqlite's "weekday 1", which resolves to *today* when today
# is already a Monday and so files Mondays into the previous week.
EPL_WEEK = "date(run_day, '-'||((strftime('%w',run_day)+6)%7)||' days')"


def build_epl(cur):
    """The EPL-solutions payload: shipped deployments, weekly adoption, quality.

    Distinct users and agents ship as (week, deployment, index) presence tuples rather
    than per-deployment counts. Summing counts overstates users by 17 of 53 here --
    the same person appears in several deployments -- and any client-side filter would
    inherit that error.
    """
    names = ",".join("?" * len(EPL_DEPLOYMENTS))
    args = list(EPL_DEPLOYMENTS)
    scope = f"product = ? AND tenant_name IN ({names})"
    scope_args = [EPL_PRODUCT] + args

    deps, d_ix = [], {}
    for r in cur.execute(f"""
        SELECT tenant_name t, COUNT(DISTINCT agent_key) agents, COUNT(*) runs,
               COUNT(DISTINCT conversation_id) convs, COUNT(DISTINCT user_id) users,
               MIN(run_day) first_day, MAX(run_day) last_day
        FROM v_agent_runs WHERE {scope} GROUP BY tenant_name""", scope_args):
        d_ix[r["t"]] = len(deps)
        deps.append({
            "name": r["t"], "type": EPL_DEPLOYMENTS[r["t"]],
            "agents": r["agents"], "runs": r["runs"], "convs": r["convs"],
            "users": r["users"], "first": r["first_day"], "last": r["last_day"],
        })

    # A continuous Monday axis. Weeks with no activity must still occupy space or a
    # dormant stretch reads as a tighter timeline than it was (there is a dead week at
    # 2026-06-22 in this data).
    bounds = cur.execute(
        f"SELECT MIN({EPL_WEEK}) a, MAX({EPL_WEEK}) b, MAX(run_day) last "
        f"FROM v_agent_runs WHERE {scope}", scope_args).fetchone()
    weeks, w_ix = [], {}
    if bounds["a"]:
        w = dt.date.fromisoformat(bounds["a"])
        end = dt.date.fromisoformat(bounds["b"])
        while w <= end:
            w_ix[w.isoformat()] = len(weeks)
            weeks.append(w.isoformat())
            w += dt.timedelta(days=7)
    # The final week is partial unless the data reaches its Sunday; charting it as a
    # complete week invents a decline that is only a truncated week.
    partial = -1
    if weeks:
        last_week_end = dt.date.fromisoformat(weeks[-1]) + dt.timedelta(days=6)
        if dt.date.fromisoformat(bounds["last"]) < last_week_end:
            partial = len(weeks) - 1

    # Runs only. Conversations deliberately do NOT live here: 10 of them span a week
    # boundary, so per-week COUNT(DISTINCT conversation_id) sums to 379 against a true
    # 366. They ride the presence tuples below instead, like users and agents.
    facts = []
    for r in cur.execute(f"""
        SELECT {EPL_WEEK} wk, tenant_name t, COUNT(*) runs
        FROM v_agent_runs WHERE {scope} GROUP BY wk, tenant_name""", scope_args):
        facts.append([w_ix[r["wk"]], d_ix[r["t"]], r["runs"]])

    convs, c_ix, conv_tuples = [], {}, []
    for r in cur.execute(f"""
        SELECT DISTINCT {EPL_WEEK} wk, tenant_name t, conversation_id c
        FROM v_agent_runs WHERE {scope} AND conversation_id IS NOT NULL""", scope_args):
        if r["c"] not in c_ix:
            c_ix[r["c"]] = len(convs); convs.append(r["c"])
        conv_tuples.append([w_ix[r["wk"]], d_ix[r["t"]], c_ix[r["c"]]])

    users, u_ix, user_tuples = [], {}, []
    for r in cur.execute(f"""
        SELECT DISTINCT {EPL_WEEK} wk, tenant_name t, user_id u
        FROM v_agent_runs WHERE {scope} AND user_id IS NOT NULL""", scope_args):
        if r["u"] not in u_ix:
            u_ix[r["u"]] = len(users); users.append(r["u"])
        user_tuples.append([w_ix[r["wk"]], d_ix[r["t"]], u_ix[r["u"]]])

    agent_tuples, a_ix, agent_names = [], {}, []
    for r in cur.execute(f"""
        SELECT DISTINCT {EPL_WEEK} wk, tenant_name t, agent_key k,
               agent_name_current n
        FROM v_agent_runs WHERE {scope} AND agent_key IS NOT NULL""", scope_args):
        if r["k"] not in a_ix:
            a_ix[r["k"]] = len(agent_names); agent_names.append(r["n"] or "(unnamed)")
        agent_tuples.append([w_ix[r["wk"]], d_ix[r["t"]], a_ix[r["k"]]])

    # Quality. Feedback lives at TURN grain and the denominator lives at CONVERSATION
    # grain, so both are carried explicitly and never mixed:
    #   pos/neg   -- rated turns. A user can thumbs-down four turns of one conversation,
    #                and one City of AI conversation is exactly that.
    #   posC/negC -- conversations containing at least one such turn. negC is what the
    #                complaint RATE divides by convs, because the claim being made is
    #                "this share of conversations produced a complaint". Dividing 41
    #                complaint turns by 379 conversations reported 10.8% against a true
    #                7.4%, and 70.8% against 45.8% for Encinitas -- it counted one unhappy
    #                conversation up to four times. Found 2026-08-11 from the
    #                table not adding up (9 + 5 turns against 13 rated conversations).
    #   rated     -- conversations with any feedback at all. NOT posC + negC: 7 shipped
    #                conversations contain both a thumbs-up and a thumbs-down, so those
    #                two overlap rather than partition.
    # The rate stays "of conversations held", not "of conversations rated", so it reads
    # as a floor.
    zero = {"pos": 0, "neg": 0, "posC": 0, "negC": 0, "rated": 0}
    fb = {i: dict(zero) for i in range(len(deps))}
    for r in cur.execute(f"""
        SELECT tenant_name t, SUM(rating='THUMBS_UP') pos,
               SUM(rating='THUMBS_DOWN') neg,
               COUNT(DISTINCT CASE WHEN rating='THUMBS_UP'
                                   THEN conversation_id END) posC,
               COUNT(DISTINCT CASE WHEN rating='THUMBS_DOWN'
                                   THEN conversation_id END) negC,
               COUNT(DISTINCT conversation_id) rated
        FROM v_feedback_turns WHERE {scope.replace('run_day', 'conv_day')}
        GROUP BY tenant_name""", scope_args):
        if r["t"] in d_ix:
            fb[d_ix[r["t"]]] = {k: r[k] or 0 for k in zero}
    for i, d in enumerate(deps):
        d.update(fb[i])

    # The agents behind each deployment row, so "3 agents" can be opened and read.
    # Runs, convs, rated, pos/neg and posC/negC all sum exactly to the deployment
    # rollup: no conversation in this data touches two agents inside one tenant, and
    # v_feedback_turns pins each agent to a single tenant. Users do NOT sum -- the same
    # person uses several agents -- which is why the rollup keeps its own distinct count
    # instead of adding these.
    ag_fb = {}
    for r in cur.execute(f"""
        SELECT agent_key k, SUM(rating='THUMBS_UP') pos,
               SUM(rating='THUMBS_DOWN') neg,
               COUNT(DISTINCT CASE WHEN rating='THUMBS_UP'
                                   THEN conversation_id END) posC,
               COUNT(DISTINCT CASE WHEN rating='THUMBS_DOWN'
                                   THEN conversation_id END) negC,
               COUNT(DISTINCT conversation_id) rated
        FROM v_feedback_turns WHERE {scope.replace('run_day', 'conv_day')}
        GROUP BY agent_key""", scope_args):
        ag_fb[r["k"]] = {k: r[k] or 0 for k in zero}

    dep_agents = [[] for _ in deps]
    for r in cur.execute(f"""
        SELECT tenant_name t, agent_key k, agent_name_current n, COUNT(*) runs,
               COUNT(DISTINCT conversation_id) convs, COUNT(DISTINCT user_id) users,
               MIN(run_day) first_day, MAX(run_day) last_day
        FROM v_agent_runs WHERE {scope} AND agent_key IS NOT NULL
        GROUP BY tenant_name, agent_key""", scope_args):
        dep_agents[d_ix[r["t"]]].append({
            "name": (r["n"] or "(unnamed)").strip(), "key": r["k"],
            "runs": r["runs"], "convs": r["convs"], "users": r["users"],
            "first": r["first_day"], "last": r["last_day"],
            **ag_fb.get(r["k"], dict(zero)),
        })
    # Two agent records can share a display name inside one deployment -- a copy, or a
    # rebuild under a new id (City of AI has two 'EPL Staff Assistant', EPL CX two
    # 'EPL Automation Support Agent'). Undisambiguated they read as an accidental
    # duplicate row, so the id fragment is appended only where it is needed.
    for group in dep_agents:
        group.sort(key=lambda a: (-a["convs"], -a["runs"], a["name"]))
        dupes = {a["name"] for a in group
                 if sum(b["name"] == a["name"] for b in group) > 1}
        for a in group:
            if a["name"] in dupes:
                a["name"] = f'{a["name"]} · {a["key"][:8]}'

    complaints = []
    for r in cur.execute(f"""
        SELECT tenant_name t, agent_name_current a, conv_day d, label l, severity s,
               fix_type f, comment c, question q, conversation_id cid
        FROM v_feedback_turns
        WHERE rating = 'THUMBS_DOWN' AND {scope.replace('run_day', 'conv_day')}
        ORDER BY conv_day DESC""", scope_args):
        complaints.append({
            # cid is carried so the fix list can say how many CONVERSATIONS these turns
            # came from -- 41 complaints from 28 conversations, not 41 unhappy users.
            "dep": d_ix[r["t"]], "cid": r["cid"] or "", "a": r["a"] or "(unnamed)",
            "d": r["d"],
            "l": r["l"] or "", "s": r["s"] or "", "f": r["f"] or "",
            "c": (r["c"] or "")[:FB_COMMENT_CHARS],
            "q": (r["q"] or "")[:FB_QUESTION_CHARS],
        })

    excluded = [[r["t"] or "(none)", r["runs"], r["convs"]] for r in cur.execute(f"""
        SELECT tenant_name t, COUNT(*) runs, COUNT(DISTINCT conversation_id) convs
        FROM v_agent_runs
        WHERE product = ? AND (tenant_name NOT IN ({names}) OR tenant_name IS NULL)
        GROUP BY tenant_name ORDER BY runs DESC""", scope_args)]

    # SQL truth for the headline numerator, so the verifier can prove the client-side
    # sum of per-deployment negC reproduces it rather than trusting that it does.
    truth = cur.execute(f"""
        SELECT COUNT(DISTINCT conversation_id) n FROM v_feedback_turns
        WHERE rating = 'THUMBS_DOWN' AND {scope.replace('run_day', 'conv_day')}
        """, scope_args).fetchone()["n"]
    both = cur.execute(f"""
        SELECT COUNT(*) n FROM (
          SELECT conversation_id FROM v_feedback_turns
          WHERE {scope.replace('run_day', 'conv_day')} GROUP BY conversation_id
          HAVING SUM(rating='THUMBS_UP') > 0 AND SUM(rating='THUMBS_DOWN') > 0)
        """, scope_args).fetchone()["n"]

    return {
        "deps": deps, "typeOrder": list(EPL_TYPE_ORDER), "weeks": weeks,
        "facts": facts, "userTuples": user_tuples, "agentTuples": agent_tuples,
        "convTuples": conv_tuples, "nUsers": len(users), "nConvs": len(convs),
        "agentNames": agent_names, "depAgents": dep_agents,
        "nComplaintConvs": truth, "nMixedConvs": both,
        "complaints": complaints, "excluded": excluded, "partialWeek": partial,
    }


# ── Users tab ────────────────────────────────────────────────────────────────
# Who is actually using the EPL CX agents, and who has stopped. Scoped on tenant alone
# (not product): the question is about people in this tenant, whatever they ran.
#
# 'anonymous' is ONE shared identity, the login everyone originally had to use, so it is
# shipped as its own row/flag rather than counted as a person. Its volume and conversation
# count over time are the only evidence of how many people still use that route; the
# conversation count is the nearer proxy for sessions, but neither is a head count.
#
# Only people with at least one run exist in this data. Foundry exposes no roster of
# tenant members, so someone who never ran an agent cannot appear here.
USERS_TENANT = "EPL CX"


def build_users(cur):
    scope = "tenant_name = ?"
    args = [USERS_TENANT]
    bounds = cur.execute(
        f"SELECT MIN({EPL_WEEK}) a, MAX({EPL_WEEK}) b, MAX(run_day) last "
        f"FROM fact_agent_run WHERE {scope}", args).fetchone()
    weeks = []
    if bounds["a"]:
        w = dt.date.fromisoformat(bounds["a"])
        end = dt.date.fromisoformat(bounds["b"])
        while w <= end:
            weeks.append(w.isoformat())
            w += dt.timedelta(days=7)
    # Same partial-final-week rule as the EPL tab: a truncated week is not a decline.
    partial = -1
    if weeks:
        week_end = dt.date.fromisoformat(weeks[-1]) + dt.timedelta(days=6)
        if dt.date.fromisoformat(bounds["last"]) < week_end:
            partial = len(weeks) - 1

    # Per-person, per-DAY tuples, so the page can answer any date range. Distinct
    # counts (agents, conversations) ride presence tuples rather than per-day counts
    # for the usual reason: summing distinct counts across days overstates them.
    dates = [r["d"] for r in cur.execute(
        f"SELECT DISTINCT run_day d FROM fact_agent_run WHERE {scope} ORDER BY d", args)]
    d_ix = {d: i for i, d in enumerate(dates)}

    rows, ix = [], {}
    for r in cur.execute(f"""
        SELECT COALESCE(user_id, '(none)') id, MAX(user_name) name, MIN(run_day) first_day
        FROM fact_agent_run WHERE {scope} GROUP BY 1""", args):
        ix[r["id"]] = len(rows)
        rows.append({"name": r["name"] or r["id"],
                     "anon": (r["name"] or "").lower() == "anonymous",
                     "first": r["first_day"]})

    day_tuples = [[ix[r["id"]], d_ix[r["d"]], r["n"]] for r in cur.execute(f"""
        SELECT COALESCE(user_id, '(none)') id, run_day d, COUNT(*) n
        FROM fact_agent_run WHERE {scope} GROUP BY 1, 2""", args)]

    a_ix, agent_tuples = {}, []
    for r in cur.execute(f"""
        SELECT DISTINCT COALESCE(user_id, '(none)') id, run_day d, agent_key k
        FROM fact_agent_run WHERE {scope} AND agent_key IS NOT NULL""", args):
        a_ix.setdefault(r["k"], len(a_ix))
        agent_tuples.append([ix[r["id"]], d_ix[r["d"]], a_ix[r["k"]]])

    # Conversations are carried for the anonymous login only: they are the nearest proxy
    # for sessions on a shared identity. (day, conversation) presence, so a range's
    # count is a true distinct, and a conversation spanning two days counts once.
    c_ix, conv_tuples = {}, []
    for r in cur.execute(f"""
        SELECT DISTINCT run_day d, conversation_id c FROM fact_agent_run
        WHERE {scope} AND LOWER(user_name) = 'anonymous'
          AND conversation_id IS NOT NULL""", args):
        c_ix.setdefault(r["c"], len(c_ix))
        conv_tuples.append([d_ix[r["d"]], c_ix[r["c"]]])

    return {"tenant": USERS_TENANT, "asOf": bounds["last"], "weeks": weeks,
            "partialWeek": partial, "dates": dates, "rows": rows,
            "dayTuples": day_tuples, "agentTuples": agent_tuples,
            "convTuples": conv_tuples}


def build_payload(conn):
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    products = [r["product"] for r in cur.execute(
        "SELECT DISTINCT product FROM v_agent_runs ORDER BY product")]
    p_ix = {p: i for i, p in enumerate(products)}

    prod_meta = {}
    for r in cur.execute("""SELECT product, portfolio, MAX(is_focus) is_focus
                            FROM v_agent_runs GROUP BY product"""):
        prod_meta[r["product"]] = (r["portfolio"], r["is_focus"])

    portfolios = sorted({v[0] for v in prod_meta.values()})
    f_ix = {p: i for i, p in enumerate(portfolios)}

    # The day domain spans run days AND feedback days. The two streams are ingested
    # separately and feedback usually runs a day or two ahead of the last ingested
    # run day, so a run-day-only domain silently drops the newest complaints from
    # the default "all time" slice -- exactly the ones worth reading.
    days = sorted({r["d"] for r in cur.execute("""
        SELECT DISTINCT run_day d FROM v_agent_runs WHERE run_day IS NOT NULL
        UNION
        SELECT DISTINCT conv_day d FROM fact_feedback_turn WHERE conv_day IS NOT NULL
    """)})
    d_ix = {d: i for i, d in enumerate(days)}

    # Day x product measures. Additive only -- distinct counts come from tuples.
    facts = []
    for r in cur.execute("""
        SELECT run_day, product,
               COUNT(*) runs,
               COUNT(DISTINCT conversation_id) convos,
               SUM(COALESCE(token_cost,0)) cost,
               SUM(COALESCE(total_tokens,0)) tokens,
               SUM(CASE WHEN status <> 'Completed' THEN 1 ELSE 0 END) fails,
               SUM(COALESCE(duration_ms,0)) dur_ms,
               SUM(CASE WHEN duration_ms IS NOT NULL THEN 1 ELSE 0 END) dur_n
        FROM v_agent_runs WHERE run_day IS NOT NULL
        GROUP BY run_day, product"""):
        facts.append([
            d_ix[r["run_day"]], p_ix[r["product"]], r["runs"], r["convos"],
            round(r["cost"], 4), r["tokens"], r["fails"], r["dur_ms"], r["dur_n"],
        ])

    users = [r["user_id"] for r in cur.execute(
        "SELECT DISTINCT COALESCE(user_id,'(unknown)') user_id FROM v_agent_runs "
        "ORDER BY user_id")]
    u_ix = {u: i for i, u in enumerate(users)}
    user_tuples = [
        [u_ix[r["u"]], d_ix[r["d"]], p_ix[r["p"]]]
        for r in cur.execute("""SELECT DISTINCT COALESCE(user_id,'(unknown)') u,
                                run_day d, product p FROM v_agent_runs
                                WHERE run_day IS NOT NULL""")]

    agent_keys = [r["agent_key"] for r in cur.execute(
        "SELECT DISTINCT agent_key FROM v_agent_runs WHERE agent_key IS NOT NULL "
        "ORDER BY agent_key")]
    a_ix = {a: i for i, a in enumerate(agent_keys)}
    agent_tuples = [
        [a_ix[r["a"]], d_ix[r["d"]], p_ix[r["p"]]]
        for r in cur.execute("""SELECT DISTINCT agent_key a, run_day d, product p
                                FROM v_agent_runs
                                WHERE run_day IS NOT NULL AND agent_key IS NOT NULL""")]

    # Agent leaderboard. Kept as its own table so it can be sorted client-side
    # without re-deriving from the day grain.
    agents = []
    for r in cur.execute("""SELECT s.agent_name, s.product, s.portfolio, s.is_focus,
                                   vt.tenant_name,
                                   s.runs, s.unique_users, s.cost, s.cost_per_run,
                                   s.avg_seconds, s.failure_pct, s.first_run, s.last_run
                            FROM v_agent_scorecard s
                            LEFT JOIN v_agent_tenant vt ON vt.agent_key = s.agent_key
                            ORDER BY s.runs DESC"""):
        agents.append({
            "name": r["agent_name"] or "(unnamed)",
            "product": r["product"], "portfolio": r["portfolio"],
            "focus": r["is_focus"] or 0,
            "tenant": r["tenant_name"] or "",
            "runs": r["runs"], "users": r["unique_users"],
            "cost": round(r["cost"] or 0, 2),
            "cpr": r["cost_per_run"] or 0,
            "secs": r["avg_seconds"] or 0,
            "fail": r["failure_pct"] or 0,
            "first": r["first_run"], "last": r["last_run"],
        })

    errors = [{"product": r["product"], "code": r["code"], "n": r["n"]}
              for r in cur.execute("""
        SELECT product,
               COALESCE(
                 CASE WHEN error_message LIKE '[%]%'
                      THEN substr(error_message, 2, instr(error_message,']')-2) END,
                 'UNCLASSIFIED') code,
               COUNT(*) n
        FROM v_agent_runs WHERE status <> 'Completed'
        GROUP BY product, code ORDER BY n DESC LIMIT 200""")]

    dormant = [{"product": r["product"], "n": r["n"]} for r in cur.execute(
        "SELECT COALESCE(product,'Unattributed') product, COUNT(*) n "
        "FROM v_dormant_agents GROUP BY product ORDER BY n DESC")]

    built_agents = [{"product": r["product"], "n": r["n"]} for r in cur.execute(
        "SELECT COALESCE(product,'Unattributed') product, COUNT(*) n "
        "FROM dim_agent GROUP BY product")]

    gaps = [{"project": r["project_name"], "agent": r["agent_name"],
             "runs": r["runs"], "cost": round(r["cost"] or 0, 2)}
            for r in cur.execute("SELECT * FROM v_tagging_gaps LIMIT 40")]

    # Stable colour assignment: slots are fixed by ALL-TIME volume, never by rank
    # within the current filter, so filtering can't repaint the survivors. The
    # long tail is permanently "Other" -- also a stable identity.
    ranked = [r["portfolio"] for r in cur.execute(
        """SELECT portfolio, COUNT(*) n FROM v_agent_runs
           GROUP BY portfolio ORDER BY n DESC""")]
    portfolio_slot = {p: (ranked.index(p) if ranked.index(p) < MAX_LINE_SERIES else -1)
                      for p in ranked}

    wf_runs = [{"name": r["workflow_name"] or "(unnamed)", "product": r["product"],
                "focus": r["is_focus"] or 0, "day": r["run_day"],
                "status": r["status"],
                "step": r["suspended_step"] or "",
                "reason": (r["suspended_reason"] or "").replace("\n", " ")[:160],
                "resumed": 1 if r["resumed_at"] else 0}
               for r in cur.execute("""SELECT workflow_name, product, is_focus, run_day,
                                              status, suspended_step, suspended_reason,
                                              resumed_at
                                       FROM v_workflow_runs
                                       ORDER BY run_day DESC""")]

    wf_adoption = [{"product": r["product"], "focus": r["is_focus"] or 0,
                    "defined": r["defined"], "ran": r["ever_ran"],
                    "execs": r["executions"]}
                   for r in cur.execute("""SELECT product, is_focus, defined, ever_ran,
                                                  executions FROM v_workflow_adoption
                                           WHERE defined > 0
                                           ORDER BY defined DESC""")]

    team_runs = cur.execute("SELECT COUNT(*) n FROM fact_team_run").fetchone()["n"]
    wf_defs = cur.execute("SELECT COUNT(*) n FROM dim_workflow").fetchone()["n"]

    # ── feedback ────────────────────────────────────────────────────────────
    # Every downvoted turn is embedded individually, because the whole point of the
    # tab is reading the actual complaints -- an aggregate cannot answer "what are
    # people saying". Full transcripts are NOT embedded (~37 KB each would run to
    # tens of MB); they stay in SQLite for label_feedback.py.
    # Tenant is interned into a lookup list rather than repeated per row: 1,144 turns
    # across 21 tenants, and the names are long ("City of Encinitas, CA (Test)").
    tenants = [r[0] for r in cur.execute(
        "SELECT DISTINCT tenant_name FROM v_feedback_turns "
        "WHERE tenant_name IS NOT NULL ORDER BY tenant_name")]
    t_ix = {t: i for i, t in enumerate(tenants)}

    fb_turns = []
    for r in cur.execute("""
        SELECT conv_day, conversation_id, turn_index, comment, question, response,
               label, severity, fix_type, label_confidence, label_rationale,
               agent_name_current, product, has_comment, tenant_name
        FROM v_feedback_turns
        WHERE rating = 'THUMBS_DOWN'
        ORDER BY conv_day DESC, conversation_id, turn_index"""):
        product = r["product"] or "Unattributed"
        if product not in p_ix:
            # Feedback on an agent that has no runs in the window the facts cover.
            continue
        fb_turns.append({
            "d": r["conv_day"],
            "p": p_ix[product],
            "a": r["agent_name_current"] or "(unnamed)",
            "tn": t_ix.get(r["tenant_name"], -1),
            "q": (r["question"] or "")[:FB_QUESTION_CHARS],
            "r": (r["response"] or "")[:FB_RESPONSE_CHARS],
            "c": (r["comment"] or "")[:FB_COMMENT_CHARS],
            "l": r["label"] or "",
            "s": r["severity"] or "",
            "f": r["fix_type"] or "",
            "cf": r["label_confidence"],
            "why": (r["label_rationale"] or "")[:400],
            "cid": r["conversation_id"],
            "t": r["turn_index"],
        })

    # Positive turns are counted, never quoted: praise carries no comment text
    # (the UI only prompts for text on a thumbs-down), so rows would be empty.
    # Grouped by tenant as well as product: the feedback tab's tenant filter has to
    # narrow the positives too, and positives live only here (fbTurns is negatives
    # only). Without the tenant column a filtered view would show one tenant's
    # thumbs-down against every tenant's thumbs-up, which reads as a false collapse
    # in satisfaction. The grouping is sparse, so this costs little.
    fb_counts = []
    for r in cur.execute("""
        SELECT conv_day, product, tenant_name,
               SUM(CASE WHEN rating='THUMBS_DOWN' THEN 1 ELSE 0 END) neg,
               SUM(CASE WHEN rating='THUMBS_UP'   THEN 1 ELSE 0 END) pos,
               COUNT(DISTINCT conversation_id) convs
        FROM v_feedback_turns
        WHERE conv_day IS NOT NULL
        GROUP BY conv_day, product, tenant_name"""):
        product = r["product"] or "Unattributed"
        if product not in p_ix:
            continue
        fb_counts.append([r["conv_day"], p_ix[product], r["neg"], r["pos"],
                          r["convs"], t_ix.get(r["tenant_name"], -1)])

    # Per-agent rates. conversations comes from the run facts, so the denominator is
    # "conversations actually held", not "conversations that happened to be rated".
    fb_agents = []
    for r in cur.execute("""SELECT agent_name, tenant_name, product, conversations,
                                   rated_conversations,
                                   negative_turns, positive_turns, negative_with_comment,
                                   negative_share_pct, negatives_per_100_convs,
                                   feedback_rate_pct, last_feedback
                            FROM v_feedback_scorecard
                            ORDER BY negative_turns DESC"""):
        product = r["product"] or "Unattributed"
        if product not in p_ix:
            continue
        fb_agents.append({
            "name": r["agent_name"] or "(unnamed)",
            "tn": t_ix.get(r["tenant_name"], -1),
            "p": p_ix[product],
            "convs": r["conversations"] or 0,
            "rated": r["rated_conversations"] or 0,
            "neg": r["negative_turns"] or 0,
            "pos": r["positive_turns"] or 0,
            "negc": r["negative_with_comment"] or 0,
            "share": r["negative_share_pct"],
            "per100": r["negatives_per_100_convs"],
            "rate": r["feedback_rate_pct"],
            "last": r["last_feedback"] or "",
        })

    fb_labelled = cur.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN label IS NOT NULL AND trim(label) <> '' "
        "THEN 1 ELSE 0 END) l FROM v_feedback_turns "
        "WHERE rating='THUMBS_DOWN' AND has_comment=1").fetchone()

    return {
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "products": products,
        "portfolios": portfolios,
        "prodPortfolio": [f_ix[prod_meta[p][0]] for p in products],
        "prodFocus": [prod_meta[p][1] or 0 for p in products],
        "days": days,
        "facts": facts,
        "userTuples": user_tuples,
        "agentTuples": agent_tuples,
        "nUsers": len(users),
        "nAgents": len(agent_keys),
        "agents": agents,
        "errors": errors,
        "dormant": dormant,
        "builtAgents": built_agents,
        "gaps": gaps,
        "teamRuns": team_runs,
        "workflowDefs": wf_defs,
        "seriesLight": SERIES_LIGHT,
        "seriesDark": SERIES_DARK,
        "maxLineSeries": MAX_LINE_SERIES,
        "portfolioSlot": [portfolio_slot.get(p, -1) for p in portfolios],
        "wfRuns": wf_runs,
        "wfAdoption": wf_adoption,
        "fbTurns": fb_turns,
        "fbCounts": fb_counts,
        "fbAgents": fb_agents,
        "tenants": tenants,
        "epl": build_epl(cur),
        "users": build_users(cur),
        "fbCommented": fb_labelled["n"] or 0,
        "fbLabelled": fb_labelled["l"] or 0,
        "fbLabels": list(FB_LABELS),
    }


HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Foundry Adoption &amp; Usage</title>
<style>
  :root {
    --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
    --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,0.10);
    --deemph:#c3c2b7; --good:#0ca30c; --warning:#fab219; --critical:#d03b3b;
    --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --s5:#e87ba4; --s6:#008300;
    --seq-hi:#1c5cab; --seq-lo:#cde2fb;
    color-scheme:light;
  }
  @media (prefers-color-scheme:dark){
    :root:where(:not([data-theme="light"])){
      --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
      --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
      --deemph:#4a4a46; --good:#0ca30c;
      --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#008300;
      --seq-hi:#86b6ef; --seq-lo:#184f95;
      color-scheme:dark;
    }
  }
  :root[data-theme="dark"]{
    --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
    --deemph:#4a4a46; --good:#0ca30c;
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#008300;
    --seq-hi:#86b6ef; --seq-lo:#184f95;
    color-scheme:dark;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--page);color:var(--ink);
       font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
       -webkit-font-smoothing:antialiased}
  .wrap{max-width:1280px;margin:0 auto;padding:28px 24px 72px}
  h1{font-size:20px;font-weight:600;margin:0 0 2px}
  .sub{color:var(--ink2);font-size:13px;margin-bottom:22px}
  .sub b{color:var(--ink);font-weight:600}

  /* filters: one row, above everything they scope */
  .filters{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;
           padding:14px 16px;background:var(--surface);border:1px solid var(--border);
           border-radius:10px;margin-bottom:8px;position:sticky;top:0;z-index:20}
  .fgroup{display:flex;flex-direction:column;gap:5px}
  .fgroup>label{font-size:11px;color:var(--muted);text-transform:uppercase;
                letter-spacing:.05em;font-weight:600}
  select,button,input[type=search]{font:inherit;color:var(--ink);background:var(--surface);
    border:1px solid var(--axis);border-radius:7px;padding:6px 9px}
  button{cursor:pointer}
  button:hover{background:var(--grid)}
  .filters button[aria-pressed=true]{background:var(--s1);border-color:var(--s1);color:#fff}
  .spacer{flex:1}
  .chip{font-size:12px;color:var(--ink2);padding:6px 0}

  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));
        gap:12px;margin:16px 0 8px}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:10px;
        padding:14px 16px}
  .tile .lab{font-size:12px;color:var(--ink2);margin-bottom:5px}
  .tile .val{font-size:26px;font-weight:600;letter-spacing:-.01em;line-height:1.15}
  .tile .note{font-size:11.5px;color:var(--muted);margin-top:3px}
  .hero{grid-column:span 2}
  .hero .val{font-size:48px;line-height:1.05}

  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}
  @media(max-width:900px){.grid2{grid-template-columns:1fr}.hero{grid-column:span 1}}
  .chead{display:flex;justify-content:space-between;align-items:baseline;
         gap:10px;margin-bottom:2px}
  .ctitle{font-size:14px;font-weight:600}
  .cnote{font-size:11.5px;color:var(--muted)}
  .tbtn{font-size:11px;padding:3px 8px;border-radius:6px;color:var(--ink2)}
  .legend{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0 2px}
  .lg{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--ink2);
      cursor:pointer;user-select:none}
  .lg.off{opacity:.35}
  .lg i{display:block;border-radius:2px}
  .lg i.line{width:14px;height:2px}
  .lg i.rect{width:11px;height:11px}
  svg{display:block;width:100%;overflow:visible}
  .gl{stroke:var(--grid);stroke-width:1}
  .ax{stroke:var(--axis);stroke-width:1}
  .tick{fill:var(--muted);font-size:10.5px;font-variant-numeric:tabular-nums}
  .catlab{fill:var(--ink2);font-size:11.5px}
  .vlab{fill:var(--ink);font-size:11px;font-weight:600;font-variant-numeric:tabular-nums}
  .hit{fill:transparent;cursor:pointer}
  .crosshair{stroke:var(--axis);stroke-width:1;pointer-events:none;opacity:0}

  .tip{position:fixed;z-index:60;pointer-events:none;opacity:0;transition:opacity .08s;
       background:var(--surface);border:1px solid var(--border);border-radius:8px;
       padding:9px 11px;box-shadow:0 6px 22px rgba(0,0,0,.16);min-width:150px;
       max-width:280px}
  .tip .th{font-size:11px;color:var(--muted);margin-bottom:5px}
  .tip .tr{display:flex;align-items:center;gap:7px;font-size:12px;margin-top:3px}
  .tip .tr i{width:12px;height:2px;border-radius:2px;flex:none}
  .tip .tr b{font-variant-numeric:tabular-nums;font-weight:600;color:var(--ink)}
  .tip .tr span{color:var(--ink2);overflow:hidden;text-overflow:ellipsis;
                white-space:nowrap}

  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--grid);
        font-variant-numeric:tabular-nums;white-space:nowrap}
  th:first-child,td:first-child{text-align:left;font-variant-numeric:normal}
  th:nth-child(2),td:nth-child(2){text-align:left;font-variant-numeric:normal}
  th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;
     letter-spacing:.04em;cursor:pointer;position:sticky;top:0;background:var(--surface)}
  th[aria-sort]{color:var(--ink)}
  tbody tr:hover{background:var(--page)}
  .tscroll{max-height:420px;overflow:auto;margin-top:8px}
  .tscroll td:first-child,.tscroll td:nth-child(2){max-width:210px;
    overflow:hidden;text-overflow:ellipsis}
  .tscroll.narrow td:first-child{max-width:150px}
  .tscroll th,.tscroll td{padding:6px 7px}
  /* expandable deployment rows */
  #tEplDeps tbody tr.eplparent{cursor:pointer}
  #tEplDeps tbody tr[aria-expanded="true"] td{border-bottom-color:transparent}
  #tEplDeps tbody tr.eplsub td{background:var(--page);color:var(--ink2)}
  #tEplDeps tbody tr.eplsub:hover td{background:var(--page)}
  /* The accent rail is what makes a run of sub-rows read as belonging to the row
     above it; the indent alone is too weak once the table is scrolled. */
  #tEplDeps tbody tr.eplsub td:first-child{padding-left:30px;
    box-shadow:inset 14px 0 0 -12px var(--s1)}
  .caret{display:inline-block;width:15px;color:var(--muted);font-size:12px;
         vertical-align:0}
  tr[aria-expanded="true"] .caret{color:var(--s1)}
  .focusdot{display:inline-block;width:6px;height:6px;border-radius:50%;
            background:var(--s1);margin-right:6px;vertical-align:1px}
  .statusdot{display:inline-block;width:8px;height:8px;border-radius:50%;
             margin-right:5px;vertical-align:0}
  .hidden{display:none}
  .tableview{margin-top:6px}

  /* view tabs */
  .tabs{display:flex;gap:4px;border-bottom:1px solid var(--border);margin-bottom:18px}
  .tabs button{background:none;border:none;border-bottom:2px solid transparent;
    border-radius:0;padding:8px 14px;color:var(--ink2);font-size:13.5px;
    font-weight:500;cursor:pointer}
  .tabs button:hover{color:var(--ink)}
  .tabs button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--s1);
    font-weight:600}
  .badge{display:inline-block;margin-left:6px;padding:1px 6px;border-radius:9px;
    background:var(--page);border:1px solid var(--border);font-size:11px;
    font-variant-numeric:tabular-nums;color:var(--ink2);font-weight:600}

  /* users tab: status is a dot AND a word, never colour alone */
  .st{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600}
  .st:before{content:"";width:8px;height:8px;border-radius:50%;background:var(--deemph)}
  .st.active:before{background:var(--good)}
  .st.slipping:before{background:var(--warning)}
  .st.dormant:before{background:var(--critical)}
  tr.anonrow td{background:var(--page);font-style:italic}

  /* feedback-tab-scoped filter bar */
  .fbbar{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:10px;
    padding:9px 12px;border:1px solid var(--border);border-radius:8px;
    background:var(--card)}
  .fbbar label{font-size:11.5px;color:var(--muted);font-weight:600}
  .fbbar select{max-width:320px}
  .fbbarnote{font-size:11.5px;color:var(--muted)}

  /* complaint list */
  .fblist{margin-top:10px;display:flex;flex-direction:column;gap:9px}
  .fbrow{border:1px solid var(--border);border-radius:8px;padding:10px 12px;
    background:var(--page)}
  .fbmeta{display:flex;flex-wrap:wrap;gap:8px;align-items:center;
    font-size:11.5px;color:var(--ink2);margin-bottom:7px}
  .fbmeta .who{color:var(--ink);font-weight:600}
  .pill{padding:1px 7px;border-radius:9px;border:1px solid var(--border);
    background:var(--surface);font-size:10.5px;letter-spacing:.02em}
  .pill.sev-misleading{border-color:var(--critical);color:var(--critical)}
  .pill.sev-unhelpful{border-color:var(--warning)}
  .pill.nolabel{color:var(--muted)}
  .pill.tenant{border-color:var(--s1);color:var(--s1);font-weight:600}
  /* The complaint is the reason this tab exists, so it gets the visual weight. */
  .fbc{font-size:13px;line-height:1.55;white-space:pre-wrap;
    border-left:2px solid var(--s1);padding-left:10px;margin:0 0 8px}
  .fbqr{font-size:11.5px;color:var(--ink2);line-height:1.5}
  .fbqr div{margin-top:4px}
  .fbqr b{color:var(--ink);font-weight:600}
  .fbqr .txt{white-space:pre-wrap}
  .fbwhy{margin-top:7px;font-size:11.5px;color:var(--muted);font-style:italic}
  .fbcid{font-family:ui-monospace,Menlo,monospace;font-size:10.5px;color:var(--muted);
    cursor:pointer;border:none;background:none;padding:0}
  .fbcid:hover{color:var(--ink)}
  .fbmore{margin-top:12px;text-align:center}
  .fbempty{color:var(--ink2);font-size:13px;padding:18px 2px}
  footer{margin-top:26px;color:var(--muted);font-size:11.5px;line-height:1.7}
  code{background:var(--page);border:1px solid var(--border);border-radius:4px;
       padding:1px 5px;font-size:11.5px}
</style>
</head>
<body>
<div class="wrap">
  <h1>Foundry adoption &amp; usage</h1>
  <div class="sub">Agent runs by Tyler portfolio and product ·
    <b id="covdays"></b> · generated <span id="gen"></span></div>

  <nav class="tabs" role="tablist" aria-label="Views">
    <button role="tab" id="tabAdoption" data-view="adoption"
            aria-selected="true" aria-controls="viewAdoption">Adoption &amp; cost</button>
    <button role="tab" id="tabFeedback" data-view="feedback"
            aria-selected="false" aria-controls="viewFeedback">Negative feedback
      <span class="badge" id="tabFbCount"></span></button>
    <button role="tab" id="tabEpl" data-view="epl"
            aria-selected="false" aria-controls="viewEpl">EPL solutions
      <span class="badge" id="tabEplCount"></span></button>
    <button role="tab" id="tabUsers" data-view="users"
            aria-selected="false" aria-controls="viewUsers">Users
      <span class="badge" id="tabUsersCount"></span></button>
  </nav>

  <div class="filters" id="filters">
    <div class="fgroup">
      <label for="fRange">Date range</label>
      <select id="fRange">
        <option value="30">Last 30 days</option>
        <option value="90">Last 90 days</option>
        <option value="mtd">Month to date</option>
        <option value="all" selected>All time</option>
      </select>
    </div>
    <div class="fgroup">
      <label for="fPortfolio">Portfolio</label>
      <select id="fPortfolio"><option value="">All portfolios</option></select>
    </div>
    <div class="fgroup">
      <label for="fProduct">Product</label>
      <select id="fProduct"><option value="">All products</option></select>
    </div>
    <div class="fgroup">
      <label for="fFocus">Scope</label>
      <button id="fFocus" aria-pressed="false" title="Limit to the EPL-strategy focus products">Focus only</button>
    </div>
    <div class="fgroup">
      <label for="fSandbox">Sandbox</label>
      <button id="fSandbox" aria-pressed="true" title="Include Internal Sandbox and Platform Engineering">Incl. non-product</button>
    </div>
    <div class="spacer"></div>
    <div class="fgroup">
      <label for="fTheme">Theme</label>
      <button id="fTheme">Dark</button>
    </div>
  </div>
  <div class="chip" id="slicechip"></div>

  <div class="view" id="viewAdoption" role="tabpanel" aria-labelledby="tabAdoption">
  <div class="kpis">
    <div class="card tile hero"><div class="lab">Agent runs</div>
      <div class="val" id="kRuns">—</div><div class="note" id="kRunsNote"></div></div>
    <div class="card tile"><div class="lab">Distinct users</div>
      <div class="val" id="kUsers">—</div><div class="note" id="kUsersNote"></div></div>
    <div class="card tile"><div class="lab">Token cost</div>
      <div class="val" id="kCost">—</div><div class="note" id="kCostNote"></div></div>
    <div class="card tile"><div class="lab">Failure rate</div>
      <div class="val" id="kFail">—</div><div class="note" id="kFailNote"></div></div>
    <div class="card tile"><div class="lab">Active agents</div>
      <div class="val" id="kAgents">—</div><div class="note" id="kAgentsNote"></div></div>
  </div>

  <div class="grid2">
    <div class="card">
      <div class="chead"><div>
        <div class="ctitle">Runs per month by portfolio</div>
        <div class="cnote" id="c1note">Six largest portfolios by all-time volume; the rest folded into Other</div>
      </div><button class="tbtn" data-table="tv1" aria-pressed="false">Table</button></div>
      <div class="legend" id="lg1"></div>
      <svg id="c1" height="250" role="img" aria-label="Runs per month by portfolio"></svg>
      <div class="tableview hidden" id="tv1"></div>
    </div>
    <div class="card">
      <div class="chead"><div>
        <div class="ctitle">Distinct users per month</div>
        <div class="cnote" id="c2note">Exact distinct counts, not a sum of per-product users</div>
      </div><button class="tbtn" data-table="tv2" aria-pressed="false">Table</button></div>
      <svg id="c2" height="250" role="img" aria-label="Distinct users per month"></svg>
      <div class="tableview hidden" id="tv2"></div>
    </div>
  </div>

  <div class="grid2">
    <div class="card">
      <div class="chead"><div>
        <div class="ctitle">Share of runs by product</div>
        <div class="cnote">Focus-scope products in blue, everything else grey</div>
      </div><button class="tbtn" data-table="tv3" aria-pressed="false">Table</button></div>
      <svg id="c3" height="330" role="img" aria-label="Share of runs by product"></svg>
      <div class="tableview hidden" id="tv3"></div>
    </div>
    <div class="card">
      <div class="chead"><div>
        <div class="ctitle">Cost per run by product</div>
        <div class="cnote">Products with 100+ runs in the slice</div>
      </div><button class="tbtn" data-table="tv4" aria-pressed="false">Table</button></div>
      <svg id="c4" height="330" role="img" aria-label="Cost per run by product"></svg>
      <div class="tableview hidden" id="tv4"></div>
    </div>
  </div>

  <div class="grid2">
    <div class="card">
      <div class="chead"><div>
        <div class="ctitle">Failure rate by product</div>
        <div class="cnote">Products with 100+ runs; ● marks rates above 10%</div>
      </div><button class="tbtn" data-table="tv5" aria-pressed="false">Table</button></div>
      <svg id="c5" height="330" role="img" aria-label="Failure rate by product"></svg>
      <div class="tableview hidden" id="tv5"></div>
    </div>
    <div class="card">
      <div class="chead"><div>
        <div class="ctitle">Top error classes</div>
        <div class="cnote">All failures in the current product scope, all time</div>
      </div></div>
      <div class="tscroll"><table id="terr"><thead><tr>
        <th>Error class</th><th>Product</th><th>Failures</th>
      </tr></thead><tbody></tbody></table></div>
    </div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="chead"><div>
      <div class="ctitle">Agent leaderboard</div>
      <div class="cnote">All-time totals per agent; click a column to sort.
        Rows respect the portfolio, product and scope filters (not the date range).</div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <select id="fAgentTenant" style="max-width:220px"><option value="">All tenants</option></select>
      <input type="search" id="fAgent" placeholder="Filter agents…" style="width:200px">
    </div>
    </div>
    <div class="tscroll"><table id="tagents"><thead><tr>
      <th data-k="name">Agent</th><th data-k="tenant">Tenant</th><th data-k="product">Product</th>
      <th data-k="runs" aria-sort="descending">Runs</th><th data-k="users">Users</th>
      <th data-k="cost">Cost</th><th data-k="cpr">$/run</th>
      <th data-k="secs">Avg s</th><th data-k="fail">Fail %</th><th data-k="last">Last run</th>
    </tr></thead><tbody></tbody></table></div>
  </div>

  <div class="grid2">
    <div class="card">
      <div class="chead"><div>
        <div class="ctitle">Visual workflow executions</div>
        <div class="cnote" id="wfnote"></div>
      </div></div>
      <div class="tscroll"><table id="twf"><thead><tr>
        <th>Workflow</th><th>Product</th><th>Status</th><th>Run</th><th>Suspended at</th>
      </tr></thead><tbody></tbody></table></div>
    </div>
    <div class="card">
      <div class="chead"><div>
        <div class="ctitle">Workflows: built vs. ever run</div>
        <div class="cnote">Definitions per product against executions — all time</div>
      </div></div>
      <div class="tscroll narrow"><table id="twfa"><thead><tr>
        <th>Product</th><th>Defined</th><th>Ran</th><th>Execs</th><th>Ran %</th>
      </tr></thead><tbody></tbody></table></div>
    </div>
  </div>

  <div class="grid2">
    <div class="card">
      <div class="chead"><div>
        <div class="ctitle">Dormant agents</div>
        <div class="cnote">Built but never produced a run — all time, ignores the date filter</div>
      </div></div>
      <div class="tscroll"><table id="tdorm"><thead><tr>
        <th>Product</th><th>Dormant</th><th>Built</th><th>Dormant %</th>
      </tr></thead><tbody></tbody></table></div>
    </div>
    <div class="card">
      <div class="chead"><div>
        <div class="ctitle">Unattributed traffic</div>
        <div class="cnote">Ranked by runs — fix the taxonomy top-down</div>
      </div></div>
      <div class="tscroll"><table id="tgaps"><thead><tr>
        <th>Project</th><th>Agent</th><th>Runs</th><th>Cost</th>
      </tr></thead><tbody></tbody></table></div>
    </div>
  </div>
  </div><!-- /viewAdoption -->

  <div class="view hidden" id="viewFeedback" role="tabpanel"
       aria-labelledby="tabFeedback">
    <div class="fbbar">
      <label for="fTenant">Tenant</label>
      <select id="fTenant"><option value="">All tenants</option></select>
      <span class="fbbarnote" id="fTenantNote"></span>
    </div>
    <div class="kpis">
      <div class="card tile hero"><div class="lab">Thumbs-down turns</div>
        <div class="val" id="kNeg">—</div><div class="note" id="kNegNote"></div></div>
      <div class="card tile"><div class="lab">With a written comment</div>
        <div class="val" id="kNegC">—</div><div class="note" id="kNegCNote"></div></div>
      <div class="card tile"><div class="lab">Negative share of rated</div>
        <div class="val" id="kNegShare">—</div>
        <div class="note" id="kNegShareNote"></div></div>
      <div class="card tile"><div class="lab">Feedback submission rate</div>
        <div class="val" id="kFbRate">—</div><div class="note" id="kFbRateNote"></div></div>
      <div class="card tile"><div class="lab">Classified</div>
        <div class="val" id="kLabelled">—</div>
        <div class="note" id="kLabelledNote"></div></div>
    </div>

    <div class="grid2">
      <div class="card">
        <div class="chead"><div>
          <div class="ctitle">Thumbs-down per month</div>
          <div class="cnote" id="cf1note"></div>
        </div><button class="tbtn" data-table="tvf1" aria-pressed="false">Table</button></div>
        <svg id="cf1" height="250" role="img"
             aria-label="Thumbs-down turns per month"></svg>
        <div class="tableview hidden" id="tvf1"></div>
      </div>
      <div class="card">
        <div class="chead"><div>
          <div class="ctitle">Failure categories</div>
          <div class="cnote" id="cf2note">What went wrong, per the classifier</div>
        </div><button class="tbtn" data-table="tvf2" aria-pressed="false">Table</button></div>
        <svg id="cf2" height="330" role="img" aria-label="Failure categories"></svg>
        <div class="tableview hidden" id="tvf2"></div>
      </div>
    </div>

    <div class="grid2">
      <div class="card">
        <div class="chead"><div>
          <div class="ctitle">Negative rate by agent</div>
          <div class="cnote">Thumbs-down per 100 conversations held. Agents with
            250+ conversations, so a handful of downvotes on a pilot can't top the
            chart. ● marks 1+ per 100.</div>
        </div><button class="tbtn" data-table="tvf3" aria-pressed="false">Table</button></div>
        <svg id="cf3" height="330" role="img" aria-label="Negative rate by agent"></svg>
        <div class="tableview hidden" id="tvf3"></div>
      </div>
      <div class="card">
        <div class="chead"><div>
          <div class="ctitle">Where the fix belongs</div>
          <div class="cnote">Classifier's judgement of what to change, by severity</div>
        </div></div>
        <div class="tscroll narrow"><table id="tfix"><thead><tr>
          <th>Fix</th><th>Misleading</th><th>Unhelpful</th><th>Minor</th><th>Total</th>
        </tr></thead><tbody></tbody></table></div>
      </div>
    </div>

    <div class="card" style="margin-top:12px">
      <div class="chead"><div>
        <div class="ctitle">Agent feedback scorecard</div>
        <div class="cnote">All-time. Conversations is the denominator from the run
          logs, so rates are comparable across agents of very different size — but
          unlike the chart above this table has no minimum, so sorting by Neg/100
          will surface low-traffic agents where a couple of downvotes dominate.
          Check the Convs column before drawing a conclusion. Respects portfolio,
          product, tenant and scope filters — not the date range.
          Agent names repeat across deployments — <em>Staff Assistant</em> is seven
          different agents in seven tenants — so read the Agent and Tenant columns
          together; a row is one agent in one tenant.</div>
      </div></div>
      <div class="tscroll"><table id="tfba"><thead><tr>
        <th data-fk="name">Agent</th><th data-fk="tenant">Tenant</th>
        <th data-fk="product">Product</th>
        <th data-fk="convs">Convs</th>
        <th data-fk="neg" aria-sort="descending">Neg</th>
        <th data-fk="negc">w/ comment</th><th data-fk="pos">Pos</th>
        <th data-fk="share">Neg share</th><th data-fk="per100">Neg/100</th>
        <th data-fk="rate">Fb rate</th><th data-fk="last">Last</th>
      </tr></thead><tbody></tbody></table></div>
    </div>

    <div class="card" style="margin-top:12px">
      <div class="chead"><div>
        <div class="ctitle">Every thumbs-down, with the comment</div>
        <div class="cnote" id="fbnote"></div>
      </div>
      <input type="search" id="fFbQuery"
             placeholder="Search comments, questions, agents…" style="width:240px">
      <select id="fFbLabel"><option value="">All categories</option></select>
      <select id="fFbSev">
        <option value="">All severities</option>
        <option value="misleading">Misleading</option>
        <option value="unhelpful">Unhelpful</option>
        <option value="minor">Minor</option>
      </select>
      <button id="fFbCommented" aria-pressed="true"
              title="Only turns where the user typed something">With comment</button>
      </div>
      <div id="fblist" class="fblist"></div>
      <div class="fbmore"><button id="fbMore" hidden>Show more</button></div>
    </div>
  </div><!-- /viewFeedback -->

  <div class="view hidden" id="viewEpl" role="tabpanel" aria-labelledby="tabEpl">
    <div class="fbbar">
      <label for="fEplScope">Show</label>
      <select id="fEplScope"><option value="">All shipped deployments</option></select>
      <span class="fbbarnote">Self-contained: this tab ignores the date, portfolio
        and product filters above so the numbers can't shift mid-presentation.</span>
    </div>

    <div class="kpis">
      <div class="card tile hero"><div class="lab">Conversations</div>
        <div class="val" id="kEplConvs">—</div>
        <div class="note" id="kEplConvsNote"></div></div>
      <div class="card tile"><div class="lab">People using it</div>
        <div class="val" id="kEplUsers">—</div>
        <div class="note" id="kEplUsersNote"></div></div>
      <div class="card tile"><div class="lab">Agents live</div>
        <div class="val" id="kEplAgents">—</div>
        <div class="note" id="kEplAgentsNote"></div></div>
      <div class="card tile"><div class="lab">Produced a complaint</div>
        <div class="val" id="kEplComplaint">—</div>
        <div class="note" id="kEplComplaintNote"></div></div>
    </div>

    <div class="card" style="margin-top:12px">
      <div class="chead"><div>
        <div class="ctitle">Adoption, week by week</div>
        <div class="cnote" id="cEplNote"></div>
      </div><button class="tbtn" data-table="tvEpl1" aria-pressed="false">Table</button></div>
      <svg id="cEpl1" height="290" role="img"
           aria-label="Conversations and distinct users per week"></svg>
      <div class="tableview hidden" id="tvEpl1"></div>
    </div>

    <div class="card" style="margin-top:12px">
      <div class="chead"><div>
        <div class="ctitle">Shipped: what's live, and where</div>
        <div class="cnote">One row per deployment — click a row to see the individual
          agents behind it. <b>Rated convs</b> counts conversations that received any
          feedback; <b>👍/👎 turns</b> count individual rated replies, so they are a
          larger number and are not expected to add up to it — one conversation here
          collected four 👎. <b>Complaint rate</b> is the share of conversations
          containing at least one 👎, so it never counts the same unhappy conversation
          twice; “—” means nothing was rated, which is not the same as no complaints.
          Every column except <b>Users</b> adds up to its deployment — the same person
          uses more than one agent.</div>
      </div><button class="tbtn" id="eplExpandAll" aria-pressed="false">Expand
        all</button></div>
      <div class="tscroll"><table id="tEplDeps"><thead><tr>
        <th data-ek="name">Deployment</th><th data-ek="type">Type</th>
        <th data-ek="agents">Agents</th><th data-ek="first">Live since</th>
        <th data-ek="last">Last activity</th><th data-ek="users">Users</th>
        <th data-ek="convs" aria-sort="descending">Convs</th>
        <th data-ek="rated">Rated convs</th>
        <th data-ek="pos">👍 turns</th><th data-ek="neg">👎 turns</th>
        <th data-ek="rate">Complaint rate</th>
      </tr></thead><tbody></tbody></table></div>
      <div class="cnote" id="eplExcluded" style="margin-top:9px"></div>
    </div>

    <div class="card" style="margin-top:12px">
      <div class="chead"><div>
        <div class="ctitle">What the complaints were</div>
        <div class="cnote" id="eplCxNote"></div>
      </div></div>
      <div class="tscroll narrow"><table id="tEplCauses"><thead><tr>
        <th>Root cause</th><th>Complaints</th><th>Where the fix belongs</th>
      </tr></thead><tbody></tbody></table></div>
      <div id="eplComplaints" class="fblist"></div>
    </div>
  </div><!-- /viewEpl -->

  <div class="view hidden" id="viewUsers" role="tabpanel" aria-labelledby="tabUsers">
    <div class="fbbar">
      <label for="fUsRange">Date range</label>
      <select id="fUsRange">
        <option value="all">All time</option>
        <option value="7">Last 7 days</option>
        <option value="30">Last 30 days</option>
        <option value="90">Last 90 days</option>
        <option value="custom">Custom</option>
      </select>
      <input type="date" id="fUsFrom" aria-label="From date">
      <span>to</span>
      <input type="date" id="fUsTo" aria-label="To date">
      <label for="fUsStatus">Show</label>
      <select id="fUsStatus">
        <option value="">Everyone</option>
        <option value="dormant">Dormant (no run in 30+ days)</option>
        <option value="slipping">Slipping (last run 8–30 days ago)</option>
        <option value="active">Active (run in the last 7 days)</option>
      </select>
      <input type="search" id="fUsQuery" placeholder="Find a person" aria-label="Find a person">
      <button type="button" id="fUsExport">Export CSV</button>
      <span class="fbbarnote">Self-contained: this tab ignores the filters above.
        Scoped to the <b id="usTenant"></b> tenant; data runs to <b id="usAsOf"></b>.
        Activity counts fall inside the range; <b>status is measured as of the range
        end</b> from each person's full history up to that day.</span>
    </div>

    <div class="kpis">
      <div class="card tile hero"><div class="lab">Named people who had run an agent by range end</div>
        <div class="val" id="kUsPeople">—</div>
        <div class="note" id="kUsPeopleNote"></div></div>
      <div class="card tile"><div class="lab">Ran in range</div>
        <div class="val" id="kUsRan">—</div><div class="note" id="kUsRanNote"></div></div>
      <div class="card tile"><div class="lab">Active at range end</div>
        <div class="val" id="kUsActive">—</div><div class="note" id="kUsActiveNote"></div></div>
      <div class="card tile"><div class="lab">Slipping</div>
        <div class="val" id="kUsSlip">—</div><div class="note">last run 8–30 days before</div></div>
      <div class="card tile"><div class="lab">Dormant</div>
        <div class="val" id="kUsDormant">—</div><div class="note">no run in the 30+ days before</div></div>
    </div>

    <div class="card" style="margin-top:12px">
      <div class="chead"><div>
        <div class="ctitle">Anonymous shared login</div>
        <div class="cnote" id="usAnonNote"></div>
      </div></div>
      <svg id="cUsAnon" height="200" role="img"
           aria-label="Anonymous runs and conversations per week"></svg>
    </div>

    <div class="card" style="margin-top:12px">
      <div class="chead"><div>
        <div class="ctitle">People</div>
        <div class="cnote">Only people who have run at least one agent appear here —
          Foundry has no list of tenant members, so someone who never used it cannot be
          shown. <b>Anonymous</b> is one shared login, not a person; it is pinned first
          and left out of the counts above. People with no runs in the range are kept
          and sort first, followed by the longest-quiet.</div>
      </div></div>
      <div class="tscroll"><table id="tUsers"><thead><tr>
        <th data-uk="name">Person</th><th data-uk="status">Status</th>
        <th data-uk="since" aria-sort="descending">Days since last run</th>
        <th data-uk="last">Last run</th><th data-uk="first">First run</th>
        <th data-uk="days">Active days in range</th><th data-uk="runs">Runs in range</th>
        <th data-uk="agents">Agents in range</th><th>Weekly runs</th>
      </tr></thead><tbody></tbody></table></div>
      <div class="cnote" id="usCount" style="margin-top:9px"></div>
    </div>
  </div><!-- /viewUsers -->

  <footer>
    Source: local warehouse built by <code>extract.py</code> from
    <code>/api/agent-logs</code> and <code>/api/transcripts</code>. Refresh with
    <code>python3 extract.py facts --incremental &amp;&amp; python3 extract.py feedback
    &amp;&amp; python3 label_feedback.py &amp;&amp; python3 build_dashboard.py</code>.<br>
    <span id="fnote"></span>
  </footer>
</div>

<div class="tip" id="tip" role="status" aria-live="polite"></div>

<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("payload").textContent);
const $ = s => document.querySelector(s);
const NS = "http://www.w3.org/2000/svg";

/* ---------- formatting ---------- */
const nf = new Intl.NumberFormat("en-US");
const fmtInt = n => nf.format(Math.round(n));
const fmtCompact = n => n >= 1e9 ? (n/1e9).toFixed(n>=1e10?0:1)+"B"
                    : n >= 1e6 ? (n/1e6).toFixed(n>=1e7?0:1)+"M"
                    : n >= 1e4 ? (n/1e3).toFixed(0)+"K"
                    : nf.format(Math.round(n));
const fmtMoney = n => n >= 1000 ? "$"+fmtCompact(n) : "$"+n.toFixed(2);
const fmtPct = n => n.toFixed(n < 10 ? 2 : 1) + "%";

/* ---------- filter state ---------- */
const state = {view:"adoption",
               range:"all", portfolio:"", product:"", focus:false, sandbox:true,
               hiddenSeries:new Set(), agentQuery:"", agentTenant:"",
               sortKey:"runs", sortDir:-1};

const NONPRODUCT = new Set(["Internal Sandbox","Platform Engineering / Internal Tooling",
                            "Unattributed"]);

/* Which product indices survive the non-date filters. */
function activeProducts(){
  const out = new Set();
  D.products.forEach((p,i) => {
    if (state.portfolio && D.portfolios[D.prodPortfolio[i]] !== state.portfolio) return;
    if (state.product && p !== state.product) return;
    if (state.focus && !D.prodFocus[i]) return;
    if (!state.sandbox && NONPRODUCT.has(p)) return;
    out.add(i);
  });
  return out;
}

/* Day-index window for the selected range. */
function dayWindow(){
  const n = D.days.length;
  if (state.range === "all") return [0, n-1];
  if (state.range === "mtd") {
    const m = D.days[n-1].slice(0,7);
    let i = n-1; while (i > 0 && D.days[i-1].startsWith(m)) i--;
    return [i, n-1];
  }
  return [Math.max(0, n - parseInt(state.range,10)), n-1];
}

/* ---------- aggregation ---------- */
function aggregate(){
  const prods = activeProducts(), [lo,hi] = dayWindow();
  const A = {runs:0, convos:0, cost:0, tokens:0, fails:0, durMs:0, durN:0};
  const byMonthPortfolio = new Map();   // month -> portfolio -> runs
  const byProduct = new Map();          // prodIdx -> {runs,cost,fails}
  const months = new Set();

  for (const [d,p,runs,convos,cost,tokens,fails,durMs,durN] of D.facts){
    if (d < lo || d > hi || !prods.has(p)) continue;
    A.runs+=runs; A.convos+=convos; A.cost+=cost; A.tokens+=tokens;
    A.fails+=fails; A.durMs+=durMs; A.durN+=durN;

    const month = D.days[d].slice(0,7);
    months.add(month);
    const pf = D.portfolios[D.prodPortfolio[p]];
    if (!byMonthPortfolio.has(month)) byMonthPortfolio.set(month, new Map());
    const mm = byMonthPortfolio.get(month);
    mm.set(pf, (mm.get(pf)||0) + runs);

    let rec = byProduct.get(p);
    if (!rec) byProduct.set(p, rec = {runs:0, cost:0, fails:0});
    rec.runs+=runs; rec.cost+=cost; rec.fails+=fails;
  }

  // Distinct counts must come from presence tuples, never from summing.
  const users = new Set(), agents = new Set();
  const usersByMonth = new Map();
  for (const [u,d,p] of D.userTuples){
    if (d < lo || d > hi || !prods.has(p)) continue;
    users.add(u);
    const m = D.days[d].slice(0,7);
    if (!usersByMonth.has(m)) usersByMonth.set(m, new Set());
    usersByMonth.get(m).add(u);
  }
  for (const [a,d,p] of D.agentTuples){
    if (d < lo || d > hi || !prods.has(p)) continue;
    agents.add(a);
  }

  return {A, months:[...months].sort(), byMonthPortfolio, byProduct,
          users:users.size, agents:agents.size, usersByMonth, prods, lo, hi};
}

/* ---------- tooltip ---------- */
const tip = $("#tip");
function showTip(x, y, title, rows){
  tip.replaceChildren();
  const h = document.createElement("div");
  h.className = "th"; h.textContent = title; tip.appendChild(h);
  for (const r of rows){
    const tr = document.createElement("div"); tr.className = "tr";
    if (r.color){ const i = document.createElement("i");
      i.style.background = r.color; tr.appendChild(i); }
    const b = document.createElement("b"); b.textContent = r.value;
    const s = document.createElement("span"); s.textContent = r.label;
    tr.append(b, s); tip.appendChild(tr);
  }
  tip.style.opacity = 1;
  const r = tip.getBoundingClientRect();
  let left = x + 14, top = y + 14;
  if (left + r.width > innerWidth - 8) left = x - r.width - 14;
  if (top + r.height > innerHeight - 8) top = y - r.height - 14;
  tip.style.left = Math.max(8,left)+"px"; tip.style.top = Math.max(8,top)+"px";
}
const hideTip = () => { tip.style.opacity = 0; };

/* ---------- svg helpers ---------- */
function el(tag, attrs){
  const n = document.createElementNS(NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}
function text(x, y, str, cls, anchor){
  const t = el("text", {x, y, class:cls});
  if (anchor) t.setAttribute("text-anchor", anchor);
  t.textContent = str;                       // untrusted labels: never innerHTML
  return t;
}
/* Axis scale. Steps are restricted to 1/2/5 x 10^k so every tick label is
   display-clean -- a 12,500 step would render as "13K" via fmtCompact and read
   as a wrong number. Tick count is searched over 4..6 and the tightest fit wins,
   so clean labels don't cost a chart full of dead headroom. */
function niceScale(v){
  if (!(v > 0)) return {max:1, step:0.25, n:4};
  let best = null;
  for (let n = 4; n <= 6; n++){
    const raw = v / n;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    for (const k of [1, 2, 5, 10]){
      const step = k * mag;
      if (step * n < v) continue;
      const cand = {max: step * n, step, n};
      if (!best || cand.max < best.max) best = cand;
    }
  }
  return best || {max:v, step:v/4, n:4};
}
/* One unit for the whole axis: an axis reading 12K / 10K / 8,000 mixes units. */
function axisFmt(max){
  const unit = max >= 1e9 ? ["B",1e9] : max >= 1e6 ? ["M",1e6]
             : max >= 1e4 ? ["K",1e3] : null;
  return v => !v ? "0"
    : unit ? (v/unit[1]).toFixed(v % unit[1] ? 1 : 0) + unit[0]
           : nf.format(Math.round(v));
}

function seriesColors(){
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  return dark ? D.seriesDark : D.seriesLight;
}
const cssVar = n => getComputedStyle(document.documentElement)
                      .getPropertyValue(n).trim();

/* ---------- chart 1: multi-line, runs per month by portfolio ---------- */
function drawLines(agg){
  const svg = $("#c1"); svg.replaceChildren();
  const W = svg.clientWidth || 560, H = 250;
  const M = {t:14, r:54, b:26, l:46};
  const months = agg.months;

  const totals = new Map();
  for (const mm of agg.byMonthPortfolio.values())
    for (const [pf,v] of mm) totals.set(pf, (totals.get(pf)||0) + v);

  // A portfolio's slot is fixed for the life of the dashboard, so a filter never
  // repaints a surviving series. Slot -1 is the permanent "Other" bucket.
  const slotOf = pf => D.portfolioSlot[D.portfolios.indexOf(pf)] ?? -1;
  const named = [...totals.keys()].filter(pf => slotOf(pf) >= 0)
                                  .sort((a,b) => slotOf(a) - slotOf(b));
  const hasOther = [...totals.keys()].some(pf => slotOf(pf) < 0);
  const keys = hasOther ? [...named, "Other"] : named;

  const colors = seriesColors();
  const colorOf = k => k === "Other" ? cssVar("--deemph") : colors[slotOf(k)];

  const val = (m,k) => {
    const mm = agg.byMonthPortfolio.get(m); if (!mm) return 0;
    if (k !== "Other") return mm.get(k) || 0;
    let s = 0; for (const [pf,v] of mm) if (slotOf(pf) < 0) s += v;
    return s;
  };

  const visible = keys.filter(k => !state.hiddenSeries.has(k));
  let peak = 0;
  for (const m of months) for (const k of visible) peak = Math.max(peak, val(m,k));
  const sc = niceScale(peak), max = sc.max, af = axisFmt(max);

  const X = i => months.length === 1 ? (M.l + (W-M.l-M.r)/2)
                 : M.l + i * (W-M.l-M.r) / (months.length-1);
  const Y = v => H - M.b - (v/max) * (H-M.t-M.b);

  for (let g=0; g<=sc.n; g++){
    const y = M.t + g*(H-M.t-M.b)/sc.n;
    svg.appendChild(el("line", {x1:M.l, x2:W-M.r, y1:y, y2:y, class:"gl"}));
    svg.appendChild(text(M.l-8, y+3.5, af(sc.step*(sc.n-g)), "tick", "end"));
  }
  svg.appendChild(el("line", {x1:M.l, x2:W-M.r, y1:H-M.b, y2:H-M.b, class:"ax"}));
  months.forEach((m,i) => {
    if (months.length > 10 && i % 2) return;
    svg.appendChild(text(X(i), H-M.b+15, m.slice(2), "tick", "middle"));
  });

  const ends = [];
  for (const k of visible){
    const pts = months.map((m,i) => [X(i), Y(val(m,k))]);
    if (pts.length > 1){
      svg.appendChild(el("path", {
        d: pts.map((p,i) => (i?"L":"M")+p[0]+" "+p[1]).join(" "),
        fill:"none", stroke:colorOf(k), "stroke-width":2,
        "stroke-linejoin":"round", "stroke-linecap":"round"}));
    }
    const last = pts[pts.length-1];
    svg.appendChild(el("circle", {cx:last[0], cy:last[1], r:4.5,
      fill:colorOf(k), stroke:cssVar("--surface"), "stroke-width":2}));
    ends.push({k, x:last[0], y:last[1], v:val(months[months.length-1], k)});
  }
  // Colliding end-labels read as noise; drop the ones that don't fit rather than
  // nudging them off their own line. The legend and tooltip still carry them.
  if (visible.length <= 4){
    ends.sort((a,b) => a.y - b.y);
    let lastY = -Infinity;
    for (const e of ends){
      if (e.y - lastY < 13) continue;
      lastY = e.y;
      svg.appendChild(text(e.x+9, e.y+4, fmtCompact(e.v), "vlab"));
    }
  }

  const cross = el("line", {y1:M.t, y2:H-M.b, class:"crosshair"});
  svg.appendChild(cross);
  const hit = el("rect", {x:M.l, y:M.t, width:Math.max(1,W-M.l-M.r),
                          height:H-M.t-M.b, class:"hit"});
  svg.appendChild(hit);
  const move = ev => {
    const box = svg.getBoundingClientRect();
    const rel = ev.clientX - box.left;
    let bi = 0, bd = Infinity;
    months.forEach((m,i) => { const d = Math.abs(X(i)-rel); if (d<bd){bd=d;bi=i;} });
    cross.setAttribute("x1", X(bi)); cross.setAttribute("x2", X(bi));
    cross.style.opacity = 1;
    const rows = visible.map(k => ({color:colorOf(k), label:k,
                                    value:fmtInt(val(months[bi],k))}))
                        .sort((a,b) => parseInt(b.value.replace(/,/g,""))
                                     - parseInt(a.value.replace(/,/g,"")));
    showTip(ev.clientX, ev.clientY, months[bi]+" · runs", rows);
  };
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerleave", () => { cross.style.opacity=0; hideTip(); });

  const lg = $("#lg1"); lg.replaceChildren();
  for (const k of keys){
    const b = document.createElement("button");
    b.className = "lg" + (state.hiddenSeries.has(k) ? " off" : "");
    b.setAttribute("aria-pressed", String(!state.hiddenSeries.has(k)));
    b.style.cssText = "background:none;border:none;padding:0";
    const i = document.createElement("i");
    i.className = "line"; i.style.background = colorOf(k);
    const s = document.createElement("span"); s.textContent = k;
    b.append(i, s);
    b.onclick = () => {
      state.hiddenSeries.has(k) ? state.hiddenSeries.delete(k)
                                : state.hiddenSeries.add(k);
      if (state.hiddenSeries.size >= keys.length) state.hiddenSeries.delete(k);
      render();
    };
    lg.appendChild(b);
  }

  tableFor("tv1", ["Month", ...keys],
    months.map(m => [m, ...keys.map(k => fmtInt(val(m,k)))]));
}

/* ---------- chart 2: single line, distinct users per month ---------- */
function drawUsers(agg){
  const svg = $("#c2"); svg.replaceChildren();
  const W = svg.clientWidth || 560, H = 250;
  const M = {t:14, r:44, b:26, l:42};
  const months = agg.months;
  const v = m => (agg.usersByMonth.get(m) || {size:0}).size;
  const sc = niceScale(Math.max(1, ...months.map(v))), max = sc.max;
  const af = axisFmt(max);
  const X = i => months.length === 1 ? (M.l + (W-M.l-M.r)/2)
                 : M.l + i*(W-M.l-M.r)/(months.length-1);
  const Y = x => H - M.b - (x/max)*(H-M.t-M.b);
  const col = cssVar("--s1");

  for (let g=0; g<=sc.n; g++){
    const y = M.t + g*(H-M.t-M.b)/sc.n;
    svg.appendChild(el("line", {x1:M.l, x2:W-M.r, y1:y, y2:y, class:"gl"}));
    svg.appendChild(text(M.l-8, y+3.5, af(sc.step*(sc.n-g)), "tick", "end"));
  }
  svg.appendChild(el("line", {x1:M.l, x2:W-M.r, y1:H-M.b, y2:H-M.b, class:"ax"}));
  months.forEach((m,i) => {
    if (months.length > 10 && i % 2) return;
    svg.appendChild(text(X(i), H-M.b+15, m.slice(2), "tick", "middle"));
  });

  const pts = months.map((m,i) => [X(i), Y(v(m))]);
  if (pts.length > 1){
    svg.appendChild(el("path", {
      d: pts.map((p,i) => (i?"L":"M")+p[0]+" "+p[1]).join(" ")
         + ` L ${pts[pts.length-1][0]} ${H-M.b} L ${pts[0][0]} ${H-M.b} Z`,
      fill:col, "fill-opacity":0.10, stroke:"none"}));
    svg.appendChild(el("path", {
      d: pts.map((p,i) => (i?"L":"M")+p[0]+" "+p[1]).join(" "),
      fill:"none", stroke:col, "stroke-width":2,
      "stroke-linejoin":"round", "stroke-linecap":"round"}));
  }
  const last = pts[pts.length-1];
  if (last){
    svg.appendChild(el("circle", {cx:last[0], cy:last[1], r:4.5, fill:col,
      stroke:cssVar("--surface"), "stroke-width":2}));
    svg.appendChild(text(last[0]+9, last[1]+4, fmtInt(v(months[months.length-1])), "vlab"));
  }

  const cross = el("line", {y1:M.t, y2:H-M.b, class:"crosshair"});
  svg.appendChild(cross);
  const hit = el("rect", {x:M.l, y:M.t, width:Math.max(1,W-M.l-M.r),
                          height:H-M.t-M.b, class:"hit"});
  svg.appendChild(hit);
  hit.addEventListener("pointermove", ev => {
    const box = svg.getBoundingClientRect(), rel = ev.clientX - box.left;
    let bi = 0, bd = Infinity;
    months.forEach((m,i) => { const d = Math.abs(X(i)-rel); if (d<bd){bd=d;bi=i;} });
    cross.setAttribute("x1", X(bi)); cross.setAttribute("x2", X(bi));
    cross.style.opacity = 1;
    showTip(ev.clientX, ev.clientY, months[bi],
      [{color:col, label:"distinct users", value:fmtInt(v(months[bi]))}]);
  });
  hit.addEventListener("pointerleave", () => { cross.style.opacity=0; hideTip(); });

  tableFor("tv2", ["Month","Distinct users"], months.map(m => [m, fmtInt(v(m))]));
}

/* ---------- horizontal bar charts ---------- */
function drawBars(cfg){
  const svg = $(cfg.svg); svg.replaceChildren();
  const W = svg.clientWidth || 560;
  const rows = cfg.rows.slice(0, 12);
  const rowH = 24, gap = 2;
  const M = {t:6, r:64, b:22, l:Math.min(210, Math.max(120, W*0.36))};
  const H = M.t + rows.length*rowH + M.b;
  svg.setAttribute("height", H);

  const sc = niceScale(Math.max(1e-9, ...rows.map(r => r.value)));
  const max = sc.max, plotW = Math.max(1, W - M.l - M.r);
  const X = v => (v/max)*plotW;

  for (let g=0; g<=sc.n; g++){
    const x = M.l + g*plotW/sc.n;
    svg.appendChild(el("line", {x1:x, x2:x, y1:M.t, y2:M.t+rows.length*rowH,
                                class:"gl"}));
    svg.appendChild(text(x, H-6, cfg.fmtAxis(sc.step*g), "tick", "middle"));
  }

  rows.forEach((r,i) => {
    const y = M.t + i*rowH;
    const bh = rowH - gap*2;                 // 2px surface gap above and below
    const w = Math.max(1, X(r.value));
    const bar = el("rect", {x:M.l, y:y+gap, width:w, height:bh,
      rx:4, ry:4, fill:r.color});
    svg.appendChild(bar);
    // square off the baseline end: overlay a small rect at the origin
    svg.appendChild(el("rect", {x:M.l, y:y+gap, width:Math.min(4,w), height:bh,
      fill:r.color}));

    const lab = r.label.length > 30 ? r.label.slice(0,29)+"…" : r.label;
    const t = text(M.l-8, y+rowH/2+4, lab, "catlab", "end");
    svg.appendChild(t);

    const vt = text(M.l+w+8, y+rowH/2+4, cfg.fmtValue(r.value), "vlab");
    svg.appendChild(vt);
    if (r.flag){
      // measure, don't estimate -- a guessed advance width collides with the label
      const adv = vt.getComputedTextLength ? vt.getComputedTextLength() : 34;
      svg.appendChild(el("circle", {cx:M.l+w+8+adv+8, cy:y+rowH/2, r:4,
        fill:cssVar("--critical")}));
    }

    const hit = el("rect", {x:0, y:y, width:W, height:rowH, class:"hit"});
    svg.appendChild(hit);
    const enter = ev => {
      bar.setAttribute("fill-opacity", 0.78);
      showTip(ev.clientX, ev.clientY, r.label, r.tip);
    };
    hit.addEventListener("pointermove", enter);
    hit.addEventListener("pointerleave", () => {
      bar.removeAttribute("fill-opacity"); hideTip(); });
  });

  tableFor(cfg.table, cfg.tableHead,
    cfg.rows.map(r => cfg.tableRow(r)));
}

function tableFor(id, head, rows){
  const host = $("#"+id); if (!host) return;
  host.replaceChildren();
  const wrap = document.createElement("div"); wrap.className = "tscroll";
  const t = document.createElement("table");
  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  for (const h of head){ const th = document.createElement("th");
    th.textContent = h; th.style.cursor = "default"; htr.appendChild(th); }
  thead.appendChild(htr); t.appendChild(thead);
  const tb = document.createElement("tbody");
  for (const r of rows){
    const tr = document.createElement("tr");
    for (const c of r){ const td = document.createElement("td");
      td.textContent = c; tr.appendChild(td); }
    tb.appendChild(tr);
  }
  t.appendChild(tb); wrap.appendChild(t); host.appendChild(wrap);
}

/* ---------- tables ---------- */
function renderAgents(){
  const prods = activeProducts();
  const allow = new Set([...prods].map(i => D.products[i]));
  const q = state.agentQuery.toLowerCase();
  const ten = state.agentTenant;
  let rows = D.agents.filter(a => allow.has(a.product) &&
    (!ten || a.tenant === ten) &&
    (!q || a.name.toLowerCase().includes(q) || a.product.toLowerCase().includes(q) ||
     a.tenant.toLowerCase().includes(q)));
  const k = state.sortKey, dir = state.sortDir;
  rows.sort((a,b) => {
    const x = a[k], y = b[k];
    if (typeof x === "string") return dir * x.localeCompare(y);
    return dir * ((x||0) - (y||0));
  });
  const tb = $("#tagents tbody"); tb.replaceChildren();
  for (const a of rows.slice(0, 300)){
    const tr = document.createElement("tr");
    const c1 = document.createElement("td");
    if (a.focus){ const d = document.createElement("span");
      d.className = "focusdot"; d.title = "Focus scope"; c1.appendChild(d); }
    c1.appendChild(document.createTextNode(a.name));
    const cells = [a.tenant || "—", a.product, fmtInt(a.runs), fmtInt(a.users),
                   "$"+a.cost.toFixed(2), "$"+a.cpr.toFixed(4),
                   a.secs.toFixed(1), a.fail.toFixed(1)+"%", a.last || "—"];
    tr.appendChild(c1);
    for (const c of cells){ const td = document.createElement("td");
      td.textContent = c; tr.appendChild(td); }
    tb.appendChild(tr);
  }
  document.querySelectorAll("#tagents th").forEach(th => {
    th.removeAttribute("aria-sort");
    if (th.dataset.k === k)
      th.setAttribute("aria-sort", dir < 0 ? "descending" : "ascending");
  });
}

function renderSideTables(){
  const prods = activeProducts();
  const allow = new Set([...prods].map(i => D.products[i]));

  const errTb = $("#terr tbody"); errTb.replaceChildren();
  for (const e of D.errors.filter(e => allow.has(e.product)).slice(0, 60)){
    const tr = document.createElement("tr");
    for (const c of [e.code, e.product, fmtInt(e.n)]){
      const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); }
    errTb.appendChild(tr);
  }

  const built = new Map(D.builtAgents.map(b => [b.product, b.n]));
  const dTb = $("#tdorm tbody"); dTb.replaceChildren();
  for (const d of D.dormant.filter(d => allow.has(d.product))){
    const b = built.get(d.product) || d.n;
    const tr = document.createElement("tr");
    for (const c of [d.product, fmtInt(d.n), fmtInt(b),
                     (100*d.n/b).toFixed(0)+"%"]){
      const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); }
    dTb.appendChild(tr);
  }

  // Visual workflows. 22 executions across 424 definitions is a table, not a chart --
  // a bar chart of 6 values would dress up a number the reader can just read.
  const [wlo, whi] = dayWindow();
  const from = D.days[wlo], to = D.days[whi];
  const wfr = D.wfRuns.filter(w => allow.has(w.product)
                                && (!w.day || (w.day >= from && w.day <= to)));
  const wfa = D.wfAdoption.filter(w => allow.has(w.product));
  const defined = wfa.reduce((s,w) => s + w.defined, 0);
  const ran = wfa.reduce((s,w) => s + w.ran, 0);
  const resumed = wfr.reduce((s,w) => s + w.resumed, 0);
  $("#wfnote").textContent = defined
    ? `${fmtInt(wfr.length)} executions · ${ran} of ${defined} definitions ever ran `
      + `(${(100*ran/defined).toFixed(1)}%) · ${resumed} ever resumed`
    : "no workflow definitions in this scope";

  const wTb = $("#twf tbody"); wTb.replaceChildren();
  if (!wfr.length){
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5; td.style.color = "var(--muted)";
    td.textContent = "No workflow executions in this slice.";
    tr.appendChild(td); wTb.appendChild(tr);
  }
  for (const w of wfr){
    const tr = document.createElement("tr");
    const c1 = document.createElement("td");
    if (w.focus){ const d = document.createElement("span");
      d.className = "focusdot"; d.title = "Focus scope"; c1.appendChild(d); }
    c1.appendChild(document.createTextNode(w.name));
    tr.appendChild(c1);
    const st = document.createElement("td");
    const dot = document.createElement("span");
    dot.className = "statusdot";
    dot.style.background = w.status === "completed" ? "var(--good)"
                         : w.status === "failed" ? "var(--critical)" : "var(--warning)";
    st.append(dot, document.createTextNode(w.status || "—"));
    const cells = [w.product, null, w.day || "—", w.step || "—"];
    cells.forEach((c,i) => {
      if (i === 1){ tr.appendChild(st); return; }
      const td = document.createElement("td"); td.textContent = c;
      if (i === 3) td.title = w.reason || "";
      tr.appendChild(td);
    });
    wTb.appendChild(tr);
  }

  const aTb = $("#twfa tbody"); aTb.replaceChildren();
  for (const w of wfa){
    const tr = document.createElement("tr");
    const c1 = document.createElement("td");
    if (w.focus){ const d = document.createElement("span");
      d.className = "focusdot"; d.title = "Focus scope"; c1.appendChild(d); }
    c1.appendChild(document.createTextNode(w.product));
    tr.appendChild(c1);
    for (const c of [fmtInt(w.defined), fmtInt(w.ran), fmtInt(w.execs),
                     (100*w.ran/w.defined).toFixed(0)+"%"]){
      const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); }
    aTb.appendChild(tr);
  }

  const gTb = $("#tgaps tbody"); gTb.replaceChildren();
  for (const g of D.gaps){
    const tr = document.createElement("tr");
    for (const c of [g.project, g.agent, fmtInt(g.runs), "$"+g.cost.toFixed(2)]){
      const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); }
    gTb.appendChild(tr);
  }
}

/* ---------- negative feedback ---------- */
const fbState = {query:"", label:"", sev:"", commentedOnly:true, page:1,
                 sortKey:"neg", sortDir:-1, tenant:""};
/* "" = all tenants. Held as the tenant's index into D.tenants, as a string, because
   it comes off a <select>. Scoped to this tab: the adoption view has its own axes. */
const fbTenant = () => fbState.tenant === "" ? -1 : +fbState.tenant;
const tenantName = i => (i >= 0 && D.tenants[i]) || "(unknown tenant)";
const FB_PAGE = 40;
const LABEL_TEXT = {
  incorrect_answer:"Incorrect answer",
  wrong_ownership_claim:"Wrong ownership / jurisdiction",
  kb_gap:"Knowledge gap",
  over_narrow_refusal:"Refused something in scope",
  incomplete_answer:"Incomplete answer",
  too_generic:"Too generic",
  missing_source_or_next_step:"No source or next step",
  language_handling:"Language handling",
  tool_or_system_error:"Tool / system error",
  formatting_or_tone:"Formatting or tone",
  other:"Other",
  not_a_defect:"Not a defect",
};
const labelText = k => LABEL_TEXT[k] || k || "(unclassified)";

/* Turns surviving the shared filters (product scope + date window). Separate from
   the list's own search/category filters so the KPIs describe the whole slice. */
function fbInSlice(){
  const prods = activeProducts(), [lo,hi] = dayWindow();
  const from = D.days[lo], to = D.days[hi];
  const ten = fbTenant();
  return D.fbTurns.filter(t => prods.has(t.p) && t.d >= from && t.d <= to
                               && (ten < 0 || t.tn === ten));
}

function fbListRows(inSlice){
  const q = fbState.query.trim().toLowerCase();
  return inSlice.filter(t => {
    if (fbState.commentedOnly && !t.c) return false;
    if (fbState.label && t.l !== fbState.label) return false;
    if (fbState.sev && t.s !== fbState.sev) return false;
    if (q && !(t.c.toLowerCase().includes(q) || t.q.toLowerCase().includes(q)
               || t.a.toLowerCase().includes(q)
               || tenantName(t.tn).toLowerCase().includes(q)
               || t.r.toLowerCase().includes(q))) return false;
    return true;
  });
}

function drawFbTrend(inSlice){
  const svg = $("#cf1"); svg.replaceChildren();
  const W = svg.clientWidth || 560, H = 250;
  const M = {t:14, r:52, b:26, l:42};
  const prods = activeProducts(), [lo,hi] = dayWindow();
  const from = D.days[lo], to = D.days[hi];

  // Positives come from the count fact, not from fbTurns (which is negatives only).
  const ten = fbTenant();
  const byMonth = new Map();
  for (const [day, p, neg, pos, , tn] of D.fbCounts){
    if (!prods.has(p) || day < from || day > to) continue;
    if (ten >= 0 && tn !== ten) continue;
    const m = day.slice(0,7);
    const cur = byMonth.get(m) || {neg:0, pos:0};
    cur.neg += neg; cur.pos += pos;
    byMonth.set(m, cur);
  }
  const commentByMonth = new Map();
  for (const t of inSlice){
    if (!t.c) continue;
    const m = t.d.slice(0,7);
    commentByMonth.set(m, (commentByMonth.get(m) || 0) + 1);
  }
  const months = [...byMonth.keys()].sort();
  if (!months.length){
    svg.appendChild(text(W/2, H/2, "No feedback in this slice", "catlab", "middle"));
    tableFor("tvf1", ["Month","Thumbs-down","Thumbs-up"], []);
    return;
  }

  const sc = niceScale(Math.max(1, ...months.map(m =>
    Math.max(byMonth.get(m).neg, byMonth.get(m).pos))));
  const max = sc.max, af = axisFmt(max);
  const X = i => months.length === 1 ? (M.l + (W-M.l-M.r)/2)
                 : M.l + i*(W-M.l-M.r)/(months.length-1);
  const Y = v => H - M.b - (v/max)*(H-M.t-M.b);
  const cNeg = cssVar("--critical"), cPos = cssVar("--good");

  for (let g=0; g<=sc.n; g++){
    const y = M.t + g*(H-M.t-M.b)/sc.n;
    svg.appendChild(el("line", {x1:M.l, x2:W-M.r, y1:y, y2:y, class:"gl"}));
    svg.appendChild(text(M.l-8, y+3.5, af(sc.step*(sc.n-g)), "tick", "end"));
  }
  svg.appendChild(el("line", {x1:M.l, x2:W-M.r, y1:H-M.b, y2:H-M.b, class:"ax"}));
  months.forEach((m,i) => {
    if (months.length > 10 && i % 2) return;
    svg.appendChild(text(X(i), H-M.b+15, m.slice(2), "tick", "middle"));
  });

  for (const [key, col] of [["pos", cPos], ["neg", cNeg]]){
    const pts = months.map((m,i) => [X(i), Y(byMonth.get(m)[key])]);
    if (pts.length > 1){
      svg.appendChild(el("path", {
        d: pts.map((p,i) => (i?"L":"M")+p[0]+" "+p[1]).join(" "),
        fill:"none", stroke:col, "stroke-width":2,
        "stroke-linejoin":"round", "stroke-linecap":"round"}));
    }
    const last = pts[pts.length-1];
    if (last){
      svg.appendChild(el("circle", {cx:last[0], cy:last[1], r:4.5, fill:col,
        stroke:cssVar("--surface"), "stroke-width":2}));
      svg.appendChild(text(last[0]+9, last[1]+4,
        fmtInt(byMonth.get(months[months.length-1])[key]), "vlab"));
    }
  }

  const cross = el("line", {y1:M.t, y2:H-M.b, class:"crosshair"});
  svg.appendChild(cross);
  const hit = el("rect", {x:M.l, y:M.t, width:Math.max(1,W-M.l-M.r),
                          height:H-M.t-M.b, class:"hit"});
  svg.appendChild(hit);
  hit.addEventListener("pointermove", ev => {
    const box = svg.getBoundingClientRect(), rel = ev.clientX - box.left;
    let bi = 0, bd = Infinity;
    months.forEach((m,i) => { const d = Math.abs(X(i)-rel); if (d<bd){bd=d;bi=i;} });
    cross.setAttribute("x1", X(bi)); cross.setAttribute("x2", X(bi));
    cross.style.opacity = 1;
    const r = byMonth.get(months[bi]);
    const rated = r.neg + r.pos;
    showTip(ev.clientX, ev.clientY, months[bi], [
      {color:cNeg, label:"thumbs-down", value:fmtInt(r.neg)},
      {color:cPos, label:"thumbs-up", value:fmtInt(r.pos)},
      {label:"negative share", value: rated ? fmtPct(100*r.neg/rated) : "—"},
      {label:"with a comment", value:fmtInt(commentByMonth.get(months[bi]) || 0)},
    ]);
  });
  hit.addEventListener("pointerleave", () => { cross.style.opacity=0; hideTip(); });

  tableFor("tvf1", ["Month","Thumbs-down","w/ comment","Thumbs-up","Neg share"],
    months.map(m => {
      const r = byMonth.get(m), rated = r.neg + r.pos;
      return [m, fmtInt(r.neg), fmtInt(commentByMonth.get(m) || 0), fmtInt(r.pos),
              rated ? fmtPct(100*r.neg/rated) : "—"];
    }));
}

function drawFbCategories(inSlice){
  const counts = new Map();
  let labelled = 0;
  for (const t of inSlice){
    if (!t.l) continue;
    labelled++;
    counts.set(t.l, (counts.get(t.l) || 0) + 1);
  }
  const note = $("#cf2note");
  if (!labelled){
    $("#cf2").replaceChildren();
    const svg = $("#cf2"), W = svg.clientWidth || 560;
    svg.setAttribute("height", 120);
    svg.appendChild(text(W/2, 56,
      "Not classified yet — run: python3 label_feedback.py", "catlab", "middle"));
    note.textContent = "The classifier has not been run over this slice";
    tableFor("tvf2", ["Category","Turns"], []);
    return;
  }
  note.textContent = `${fmtInt(labelled)} classified turns in this slice`;
  const s1 = cssVar("--s1"), de = cssVar("--deemph");
  const rows = D.fbLabels.filter(k => counts.has(k)).map(k => ({
    label: labelText(k), value: counts.get(k),
    // not_a_defect is de-emphasised: it is the pile you do NOT act on.
    color: k === "not_a_defect" ? de : s1,
    flag: false,
    tip: [{value: fmtInt(counts.get(k)), label:"turns", color:s1},
          {value: fmtPct(100*counts.get(k)/labelled), label:"of classified"}],
  })).sort((a,b) => b.value-a.value);

  drawBars({
    svg:"#cf2", table:"tvf2", rows,
    fmtAxis: fmtCompact, fmtValue: v => fmtInt(v),
    tableHead:["Category","Turns","% of classified"],
    tableRow: r => [r.label, fmtInt(r.value), r.tip[1].value],
  });
}

function drawFbAgentRates(){
  const prods = activeProducts();
  // A minimum conversation count is essential: without it the chart ranks pilots
  // with three conversations and one downvote at 33 per 100.
  const MIN_CONVS = 250;
  const ten = fbTenant();
  const keep = D.fbAgents.filter(a => prods.has(a.p) && a.convs >= MIN_CONVS
                                      && a.per100 != null
                                      && (ten < 0 || a.tn === ten));
  // Two bars reading 'Staff Assistant' are unreadable, so qualify a label with its
  // tenant only when the name is actually ambiguous among the bars being drawn.
  const nameCount = new Map();
  for (const a of keep) nameCount.set(a.name, (nameCount.get(a.name)||0) + 1);
  const rows = keep
    .map(a => ({
      label: nameCount.get(a.name) > 1 ? `${a.name} · ${tenantName(a.tn)}` : a.name,
      value: a.per100,
      color: a.per100 >= 1 ? cssVar("--critical") : cssVar("--s1"),
      flag: a.per100 >= 1,
      tip: [{value: a.per100.toFixed(2), label:"neg per 100 convs",
             color: cssVar("--critical")},
            {value: fmtInt(a.neg), label:"thumbs-down"},
            {value: fmtInt(a.convs), label:"conversations"},
            {value: a.share == null ? "—" : fmtPct(a.share), label:"neg share of rated"},
            {value: tenantName(a.tn), label:"tenant"}],
    }))
    .sort((a,b) => b.value-a.value);

  if (!rows.length){
    const svg = $("#cf3"); svg.replaceChildren();
    svg.setAttribute("height", 120);
    svg.appendChild(text((svg.clientWidth||560)/2, 56,
      `No agent with ${MIN_CONVS}+ conversations in this slice`, "catlab", "middle"));
    tableFor("tvf3", ["Agent","Neg/100"], []);
    return;
  }
  drawBars({
    svg:"#cf3", table:"tvf3", rows,
    fmtAxis: v => v.toFixed(1), fmtValue: v => v.toFixed(2),
    tableHead:["Agent","Tenant","Neg/100 convs","Thumbs-down","Conversations",
               "Neg share"],
    tableRow: r => [r.label, r.tip[4].value, r.value.toFixed(2), r.tip[1].value,
                    r.tip[2].value, r.tip[3].value],
  });
}

function renderFbFixTable(inSlice){
  const tb = $("#tfix tbody"); tb.replaceChildren();
  const grid = new Map();
  for (const t of inSlice){
    if (!t.f) continue;
    const row = grid.get(t.f) || {misleading:0, unhelpful:0, minor:0, total:0};
    if (t.s in row) row[t.s]++;
    row.total++;
    grid.set(t.f, row);
  }
  const FIX_TEXT = {knowledge_base:"Knowledge base", instructions:"Instructions",
                    routing:"Routing", model:"Model", none:"Not actionable"};
  const rows = [...grid.entries()].sort((a,b) => b[1].total-a[1].total);
  if (!rows.length){
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5; td.className = "fbempty";
    td.textContent = "Run label_feedback.py to populate this.";
    tr.appendChild(td); tb.appendChild(tr);
    return;
  }
  for (const [fix, r] of rows){
    const tr = document.createElement("tr");
    for (const c of [FIX_TEXT[fix] || fix, fmtInt(r.misleading), fmtInt(r.unhelpful),
                     fmtInt(r.minor), fmtInt(r.total)]){
      const td = document.createElement("td"); td.textContent = c; tr.appendChild(td);
    }
    tb.appendChild(tr);
  }
}

function renderFbScorecard(){
  const prods = activeProducts();
  const ten = fbTenant();
  const rows = D.fbAgents.filter(a => prods.has(a.p) && (ten < 0 || a.tn === ten));
  const k = fbState.sortKey, dir = fbState.sortDir;
  const cell = (a, key) => key === "product" ? D.products[a.p]
                         : key === "tenant"  ? tenantName(a.tn)
                         : a[key];
  rows.sort((a,b) => {
    let x = cell(a, k);
    let y = cell(b, k);
    if (x == null) x = -1; if (y == null) y = -1;
    return typeof x === "string" ? dir*x.localeCompare(y) : dir*(x-y);
  });
  const tb = $("#tfba tbody"); tb.replaceChildren();
  for (const a of rows.slice(0, 200)){
    const tr = document.createElement("tr");
    for (const c of [a.name, tenantName(a.tn), D.products[a.p],
                     fmtInt(a.convs), fmtInt(a.neg),
                     fmtInt(a.negc), fmtInt(a.pos),
                     a.share == null ? "—" : fmtPct(a.share),
                     a.per100 == null ? "—" : a.per100.toFixed(2),
                     a.rate == null ? "—" : fmtPct(a.rate), a.last]){
      const td = document.createElement("td"); td.textContent = c; tr.appendChild(td);
    }
    tb.appendChild(tr);
  }
  document.querySelectorAll("#tfba th").forEach(th => {
    th.removeAttribute("aria-sort");
    if (th.dataset.fk === k)
      th.setAttribute("aria-sort", dir < 0 ? "descending" : "ascending");
  });
}

function renderFbList(inSlice){
  const rows = fbListRows(inSlice);
  const host = $("#fblist"); host.replaceChildren();
  const shown = Math.min(rows.length, FB_PAGE*fbState.page);

  $("#fbnote").textContent = rows.length
    ? `${fmtInt(rows.length)} matching turns · showing ${fmtInt(shown)}. `
      + "Newest first. Click a conversation id to copy it."
    : "Nothing matches these filters.";

  if (!rows.length){
    const d = document.createElement("div");
    d.className = "fbempty";
    d.textContent = D.fbTurns.length
      ? "No thumbs-down feedback matches the current filters."
      : "No feedback extracted yet — run: python3 extract.py feedback";
    host.appendChild(d);
    $("#fbMore").hidden = true;
    return;
  }

  for (const t of rows.slice(0, shown)){
    const card = document.createElement("div");
    card.className = "fbrow";

    const meta = document.createElement("div");
    meta.className = "fbmeta";
    const who = document.createElement("span");
    who.className = "who"; who.textContent = t.a;
    meta.appendChild(who);
    // Tenant sits next to the agent name, not further down the row: with names like
    // 'Staff Assistant' the name alone does not identify which deployment complained.
    const ten = document.createElement("span");
    ten.className = "pill tenant"; ten.textContent = tenantName(t.tn);
    meta.appendChild(ten);
    for (const part of [D.products[t.p], t.d]){
      const s = document.createElement("span"); s.textContent = part;
      meta.appendChild(s);
    }
    const lab = document.createElement("span");
    lab.className = "pill" + (t.l ? "" : " nolabel");
    lab.textContent = labelText(t.l);
    meta.appendChild(lab);
    if (t.s){
      const sev = document.createElement("span");
      sev.className = "pill sev-" + t.s; sev.textContent = t.s;
      meta.appendChild(sev);
    }
    if (t.f){
      const fix = document.createElement("span");
      fix.className = "pill"; fix.textContent = "fix: " + t.f;
      meta.appendChild(fix);
    }
    const cid = document.createElement("button");
    cid.className = "fbcid";
    cid.textContent = t.cid.slice(0,8) + "… turn " + t.t;
    cid.title = "Copy conversation id " + t.cid;
    cid.onclick = () => {
      navigator.clipboard && navigator.clipboard.writeText(t.cid);
      cid.textContent = "copied";
      setTimeout(() => { cid.textContent = t.cid.slice(0,8)+"… turn "+t.t; }, 1200);
    };
    meta.appendChild(cid);
    card.appendChild(meta);

    if (t.c){
      const c = document.createElement("p");
      c.className = "fbc"; c.textContent = t.c;
      card.appendChild(c);
    }
    const qr = document.createElement("div");
    qr.className = "fbqr";
    for (const [labelStr, val] of [["Asked", t.q], ["Answered", t.r]]){
      if (!val) continue;
      const d = document.createElement("div");
      const b = document.createElement("b"); b.textContent = labelStr + ": ";
      const s = document.createElement("span");
      s.className = "txt"; s.textContent = val;
      d.appendChild(b); d.appendChild(s); qr.appendChild(d);
    }
    card.appendChild(qr);
    if (t.why){
      const w = document.createElement("div");
      w.className = "fbwhy"; w.textContent = "Classifier: " + t.why;
      card.appendChild(w);
    }
    host.appendChild(card);
  }
  $("#fbMore").hidden = shown >= rows.length;
}

function renderFeedback(){
  const inSlice = fbInSlice();
  const prods = activeProducts(), [lo,hi] = dayWindow();
  const from = D.days[lo], to = D.days[hi];

  const ten = fbTenant();
  let pos = 0, convs = 0;
  for (const [day, p, neg, pu, cv, tn] of D.fbCounts){
    if (!prods.has(p) || day < from || day > to) continue;
    if (ten >= 0 && tn !== ten) continue;
    pos += pu; convs += cv;
  }
  const commented = inSlice.filter(t => t.c).length;
  const labelled = inSlice.filter(t => t.l).length;
  const rated = inSlice.length + pos;

  // Denominator for the submission rate: conversations actually held, from the run
  // facts -- not the conversations that happened to get rated.
  let heldConvs = 0;
  for (const [d,p,runs,cv] of D.facts){
    if (d < lo || d > hi || !prods.has(p)) continue;
    heldConvs += cv;
  }

  $("#kNeg").textContent = fmtInt(inSlice.length);
  $("#kNegNote").textContent = `${fmtInt(pos)} thumbs-up in the same slice`;
  $("#kNegC").textContent = fmtInt(commented);
  $("#kNegCNote").textContent = inSlice.length
    ? fmtPct(100*commented/inSlice.length) + " of thumbs-down explained"
    : "—";
  $("#kNegShare").textContent = rated ? fmtPct(100*inSlice.length/rated) : "—";
  $("#kNegShareNote").textContent = rated
    ? fmtInt(rated) + " rated turns total" : "no ratings in slice";
  // heldConvs comes from the run facts, which are grouped by day and product only --
  // there is no tenant column there. Under a tenant filter the numerator narrows and
  // the denominator does not, so the ratio would understate the rate by roughly the
  // tenant's share of traffic. Blanked rather than shown wrong.
  if (ten >= 0){
    $("#kFbRate").textContent = "—";
    $("#kFbRateNote").textContent =
      `${fmtInt(convs)} rated conversations · rate needs a tenant-scoped denominator `
      + "the run facts don't carry";
  } else {
    $("#kFbRate").textContent = heldConvs ? fmtPct(100*convs/heldConvs) : "—";
    $("#kFbRateNote").textContent = heldConvs
      ? fmtInt(convs) + " rated of " + fmtCompact(heldConvs) + " conversations"
      : "—";
  }
  // Denominator is the *commented* turns: uncommented downvotes are skipped by
  // design, not pending, so counting them would show a permanent shortfall.
  $("#kLabelled").textContent = commented
    ? fmtPct(100*labelled/commented) : "—";
  $("#kLabelledNote").textContent = commented
    ? `${fmtInt(labelled)} of ${fmtInt(commented)} explained complaints`
    : "nothing to classify";

  $("#cf1note").textContent = "Thumbs-down in red, thumbs-up in green. "
    + "Users are only prompted for text on a thumbs-down.";

  drawFbTrend(inSlice);
  drawFbCategories(inSlice);
  drawFbAgentRates();
  renderFbFixTable(inSlice);
  renderFbScorecard();
  renderFbList(inSlice);
}

/* Shared by both views, so the filter chip always describes what is on screen. */
function renderSlice(){
  const prods = activeProducts(), [lo,hi] = dayWindow();
  const scope = [];
  if (state.portfolio) scope.push(state.portfolio);
  if (state.product) scope.push(state.product);
  if (state.focus) scope.push("focus scope only");
  if (!state.sandbox) scope.push("product traffic only");
  $("#slicechip").textContent =
    `${D.days[lo]} → ${D.days[hi]} · ${prods.size} of ${D.products.length} products`
    + (scope.length ? " · " + scope.join(" · ") : "");
}

/* ---------- EPL solutions ---------- */
const E = D.epl;
/* "" = everything shipped, "type:Customer" = a whole class, "dep:3" = one deployment.
   Deliberately independent of the global filters: this tab is presented from, and a
   stray date filter silently restating the numbers mid-meeting is the failure mode. */
const eplState = {scope:"", sortKey:"convs", sortDir:-1, open:new Set()};
const U = D.users;
const usState = {status:"", query:"", sortKey:"since", sortDir:-1,
                 from:U.dates[0], to:U.asOf};
let usShown = null;     // the rows currently in the table, in order -- what Export writes

function eplDepSet(){
  const s = eplState.scope;
  const out = new Set();
  E.deps.forEach((d,i) => {
    if (!s) out.add(i);
    else if (s.startsWith("type:") && d.type === s.slice(5)) out.add(i);
    else if (s.startsWith("dep:") && i === +s.slice(4)) out.add(i);
  });
  return out;
}

/* Distinct users/agents from presence tuples, never summed per deployment: the same
   person shows up in several deployments (53 real users vs 70 if summed). */
function eplDistinct(tuples, deps, weekFrom, weekTo){
  const seen = new Set();
  for (const [w, d, ix] of tuples)
    if (deps.has(d) && w >= weekFrom && w <= weekTo) seen.add(ix);
  return seen.size;
}

function eplWeekly(deps){
  const n = E.weeks.length;
  const rows = Array.from({length:n}, () => ({convs:0, runs:0, users:0, agents:0}));
  for (const [w, d, runs] of E.facts){
    if (!deps.has(d)) continue;
    rows[w].runs += runs;
  }
  // Per-week distinct counts, bucketed by week. A conversation active in two weeks
  // counts in both -- correct for "activity that week", which is why these rows are
  // never summed to produce a total. Totals come from eplDistinct over the full range.
  for (const [key, tuples] of [["users", E.userTuples], ["agents", E.agentTuples],
                               ["convs", E.convTuples]]){
    const perWeek = Array.from({length:n}, () => new Set());
    for (const [w, d, ix] of tuples) if (deps.has(d)) perWeek[w].add(ix);
    perWeek.forEach((s,i) => { rows[i][key] = s.size; });
  }
  return rows;
}

/* The window of weeks the CURRENT SELECTION was actually alive for, as [lo, hi].
   E.weeks spans every shipped deployment, which is right for "all" and wrong for one:
   EPL CX went live 2026-07-10, so padding it back to the March start draws four months
   of empty chart before the first bar. Only the empty head and tail are dropped --
   interior gaps stay, because a dormant week inside a live run is a real dormant week
   (2026-06-22 is one) and squeezing it out would compress the timeline. */
function eplWeekSpan(rows){
  const live = i => rows[i].runs || rows[i].convs || rows[i].users;
  let lo = 0;
  while (lo < rows.length && !live(lo)) lo++;
  if (lo >= rows.length) return [0, -1];
  let hi = rows.length - 1;
  while (hi > lo && !live(hi)) hi--;
  return [lo, hi];
}

function drawEplWeekly(allRows, lo, hi){
  const svg = $("#cEpl1"); svg.replaceChildren();
  const W = svg.clientWidth || 720, H = 290;
  const M = {t:16, r:46, b:52, l:46};
  const rows = hi >= lo ? allRows.slice(lo, hi + 1) : [];
  const wk = hi >= lo ? E.weeks.slice(lo, hi + 1) : [];
  // Partial-week index rebased onto the trimmed axis; -1 when it fell outside it.
  const partialIx = (E.partialWeek >= lo && E.partialWeek <= hi)
    ? E.partialWeek - lo : -1;
  const n = rows.length;
  if (!n){
    svg.appendChild(text(W/2, H/2, "No deployments in scope", "catlab", "middle"));
    tableFor("tvEpl1", ["Week","Conversations","Users"], []);
    return;
  }
  const cMax = niceScale(Math.max(1, ...rows.map(r => r.convs)));
  const uMax = niceScale(Math.max(1, ...rows.map(r => r.users)));
  const plotW = W - M.l - M.r, plotH = H - M.t - M.b;
  const bw = Math.max(4, Math.min(30, plotW/n - 4));
  const X = i => M.l + (i + 0.5)*plotW/n;
  const Yc = v => H - M.b - (v/cMax.max)*plotH;
  const Yu = v => H - M.b - (v/uMax.max)*plotH;
  const cBar = cssVar("--s1"), cUser = cssVar("--s2") || cssVar("--critical");
  const afC = axisFmt(cMax.max);

  // Both axes must be divided the same number of times, or the right-hand labels stop
  // short of zero and the line appears to start partway up the chart. The gridlines
  // belong to the left scale, so the right scale is subdivided to match it rather than
  // using its own niceScale step count.
  for (let g=0; g<=cMax.n; g++){
    const y = M.t + g*plotH/cMax.n;
    const frac = (cMax.n - g)/cMax.n;
    svg.appendChild(el("line", {x1:M.l, x2:W-M.r, y1:y, y2:y, class:"gl"}));
    svg.appendChild(text(M.l-8, y+3.5, afC(cMax.step*(cMax.n-g)), "tick", "end"));
    svg.appendChild(text(W-M.r+8, y+3.5,
      String(Math.round(uMax.max*frac)), "tick", "start"));
  }
  svg.appendChild(el("line", {x1:M.l, x2:W-M.r, y1:H-M.b, y2:H-M.b, class:"ax"}));

  rows.forEach((r,i) => {
    const isPartial = i === partialIx;
    const h = Math.max(0, H - M.b - Yc(r.convs));
    if (r.convs > 0){
      const bar = el("rect", {x:X(i)-bw/2, y:Yc(r.convs), width:bw, height:h,
        fill:cBar, rx:2});
      // The final week is usually a day or two long; drawn hollow so the dip reads as
      // "week still in progress" instead of a fall in usage.
      if (isPartial){ bar.setAttribute("fill", "none");
                      bar.setAttribute("stroke", cBar);
                      bar.setAttribute("stroke-dasharray", "3 2"); }
      bar.addEventListener("mouseenter", ev => showTip(ev.clientX, ev.clientY,
        wk[i] + (isPartial ? " (week in progress)" : ""),
        [{value:fmtInt(r.convs), label:"conversations active", color:cBar},
         {value:fmtInt(r.users), label:"people", color:cUser},
         {value:fmtInt(r.agents), label:"agents live"},
         {value:fmtInt(r.runs), label:"runs"}]));
      bar.addEventListener("mouseleave", hideTip);
      svg.appendChild(bar);
    }
  });

  // The line STOPS at the last complete week. Carrying it into a part-week drags it to
  // near zero and draws a collapse in usage that is only "the week isn't over" -- the
  // exact misreading the hollow bar exists to prevent. The partial point is drawn
  // hollow and detached so it is visible without being joined to the trend.
  const pts = rows.map((r,i) => [X(i), Yu(r.users)]);
  const lastComplete = partialIx >= 0 ? partialIx - 1 : rows.length - 1;
  const solid = pts.slice(0, lastComplete + 1);
  if (solid.length > 1){
    svg.appendChild(el("path", {
      d: solid.map((p,i) => (i?"L":"M")+p[0]+" "+p[1]).join(" "),
      fill:"none", stroke:cUser, "stroke-width":2.4,
      "stroke-linejoin":"round", "stroke-linecap":"round"}));
  }
  rows.forEach((r,i) => {
    if (!r.users) return;
    const partial = i === partialIx;
    const dot = el("circle", {cx:X(i), cy:Yu(r.users), r:3.4,
      fill: partial ? "none" : cUser, stroke: partial ? cUser : cssVar("--surface"),
      "stroke-width": partial ? 1.6 : 1.5});
    if (partial) dot.setAttribute("stroke-dasharray", "2 1.5");
    svg.appendChild(dot);
  });

  const step = n > 14 ? Math.ceil(n/9) : 1;
  wk.forEach((w,i) => {
    if (i % step && i !== n-1) return;
    svg.appendChild(text(X(i), H-M.b+15, w.slice(5), "tick", "middle"));
  });
  svg.appendChild(text(M.l, H-8, "conversations active (bars, left)", "catlab", "start"));
  svg.appendChild(text(W-M.r, H-8, "people (line, right)", "catlab", "end"));

  tableFor("tvEpl1", ["Week","Conversations active","People","Agents live","Runs"],
    rows.map((r,i) => [wk[i] + (i===partialIx ? " (partial)" : ""),
                       fmtInt(r.convs), fmtInt(r.users), fmtInt(r.agents),
                       fmtInt(r.runs)]));
}

function renderEplDeps(deps){
  const rows = E.deps.map((d,i) => ({...d, i}))
    .filter(d => deps.has(d.i))
    // negC, not neg: the rate claims a share OF CONVERSATIONS, so its numerator has to
    // be conversations that contain a complaint. Using the turn count reported 70.8%
    // for Encinitas where the truth is 45.8%.
    .map(d => ({...d, rate: d.rated ? 100*d.negC/d.convs : null}));
  const k = eplState.sortKey, dir = eplState.sortDir;
  rows.sort((a,b) => {
    let x = a[k], y = b[k];
    if (x == null) x = -1; if (y == null) y = -1;
    return typeof x === "string" ? dir*x.localeCompare(y) : dir*(x-y);
  });
  const tb = $("#tEplDeps tbody"); tb.replaceChildren();
  for (const d of rows){
    const open = eplState.open.has(d.i);
    const kids = E.depAgents[d.i] || [];
    const tr = document.createElement("tr");
    const toggle = () => {
      if (eplState.open.has(d.i)) eplState.open.delete(d.i);
      else eplState.open.add(d.i);
      renderEplDeps(deps);
    };
    if (kids.length){
      tr.className = "eplparent";
      tr.setAttribute("aria-expanded", String(open));
      // The whole row toggles, not a caret hitbox -- this table gets read off a
      // projector, where a 13px target is a missed click.
      tr.tabIndex = 0;
      tr.addEventListener("click", toggle);
      tr.addEventListener("keydown", ev => {
        if (ev.key === "Enter" || ev.key === " "){ ev.preventDefault(); toggle(); }
      });
    }
    const cells = [d.name, d.type, fmtInt(d.agents), d.first, d.last,
                   fmtInt(d.users), fmtInt(d.convs), fmtInt(d.rated),
                   fmtInt(d.pos), fmtInt(d.neg),
                   d.rate == null ? "—" : d.rate.toFixed(1) + "%"];
    cells.forEach((c,ci) => {
      const td = document.createElement("td");
      if (ci === 0 && kids.length){
        const car = document.createElement("span");
        car.className = "caret"; car.textContent = open ? "▾" : "▸";
        td.append(car, document.createTextNode(c));
        td.title = c;
      } else {
        td.textContent = c;
      }
      tr.appendChild(td);
    });
    tb.appendChild(tr);
    if (!open) continue;
    for (const a of kids){
      // Same grain and denominator as the rollup: conversations containing a complaint
      // over conversations held, and "—" when nothing was rated at all.
      const rate = a.rated ? 100*a.negC/a.convs : null;
      const sub = document.createElement("tr");
      sub.className = "eplsub";
      const scells = [a.name, "", "", a.first, a.last, fmtInt(a.users),
                      fmtInt(a.convs), fmtInt(a.rated), fmtInt(a.pos), fmtInt(a.neg),
                      rate == null ? "—" : rate.toFixed(1) + "%"];
      scells.forEach((c,ci) => {
        const td = document.createElement("td");
        td.textContent = c;
        if (ci === 0) td.title = a.name + " · agent " + a.key;
        sub.appendChild(td);
      });
      tb.appendChild(sub);
    }
  }
  const anyOpen = rows.some(d => eplState.open.has(d.i));
  const btn = $("#eplExpandAll");
  btn.setAttribute("aria-pressed", String(anyOpen));
  btn.textContent = anyOpen ? "Collapse all" : "Expand all";
  document.querySelectorAll("#tEplDeps th").forEach(th => {
    th.removeAttribute("aria-sort");
    if (th.dataset.ek === k)
      th.setAttribute("aria-sort", dir < 0 ? "descending" : "ascending");
  });

  const ex = E.excluded;
  $("#eplExcluded").textContent = ex.length
    ? "Not shown (not shipped deployments): "
      + ex.map(([t,r]) => `${t} ${fmtInt(r)} runs`).join(" · ")
    : "";
}

function renderEplCauses(deps){
  const mine = E.complaints.filter(c => deps.has(c.dep));
  const byLabel = new Map(), byFix = new Map();
  for (const c of mine){
    byLabel.set(c.l, (byLabel.get(c.l)||0) + 1);
    const key = c.l + "|" + c.f;
    byFix.set(key, (byFix.get(key)||0) + 1);
  }
  const FIX_TEXT = {knowledge_base:"Knowledge base", instructions:"Instructions",
                    routing:"Routing", model:"Model", none:"Not actionable", "":"—"};
  const tb = $("#tEplCauses tbody"); tb.replaceChildren();
  const ordered = [...byLabel.entries()].sort((a,b) => b[1]-a[1]);
  if (!ordered.length){
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3; td.className = "fbempty";
    td.textContent = "No thumbs-down feedback for this selection.";
    tr.appendChild(td); tb.appendChild(tr);
  }
  for (const [label, count] of ordered){
    const fixes = [...byFix.entries()].filter(([k]) => k.startsWith(label+"|"))
      .sort((a,b) => b[1]-a[1])
      .map(([k,v]) => `${FIX_TEXT[k.split("|")[1]] ?? k.split("|")[1]} (${v})`);
    const tr = document.createElement("tr");
    for (const c of [labelText(label), fmtInt(count), fixes.join(", ")]){
      const td = document.createElement("td"); td.textContent = c; tr.appendChild(td);
    }
    tb.appendChild(tr);
  }

  // Turns, not conversations, and said so: this is the fix list, and two complaints in
  // one conversation are two things to fix even though they are one unhappy user.
  const cConvs = new Set(mine.map(c => c.cid)).size;
  $("#eplCxNote").textContent = mine.length
    ? `${fmtInt(mine.length)} thumbs-down turn`
      + (mine.length === 1 ? "" : "s")
      + ` across ${fmtInt(cConvs)} conversation`
      + (cConvs === 1 ? "" : "s")
      + ", grouped by what went wrong. Every one is quoted below — this is the fix "
      + "list, not a score."
    : "No thumbs-down feedback for this selection.";

  const host = $("#eplComplaints"); host.replaceChildren();
  for (const c of mine){
    const card = document.createElement("div");
    card.className = "fbrow";
    const meta = document.createElement("div");
    meta.className = "fbmeta";
    const who = document.createElement("span");
    who.className = "who"; who.textContent = c.a;
    meta.appendChild(who);
    const dep = document.createElement("span");
    dep.className = "pill tenant"; dep.textContent = E.deps[c.dep].name;
    meta.appendChild(dep);
    const day = document.createElement("span"); day.textContent = c.d;
    meta.appendChild(day);
    const lab = document.createElement("span");
    lab.className = "pill" + (c.l ? "" : " nolabel");
    lab.textContent = labelText(c.l);
    meta.appendChild(lab);
    if (c.s){
      const sev = document.createElement("span");
      sev.className = "pill sev-" + c.s; sev.textContent = c.s;
      meta.appendChild(sev);
    }
    card.appendChild(meta);
    if (c.q){
      const q = document.createElement("div");
      q.className = "fbqr";
      const b = document.createElement("b"); b.textContent = "Asked: ";
      const t = document.createElement("span");
      t.className = "txt"; t.textContent = c.q;
      q.append(b, t);
      card.appendChild(q);
    }
    const body = document.createElement("div");
    body.className = "fbc";
    body.textContent = c.c || "(no comment)";
    card.appendChild(body);
    host.appendChild(card);
  }
}

function renderEpl(){
  const deps = eplDepSet();
  const lastW = E.weeks.length - 1;
  // Distinct over the whole range, not a sum of the weekly bars: 10 conversations
  // span a week boundary, so summing weeks reports 379 against a true 366.
  const convs = eplDistinct(E.convTuples, deps, 0, lastW);
  const runs  = E.facts.reduce((a,[w,d,r]) => a + (deps.has(d) ? r : 0), 0);
  const users = eplDistinct(E.userTuples, deps, 0, lastW);
  const agents = eplDistinct(E.agentTuples, deps, 0, lastW);
  const sel = E.deps.filter((d,i) => deps.has(i));
  // neg/pos are turns, negC/posC are the conversations those turns sit in. All four sum
  // safely across deployments because a conversation belongs to exactly one tenant.
  const neg = sel.reduce((a,d) => a + d.neg, 0);
  const pos = sel.reduce((a,d) => a + d.pos, 0);
  const negC = sel.reduce((a,d) => a + d.negC, 0);
  const rated = sel.reduce((a,d) => a + d.rated, 0);

  $("#kEplConvs").textContent = fmtInt(convs);
  $("#kEplConvsNote").textContent =
    `${fmtInt(runs)} runs across ${fmtInt(sel.length)} deployment`
    + (sel.length === 1 ? "" : "s");
  $("#kEplUsers").textContent = fmtInt(users);

  // Growth claim, stated only when it is actually supported: compare the most recent
  // complete week against the first week that had any user at all. Both ends come from
  // the selection's own live window, not the full axis.
  const weekly = eplWeekly(deps);
  const [wLo, wHi] = eplWeekSpan(weekly);
  const lastComplete = E.partialWeek === wHi ? wHi - 1 : wHi;
  const firstActive = weekly.findIndex(r => r.users > 0);
  const nowU = lastComplete >= wLo ? weekly[lastComplete].users : 0;
  const thenU = firstActive >= 0 ? weekly[firstActive].users : 0;
  $("#kEplUsersNote").textContent = (firstActive >= 0 && nowU)
    ? `${thenU} in week 1 → ${nowU} in the latest full week`
    : "distinct people, counted once across deployments";
  $("#kEplAgents").textContent = fmtInt(agents);
  $("#kEplAgentsNote").textContent = "agents that have served real traffic";

  if (rated){
    // Conversations containing a complaint, over conversations held. The turn counts
    // stay in the note, labelled as turns, because they are the size of the fix list --
    // but they are not the numerator of a rate whose denominator is conversations.
    $("#kEplComplaint").textContent = (100*negC/convs).toFixed(1) + "%";
    $("#kEplComplaintNote").textContent =
      `${fmtInt(negC)} of ${fmtInt(convs)} conversations · ${fmtInt(pos)} 👍 / `
      + `${fmtInt(neg)} 👎 turns from ${fmtInt(rated)} rated (a floor: `
      + `${(100*rated/convs).toFixed(1)}% submitted feedback)`;
  } else {
    $("#kEplComplaint").textContent = "—";
    $("#kEplComplaintNote").textContent = "no feedback submitted for this selection";
  }

  // The axis is the selection's live window, so the week count has to be read off that
  // window too -- quoting the full 22-week span under a chart showing 5 weeks is the
  // kind of mismatch someone in the room will catch.
  const span = wHi >= wLo ? wHi - wLo + 1 : 0;
  const inProgress = E.partialWeek >= wLo && E.partialWeek <= wHi;
  const complete = Math.max(0, span - (inProgress ? 1 : 0));
  $("#cEplNote").textContent = span
    ? `Monday-anchored weeks, ${E.weeks[wLo]} → ${E.weeks[wHi]}. `
      + `${fmtInt(complete)} complete week` + (complete === 1 ? "" : "s")
      + (inProgress
          ? `, plus ${E.weeks[E.partialWeek]} still in progress (drawn hollow).` : ".")
      + (wLo > 0 || wHi < E.weeks.length - 1
          ? " Trimmed to the weeks this selection was live." : "")
      + " People is a distinct count, so it never sums deployments."
    : "No activity for this selection.";

  drawEplWeekly(weekly, wLo, wHi);
  renderEplDeps(deps);
  renderEplCauses(deps);
}

/* ---------- Users tab ---------- */
const DAY_MS = 86400000;
const usT = iso => Date.parse(iso + "T00:00:00Z");
const usIso = t => new Date(t).toISOString().slice(0,10);
const usDiff = (a, b) => Math.round((usT(a) - usT(b)) / DAY_MS);   // a minus b, in days
const usAddDays = (iso, n) => usIso(usT(iso) + n*DAY_MS);
// Thresholds, measured at the range end: Active <= 7 days since last run, Slipping
// 8-30, Dormant > 30.
const usStatus = since => since <= 7 ? "active" : since <= 30 ? "slipping" : "dormant";
const US_LABEL = {active:"Active", slipping:"Slipping", dormant:"Dormant"};
const US_RANK = {dormant:0, slipping:1, active:2};

// Week index of any date, clamped to the weeks that exist in the data.
function usWeekIdx(iso){
  const t = usT(iso), mon = usIso(t - ((new Date(t).getUTCDay()+6)%7)*DAY_MS);
  if (mon <= U.weeks[0]) return 0;
  if (mon >= U.weeks[U.weeks.length-1]) return U.weeks.length-1;
  return U.weeks.indexOf(mon);
}
// Index the per-day tuples once; every range is answered from these.
const usIdx = (() => {
  const wkOf = U.dates.map(usWeekIdx);
  const runs = U.rows.map(() => []), agents = U.rows.map(() => []);
  for (const [u,d,n] of U.dayTuples) runs[u].push([d,n]);
  for (const [u,d,a] of U.agentTuples) agents[u].push([d,a]);
  return {wkOf, runs, agents};
})();

// Everyone who had run anything by `to`, with activity measured inside [from, to].
// Someone whose first run is after `to` did not exist yet at that point, so is left out.
function usCompute(from, to){
  const out = [];
  U.rows.forEach((r,i) => {
    let runs = 0, days = 0, lastAny = null;
    const wk = new Array(U.weeks.length).fill(0);
    for (const [d,n] of usIdx.runs[i]){
      const day = U.dates[d];
      if (day > to) continue;
      if (lastAny === null || day > lastAny) lastAny = day;
      if (day >= from){ runs += n; days++; wk[usIdx.wkOf[d]] += n; }
    }
    if (lastAny === null) return;
    const ag = new Set();
    for (const [d,a] of usIdx.agents[i])
      if (U.dates[d] >= from && U.dates[d] <= to) ag.add(a);
    const since = usDiff(to, lastAny);
    out.push({...r, i, runs, days, agents: ag.size, wk, last: lastAny, since,
              status: usStatus(since)});
  });
  return out;
}

function usSpark(wk, partialIdx){
  const w = 6, gap = 2, h = 18, max = Math.max(1, ...wk);
  const svg = el("svg", {width: Math.max(1, wk.length)*(w+gap), height: h, role:"img",
    "aria-label": "Weekly runs: " + wk.join(", ")});
  wk.forEach((v,i) => {
    const bh = v ? Math.max(2, h*v/max) : 1;
    const r = el("rect", {x:i*(w+gap), y:h-bh, width:w, height:bh, rx:1,
      fill: v ? "var(--s1)" : "var(--grid)"});
    if (i === partialIdx) r.setAttribute("opacity", ".5");
    svg.appendChild(r);
  });
  return svg;
}

function drawUsAnon(runs, convs, wLo, wHi){
  const svg = $("#cUsAnon"); svg.replaceChildren();
  const W = svg.clientWidth || 700, H = 200, L = 40, R = 10, T = 12, B = 28;
  const n = wHi - wLo + 1;
  if (!runs || n < 1) return;
  const max = Math.max(1, ...runs);
  const bw = (W-L-R)/n;
  for (let t = 0; t <= 4; t++){
    const v = max*t/4, y = H-B-(H-B-T)*t/4;
    svg.appendChild(el("line", {x1:L, x2:W-R, y1:y, y2:y, stroke:"var(--grid)"}));
    svg.appendChild(text(L-5, y+4, fmtInt(v), "tick", "end"));
  }
  const pts = [];
  runs.forEach((v,i) => {
    const wi = wLo + i, x = L + i*bw, bh = (H-B-T)*v/max;
    const bar = el("rect", {x:x+bw*.15, y:H-B-bh, width:bw*.7, height:bh,
      fill:"var(--s1)", opacity: wi === U.partialWeek ? ".45" : "1"});
    const tip = el("title", {}); tip.textContent =
      `Week of ${U.weeks[wi]}: ${v} runs, ${convs[i]} conversations`
      + (wi === U.partialWeek ? " (week in progress)" : "");
    bar.appendChild(tip); svg.appendChild(bar);
    pts.push([x+bw/2, H-B-(H-B-T)*convs[i]/max]);
    if (n <= 16 || i % 2 === 0)
      svg.appendChild(text(x+bw/2, H-B+14, U.weeks[wi].slice(5), "tick", "middle"));
  });
  svg.appendChild(el("polyline", {points: pts.map(p => p.join(",")).join(" "),
    fill:"none", stroke:"var(--s2)", "stroke-width":2}));
  pts.forEach(p => svg.appendChild(el("circle", {cx:p[0], cy:p[1], r:3, fill:"var(--s2)"})));
  svg.appendChild(text(L, 10, "■ runs   ● conversations", "tick", "start"));
}

// RFC 4180 quoting, plus formula-injection defence: a name that starts with = + - @ would
// be executed by Excel on open, so text fields get a leading apostrophe. Numbers never do.
function csvField(v){
  let t = String(v ?? "");
  if (typeof v === "string" && /^[=+\-@\t\r]/.test(t)) t = "'" + t;
  return /[",\r\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
}
function exportUsersCsv(){
  if (!usShown) return;
  const {rows, from, to} = usShown;
  const head = ["Person", "Status", "Days since last run", "Last run", "First run",
    "Active days in range", "Runs in range", "Agents in range", "No activity in range",
    "Range start", "Range end", "Tenant"];
  const lines = [head.map(csvField).join(",")];
  for (const r of rows){
    lines.push([r.anon ? "Anonymous (shared login)" : r.name, US_LABEL[r.status],
      r.since, r.last, r.first, r.days, r.runs, r.agents, r.runs === 0 ? "Yes" : "No",
      from, to, U.tenant].map(csvField).join(","));
  }
  // BOM so Excel reads the file as UTF-8 (accented names); CRLF per the RFC.
  const blob = new Blob(["\uFEFF" + lines.join("\r\n") + "\r\n"],
    {type: "text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${U.tenant.toLowerCase().replace(/[^a-z0-9]+/g, "-")}-user-activity_${from}_to_${to}.csv`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

function renderUsers(){
  $("#usTenant").textContent = U.tenant;
  $("#usAsOf").textContent = U.asOf;
  const from = usState.from, to = usState.to;
  $("#fUsFrom").value = from; $("#fUsTo").value = to;
  const all = usCompute(from, to);
  const people = all.filter(r => !r.anon), anon = all.find(r => r.anon);

  const cnt = s => people.filter(r => r.status === s).length;
  const ran = people.filter(r => r.runs > 0).length;
  $("#kUsPeople").textContent = fmtInt(people.length);
  $("#kUsPeopleNote").textContent = `range ${from} → ${to}`;
  $("#kUsRan").textContent = fmtInt(ran);
  $("#kUsRanNote").textContent = people.length
    ? fmtPct(100*ran/people.length) + " of named people" : "";
  $("#kUsActive").textContent = fmtInt(cnt("active"));
  $("#kUsActiveNote").textContent = people.length
    ? fmtPct(100*cnt("active")/people.length) + " of named people" : "";
  $("#kUsSlip").textContent = fmtInt(cnt("slipping"));
  $("#kUsDormant").textContent = fmtInt(cnt("dormant"));

  // Anonymous: weekly runs and distinct conversations, clipped to the range. The chart
  // stays weekly, so the first and last bars may include days outside a custom range.
  const wLo = usWeekIdx(from), wHi = usWeekIdx(to), nW = wHi - wLo + 1;
  const aRuns = new Array(nW).fill(0), aConvSets = Array.from({length:nW}, () => new Set());
  if (anon){
    for (const [d,n] of usIdx.runs[anon.i]){
      const day = U.dates[d];
      if (day >= from && day <= to) aRuns[usIdx.wkOf[d] - wLo] += n;
    }
  }
  const anonConvs = new Set();
  for (const [d,c] of U.convTuples){
    const day = U.dates[d];
    if (day >= from && day <= to){ aConvSets[usIdx.wkOf[d] - wLo].add(c); anonConvs.add(c); }
  }
  if (anon){
    const total = people.reduce((s,r) => s+r.runs, 0) + anon.runs;
    let cmp = "";
    const len = usDiff(to, from) + 1, pFrom = usAddDays(from, -len), pTo = usAddDays(from, -1);
    if (pFrom >= U.dates[0]){
      const prev = usCompute(pFrom, pTo).find(r => r.anon);
      cmp = ` The ${len} days before (${pFrom} → ${pTo}): ${fmtInt(prev ? prev.runs : 0)} runs.`;
    }
    $("#usAnonNote").textContent =
      `${fmtInt(anon.runs)} runs in range` + (total ? ` (${fmtPct(100*anon.runs/total)} of all runs)` : "")
      + `, ${fmtInt(anonConvs.size)} conversations; last run on or before ${to}: `
      + `${anon.last} (${anon.since} days earlier).` + cmp
      + " This is one shared login, so conversations are the closest proxy for sessions — "
      + "it cannot be turned into a head count. Bars are whole weeks.";
  } else {
    $("#usAnonNote").textContent = "No anonymous activity by the end of this range.";
  }
  drawUsAnon(aRuns, aConvSets.map(s => s.size), wLo, wHi);

  const q = usState.query.trim().toLowerCase();
  let rows = people.filter(r =>
    (!usState.status || r.status === usState.status)
    && (!q || r.name.toLowerCase().includes(q)));
  const k = usState.sortKey, dir = usState.sortDir;
  rows.sort((a,b) => {
    if (k === "since"){
      // Nobody-ran-in-range first: that is the group this view exists to find.
      const za = a.runs === 0, zb = b.runs === 0;
      // dir = -1 (the default, longest-quiet first) puts them first; +1 puts them last.
      if (za !== zb) return za ? dir : -dir;
    }
    let x = k === "status" ? US_RANK[a.status] : a[k];
    let y = k === "status" ? US_RANK[b.status] : b[k];
    const c = typeof x === "string" ? x.localeCompare(y) : x - y;
    return dir*c || a.name.localeCompare(b.name);
  });
  if (anon && (!usState.status || anon.status === usState.status) && !q) rows.unshift(anon);
  usShown = {rows, from, to};

  const partialIdx = U.partialWeek - wLo;
  const tb = $("#tUsers tbody"); tb.replaceChildren();
  for (const r of rows){
    const tr = document.createElement("tr");
    if (r.anon) tr.className = "anonrow";
    const cell = (v, num) => {
      const td = document.createElement("td"); td.textContent = v;
      if (num) td.style.textAlign = "right"; return td; };
    tr.appendChild(cell(r.anon ? "Anonymous (shared login)" : r.name));
    const st = document.createElement("td"), sp = document.createElement("span");
    sp.className = "st " + r.status; sp.textContent = US_LABEL[r.status];
    st.appendChild(sp);
    if (r.runs === 0){
      const none = document.createElement("span");
      none.className = "cnote"; none.textContent = " · no activity in range";
      st.appendChild(none);
    }
    tr.appendChild(st);
    tr.appendChild(cell(fmtInt(r.since), true));
    tr.appendChild(cell(r.last)); tr.appendChild(cell(r.first));
    tr.appendChild(cell(fmtInt(r.days), true)); tr.appendChild(cell(fmtInt(r.runs), true));
    tr.appendChild(cell(fmtInt(r.agents), true));
    const sc = document.createElement("td");
    sc.appendChild(usSpark(r.wk.slice(wLo, wHi+1), partialIdx)); tr.appendChild(sc);
    tb.appendChild(tr);
  }
  $("#usCount").textContent = `${fmtInt(rows.filter(r => !r.anon).length)} of `
    + `${fmtInt(people.length)} named people shown. Weekly runs: oldest week on the `
    + `left (${U.weeks[wLo]}), newest on the right; the latest week may be partial. `
    + `Last run and days-since look back over all history up to ${to}.`;
  document.querySelectorAll("#tUsers th[data-uk]").forEach(th => {
    th.removeAttribute("aria-sort");
    if (th.dataset.uk === usState.sortKey)
      th.setAttribute("aria-sort", usState.sortDir < 0 ? "descending" : "ascending");
  });
}

function switchView(view){
  document.querySelectorAll(".tabs button").forEach(b =>
    b.setAttribute("aria-selected", String(b.dataset.view === view)));
  $("#viewAdoption").classList.toggle("hidden", view !== "adoption");
  $("#viewFeedback").classList.toggle("hidden", view !== "feedback");
  $("#viewEpl").classList.toggle("hidden", view !== "epl");
  $("#viewUsers").classList.toggle("hidden", view !== "users");
  // The global filter bar drives the adoption and feedback views only; leaving it (or
  // the chip that describes it) on screen for a tab that ignores it invites reading
  // filtered numbers off this tab.
  $("#filters").classList.toggle("hidden", view === "epl" || view === "users");
  $("#slicechip").classList.toggle("hidden", view === "epl" || view === "users");
  state.view = view;
  render();
}

/* ---------- render ---------- */
function render(){
  if (state.view === "epl"){ renderEpl(); return; }
  if (state.view === "users"){ renderUsers(); return; }
  if (state.view === "feedback"){
    // Charts need a laid-out container to measure, so the hidden view is never
    // drawn -- switchView re-renders on the way in.
    renderFeedback();
    renderSlice();
    return;
  }
  const agg = aggregate();
  const A = agg.A;

  $("#kRuns").textContent = fmtInt(A.runs);
  $("#kRunsNote").textContent = `${fmtCompact(A.tokens)} tokens · `
    + `${fmtInt(A.runs && agg.months.length ? A.runs/agg.months.length : 0)} runs/month avg`;
  $("#kUsers").textContent = fmtInt(agg.users);
  $("#kUsersNote").textContent = agg.users
    ? (A.runs/agg.users).toFixed(0) + " runs per user" : "no activity in slice";
  $("#kCost").textContent = fmtMoney(A.cost);
  $("#kCostNote").textContent = A.runs
    ? "$" + (A.cost/A.runs).toFixed(4) + " per run" : "—";
  const failPct = A.runs ? 100*A.fails/A.runs : 0;
  $("#kFail").textContent = A.runs ? fmtPct(failPct) : "—";
  $("#kFailNote").textContent = fmtInt(A.fails) + " failed runs";
  $("#kAgents").textContent = fmtInt(agg.agents);
  $("#kAgentsNote").textContent = A.durN
    ? (A.durMs/A.durN/1000).toFixed(1) + "s avg run" : "—";

  renderSlice();

  const lastMonth = agg.months[agg.months.length-1];
  const partial = lastMonth && D.days[D.days.length-1].slice(0,7) === lastMonth
                  && D.days[D.days.length-1].slice(8) !== "31";
  const partialNote = partial
    ? ` · ${lastMonth} is partial (through ${D.days[D.days.length-1]})` : "";
  $("#c1note").textContent =
    "Colours are fixed per portfolio, so filtering never repaints a series"
    + partialNote;
  $("#c2note").textContent =
    "Exact distinct counts, not a sum of per-product users" + partialNote;

  drawLines(agg);
  drawUsers(agg);

  const prodRows = [...agg.byProduct.entries()]
    .map(([i,r]) => ({i, ...r})).sort((a,b) => b.runs-a.runs);
  const totalRuns = A.runs || 1;
  const s1 = cssVar("--s1"), de = cssVar("--deemph");

  drawBars({
    svg:"#c3", table:"tv3",
    rows: prodRows.map(r => ({
      label: D.products[r.i], value: r.runs,
      color: D.prodFocus[r.i] ? s1 : de,
      flag: false,
      tip: [{value: fmtInt(r.runs), label:"runs", color: D.prodFocus[r.i] ? s1 : de},
            {value: (100*r.runs/totalRuns).toFixed(2)+"%", label:"of slice"},
            {value: fmtMoney(r.cost), label:"cost"}],
    })),
    fmtAxis: fmtCompact, fmtValue: v => fmtCompact(v),
    tableHead:["Product","Runs","% of slice","Cost"],
    tableRow: r => [r.label, fmtInt(r.value),
                    (100*r.value/totalRuns).toFixed(2)+"%",
                    r.tip[2].value],
  });

  const big = prodRows.filter(r => r.runs >= 100);
  drawBars({
    svg:"#c4", table:"tv4",
    rows: big.map(r => ({
      label: D.products[r.i], value: r.cost/r.runs,
      color: D.prodFocus[r.i] ? s1 : de, flag:false,
      tip: [{value:"$"+(r.cost/r.runs).toFixed(4), label:"per run",
             color: D.prodFocus[r.i] ? s1 : de},
            {value: fmtMoney(r.cost), label:"total cost"},
            {value: fmtInt(r.runs), label:"runs"}],
    })).sort((a,b) => b.value-a.value),
    fmtAxis: v => "$"+v.toFixed(2), fmtValue: v => "$"+v.toFixed(3),
    tableHead:["Product","$/run","Total cost","Runs"],
    tableRow: r => [r.label, "$"+r.value.toFixed(4), r.tip[1].value, r.tip[2].value],
  });

  drawBars({
    svg:"#c5", table:"tv5",
    rows: big.map(r => ({
      label: D.products[r.i], value: 100*r.fails/r.runs,
      color: D.prodFocus[r.i] ? s1 : de,
      flag: 100*r.fails/r.runs > 10,
      tip: [{value:(100*r.fails/r.runs).toFixed(2)+"%", label:"failure rate",
             color: D.prodFocus[r.i] ? s1 : de},
            {value: fmtInt(r.fails), label:"failed runs"},
            {value: fmtInt(r.runs), label:"total runs"}],
    })).sort((a,b) => b.value-a.value),
    fmtAxis: v => v.toFixed(0)+"%", fmtValue: v => v.toFixed(1)+"%",
    tableHead:["Product","Failure %","Failed","Runs"],
    tableRow: r => [r.label, r.value.toFixed(2)+"%", r.tip[1].value, r.tip[2].value],
  });

  renderAgents();
  renderSideTables();
}

/* ---------- wiring ---------- */
function initFilters(){
  $("#gen").textContent = D.generated;
  $("#covdays").textContent =
    `${D.days[0]} → ${D.days[D.days.length-1]} · ${fmtInt(D.agents.length)} agents with traffic`;
  $("#fnote").textContent =
    `Visual-workflow executions are collected by fanning out over `
    + `/api/visual-workflows/{id}/history — Foundry has no aggregate workflow-run log. `
    + `They carry no token, cost or user data, so they appear only in their own panel. `
    + `Team runs (${fmtInt(D.teamRuns)}) are in the warehouse but not surfaced here yet.`;

  const pf = $("#fPortfolio");
  for (const p of D.portfolios){
    const o = document.createElement("option"); o.value = p; o.textContent = p;
    pf.appendChild(o);
  }
  const pr = $("#fProduct");
  const refreshProducts = () => {
    const cur = state.product;
    pr.replaceChildren();
    const all = document.createElement("option");
    all.value = ""; all.textContent = "All products"; pr.appendChild(all);
    D.products.forEach((p,i) => {
      if (state.portfolio && D.portfolios[D.prodPortfolio[i]] !== state.portfolio) return;
      const o = document.createElement("option"); o.value = p; o.textContent = p;
      pr.appendChild(o);
    });
    pr.value = [...pr.options].some(o => o.value === cur) ? cur : "";
    state.product = pr.value;
  };
  refreshProducts();

  $("#fRange").onchange = e => { state.range = e.target.value; render(); };
  pf.onchange = e => { state.portfolio = e.target.value; refreshProducts(); render(); };
  pr.onchange = e => { state.product = e.target.value; render(); };
  $("#fFocus").onclick = e => {
    state.focus = !state.focus;
    e.target.setAttribute("aria-pressed", String(state.focus)); render();
  };
  $("#fSandbox").onclick = e => {
    state.sandbox = !state.sandbox;
    e.target.setAttribute("aria-pressed", String(state.sandbox));
    e.target.textContent = state.sandbox ? "Incl. non-product" : "Excl. non-product";
    render();
  };
  $("#fTheme").onclick = e => {
    const dark = document.documentElement.getAttribute("data-theme") === "dark";
    document.documentElement.setAttribute("data-theme", dark ? "light" : "dark");
    e.target.textContent = dark ? "Dark" : "Light";
    render();
  };
  $("#fAgent").oninput = e => { state.agentQuery = e.target.value; renderAgents(); };
  const agentTenants = [...new Set(D.agents.map(a => a.tenant).filter(Boolean))].sort();
  const tenSelAgent = $("#fAgentTenant");
  for (const t of agentTenants){
    const o = document.createElement("option"); o.value = t; o.textContent = t;
    tenSelAgent.appendChild(o);
  }
  tenSelAgent.onchange = e => { state.agentTenant = e.target.value; renderAgents(); };

  /* ---- feedback tab ---- */
  document.querySelectorAll(".tabs button").forEach(b => {
    b.onclick = () => switchView(b.dataset.view);
  });
  $("#tabFbCount").textContent = fmtInt(D.fbTurns.length);

  const labelSel = $("#fFbLabel");
  const present = new Set(D.fbTurns.map(t => t.l).filter(Boolean));
  for (const k of D.fbLabels){
    if (!present.has(k)) continue;
    const o = document.createElement("option");
    o.value = k; o.textContent = labelText(k);
    labelSel.appendChild(o);
  }
  if (!present.size) labelSel.disabled = true;

  // Tenant options carry their thumbs-down count, so the list doubles as a ranking
  // of which deployments are complaining. Ordered by that count, not alphabetically.
  const tenSel = $("#fTenant");
  const negByTenant = new Map();
  for (const t of D.fbTurns)
    negByTenant.set(t.tn, (negByTenant.get(t.tn) || 0) + 1);
  const tenOrder = [...negByTenant.entries()].sort((a,b) => b[1]-a[1]);
  for (const [ix, n] of tenOrder){
    const o = document.createElement("option");
    o.value = String(ix);
    o.textContent = `${tenantName(ix)} (${fmtInt(n)})`;
    tenSel.appendChild(o);
  }
  const collisions = new Map();
  for (const a of D.fbAgents)
    collisions.set(a.name, (collisions.get(a.name) || 0) + 1);
  const dupNames = [...collisions.values()].filter(n => n > 1).length;
  $("#fTenantNote").textContent =
    `${fmtInt(tenOrder.length)} tenants have feedback`
    + (dupNames ? ` · ${dupNames} agent names are reused across them` : "");

  const resetPage = () => { fbState.page = 1; };
  tenSel.onchange = e => {
    fbState.tenant = e.target.value; resetPage(); renderFeedback(); };
  $("#fFbQuery").oninput = e => {
    fbState.query = e.target.value; resetPage(); renderFeedback(); };
  labelSel.onchange = e => {
    fbState.label = e.target.value; resetPage(); renderFeedback(); };
  $("#fFbSev").onchange = e => {
    fbState.sev = e.target.value; resetPage(); renderFeedback(); };
  $("#fFbCommented").onclick = e => {
    fbState.commentedOnly = !fbState.commentedOnly;
    e.target.setAttribute("aria-pressed", String(fbState.commentedOnly));
    e.target.textContent = fbState.commentedOnly ? "With comment" : "All thumbs-down";
    resetPage(); renderFeedback();
  };
  $("#fbMore").onclick = () => { fbState.page++; renderFeedback(); };

  /* ---- EPL solutions tab ---- */
  $("#tabEplCount").textContent = fmtInt(E.deps.length);
  const eplSel = $("#fEplScope");
  for (const t of E.typeOrder){
    const members = E.deps.filter(d => d.type === t);
    if (!members.length) continue;
    const o = document.createElement("option");
    o.value = "type:" + t;
    o.textContent = `${t} (${members.length})`;
    eplSel.appendChild(o);
  }
  // Individual deployments, busiest first, so the list is useful rather than alphabetical.
  const byConvs = E.deps.map((d,i) => [i,d]).sort((a,b) => b[1].convs - a[1].convs);
  for (const [i,d] of byConvs){
    const o = document.createElement("option");
    o.value = "dep:" + i;
    o.textContent = `   ${d.name} — ${fmtInt(d.convs)} convs`;
    eplSel.appendChild(o);
  }
  eplSel.onchange = e => { eplState.scope = e.target.value; renderEpl(); };

  /* ---- Users tab ---- */
  $("#tabUsersCount").textContent = fmtInt(U.rows.filter(r => !r.anon).length);
  const usFrom = $("#fUsFrom"), usTo = $("#fUsTo"), usRange = $("#fUsRange");
  usFrom.min = usTo.min = U.dates[0]; usFrom.max = usTo.max = U.asOf;
  usRange.onchange = e => {
    const v = e.target.value;
    if (v === "custom") return;
    usState.to = U.asOf;
    usState.from = v === "all" ? U.dates[0]
      : (d => d < U.dates[0] ? U.dates[0] : d)(usAddDays(U.asOf, -(+v - 1)));
    renderUsers();
  };
  const usCustom = () => {
    let f = usFrom.value || U.dates[0], t = usTo.value || U.asOf;
    if (f > t) [f, t] = [t, f];                     // a reversed pair is a slip, not an error
    usState.from = f; usState.to = t;
    usRange.value = "custom";
    renderUsers();
  };
  usFrom.onchange = usCustom; usTo.onchange = usCustom;
  $("#fUsExport").onclick = exportUsersCsv;
  $("#fUsStatus").onchange = e => { usState.status = e.target.value; renderUsers(); };
  $("#fUsQuery").oninput = e => { usState.query = e.target.value; renderUsers(); };
  document.querySelectorAll("#tUsers th[data-uk]").forEach(th => {
    th.onclick = () => {
      const k = th.dataset.uk;
      if (usState.sortKey === k) usState.sortDir *= -1;
      else { usState.sortKey = k;
             usState.sortDir = (k === "name" || k === "first" || k === "last"
                                || k === "status") ? 1 : -1; }
      renderUsers();
    };
  });

  $("#eplExpandAll").onclick = () => {
    // Expand-all applies to the deployments currently in scope, not all nine, so the
    // button never leaves rows open that the next scope change hides.
    const inScope = [...eplDepSet()];
    if (inScope.some(i => eplState.open.has(i))) eplState.open.clear();
    else inScope.forEach(i => eplState.open.add(i));
    renderEplDeps(eplDepSet());
  };

  document.querySelectorAll("#tEplDeps th").forEach(th => {
    th.onclick = () => {
      const k = th.dataset.ek;
      if (!k) return;
      if (eplState.sortKey === k) eplState.sortDir *= -1;
      else { eplState.sortKey = k;
             eplState.sortDir = (k === "name" || k === "type" || k === "first"
                                 || k === "last") ? 1 : -1; }
      renderEpl();
    };
  });

  document.querySelectorAll("#tfba th").forEach(th => {
    th.onclick = () => {
      const k = th.dataset.fk;
      if (!k) return;
      if (fbState.sortKey === k) fbState.sortDir *= -1;
      else { fbState.sortKey = k;
             fbState.sortDir = (k === "name" || k === "product" || k === "tenant"
                                || k === "last")
                               ? 1 : -1; }
      renderFbScorecard();
    };
  });

  document.querySelectorAll("[data-table]").forEach(b => {
    b.onclick = () => {
      const host = $("#"+b.dataset.table);
      const on = host.classList.toggle("hidden") === false;
      b.setAttribute("aria-pressed", String(on));
      b.textContent = on ? "Chart" : "Table";
      const svg = b.closest(".card").querySelector("svg");
      if (svg) svg.classList.toggle("hidden", on);
      const lg = b.closest(".card").querySelector(".legend");
      if (lg) lg.classList.toggle("hidden", on);
    };
  });

  document.querySelectorAll("#tagents th").forEach(th => {
    th.onclick = () => {
      const k = th.dataset.k;
      if (state.sortKey === k) state.sortDir *= -1;
      else { state.sortKey = k; state.sortDir = (k === "name" || k === "product") ? 1 : -1; }
      renderAgents();
    };
  });

  addEventListener("resize", () => { clearTimeout(window.__rt);
    window.__rt = setTimeout(render, 140); });
}

initFilters();
render();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    payload = build_payload(conn)

    # </script> inside the JSON would close the host <script> tag early.
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html = HTML.replace("__PAYLOAD__", blob)
    with open(args.out, "w") as fh:
        fh.write(html)

    kb = os.path.getsize(args.out) / 1024
    print(f"wrote {args.out}  ({kb:,.0f} KB)")
    print(f"  products={len(payload['products'])} portfolios={len(payload['portfolios'])} "
          f"days={len(payload['days'])} facts={len(payload['facts'])}")
    print(f"  userTuples={len(payload['userTuples'])} "
          f"agentTuples={len(payload['agentTuples'])} agents={len(payload['agents'])}")


if __name__ == "__main__":
    main()
