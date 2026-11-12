#!/usr/bin/env python3
"""Forge POC: the Adoption and Negative-feedback tabs on Tyler Forge components.

Deliberately a separate generator from build_dashboard.py so the shipping
dashboard keeps working while we evaluate. Reuses build_dashboard.build_payload
verbatim -- the data layer is untouched.

The chart maths (aggregate/niceScale/axisFmt/drawLines/drawUsers/drawBars) is
PORTED VERBATIM from dashboard.html rather than reimplemented. Reimplementing it
during the feedback-tab POC produced two silent numeric bugs; the only edits here
are `D` -> `P` and CSS variables -> Forge design tokens.

Run:  python3 build_forge_poc.py && python3 -m http.server 8765
Then: http://localhost:8765/dashboard-forge.html
Check: node verify_forge_poc.js

Assets are vendored, so the page works offline:
  forge-bundle.js  -- npx esbuild forge-entry.js --bundle --format=esm --minify \
                        --outfile=forge-bundle.js
  forge{,-core,-dark}.css -- copied from node_modules/@tylertech/forge/dist/
"""

import json
import sqlite3
from pathlib import Path

import build_dashboard as bd

DB = Path(__file__).parent / "foundry_analytics.db"
OUT = Path(__file__).parent / "dashboard-forge.html"


def epl_complaints(conn, deps):
    """Re-read the EPL complaints WITH the conversation id and turn index.

    `build_epl` deliberately selects neither, so its complaint cards have no route
    back to the transcript. Rather than edit the shipping generator, this rebuilds
    the same list from the same view with two extra columns.

    The list is rebuilt rather than positionally zipped onto the original: the
    shipping query orders only by `conv_day DESC`, so ties have no defined order and
    two executions could interleave them differently -- which would silently attach
    the wrong conversation id to a quote. Ordering here is fully deterministic.
    """
    d_ix = {d["name"]: i for i, d in enumerate(deps)}
    names = ",".join("?" * len(bd.EPL_DEPLOYMENTS))
    cur = conn.cursor()
    rows = cur.execute(f"""
        SELECT tenant_name t, agent_name_current a, conv_day d, label l, severity s,
               fix_type f, comment c, question q, conversation_id cid, turn_index ti
        FROM v_feedback_turns
        WHERE rating = 'THUMBS_DOWN' AND product = ?
              AND tenant_name IN ({names})
        ORDER BY conv_day DESC, conversation_id, turn_index
    """, [bd.EPL_PRODUCT] + list(bd.EPL_DEPLOYMENTS))

    out = []
    for r in rows:
        if r["t"] not in d_ix:      # not a shipped deployment
            continue
        out.append({
            "dep": d_ix[r["t"]], "a": r["a"] or "(unnamed)", "d": r["d"],
            "l": r["l"] or "", "s": r["s"] or "", "f": r["f"] or "",
            "c": (r["c"] or "")[:bd.FB_COMMENT_CHARS],
            "q": (r["q"] or "")[:bd.FB_QUESTION_CHARS],
            # Never truncated: a partial conversation id cannot be looked up.
            "cid": r["cid"] or "", "t": r["ti"],
        })
    return out


def poc_payload(conn):
    """Full payload with the feedback question/answer text truncated.

    `q`, `r` and `why` are the bulk of the page weight, so they are clipped and the
    UI marks a clipped value with a trailing ellipsis. `cid` is NEVER truncated --
    it is the conversation id behind the copy button, and a partial id is useless.
    The turn index `t` rides along so a reader can find the exact turn.

    Everything the Adoption tab needs -- notably the 9-column `facts` rows and the
    presence tuples -- is kept intact, because distinct user/agent counts cannot be
    recovered from sums.
    """
    P = bd.build_payload(conn)

    def clip(s, n):
        s = s or ""
        return s if len(s) <= n else s[:n].rstrip() + "…"

    turns = [{
        "d": t.get("d"), "p": t.get("p"), "a": t.get("a"), "tn": t.get("tn"),
        "c": t.get("c") or "", "l": t.get("l") or "", "s": t.get("s") or "",
        "f": t.get("f") or "", "cf": t.get("cf"),
        "cid": t.get("cid") or "", "t": t.get("t"),
        "q": clip(t.get("q"), 320),
        "r": clip(t.get("r"), 320),
        "why": clip(t.get("why"), 400),
    } for t in P["fbTurns"]]

    P["epl"]["complaints"] = epl_complaints(conn, P["epl"]["deps"])

    keep = ("generated", "days", "facts", "products", "portfolios", "prodPortfolio",
            "prodFocus", "portfolioSlot", "userTuples", "agentTuples", "agents",
            "errors", "dormant", "builtAgents", "wfRuns", "wfAdoption", "gaps",
            "seriesLight", "seriesDark", "tenants", "fbCounts", "fbAgents",
            "fbLabels", "nUsers", "nAgents",
            # The EPL tab is self-contained: `epl` carries its own weeks, facts and
            # presence tuples, independent of the global day/product indices.
            "epl")
    out = {k: P[k] for k in keep if k in P}
    out["fbTurns"] = turns
    return out


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Foundry Adoption &amp; Usage — Forge POC</title>
<link rel="stylesheet" href="./forge-core.css">
<link rel="stylesheet" href="./forge.css">
<!-- forge-dark.css redefines the tokens on :root unconditionally, so it cannot be
     gated behind a class. Ship it disabled and toggle the sheet itself. -->
<link rel="stylesheet" href="./forge-dark.css" id="darkCss" disabled>
<style>
  /* forge-core.css pins body to 100dvh, which is the forge-scaffold contract:
     the scaffold owns the viewport and its body slot scrolls internally. */
  .page { padding: 14px 20px 40px; max-width: 1680px; margin: 0 auto; }

  /* Charts stay hand-rolled SVG -- Forge has no charting components -- but every
     colour and font here is a Forge token, so they track the theme. */
  /* Token choices matched to the originals by resolved value, not by name:
       --grid   #e1e0d9 -> --forge-theme-outline                    (#e0e0e0)
       --axis   #c3c2b7 -> --forge-theme-surface-container-medium    (#c2c2c2)
       --deemph #c3c2b7 -> --forge-theme-surface-container-medium    (see DEEMPH below)
     outline-low is #9e9e9e and text-lowest is only 12% alpha, so neither works for a
     gridline or a bar fill. Gridlines are solid, as in the original. */
  .ax  { stroke: var(--forge-theme-surface-container-medium); stroke-width: 1; }
  .gl  { stroke: var(--forge-theme-outline); stroke-width: 1; }
  .tick   { fill: var(--forge-theme-text-low); font-size: 10.5px;
            font-variant-numeric: tabular-nums; }
  .catlab { fill: var(--forge-theme-text-medium); font-size: 11.5px; }
  .vlab   { fill: var(--forge-theme-text-high); font-size: 11px; font-weight: 600;
            font-variant-numeric: tabular-nums; }
  .hit { fill: transparent; cursor: crosshair; }
  .crosshair { stroke: var(--forge-theme-surface-container-medium); stroke-width: 1;
               opacity: 0; pointer-events: none; }
  /* Let the SVG fill its card; the JS measures the rendered box, not clientWidth
     (which reads 0/stale before the Forge card has upgraded and laid out). */
  svg.chart { width: 100%; display: block; }

  /* Floating data tooltip. Forge's tooltip anchors to an element and shows a
     string, so it can't carry a multi-row series readout -- this stays custom. */
  #tip { position: fixed; z-index: 50; pointer-events: none; opacity: 0;
         transition: opacity .09s; background: var(--forge-theme-surface-bright);
         border: 1px solid var(--forge-theme-outline-low); border-radius: 6px;
         padding: 8px 10px; box-shadow: 0 6px 20px rgba(0,0,0,.16);
         font-size: 12px; min-width: 128px; }
  #tip .th { font-weight: 600; margin-bottom: 5px;
             color: var(--forge-theme-text-high); }
  #tip .tr { display: flex; align-items: center; gap: 6px; line-height: 1.7;
             color: var(--forge-theme-text-medium); }
  #tip .tr i { width: 9px; height: 9px; border-radius: 2px; flex: none; }
  #tip .tr b { font-variant-numeric: tabular-nums; font-weight: 600;
               color: var(--forge-theme-text-high); }

  .kpis { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px;
          margin-bottom: 14px; }
  .kpis.kpis-4 { grid-template-columns: repeat(4, 1fr); }
  .kpis forge-card { padding: 14px 16px; }
  .kpi-val { font-size: 26px; font-weight: 600; line-height: 1.1;
             font-variant-numeric: tabular-nums; color: var(--forge-theme-text-high); }
  .kpi-hero .kpi-val { color: var(--forge-theme-primary); }
  .fb-hero .kpi-val { color: var(--forge-theme-error); }
  .kpi-lab { font-size: 11.5px; color: var(--forge-theme-text-medium);
             margin-top: 5px; }
  .kpi-note { font-size: 11.5px; color: var(--forge-theme-text-low); margin-top: 3px; }

  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
           margin-bottom: 12px; }
  forge-card { padding: 16px; }
  .chead { display: flex; justify-content: space-between; align-items: flex-start;
           gap: 12px; margin-bottom: 8px; }
  .ctitle { font-size: 14px; font-weight: 600; color: var(--forge-theme-text-high); }
  .cnote { font-size: 11.5px; color: var(--forge-theme-text-low); max-width: 64ch;
           margin-top: 3px; }

  .filters { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap;
             margin: 4px 0 12px; }
  .filters forge-select, .filters forge-text-field { min-width: 172px; }
  .slicechip { font-size: 11.5px; color: var(--forge-theme-text-low);
               margin-bottom: 12px; }

  .fblist { display: flex; flex-direction: column; gap: 10px; margin-top: 10px; }
  .fbitem { border: 1px solid var(--forge-theme-outline-low); border-radius: 6px;
            padding: 12px 14px; background: var(--forge-theme-surface); }
  .fbmeta { display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
            font-size: 11.5px; color: var(--forge-theme-text-low);
            margin-bottom: 6px; }
  .fbcomment { font-size: 13px; color: var(--forge-theme-text-high);
               margin-bottom: 6px; }
  .fbwhy { font-size: 12px; color: var(--forge-theme-text-medium);
           border-left: 2px solid var(--forge-theme-outline); padding-left: 10px; }
  /* Asked/Answered excerpts. Clamped so one verbose answer can't push the next row
     off the screen; the copy button is the route to the untruncated transcript. */
  .fbqr { display: flex; flex-direction: column; gap: 4px; margin-bottom: 7px; }
  .fbqr div { font-size: 12px; color: var(--forge-theme-text-medium);
              display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
              overflow: hidden; }
  .fbqr b { color: var(--forge-theme-text-high); font-weight: 600; }
  .fbcid { margin-left: auto; flex: none; }
  .fbcid::part(root) { font-variant-numeric: tabular-nums; }
  .fbempty { font-size: 12.5px; color: var(--forge-theme-text-low);
             padding: 18px 2px; }

  .focusdot { display: inline-block; width: 6px; height: 6px; border-radius: 50%;
              background: var(--forge-theme-primary); margin-right: 6px;
              vertical-align: middle; }
  .hidden { display: none !important; }
  .tablewrap { overflow: auto; max-height: 520px; }
  .tablewrap.short { max-height: 300px; }
</style>
</head>
<body>
<forge-scaffold>
  <forge-app-bar slot="header" title-text="Foundry Adoption &amp; Usage">
    <forge-icon-button slot="end" aria-label="Toggle dark theme" id="themeBtn"
                       aria-pressed="false">
      <forge-icon name="brightness_6"></forge-icon>
    </forge-icon-button>
  </forge-app-bar>

  <div slot="body" class="page">
    <forge-tab-bar active-tab="0" id="tabs">
      <forge-tab>Adoption &amp; cost</forge-tab>
      <forge-tab>Negative feedback <forge-badge id="tabFbCount"></forge-badge></forge-tab>
      <forge-tab>EPL solutions <forge-badge id="tabEplCount"></forge-badge></forge-tab>
    </forge-tab-bar>

    <!-- Global filters: shared by both converted tabs, exactly as in
         dashboard.html where they sit outside the view panels. -->
    <div class="filters" id="globalFilters">
      <forge-select id="fRange" label="Date range" value="all">
        <forge-option value="30">Last 30 days</forge-option>
        <forge-option value="90">Last 90 days</forge-option>
        <forge-option value="mtd">Month to date</forge-option>
        <forge-option value="all">All time</forge-option>
      </forge-select>
      <forge-select id="fPortfolio" label="Portfolio">
        <forge-option value="">All portfolios</forge-option>
      </forge-select>
      <forge-select id="fProduct" label="Product">
        <forge-option value="">All products</forge-option>
      </forge-select>
      <forge-button-toggle-group id="fFocus" aria-label="Scope">
        <forge-button-toggle value="focus">Focus only</forge-button-toggle>
      </forge-button-toggle-group>
      <forge-button-toggle-group id="fSandbox" aria-label="Sandbox">
        <forge-button-toggle value="sandbox">Incl. non-product</forge-button-toggle>
      </forge-button-toggle-group>
      <forge-select id="fTenant" label="Tenant" class="hidden">
        <forge-option value="">All tenants</forge-option>
      </forge-select>
    </div>
    <div class="slicechip" id="slicechip"></div>

    <!-- ================= Adoption & cost ================= -->
    <div id="viewAdoption">
      <div class="kpis">
        <forge-card class="kpi-hero">
          <div class="kpi-val" id="kRuns">—</div>
          <div class="kpi-lab">Agent runs</div>
          <div class="kpi-note" id="kRunsNote"></div>
        </forge-card>
        <forge-card>
          <div class="kpi-val" id="kUsers">—</div>
          <div class="kpi-lab">Distinct users</div>
          <div class="kpi-note" id="kUsersNote"></div>
        </forge-card>
        <forge-card>
          <div class="kpi-val" id="kCost">—</div>
          <div class="kpi-lab">Token cost</div>
          <div class="kpi-note" id="kCostNote"></div>
        </forge-card>
        <forge-card>
          <div class="kpi-val" id="kFail">—</div>
          <div class="kpi-lab">Failure rate</div>
          <div class="kpi-note" id="kFailNote"></div>
        </forge-card>
        <forge-card>
          <div class="kpi-val" id="kAgents">—</div>
          <div class="kpi-lab">Active agents</div>
          <div class="kpi-note" id="kAgentsNote"></div>
        </forge-card>
      </div>

      <div class="grid2">
        <forge-card>
          <div class="chead">
            <div><div class="ctitle">Runs per month by portfolio</div>
                 <div class="cnote" id="c1note"></div></div>
            <forge-button-toggle-group data-toggle="tv1">
              <forge-button-toggle value="table">Table</forge-button-toggle>
            </forge-button-toggle-group>
          </div>
          <forge-chip-set type="filter" id="lg1"></forge-chip-set>
          <svg id="c1" class="chart" height="250" role="img"
               aria-label="Runs per month by portfolio"></svg>
          <div id="tv1" class="tablewrap short hidden"><forge-table dense></forge-table></div>
        </forge-card>
        <forge-card>
          <div class="chead">
            <div><div class="ctitle">Distinct users per month</div>
                 <div class="cnote" id="c2note"></div></div>
            <forge-button-toggle-group data-toggle="tv2">
              <forge-button-toggle value="table">Table</forge-button-toggle>
            </forge-button-toggle-group>
          </div>
          <svg id="c2" class="chart" height="250" role="img"
               aria-label="Distinct users per month"></svg>
          <div id="tv2" class="tablewrap short hidden"><forge-table dense></forge-table></div>
        </forge-card>
      </div>

      <div class="grid2">
        <forge-card>
          <div class="chead">
            <div><div class="ctitle">Share of runs by product</div>
                 <div class="cnote">Focus-scope products in the primary colour,
                   everything else muted</div></div>
            <forge-button-toggle-group data-toggle="tv3">
              <forge-button-toggle value="table">Table</forge-button-toggle>
            </forge-button-toggle-group>
          </div>
          <svg id="c3" class="chart" height="330" role="img"
               aria-label="Share of runs by product"></svg>
          <div id="tv3" class="tablewrap short hidden"><forge-table dense></forge-table></div>
        </forge-card>
        <forge-card>
          <div class="chead">
            <div><div class="ctitle">Cost per run by product</div>
                 <div class="cnote">Products with 100+ runs in the slice</div></div>
            <forge-button-toggle-group data-toggle="tv4">
              <forge-button-toggle value="table">Table</forge-button-toggle>
            </forge-button-toggle-group>
          </div>
          <svg id="c4" class="chart" height="330" role="img"
               aria-label="Cost per run by product"></svg>
          <div id="tv4" class="tablewrap short hidden"><forge-table dense></forge-table></div>
        </forge-card>
      </div>

      <div class="grid2">
        <forge-card>
          <div class="chead">
            <div><div class="ctitle">Failure rate by product</div>
                 <div class="cnote">Products with 100+ runs; ● marks rates above 10%</div></div>
            <forge-button-toggle-group data-toggle="tv5">
              <forge-button-toggle value="table">Table</forge-button-toggle>
            </forge-button-toggle-group>
          </div>
          <svg id="c5" class="chart" height="330" role="img"
               aria-label="Failure rate by product"></svg>
          <div id="tv5" class="tablewrap short hidden"><forge-table dense></forge-table></div>
        </forge-card>
        <forge-card>
          <div class="chead">
            <div><div class="ctitle">Top error classes</div>
                 <div class="cnote">All failures in the current product scope, all time</div></div>
          </div>
          <div class="tablewrap"><forge-table id="terr" dense></forge-table></div>
        </forge-card>
      </div>

      <forge-card style="margin-bottom:12px">
        <div class="chead">
          <div><div class="ctitle">Agent leaderboard</div>
               <div class="cnote">All-time totals per agent; click a column to sort.
                 Rows respect the portfolio, product and scope filters (not the date
                 range).</div></div>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <forge-select id="fAgentTenant" label="Tenant">
              <forge-option value="">All tenants</forge-option>
            </forge-select>
            <forge-text-field>
              <label for="fAgent">Filter agents</label>
              <input id="fAgent" type="search" placeholder="Agent, tenant or product…">
            </forge-text-field>
          </div>
        </div>
        <div class="tablewrap"><forge-table id="tagents" dense></forge-table></div>
      </forge-card>

      <div class="grid2">
        <forge-card>
          <div class="chead">
            <div><div class="ctitle">Visual workflow executions</div>
                 <div class="cnote" id="wfnote"></div></div>
          </div>
          <div class="tablewrap"><forge-table id="twf" dense></forge-table></div>
        </forge-card>
        <forge-card>
          <div class="chead">
            <div><div class="ctitle">Workflows: built vs. ever run</div>
                 <div class="cnote">Definitions per product against executions — all time</div></div>
          </div>
          <div class="tablewrap"><forge-table id="twfa" dense></forge-table></div>
        </forge-card>
      </div>

      <div class="grid2">
        <forge-card>
          <div class="chead">
            <div><div class="ctitle">Dormant agents</div>
                 <div class="cnote">Built but never produced a run — all time, ignores
                   the date filter</div></div>
          </div>
          <div class="tablewrap"><forge-table id="tdorm" dense></forge-table></div>
        </forge-card>
        <forge-card>
          <div class="chead">
            <div><div class="ctitle">Unattributed traffic</div>
                 <div class="cnote">Ranked by runs — fix the taxonomy top-down</div></div>
          </div>
          <div class="tablewrap"><forge-table id="tgaps" dense></forge-table></div>
        </forge-card>
      </div>
    </div>

    <!-- ================= Negative feedback ================= -->
    <div id="viewFeedback" class="hidden">
      <div class="filters">
        <forge-text-field>
          <label for="fQuery">Search</label>
          <input id="fQuery" type="search" placeholder="Comments, questions, agents…">
        </forge-text-field>
        <forge-select id="fLabel" label="Category">
          <forge-option value="">All categories</forge-option>
        </forge-select>
        <forge-select id="fSev" label="Severity">
          <forge-option value="">All severities</forge-option>
          <forge-option value="misleading">Misleading</forge-option>
          <forge-option value="unhelpful">Unhelpful</forge-option>
          <forge-option value="minor">Minor</forge-option>
        </forge-select>
        <forge-button-toggle-group id="fCommented">
          <forge-button-toggle value="commented">With comment</forge-button-toggle>
        </forge-button-toggle-group>
      </div>

      <div class="kpis">
        <forge-card class="fb-hero">
          <div class="kpi-val" id="kNeg">—</div>
          <div class="kpi-lab">Thumbs-down turns</div>
          <div class="kpi-note" id="kNegNote"></div>
        </forge-card>
        <forge-card>
          <div class="kpi-val" id="kNegC">—</div>
          <div class="kpi-lab">With a written comment</div>
          <div class="kpi-note" id="kNegCNote"></div>
        </forge-card>
        <forge-card>
          <div class="kpi-val" id="kNegShare">—</div>
          <div class="kpi-lab">Negative share of rated</div>
          <div class="kpi-note" id="kNegShareNote"></div>
        </forge-card>
        <forge-card>
          <div class="kpi-val" id="kFbRate">—</div>
          <div class="kpi-lab">Feedback submission rate</div>
          <div class="kpi-note" id="kFbRateNote"></div>
        </forge-card>
        <forge-card>
          <div class="kpi-val" id="kLabelled">—</div>
          <div class="kpi-lab">Classified</div>
          <div class="kpi-note" id="kLabelledNote"></div>
        </forge-card>
      </div>

      <div class="grid2">
        <forge-card>
          <div class="chead">
            <div><div class="ctitle">Thumbs-down per month</div>
                 <div class="cnote" id="cf1note"></div></div>
          </div>
          <svg id="cf1" class="chart" height="250" role="img"
               aria-label="Thumbs-down turns per month"></svg>
        </forge-card>
        <forge-card>
          <div class="chead">
            <div><div class="ctitle">Failure categories</div>
                 <div class="cnote">What went wrong, per the classifier</div></div>
          </div>
          <svg id="cf2" class="chart" height="330" role="img"
               aria-label="Failure categories"></svg>
        </forge-card>
      </div>

      <div class="grid2">
        <forge-card>
          <div class="chead">
            <div><div class="ctitle">Negative rate by agent</div>
                 <div class="cnote">Thumbs-down per 100 conversations held. Agents with
                   250+ conversations, so a handful of downvotes on a pilot can't top
                   the chart. ● marks 1+ per 100.</div></div>
          </div>
          <svg id="cf3" class="chart" height="330" role="img"
               aria-label="Negative rate by agent"></svg>
        </forge-card>
        <forge-card>
          <div class="chead">
            <div><div class="ctitle">Where the fix belongs</div>
                 <div class="cnote">Classifier's judgement of what to change, by severity</div></div>
          </div>
          <forge-table id="tfix" dense></forge-table>
        </forge-card>
      </div>

      <forge-card style="margin-bottom:12px">
        <div class="chead">
          <div><div class="ctitle">Agent feedback scorecard</div>
               <div class="cnote">All-time. Conversations is the denominator from the run
                 logs, so rates are comparable across agents of very different size — but
                 unlike the chart above this table has no minimum, so sorting by Neg/100
                 will surface low-traffic agents where a couple of downvotes dominate.
                 Agent names repeat across deployments, so read Agent and Tenant
                 together; a row is one agent in one tenant.</div></div>
        </div>
        <div class="tablewrap"><forge-table id="tfba" dense></forge-table></div>
      </forge-card>

      <forge-card>
        <div class="chead">
          <div><div class="ctitle">Every thumbs-down, with the comment</div>
               <div class="cnote" id="fbnote"></div></div>
        </div>
        <div id="fblist" class="fblist"></div>
        <div style="margin-top:12px">
          <forge-button variant="outlined" id="fbMore" class="hidden">Show more</forge-button>
        </div>
      </forge-card>
    </div>

    <!-- ================= EPL solutions ================= -->
    <div id="viewEpl" class="hidden">
      <div class="filters">
        <!-- Sentinel is "all", not "": forge-select renders nothing for an empty value,
             which would leave this presentation-facing control showing no current
             scope at all. -->
        <forge-select id="fEplScope" label="Show" value="all">
          <forge-option value="all">All shipped deployments</forge-option>
        </forge-select>
      </div>
      <forge-inline-message theme="info" id="eplSelfContained">
        Self-contained: this tab ignores the date, portfolio and product filters so the
        numbers can't shift mid-presentation. They are hidden here rather than left
        visible-but-inert.
      </forge-inline-message>

      <div class="kpis kpis-4" style="margin-top:14px">
        <forge-card class="kpi-hero">
          <div class="kpi-val" id="kEplConvs">—</div>
          <div class="kpi-lab">Conversations</div>
          <div class="kpi-note" id="kEplConvsNote"></div>
        </forge-card>
        <forge-card>
          <div class="kpi-val" id="kEplUsers">—</div>
          <div class="kpi-lab">People using it</div>
          <div class="kpi-note" id="kEplUsersNote"></div>
        </forge-card>
        <forge-card>
          <div class="kpi-val" id="kEplAgents">—</div>
          <div class="kpi-lab">Agents live</div>
          <div class="kpi-note" id="kEplAgentsNote"></div>
        </forge-card>
        <forge-card>
          <div class="kpi-val" id="kEplComplaint">—</div>
          <div class="kpi-lab">Produced a complaint</div>
          <div class="kpi-note" id="kEplComplaintNote"></div>
        </forge-card>
      </div>

      <forge-card style="margin-bottom:12px">
        <div class="chead">
          <div><div class="ctitle">Adoption, week by week</div>
               <div class="cnote" id="cEplNote"></div></div>
          <forge-button-toggle-group data-toggle="tvEpl1">
            <forge-button-toggle value="table">Table</forge-button-toggle>
          </forge-button-toggle-group>
        </div>
        <svg id="cEpl1" class="chart" height="290" role="img"
             aria-label="Conversations and distinct users per week"></svg>
        <div id="tvEpl1" class="tablewrap short hidden"><forge-table dense></forge-table></div>
      </forge-card>

      <forge-card style="margin-bottom:12px">
        <div class="chead">
          <div><div class="ctitle">Shipped: what's live, and where</div>
               <div class="cnote">One row per deployment. <b>Rated convs</b> counts
                 conversations that received any feedback; <b>👍/👎 turns</b> count
                 individual rated replies, so they are a larger number and are not
                 expected to add up to it — one conversation here collected four 👎.
                 <b>Complaint rate</b> is the share of conversations containing at least
                 one 👎, so it never counts the same unhappy conversation twice; “—”
                 means nothing was rated, which is not the same as no
                 complaints.</div></div>
        </div>
        <div class="tablewrap"><forge-table id="tEplDeps" dense></forge-table></div>
        <div class="cnote" id="eplExcluded" style="margin-top:9px"></div>
      </forge-card>

      <forge-card>
        <div class="chead">
          <div><div class="ctitle">What the complaints were</div>
               <div class="cnote" id="eplCxNote"></div></div>
        </div>
        <div class="tablewrap short"><forge-table id="tEplCauses" dense></forge-table></div>
        <div id="eplComplaints" class="fblist"></div>
      </forge-card>
    </div>
  </div>
</forge-scaffold>

<div id="tip" role="status" aria-live="polite"></div>

<script type="module" src="./forge-bundle.js"></script>
<script id="payload" type="application/json">__PAYLOAD__</script>
<script type="module">
const P = JSON.parse(document.getElementById('payload').textContent);
const $ = s => document.querySelector(s);
const NS = 'http://www.w3.org/2000/svg';

/* ---------- formatters: ported verbatim from dashboard.html ---------- */
const nf = new Intl.NumberFormat('en-US');
const fmtInt = n => nf.format(Math.round(n));
const fmtCompact = n => n >= 1e9 ? (n/1e9).toFixed(n>=1e10?0:1)+'B'
                      : n >= 1e6 ? (n/1e6).toFixed(n>=1e7?0:1)+'M'
                      : n >= 1e4 ? (n/1e3).toFixed(0)+'K'
                      : nf.format(Math.round(n));
const fmtMoney = n => n >= 1000 ? '$'+fmtCompact(n) : '$'+n.toFixed(2);
const fmtPct = n => n.toFixed(n < 10 ? 2 : 1) + '%';

/* ---------- filter state ---------- */
const state = {view:'adoption',
               range:'all', portfolio:'', product:'', focus:false, sandbox:true,
               hiddenSeries:new Set(), agentQuery:'', agentTenant:'',
               sortKey:'runs', sortDir:-1,
               tenant:'', q:'', label:'', sev:'', commented:true, limit:40};

const NONPRODUCT = new Set(['Internal Sandbox','Platform Engineering / Internal Tooling',
                            'Unattributed']);

/* Which product indices survive the non-date filters. */
function activeProducts(){
  const out = new Set();
  P.products.forEach((p,i) => {
    if (state.portfolio && P.portfolios[P.prodPortfolio[i]] !== state.portfolio) return;
    if (state.product && p !== state.product) return;
    if (state.focus && !P.prodFocus[i]) return;
    if (!state.sandbox && NONPRODUCT.has(p)) return;
    out.add(i);
  });
  return out;
}

/* Day-index window for the selected range. */
function dayWindow(){
  const n = P.days.length;
  if (state.range === 'all') return [0, n-1];
  if (state.range === 'mtd') {
    const m = P.days[n-1].slice(0,7);
    let i = n-1; while (i > 0 && P.days[i-1].startsWith(m)) i--;
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

  for (const [d,p,runs,convos,cost,tokens,fails,durMs,durN] of P.facts){
    if (d < lo || d > hi || !prods.has(p)) continue;
    A.runs+=runs; A.convos+=convos; A.cost+=cost; A.tokens+=tokens;
    A.fails+=fails; A.durMs+=durMs; A.durN+=durN;

    const month = P.days[d].slice(0,7);
    months.add(month);
    const pf = P.portfolios[P.prodPortfolio[p]];
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
  for (const [u,d,p] of P.userTuples){
    if (d < lo || d > hi || !prods.has(p)) continue;
    users.add(u);
    const m = P.days[d].slice(0,7);
    if (!usersByMonth.has(m)) usersByMonth.set(m, new Set());
    usersByMonth.get(m).add(u);
  }
  for (const [a,d,p] of P.agentTuples){
    if (d < lo || d > hi || !prods.has(p)) continue;
    agents.add(a);
  }

  return {A, months:[...months].sort(), byMonthPortfolio, byProduct,
          users:users.size, agents:agents.size, usersByMonth, prods, lo, hi};
}

/* ---------- tooltip ---------- */
const tip = $('#tip');
function showTip(x, y, title, rows){
  tip.replaceChildren();
  const h = document.createElement('div');
  h.className = 'th'; h.textContent = title; tip.appendChild(h);
  for (const r of rows){
    const tr = document.createElement('div'); tr.className = 'tr';
    if (r.color){ const i = document.createElement('i');
      i.style.background = r.color; tr.appendChild(i); }
    const b = document.createElement('b'); b.textContent = r.value;
    const s = document.createElement('span'); s.textContent = r.label;
    tr.append(b, s); tip.appendChild(tr);
  }
  tip.style.opacity = 1;
  const r = tip.getBoundingClientRect();
  let left = x + 14, top = y + 14;
  if (left + r.width > innerWidth - 8) left = x - r.width - 14;
  if (top + r.height > innerHeight - 8) top = y - r.height - 14;
  tip.style.left = Math.max(8,left)+'px'; tip.style.top = Math.max(8,top)+'px';
}
const hideTip = () => { tip.style.opacity = 0; };

/* ---------- svg helpers ---------- */
function el(tag, attrs){
  const n = document.createElementNS(NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}
function text(x, y, str, cls, anchor){
  const t = el('text', {x, y, class:cls});
  if (anchor) t.setAttribute('text-anchor', anchor);
  t.textContent = str;                       // untrusted labels: never innerHTML
  return t;
}
/* Axis scale. Steps restricted to 1/2/5 x 10^k so every tick label is
   display-clean -- a 12,500 step renders as "13K" and reads as a wrong number. */
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
  const unit = max >= 1e9 ? ['B',1e9] : max >= 1e6 ? ['M',1e6]
             : max >= 1e4 ? ['K',1e3] : null;
  return v => !v ? '0'
    : unit ? (v/unit[1]).toFixed(v % unit[1] ? 1 : 0) + unit[0]
           : nf.format(Math.round(v));
}

const isDark = () => !$('#darkCss').disabled;
function seriesColors(){ return isDark() ? P.seriesDark : P.seriesLight; }
const token = n => getComputedStyle(document.documentElement)
                     .getPropertyValue(n).trim();
// Forge equivalents of the original --s1 / --deemph / --critical / --surface.
// DEEMPH: matched by resolved value (#c2c2c2 light / #7c7c7c dark) against the
// original #c3c2b7 / #4a4a46. --forge-theme-text-lowest is only 12% alpha, which
// disappears as a bar fill -- and deemph colours every non-focus product in charts
// 3-5, i.e. most of the bars on the page.
const C = () => ({
  s1: token('--forge-theme-primary'),
  deemph: token('--forge-theme-surface-container-medium'),
  critical: token('--forge-theme-error'),
  surface: token('--forge-theme-surface'),
});
const svgW = svg => Math.round(svg.getBoundingClientRect().width) || 560;

/* Populate one of the chart-companion forge-tables. Replaces tableFor(), which
   built a raw <table>: forge-table is data-driven, so head/rows become a column
   config plus an array of objects. */
function tableFor(id, head, rows){
  const host = $('#'+id); if (!host) return;
  const t = host.querySelector('forge-table'); if (!t) return;
  const keys = head.map((_,i) => 'c'+i);
  t.columnConfigurations = head.map((h,i) => ({
    property: keys[i], header: h, sortable: true,
    align: i ? 'right' : 'left',
  }));
  t.data = rows.map(r => Object.fromEntries(r.map((v,i) => [keys[i], v])));
}

/* ---------- chart 1: multi-line, runs per month by portfolio ---------- */
function drawLines(agg){
  const svg = $('#c1'); svg.replaceChildren();
  const W = svgW(svg), H = 250;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const M = {t:14, r:54, b:26, l:46};
  const months = agg.months;
  const col = C();

  const totals = new Map();
  for (const mm of agg.byMonthPortfolio.values())
    for (const [pf,v] of mm) totals.set(pf, (totals.get(pf)||0) + v);

  // A portfolio's slot is fixed for the life of the dashboard, so a filter never
  // repaints a surviving series. Slot -1 is the permanent "Other" bucket.
  const slotOf = pf => P.portfolioSlot[P.portfolios.indexOf(pf)] ?? -1;
  const named = [...totals.keys()].filter(pf => slotOf(pf) >= 0)
                                  .sort((a,b) => slotOf(a) - slotOf(b));
  const hasOther = [...totals.keys()].some(pf => slotOf(pf) < 0);
  const keys = hasOther ? [...named, 'Other'] : named;

  const colors = seriesColors();
  const colorOf = k => k === 'Other' ? col.deemph : colors[slotOf(k)];

  const val = (m,k) => {
    const mm = agg.byMonthPortfolio.get(m); if (!mm) return 0;
    if (k !== 'Other') return mm.get(k) || 0;
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
    svg.appendChild(el('line', {x1:M.l, x2:W-M.r, y1:y, y2:y, class:'gl'}));
    svg.appendChild(text(M.l-8, y+3.5, af(sc.step*(sc.n-g)), 'tick', 'end'));
  }
  svg.appendChild(el('line', {x1:M.l, x2:W-M.r, y1:H-M.b, y2:H-M.b, class:'ax'}));
  months.forEach((m,i) => {
    if (months.length > 10 && i % 2) return;
    svg.appendChild(text(X(i), H-M.b+15, m.slice(2), 'tick', 'middle'));
  });

  const ends = [];
  for (const k of visible){
    const pts = months.map((m,i) => [X(i), Y(val(m,k))]);
    if (pts.length > 1){
      svg.appendChild(el('path', {
        d: pts.map((p,i) => (i?'L':'M')+p[0]+' '+p[1]).join(' '),
        fill:'none', stroke:colorOf(k), 'stroke-width':2,
        'stroke-linejoin':'round', 'stroke-linecap':'round'}));
    }
    const last = pts[pts.length-1];
    if (!last) continue;
    svg.appendChild(el('circle', {cx:last[0], cy:last[1], r:4.5,
      fill:colorOf(k), stroke:col.surface, 'stroke-width':2}));
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
      svg.appendChild(text(e.x+9, e.y+4, fmtCompact(e.v), 'vlab'));
    }
  }

  const cross = el('line', {y1:M.t, y2:H-M.b, class:'crosshair'});
  svg.appendChild(cross);
  const hit = el('rect', {x:M.l, y:M.t, width:Math.max(1,W-M.l-M.r),
                          height:H-M.t-M.b, class:'hit'});
  svg.appendChild(hit);
  const move = ev => {
    const box = svg.getBoundingClientRect();
    const rel = (ev.clientX - box.left) * (W / box.width);
    let bi = 0, bd = Infinity;
    months.forEach((m,i) => { const d = Math.abs(X(i)-rel); if (d<bd){bd=d;bi=i;} });
    cross.setAttribute('x1', X(bi)); cross.setAttribute('x2', X(bi));
    cross.style.opacity = 1;
    const rows = visible.map(k => ({color:colorOf(k), label:k,
                                    value:fmtInt(val(months[bi],k))}))
                        .sort((a,b) => parseInt(b.value.replace(/,/g,''))
                                     - parseInt(a.value.replace(/,/g,'')));
    showTip(ev.clientX, ev.clientY, months[bi]+' · runs', rows);
  };
  hit.addEventListener('pointermove', move);
  hit.addEventListener('pointerleave', () => { cross.style.opacity=0; hideTip(); });

  // Legend as a Forge filter chip-set. The swatch is inline rather than in the
  // `start` slot: filter chips put their selection checkmark in `start`, which would
  // replace the swatch and lose the series-to-colour mapping the chart depends on.
  const lg = $('#lg1'); lg.replaceChildren();
  for (const k of keys){
    const chip = document.createElement('forge-chip');
    chip.value = k;
    chip.selected = !state.hiddenSeries.has(k);
    const swatch = document.createElement('span');
    swatch.setAttribute('aria-hidden', 'true');
    swatch.style.cssText = 'width:10px;height:10px;border-radius:2px;flex:none;'
      + 'display:inline-block;margin-right:6px;vertical-align:-1px;'
      + `background:${colorOf(k)}`;
    chip.append(swatch, document.createTextNode(k));
    lg.appendChild(chip);
  }

  tableFor('tv1', ['Month', ...keys],
    months.map(m => [m, ...keys.map(k => fmtInt(val(m,k)))]));
}

/* ---------- chart 2: single line, distinct users per month ---------- */
function drawUsers(agg){
  const svg = $('#c2'); svg.replaceChildren();
  const W = svgW(svg), H = 250;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const M = {t:14, r:44, b:26, l:42};
  const months = agg.months;
  const v = m => (agg.usersByMonth.get(m) || {size:0}).size;
  const sc = niceScale(Math.max(1, ...months.map(v))), max = sc.max;
  const af = axisFmt(max);
  const X = i => months.length === 1 ? (M.l + (W-M.l-M.r)/2)
                 : M.l + i*(W-M.l-M.r)/(months.length-1);
  const Y = x => H - M.b - (x/max)*(H-M.t-M.b);
  const col = C().s1, surface = C().surface;

  for (let g=0; g<=sc.n; g++){
    const y = M.t + g*(H-M.t-M.b)/sc.n;
    svg.appendChild(el('line', {x1:M.l, x2:W-M.r, y1:y, y2:y, class:'gl'}));
    svg.appendChild(text(M.l-8, y+3.5, af(sc.step*(sc.n-g)), 'tick', 'end'));
  }
  svg.appendChild(el('line', {x1:M.l, x2:W-M.r, y1:H-M.b, y2:H-M.b, class:'ax'}));
  months.forEach((m,i) => {
    if (months.length > 10 && i % 2) return;
    svg.appendChild(text(X(i), H-M.b+15, m.slice(2), 'tick', 'middle'));
  });

  const pts = months.map((m,i) => [X(i), Y(v(m))]);
  if (pts.length > 1){
    svg.appendChild(el('path', {
      d: pts.map((p,i) => (i?'L':'M')+p[0]+' '+p[1]).join(' ')
         + ` L ${pts[pts.length-1][0]} ${H-M.b} L ${pts[0][0]} ${H-M.b} Z`,
      fill:col, 'fill-opacity':0.10, stroke:'none'}));
    svg.appendChild(el('path', {
      d: pts.map((p,i) => (i?'L':'M')+p[0]+' '+p[1]).join(' '),
      fill:'none', stroke:col, 'stroke-width':2,
      'stroke-linejoin':'round', 'stroke-linecap':'round'}));
  }
  const last = pts[pts.length-1];
  if (last){
    svg.appendChild(el('circle', {cx:last[0], cy:last[1], r:4.5, fill:col,
      stroke:surface, 'stroke-width':2}));
    svg.appendChild(text(last[0]+9, last[1]+4, fmtInt(v(months[months.length-1])), 'vlab'));
  }

  const cross = el('line', {y1:M.t, y2:H-M.b, class:'crosshair'});
  svg.appendChild(cross);
  const hit = el('rect', {x:M.l, y:M.t, width:Math.max(1,W-M.l-M.r),
                          height:H-M.t-M.b, class:'hit'});
  svg.appendChild(hit);
  hit.addEventListener('pointermove', ev => {
    const box = svg.getBoundingClientRect();
    const rel = (ev.clientX - box.left) * (W / box.width);
    let bi = 0, bd = Infinity;
    months.forEach((m,i) => { const d = Math.abs(X(i)-rel); if (d<bd){bd=d;bi=i;} });
    cross.setAttribute('x1', X(bi)); cross.setAttribute('x2', X(bi));
    cross.style.opacity = 1;
    showTip(ev.clientX, ev.clientY, months[bi],
      [{color:col, label:'distinct users', value:fmtInt(v(months[bi]))}]);
  });
  hit.addEventListener('pointerleave', () => { cross.style.opacity=0; hideTip(); });

  tableFor('tv2', ['Month','Distinct users'], months.map(m => [m, fmtInt(v(m))]));
}

/* ---------- horizontal bar charts ---------- */
function drawBars(cfg){
  const svg = $(cfg.svg); svg.replaceChildren();
  const W = svgW(svg);
  const rows = cfg.rows.slice(0, 12);
  const rowH = 24, gap = 2;
  const M = {t:6, r:64, b:22, l:Math.min(210, Math.max(120, W*0.36))};
  const H = M.t + rows.length*rowH + M.b;
  svg.setAttribute('height', H);
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const critical = C().critical;

  const sc = niceScale(Math.max(1e-9, ...rows.map(r => r.value)));
  const max = sc.max, plotW = Math.max(1, W - M.l - M.r);
  const X = v => (v/max)*plotW;

  for (let g=0; g<=sc.n; g++){
    const x = M.l + g*plotW/sc.n;
    svg.appendChild(el('line', {x1:x, x2:x, y1:M.t, y2:M.t+rows.length*rowH,
                                class:'gl'}));
    svg.appendChild(text(x, H-6, cfg.fmtAxis(sc.step*g), 'tick', 'middle'));
  }

  rows.forEach((r,i) => {
    const y = M.t + i*rowH;
    const bh = rowH - gap*2;                 // 2px surface gap above and below
    const w = Math.max(1, X(r.value));
    const bar = el('rect', {x:M.l, y:y+gap, width:w, height:bh,
      rx:4, ry:4, fill:r.color});
    svg.appendChild(bar);
    // square off the baseline end: overlay a small rect at the origin
    svg.appendChild(el('rect', {x:M.l, y:y+gap, width:Math.min(4,w), height:bh,
      fill:r.color}));

    const lab = r.label.length > 30 ? r.label.slice(0,29)+'…' : r.label;
    svg.appendChild(text(M.l-8, y+rowH/2+4, lab, 'catlab', 'end'));

    const vt = text(M.l+w+8, y+rowH/2+4, cfg.fmtValue(r.value), 'vlab');
    svg.appendChild(vt);
    if (r.flag){
      // measure, don't estimate -- a guessed advance width collides with the label
      const adv = vt.getComputedTextLength ? vt.getComputedTextLength() : 34;
      svg.appendChild(el('circle', {cx:M.l+w+8+adv+8, cy:y+rowH/2, r:4,
        fill:critical}));
    }

    const hit = el('rect', {x:0, y:y, width:W, height:rowH, class:'hit'});
    svg.appendChild(hit);
    const enter = ev => {
      bar.setAttribute('fill-opacity', 0.78);
      showTip(ev.clientX, ev.clientY, r.label, r.tip);
    };
    hit.addEventListener('pointermove', enter);
    hit.addEventListener('pointerleave', () => {
      bar.removeAttribute('fill-opacity'); hideTip(); });
  });

  if (cfg.table) tableFor(cfg.table, cfg.tableHead, cfg.rows.map(r => cfg.tableRow(r)));
}

/* ---------- adoption tables ---------- */
const AGENT_COLS = [
  {property:'name', header:'Agent', sortable:true, width:250,
   // Forge calls this as (rowIndex, div, rowData, columnIndex) -- the row object is the
   // THIRD argument. Taking it second yields the <div>, whose .name is undefined, and the
   // whole column silently renders the string "undefined". A DocumentFragment is also not
   // an accepted return type, so wrap in a real element.
   template: (_i, _div, d) => {
     const f = document.createElement('span');
     if (d.focus){ const s = document.createElement('span');
       s.className = 'focusdot'; s.title = 'Focus scope'; f.appendChild(s); }
     f.appendChild(document.createTextNode(d.name));
     return f;
   }},
  {property:'tenant', header:'Tenant', sortable:true, width:200,
   transform:v => v || '—'},
  {property:'product', header:'Product', sortable:true, width:180},
  {property:'runs', header:'Runs', align:'right', sortable:true,
   initialSort:true, sortDirection:'desc', transform:v => fmtInt(v)},
  {property:'users', header:'Users', align:'right', sortable:true, transform:v => fmtInt(v)},
  {property:'cost', header:'Cost', align:'right', sortable:true,
   transform:v => '$'+v.toFixed(2)},
  {property:'cpr', header:'$/run', align:'right', sortable:true,
   transform:v => '$'+v.toFixed(4)},
  {property:'secs', header:'Avg s', align:'right', sortable:true,
   transform:v => v.toFixed(1)},
  {property:'fail', header:'Fail %', align:'right', sortable:true,
   transform:v => v.toFixed(1)+'%'},
  {property:'last', header:'Last run', sortable:true, transform:v => v || '—'},
];

function renderAgents(){
  const prods = activeProducts();
  const allow = new Set([...prods].map(i => P.products[i]));
  const q = state.agentQuery.toLowerCase();
  const ten = state.agentTenant;
  const rows = P.agents.filter(a => allow.has(a.product) &&
    (!ten || a.tenant === ten) &&
    (!q || a.name.toLowerCase().includes(q) || a.product.toLowerCase().includes(q) ||
     a.tenant.toLowerCase().includes(q)));
  const t = $('#tagents');
  t.columnConfigurations = AGENT_COLS;
  // forge-table sorts client-side from initialSort/sortable, so the 300-row cap is
  // applied to the pre-sorted list exactly as the original did.
  t.data = rows.slice().sort((a,b) => {
    const x = a[state.sortKey], y = b[state.sortKey];
    if (typeof x === 'string') return state.sortDir * x.localeCompare(y);
    return state.sortDir * ((x||0) - (y||0));
  }).slice(0, 300);
}

function renderSideTables(){
  const prods = activeProducts();
  const allow = new Set([...prods].map(i => P.products[i]));

  const terr = $('#terr');
  terr.columnConfigurations = [
    {property:'code', header:'Error class', sortable:true},
    {property:'product', header:'Product', sortable:true},
    {property:'n', header:'Failures', align:'right', sortable:true,
     initialSort:true, sortDirection:'desc', transform:v => fmtInt(v)},
  ];
  terr.data = P.errors.filter(e => allow.has(e.product)).slice(0, 60);

  const built = new Map(P.builtAgents.map(b => [b.product, b.n]));
  const tdorm = $('#tdorm');
  tdorm.columnConfigurations = [
    {property:'product', header:'Product', sortable:true},
    {property:'n', header:'Dormant', align:'right', sortable:true,
     initialSort:true, sortDirection:'desc', transform:v => fmtInt(v)},
    {property:'built', header:'Built', align:'right', sortable:true,
     transform:v => fmtInt(v)},
    {property:'pctRaw', header:'Dormant %', align:'right', sortable:true,
     transform:v => v.toFixed(0)+'%'},
  ];
  tdorm.data = P.dormant.filter(d => allow.has(d.product)).map(d => {
    const b = built.get(d.product) || d.n;
    return {product:d.product, n:d.n, built:b, pctRaw:100*d.n/b};
  });

  // Visual workflows. 22 executions across 424 definitions is a table, not a chart --
  // a bar chart of 6 values would dress up a number the reader can just read.
  const [wlo, whi] = dayWindow();
  const from = P.days[wlo], to = P.days[whi];
  const wfr = P.wfRuns.filter(w => allow.has(w.product)
                                && (!w.day || (w.day >= from && w.day <= to)));
  const wfa = P.wfAdoption.filter(w => allow.has(w.product));
  const defined = wfa.reduce((s,w) => s + w.defined, 0);
  const ran = wfa.reduce((s,w) => s + w.ran, 0);
  const resumed = wfr.reduce((s,w) => s + w.resumed, 0);
  $('#wfnote').textContent = defined
    ? `${fmtInt(wfr.length)} executions · ${ran} of ${defined} definitions ever ran `
      + `(${(100*ran/defined).toFixed(1)}%) · ${resumed} ever resumed`
    : 'no workflow definitions in this scope';

  const twf = $('#twf');
  twf.columnConfigurations = [
    {property:'name', header:'Workflow', sortable:true,
     template: (_i, _div, d) => {
       const f = document.createElement('span');
       if (d.focus){ const s = document.createElement('span');
         s.className = 'focusdot'; s.title = 'Focus scope'; f.appendChild(s); }
       f.appendChild(document.createTextNode(d.name));
       return f;
     }},
    {property:'product', header:'Product', sortable:true},
    {property:'status', header:'Status', sortable:true,
     template: (_i, _div, d) => {
       const b = document.createElement('forge-badge');
       b.setAttribute('theme', d.status === 'completed' ? 'success'
                             : d.status === 'failed' ? 'error' : 'warning');
       b.textContent = d.status || '—';
       return b;
     }},
    {property:'day', header:'Run', sortable:true, transform:v => v || '—'},
    {property:'step', header:'Suspended at', sortable:true, transform:v => v || '—'},
  ];
  twf.data = wfr;

  const twfa = $('#twfa');
  twfa.columnConfigurations = [
    {property:'product', header:'Product', sortable:true},
    {property:'defined', header:'Defined', align:'right', sortable:true,
     transform:v => fmtInt(v)},
    {property:'ran', header:'Ran', align:'right', sortable:true, transform:v => fmtInt(v)},
    {property:'execs', header:'Execs', align:'right', sortable:true,
     initialSort:true, sortDirection:'desc', transform:v => fmtInt(v)},
    {property:'ranPct', header:'Ran %', align:'right', sortable:true,
     transform:v => v.toFixed(0)+'%'},
  ];
  twfa.data = wfa.map(w => ({...w, ranPct: 100*w.ran/w.defined}));

  const tgaps = $('#tgaps');
  tgaps.columnConfigurations = [
    {property:'project', header:'Project', sortable:true},
    {property:'agent', header:'Agent', sortable:true},
    {property:'runs', header:'Runs', align:'right', sortable:true,
     initialSort:true, sortDirection:'desc', transform:v => fmtInt(v)},
    {property:'cost', header:'Cost', align:'right', sortable:true,
     transform:v => '$'+v.toFixed(2)},
  ];
  tgaps.data = P.gaps;
}

function renderSlice(){
  const prods = activeProducts(), [lo,hi] = dayWindow();
  const bits = [`${P.days[lo]} → ${P.days[hi]}`,
                `${prods.size} of ${P.products.length} products`];
  if (state.portfolio) bits.push(state.portfolio);
  if (state.product) bits.push(state.product);
  if (state.focus) bits.push('focus only');
  if (!state.sandbox) bits.push('non-product excluded');
  if (state.view === 'feedback' && state.tenant !== '')
    bits.push(P.tenants[+state.tenant]);
  $('#slicechip').textContent = bits.join(' · ');
}

/* ---------- adoption render ---------- */
function renderAdoption(){
  const agg = aggregate();
  const A = agg.A;

  $('#kRuns').textContent = fmtInt(A.runs);
  $('#kRunsNote').textContent = `${fmtCompact(A.tokens)} tokens · `
    + `${fmtInt(A.runs && agg.months.length ? A.runs/agg.months.length : 0)} runs/month avg`;
  $('#kUsers').textContent = fmtInt(agg.users);
  $('#kUsersNote').textContent = agg.users
    ? (A.runs/agg.users).toFixed(0) + ' runs per user' : 'no activity in slice';
  $('#kCost').textContent = fmtMoney(A.cost);
  $('#kCostNote').textContent = A.runs
    ? '$' + (A.cost/A.runs).toFixed(4) + ' per run' : '—';
  const failPct = A.runs ? 100*A.fails/A.runs : 0;
  $('#kFail').textContent = A.runs ? fmtPct(failPct) : '—';
  $('#kFailNote').textContent = fmtInt(A.fails) + ' failed runs';
  $('#kAgents').textContent = fmtInt(agg.agents);
  $('#kAgentsNote').textContent = A.durN
    ? (A.durMs/A.durN/1000).toFixed(1) + 's avg run' : '—';

  const lastMonth = agg.months[agg.months.length-1];
  const partial = lastMonth && P.days[P.days.length-1].slice(0,7) === lastMonth
                  && P.days[P.days.length-1].slice(8) !== '31';
  const partialNote = partial
    ? ` · ${lastMonth} is partial (through ${P.days[P.days.length-1]})` : '';
  $('#c1note').textContent =
    'Colours are fixed per portfolio, so filtering never repaints a series'
    + partialNote;
  $('#c2note').textContent =
    'Exact distinct counts, not a sum of per-product users' + partialNote;

  drawLines(agg);
  drawUsers(agg);

  const prodRows = [...agg.byProduct.entries()]
    .map(([i,r]) => ({i, ...r})).sort((a,b) => b.runs-a.runs);
  const totalRuns = A.runs || 1;
  const col = C(), s1 = col.s1, de = col.deemph;

  drawBars({
    svg:'#c3', table:'tv3',
    rows: prodRows.map(r => ({
      label: P.products[r.i], value: r.runs,
      color: P.prodFocus[r.i] ? s1 : de,
      flag: false,
      tip: [{value: fmtInt(r.runs), label:'runs', color: P.prodFocus[r.i] ? s1 : de},
            {value: (100*r.runs/totalRuns).toFixed(2)+'%', label:'of slice'},
            {value: fmtMoney(r.cost), label:'cost'}],
    })),
    fmtAxis: fmtCompact, fmtValue: v => fmtCompact(v),
    tableHead:['Product','Runs','% of slice','Cost'],
    tableRow: r => [r.label, fmtInt(r.value),
                    (100*r.value/totalRuns).toFixed(2)+'%',
                    r.tip[2].value],
  });

  const big = prodRows.filter(r => r.runs >= 100);
  drawBars({
    svg:'#c4', table:'tv4',
    rows: big.map(r => ({
      label: P.products[r.i], value: r.cost/r.runs,
      color: P.prodFocus[r.i] ? s1 : de, flag:false,
      tip: [{value:'$'+(r.cost/r.runs).toFixed(4), label:'per run',
             color: P.prodFocus[r.i] ? s1 : de},
            {value: fmtMoney(r.cost), label:'total cost'},
            {value: fmtInt(r.runs), label:'runs'}],
    })).sort((a,b) => b.value-a.value),
    fmtAxis: v => '$'+v.toFixed(2), fmtValue: v => '$'+v.toFixed(3),
    tableHead:['Product','$/run','Total cost','Runs'],
    tableRow: r => [r.label, '$'+r.value.toFixed(4), r.tip[1].value, r.tip[2].value],
  });

  drawBars({
    svg:'#c5', table:'tv5',
    rows: big.map(r => ({
      label: P.products[r.i], value: 100*r.fails/r.runs,
      color: P.prodFocus[r.i] ? s1 : de,
      flag: 100*r.fails/r.runs > 10,
      tip: [{value:(100*r.fails/r.runs).toFixed(2)+'%', label:'failure rate',
             color: P.prodFocus[r.i] ? s1 : de},
            {value: fmtInt(r.fails), label:'failed runs'},
            {value: fmtInt(r.runs), label:'total runs'}],
    })).sort((a,b) => b.value-a.value),
    fmtAxis: v => v.toFixed(0)+'%', fmtValue: v => v.toFixed(1)+'%',
    tableHead:['Product','Failure %','Failed','Runs'],
    tableRow: r => [r.label, r.value.toFixed(2)+'%', r.tip[1].value, r.tip[2].value],
  });

  renderAgents();
  renderSideTables();
}

/* ================= negative feedback ================= */
/* The real names, copied from dashboard.html. Mechanically un-snake-casing the keys
   produces wrong labels, not merely ugly ones -- "kb_gap" becomes "Kb gap" rather than
   "Knowledge gap", and "over_narrow_refusal" loses its meaning entirely. */
const LABEL_MAP = {
  incorrect_answer:'Incorrect answer',
  wrong_ownership_claim:'Wrong ownership / jurisdiction',
  kb_gap:'Knowledge gap',
  over_narrow_refusal:'Refused something in scope',
  incomplete_answer:'Incomplete answer',
  too_generic:'Too generic',
  missing_source_or_next_step:'No source or next step',
  language_handling:'Language handling',
  tool_or_system_error:'Tool / system error',
  formatting_or_tone:'Formatting or tone',
  other:'Other',
  not_a_defect:'Not a defect',
};
const LABEL_TEXT = k => LABEL_MAP[k] || k || '(unclassified)';
const FIX_TEXT = {knowledge_base:'Knowledge base', instructions:'Instructions',
                  routing:'Routing', model:'Model', none:'Not actionable', '':'—'};
const fixText = k => FIX_TEXT[k] ?? k;
const pctStr = (a, b) => b ? fbPct(100 * a / b) : '—';
// dashboard.html's fmtPct: 2dp below 10, 1dp at or above.
const fbPct = v => v.toFixed(v < 10 ? 2 : 1) + '%';

/* Tenant/product/date scope only -- drives the KPIs and every chart. */
function fbInSlice(){
  const prods = activeProducts(), [lo,hi] = dayWindow();
  const from = P.days[lo], to = P.days[hi];
  const ten = state.tenant === '' ? -1 : +state.tenant;
  return P.fbTurns.filter(t => prods.has(t.p) && t.d >= from && t.d <= to
                               && (ten < 0 || t.tn === ten));
}
/* The narrower filters apply to the itemised list ONLY, never the KPIs/charts. */
function fbListRows(inSlice){
  const q = state.q.trim().toLowerCase();
  return inSlice.filter(t => {
    if (state.commented && !t.c) return false;
    if (state.label && t.l !== state.label) return false;
    if (state.sev && t.s !== state.sev) return false;
    if (q && !((t.c + ' ' + t.q + ' ' + t.a + ' ' +
                (t.tn >= 0 ? P.tenants[t.tn] : '')).toLowerCase().includes(q)))
      return false;
    return true;
  });
}

let lastSlice = [];

function renderFeedback(){
  const S = fbInSlice();
  lastSlice = S;
  const prods = activeProducts(), [lo,hi] = dayWindow();
  const from = P.days[lo], to = P.days[hi];
  const ten = state.tenant === '' ? -1 : +state.tenant;

  let pos = 0, convs = 0;
  for (const [day, p, neg, pu, cv, tn] of P.fbCounts){
    if (!prods.has(p) || day < from || day > to) continue;
    if (ten >= 0 && tn !== ten) continue;
    pos += pu; convs += cv;
  }
  const neg = S.length;
  const commented = S.filter(t => t.c).length;
  const labelled = S.filter(t => t.l).length;
  const rated = neg + pos;

  // Denominator for the submission rate: conversations actually held, from the run
  // facts -- not the conversations that happened to get rated.
  let heldConvs = 0;
  for (const [d,p,runs,cv] of P.facts){
    if (d < lo || d > hi || !prods.has(p)) continue;
    heldConvs += cv;
  }

  $('#kNeg').textContent = fmtInt(neg);
  $('#kNegNote').textContent = `${fmtInt(pos)} thumbs-up in the same slice`;
  $('#kNegC').textContent = fmtInt(commented);
  $('#kNegCNote').textContent = neg
    ? fbPct(100*commented/neg) + ' of thumbs-down explained' : '—';
  $('#kNegShare').textContent = rated ? fbPct(100*neg/rated) : '—';
  $('#kNegShareNote').textContent = rated
    ? fmtInt(rated) + ' rated turns total' : 'no ratings in slice';
  // The run facts are grouped by day and product only -- there is no tenant column.
  // Under a tenant filter the numerator narrows and the denominator does not, so the
  // ratio would understate the rate. Blanked rather than shown wrong.
  if (ten >= 0){
    $('#kFbRate').textContent = '—';
    $('#kFbRateNote').textContent =
      `${fmtInt(convs)} rated conversations · rate needs a tenant-scoped denominator `
      + `the run facts don't carry`;
  } else {
    $('#kFbRate').textContent = heldConvs ? fbPct(100*convs/heldConvs) : '—';
    $('#kFbRateNote').textContent = heldConvs
      ? fmtInt(convs) + ' rated of ' + fmtCompact(heldConvs) + ' conversations' : '—';
  }
  // Denominator is the *commented* turns: uncommented downvotes are skipped by
  // design, not pending, so counting them would show a permanent shortfall.
  $('#kLabelled').textContent = commented ? fbPct(100*labelled/commented) : '—';
  $('#kLabelledNote').textContent = commented
    ? `${fmtInt(labelled)} of ${fmtInt(commented)} explained complaints` : '—';

  drawFbCharts(S);

  const fixMap = new Map();
  for (const t of S){
    if (!t.f) continue;
    const row = fixMap.get(t.f)
      || {fix: fixText(t.f), misleading:0, unhelpful:0, minor:0, total:0};
    if (t.s in row) row[t.s]++;
    row.total++;
    fixMap.set(t.f, row);
  }
  const tfix = $('#tfix');
  tfix.columnConfigurations = [
    {property:'fix', header:'Fix', sortable:true},
    {property:'misleading', header:'Misleading', align:'right', sortable:true},
    {property:'unhelpful', header:'Unhelpful', align:'right', sortable:true},
    {property:'minor', header:'Minor', align:'right', sortable:true},
    {property:'total', header:'Total', align:'right', sortable:true,
     initialSort:true, sortDirection:'desc'},
  ];
  tfix.data = [...fixMap.values()].sort((a,b) => b.total-a.total);

  const tfba = $('#tfba');
  tfba.columnConfigurations = [
    {property:'name', header:'Agent', sortable:true, width:240},
    {property:'tenant', header:'Tenant', sortable:true, width:190},
    {property:'product', header:'Product', sortable:true, width:170},
    {property:'convs', header:'Convs', align:'right', sortable:true,
     transform:v => fmtInt(v)},
    {property:'neg', header:'Neg', align:'right', sortable:true,
     initialSort:true, sortDirection:'desc'},
    {property:'negc', header:'w/ comment', align:'right', sortable:true},
    {property:'pos', header:'Pos', align:'right', sortable:true},
    {property:'share', header:'Neg share', align:'right', sortable:true,
     transform:v => v == null ? '—' : v+'%'},
    {property:'per100', header:'Neg/100', align:'right', sortable:true,
     transform:v => v == null ? '—' : v.toFixed(2)},
    {property:'rate', header:'Fb rate', align:'right', sortable:true,
     transform:v => v == null ? '—' : v+'%'},
    {property:'last', header:'Last', sortable:true},
  ];
  const allow = new Set([...prods].map(i => P.products[i]));
  tfba.data = P.fbAgents
    .filter(a => (ten < 0 || a.tn === ten)
              && (a.p < 0 || allow.has(P.products[a.p])))
    .map(a => ({...a,
      tenant: a.tn >= 0 ? P.tenants[a.tn] : '—',
      product: a.p >= 0 ? P.products[a.p] : '—'}));

  const L = fbListRows(S);
  const shown = L.slice(0, state.limit);
  const list = $('#fblist');
  list.textContent = '';

  if (!L.length){
    $('#fbnote').textContent = 'Nothing matches these filters.';
    const e = document.createElement('div');
    e.className = 'fbempty';
    e.textContent = P.fbTurns.length
      ? 'No thumbs-down feedback matches the current filters.'
      : 'No feedback extracted yet — run: python3 extract.py feedback';
    list.appendChild(e);
    $('#fbMore').classList.add('hidden');
    return;
  }

  $('#fbnote').textContent =
    `${fmtInt(L.length)} matching turns · showing ${fmtInt(shown.length)}. `
    + 'Question and answer text is truncated — copy the conversation id to open the '
    + 'full transcript.'
    + (L.length < neg ? ` ${fmtInt(neg)} in the slice before list filters.` : '');

  for (const t of shown){
    const d = document.createElement('div');
    d.className = 'fbitem';

    const meta = document.createElement('div');
    meta.className = 'fbmeta';
    meta.appendChild(strong(t.a));
    // Tenant sits next to the agent name, not further down the row: with names like
    // 'Staff Assistant' the name alone does not identify which deployment complained.
    meta.appendChild(badge(t.tn >= 0 ? P.tenants[t.tn] : '(unknown tenant)'));
    meta.append(dot(), span(t.p >= 0 ? P.products[t.p] : 'Unattributed'),
                dot(), span(t.d));
    // Always render a category pill, so an unclassified turn is visibly unclassified
    // rather than just missing a pill.
    meta.append(dot(), badge(LABEL_TEXT(t.l)));
    if (t.s) meta.appendChild(sevBadge(t.s));
    if (t.f) meta.appendChild(badge('fix: ' + fixText(t.f)));
    meta.appendChild(cidButton(t));
    d.appendChild(meta);

    if (t.c){ const c = document.createElement('div');
      c.className = 'fbcomment'; c.textContent = t.c; d.appendChild(c); }

    const qr = document.createElement('div');
    qr.className = 'fbqr';
    for (const [lab, val] of [['Asked', t.q], ['Answered', t.r]]){
      if (!val) continue;
      const row = document.createElement('div');
      const b = document.createElement('b'); b.textContent = lab + ': ';
      const s = document.createElement('span'); s.textContent = val;
      row.append(b, s); qr.appendChild(row);
    }
    if (qr.children.length) d.appendChild(qr);

    if (t.why){ const w = document.createElement('div');
      w.className = 'fbwhy'; w.textContent = 'Classifier: ' + t.why;
      d.appendChild(w); }
    list.appendChild(d);
  }
  $('#fbMore').classList.toggle('hidden', shown.length >= L.length);
}

/* The conversation id is the only route from a truncated excerpt back to the full
   transcript, so the copy has to be reliable: navigator.clipboard is unavailable in
   a non-secure context (plain http on a LAN address), and failing silently there
   would strand the reader. Falls back to a hidden textarea, and surfaces an explicit
   failure rather than a false "copied". */
function cidButton(t){
  const full = t.cid || '';
  const label = () => (full ? full.slice(0,8) + '… turn ' + t.t : 'no conversation id');
  const btn = document.createElement('forge-button');
  btn.setAttribute('variant', 'text');
  btn.className = 'fbcid';
  if (!full){ btn.setAttribute('disabled', ''); btn.textContent = label(); return btn; }

  const icon = document.createElement('forge-icon');
  icon.setAttribute('name', 'clipboard');
  icon.setAttribute('slot', 'start');
  const txt = document.createElement('span');
  txt.textContent = label();
  btn.append(icon, txt);
  btn.title = 'Copy conversation id ' + full;

  btn.addEventListener('click', async () => {
    txt.textContent = (await copyText(full))
      ? 'copied' : 'copy failed — id is in the tooltip';
    setTimeout(() => { txt.textContent = label(); }, 1400);
  });
  return btn;
}

/* Try the async Clipboard API, then fall through to the legacy selection copy.
   writeText rejects when the document lacks user activation or focus, which is not
   the same as "copying is impossible here" -- so a rejection must fall through
   rather than end the attempt. Returns false only if both routes fail. */
async function copyText(s){
  if (navigator.clipboard && window.isSecureContext){
    try { await navigator.clipboard.writeText(s); return true; } catch { /* fall through */ }
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = s;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0';
    document.body.appendChild(ta);
    ta.focus({preventScroll:true});
    ta.setSelectionRange(0, s.length);
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch { return false; }
}

function drawFbCharts(S){
  const col = C();
  const byMonth = new Map();
  for (const t of S){
    const m = (t.d || '').slice(0,7);
    if (m) byMonth.set(m, (byMonth.get(m)||0) + 1);
  }
  const months = [...byMonth.entries()].sort((a,b) => a[0].localeCompare(b[0]));
  colBars($('#cf1'), months, col.critical);
  $('#cf1note').textContent = months.length
    ? `${months[0][0]} — ${months[months.length-1][0]}` : 'No matching turns';

  const byLabel = new Map();
  for (const t of S) if (t.l) byLabel.set(t.l, (byLabel.get(t.l)||0) + 1);
  drawBars({
    svg:'#cf2',
    rows: [...byLabel.entries()].sort((a,b) => b[1]-a[1]).slice(0,12)
      .map(([l,v]) => ({label: LABEL_TEXT(l), value: v, color: col.critical,
                        flag:false, tip:[{value:fmtInt(v), label:'turns',
                                          color:col.critical}]})),
    fmtAxis: fmtCompact, fmtValue: v => fmtInt(v),
  });

  const ten = state.tenant === '' ? -1 : +state.tenant;
  const prods = activeProducts();
  const allow = new Set([...prods].map(i => P.products[i]));
  const agents = P.fbAgents.filter(a => (ten < 0 || a.tn === ten)
    && (a.p < 0 || allow.has(P.products[a.p])) && a.convs >= 250 && a.per100 != null);
  drawBars({
    svg:'#cf3',
    rows: agents.slice().sort((a,b) => b.per100-a.per100).slice(0,12)
      .map(a => ({label:a.name, value:a.per100,
                  color: a.per100 >= 1 ? col.critical : col.s1,
                  flag: a.per100 >= 1,
                  tip:[{value:a.per100.toFixed(2), label:'neg per 100 convs',
                        color: a.per100 >= 1 ? col.critical : col.s1},
                       {value:fmtInt(a.neg), label:'thumbs-down'},
                       {value:fmtInt(a.convs), label:'conversations'}]})),
    fmtAxis: v => v.toFixed(0), fmtValue: v => v.toFixed(2),
  });
}

/* Vertical column chart -- only the feedback trend uses this shape. */
function colBars(svg, rows, color){
  svg.replaceChildren();
  const W = svgW(svg), H = +svg.getAttribute('height');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const M = {t:12, r:12, b:40, l:44};
  const sc = niceScale(Math.max(1, ...rows.map(r => r[1])));
  const max = sc.max, af = axisFmt(max);
  const iw = W - M.l - M.r, ih = H - M.t - M.b;
  const bw = rows.length ? Math.min(46, iw/rows.length*0.62) : 0;

  for (let g=0; g<=sc.n; g++){
    const y = M.t + g*ih/sc.n;
    svg.appendChild(el('line', {x1:M.l, x2:W-M.r, y1:y, y2:y, class:'gl'}));
    svg.appendChild(text(M.l-8, y+3.5, af(sc.step*(sc.n-g)), 'tick', 'end'));
  }
  svg.appendChild(el('line', {x1:M.l, x2:W-M.r, y1:H-M.b, y2:H-M.b, class:'ax'}));

  rows.forEach((r,i) => {
    const cx = M.l + iw*(i+0.5)/rows.length;
    const h = ih * r[1]/max;
    const bar = el('rect', {x:cx-bw/2, y:M.t+ih-h, width:bw, height:h, rx:3,
                            fill:color});
    svg.appendChild(bar);
    if (bw > 18) svg.appendChild(text(cx, M.t+ih-h-5, fmtInt(r[1]), 'vlab', 'middle'));
    svg.appendChild(text(cx, H-M.b+16, r[0].slice(2), 'tick', 'middle'));
    const hit = el('rect', {x:cx-iw/rows.length/2, y:M.t, width:iw/rows.length,
                            height:ih, class:'hit'});
    svg.appendChild(hit);
    hit.addEventListener('pointermove', ev => {
      bar.setAttribute('fill-opacity', 0.78);
      showTip(ev.clientX, ev.clientY, r[0],
        [{color, label:'thumbs-down turns', value:fmtInt(r[1])}]);
    });
    hit.addEventListener('pointerleave', () => {
      bar.removeAttribute('fill-opacity'); hideTip(); });
  });
}

function span(t){ const e = document.createElement('span'); e.textContent = t; return e; }
function strong(t){ const e = document.createElement('strong'); e.textContent = t; return e; }
function dot(){ return span('·'); }
function badge(t, strongTone){
  const b = document.createElement('forge-badge');
  if (strongTone) b.setAttribute('theme', 'error');
  b.textContent = t;
  return b;
}
/* Severity gets its own themes so it is not read as just another category pill --
   the original used distinct sev-* classes for the same reason. */
const SEV_THEME = {misleading:'error', unhelpful:'warning', minor:'info'};
function sevBadge(s){
  const b = document.createElement('forge-badge');
  b.setAttribute('theme', SEV_THEME[s] || 'default');
  if (s === 'misleading') b.setAttribute('strong', '');
  b.textContent = s;
  return b;
}

/* ================= EPL solutions ================= */
const E = P.epl;
/* "" = everything shipped, "type:Customer" = a whole class, "dep:3" = one deployment.
   Deliberately independent of the global filters: this tab is presented from, and a
   stray date filter silently restating the numbers mid-meeting is the failure mode. */
const eplState = {scope:''};

function eplDepSet(){
  const s = eplState.scope;
  const out = new Set();
  E.deps.forEach((d,i) => {
    if (!s) out.add(i);
    else if (s.startsWith('type:') && d.type === s.slice(5)) out.add(i);
    else if (s.startsWith('dep:') && i === +s.slice(4)) out.add(i);
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
  for (const [key, tuples] of [['users', E.userTuples], ['agents', E.agentTuples],
                               ['convs', E.convTuples]]){
    const perWeek = Array.from({length:n}, () => new Set());
    for (const [w, d, ix] of tuples) if (deps.has(d)) perWeek[w].add(ix);
    perWeek.forEach((s,i) => { rows[i][key] = s.size; });
  }
  return rows;
}

function drawEplWeekly(deps){
  const svg = $('#cEpl1'); svg.replaceChildren();
  const W = svgW(svg), H = 290;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const M = {t:16, r:46, b:52, l:46};
  const rows = eplWeekly(deps);
  const n = rows.length;
  if (!n){
    svg.appendChild(text(W/2, H/2, 'No deployments in scope', 'catlab', 'middle'));
    tableFor('tvEpl1', ['Week','Conversations','Users'], []);
    return;
  }
  const cMax = niceScale(Math.max(1, ...rows.map(r => r.convs)));
  const uMax = niceScale(Math.max(1, ...rows.map(r => r.users)));
  const plotW = W - M.l - M.r, plotH = H - M.t - M.b;
  const bw = Math.max(4, Math.min(30, plotW/n - 4));
  const X = i => M.l + (i + 0.5)*plotW/n;
  const Yc = v => H - M.b - (v/cMax.max)*plotH;
  const Yu = v => H - M.b - (v/uMax.max)*plotH;
  const col = C();
  // The originals were --s1 and --s2 (the fixed series palette), not theme tokens, so
  // the two marks stay distinguishable in both themes.
  const series = seriesColors();
  const cBar = series[0] || col.s1, cUser = series[1] || col.critical;
  const afC = axisFmt(cMax.max);

  // Both axes must be divided the same number of times, or the right-hand labels stop
  // short of zero and the line appears to start partway up the chart. The gridlines
  // belong to the left scale, so the right scale is subdivided to match it rather than
  // using its own niceScale step count.
  for (let g=0; g<=cMax.n; g++){
    const y = M.t + g*plotH/cMax.n;
    const frac = (cMax.n - g)/cMax.n;
    svg.appendChild(el('line', {x1:M.l, x2:W-M.r, y1:y, y2:y, class:'gl'}));
    svg.appendChild(text(M.l-8, y+3.5, afC(cMax.step*(cMax.n-g)), 'tick', 'end'));
    svg.appendChild(text(W-M.r+8, y+3.5,
      String(Math.round(uMax.max*frac)), 'tick', 'start'));
  }
  svg.appendChild(el('line', {x1:M.l, x2:W-M.r, y1:H-M.b, y2:H-M.b, class:'ax'}));

  rows.forEach((r,i) => {
    const isPartial = i === E.partialWeek;
    const h = Math.max(0, H - M.b - Yc(r.convs));
    if (r.convs > 0){
      const bar = el('rect', {x:X(i)-bw/2, y:Yc(r.convs), width:bw, height:h,
        fill:cBar, rx:2});
      // The final week is usually a day or two long; drawn hollow so the dip reads as
      // "week still in progress" instead of a fall in usage.
      if (isPartial){ bar.setAttribute('fill', 'none');
                      bar.setAttribute('stroke', cBar);
                      bar.setAttribute('stroke-dasharray', '3 2'); }
      bar.addEventListener('pointerenter', ev => showTip(ev.clientX, ev.clientY,
        E.weeks[i] + (isPartial ? ' (week in progress)' : ''),
        [{value:fmtInt(r.convs), label:'conversations active', color:cBar},
         {value:fmtInt(r.users), label:'people', color:cUser},
         {value:fmtInt(r.agents), label:'agents live'},
         {value:fmtInt(r.runs), label:'runs'}]));
      bar.addEventListener('pointerleave', hideTip);
      svg.appendChild(bar);
    }
  });

  // The line STOPS at the last complete week. Carrying it into a part-week drags it to
  // near zero and draws a collapse in usage that is only "the week isn't over" -- the
  // exact misreading the hollow bar exists to prevent. The partial point is drawn
  // hollow and detached so it is visible without being joined to the trend.
  const pts = rows.map((r,i) => [X(i), Yu(r.users)]);
  const lastComplete = E.partialWeek >= 0 ? E.partialWeek - 1 : rows.length - 1;
  const solid = pts.slice(0, lastComplete + 1);
  if (solid.length > 1){
    svg.appendChild(el('path', {
      d: solid.map((p,i) => (i?'L':'M')+p[0]+' '+p[1]).join(' '),
      fill:'none', stroke:cUser, 'stroke-width':2.4,
      'stroke-linejoin':'round', 'stroke-linecap':'round'}));
  }
  rows.forEach((r,i) => {
    if (!r.users) return;
    const partial = i === E.partialWeek;
    const d = el('circle', {cx:X(i), cy:Yu(r.users), r:3.4,
      fill: partial ? 'none' : cUser, stroke: partial ? cUser : col.surface,
      'stroke-width': partial ? 1.6 : 1.5});
    if (partial) d.setAttribute('stroke-dasharray', '2 1.5');
    svg.appendChild(d);
  });

  const step = n > 14 ? Math.ceil(n/9) : 1;
  E.weeks.forEach((w,i) => {
    if (i % step && i !== n-1) return;
    svg.appendChild(text(X(i), H-M.b+15, w.slice(5), 'tick', 'middle'));
  });
  svg.appendChild(text(M.l, H-8, 'conversations active (bars, left)', 'catlab', 'start'));
  svg.appendChild(text(W-M.r, H-8, 'people (line, right)', 'catlab', 'end'));

  tableFor('tvEpl1', ['Week','Conversations active','People','Agents live','Runs'],
    rows.map((r,i) => [E.weeks[i] + (i===E.partialWeek ? ' (partial)' : ''),
                       fmtInt(r.convs), fmtInt(r.users), fmtInt(r.agents),
                       fmtInt(r.runs)]));
}

function renderEplDeps(deps){
  const rows = E.deps.map((d,i) => ({...d, i}))
    .filter(d => deps.has(d.i))
    // negC (conversations containing a complaint), not neg (complaint turns): the rate
    // claims a share OF CONVERSATIONS. See build_dashboard.py's build_epl for why.
    .map(d => ({...d, rate: d.rated ? 100*d.negC/d.convs : null}));
  const t = $('#tEplDeps');
  t.columnConfigurations = [
    {property:'name', header:'Deployment', sortable:true, width:230},
    {property:'type', header:'Type', sortable:true},
    {property:'agents', header:'Agents', align:'right', sortable:true,
     transform:v => fmtInt(v)},
    {property:'first', header:'Live since', sortable:true},
    {property:'last', header:'Last activity', sortable:true},
    {property:'users', header:'Users', align:'right', sortable:true,
     transform:v => fmtInt(v)},
    {property:'convs', header:'Convs', align:'right', sortable:true,
     initialSort:true, sortDirection:'desc', transform:v => fmtInt(v)},
    {property:'rated', header:'Rated convs', align:'right', sortable:true,
     transform:v => fmtInt(v)},
    {property:'pos', header:'👍 turns', align:'right', sortable:true,
     transform:v => fmtInt(v)},
    {property:'neg', header:'👎 turns', align:'right', sortable:true,
     transform:v => fmtInt(v)},
    // Null rate renders as an em dash: no feedback is not the same as no complaints.
    {property:'rate', header:'Complaint rate', align:'right', sortable:true,
     transform:v => v == null ? '—' : v.toFixed(1) + '%'},
  ];
  t.data = rows;

  const ex = E.excluded;
  $('#eplExcluded').textContent = ex.length
    ? 'Not shown (not shipped deployments): '
      + ex.map(([t2,r]) => `${t2} ${fmtInt(r)} runs`).join(' · ')
    : '';
}

function renderEplCauses(deps){
  const mine = E.complaints.filter(c => deps.has(c.dep));
  const byLabel = new Map(), byFix = new Map();
  for (const c of mine){
    byLabel.set(c.l, (byLabel.get(c.l)||0) + 1);
    const key = c.l + '|' + c.f;
    byFix.set(key, (byFix.get(key)||0) + 1);
  }
  const ordered = [...byLabel.entries()].sort((a,b) => b[1]-a[1]);
  const t = $('#tEplCauses');
  t.columnConfigurations = [
    {property:'cause', header:'Root cause', sortable:true},
    {property:'n', header:'Complaints', align:'right', sortable:true,
     initialSort:true, sortDirection:'desc'},
    {property:'fixes', header:'Where the fix belongs', sortable:false},
  ];
  t.data = ordered.map(([label, count]) => ({
    cause: LABEL_TEXT(label),
    n: count,
    fixes: [...byFix.entries()].filter(([k]) => k.startsWith(label+'|'))
      .sort((a,b) => b[1]-a[1])
      .map(([k,v]) => `${fixText(k.split('|')[1])} (${v})`).join(', '),
  }));

  $('#eplCxNote').textContent = mine.length
    ? `${fmtInt(mine.length)} thumbs-down, grouped by what went wrong. `
      + 'Every one is quoted below — this is the fix list, not a score.'
    : 'No thumbs-down feedback for this selection.';

  const host = $('#eplComplaints'); host.replaceChildren();
  if (!mine.length){
    const e = document.createElement('div');
    e.className = 'fbempty';
    e.textContent = 'No thumbs-down feedback for this selection.';
    host.appendChild(e);
    return;
  }
  for (const c of mine){
    const card = document.createElement('div');
    card.className = 'fbitem';
    const meta = document.createElement('div');
    meta.className = 'fbmeta';
    meta.appendChild(strong(c.a));
    meta.appendChild(badge(E.deps[c.dep].name));
    meta.append(dot(), span(c.d), dot(), badge(LABEL_TEXT(c.l)));
    if (c.s) meta.appendChild(sevBadge(c.s));
    if (c.f) meta.appendChild(badge('fix: ' + fixText(c.f)));
    // Same escape hatch as the feedback tab: the quote is truncated, the id is not.
    meta.appendChild(cidButton(c));
    card.appendChild(meta);
    if (c.q){
      const qr = document.createElement('div');
      qr.className = 'fbqr';
      const row = document.createElement('div');
      const b = document.createElement('b'); b.textContent = 'Asked: ';
      const s = document.createElement('span'); s.textContent = c.q;
      row.append(b, s); qr.appendChild(row);
      card.appendChild(qr);
    }
    const body = document.createElement('div');
    body.className = 'fbcomment';
    body.textContent = c.c || '(no comment)';
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
  // neg/pos are turns; negC is the conversations those complaint turns sit in.
  const neg = sel.reduce((a,d) => a + d.neg, 0);
  const pos = sel.reduce((a,d) => a + d.pos, 0);
  const negC = sel.reduce((a,d) => a + d.negC, 0);
  const rated = sel.reduce((a,d) => a + d.rated, 0);

  $('#kEplConvs').textContent = fmtInt(convs);
  $('#kEplConvsNote').textContent =
    `${fmtInt(runs)} runs across ${fmtInt(sel.length)} deployment`
    + (sel.length === 1 ? '' : 's');
  $('#kEplUsers').textContent = fmtInt(users);

  // Growth claim, stated only when it is actually supported: compare the most recent
  // complete week against the first week that had any user at all.
  const weekly = eplWeekly(deps);
  const lastComplete = E.partialWeek === lastW ? lastW - 1 : lastW;
  const firstActive = weekly.findIndex(r => r.users > 0);
  const nowU = lastComplete >= 0 ? weekly[lastComplete].users : 0;
  const thenU = firstActive >= 0 ? weekly[firstActive].users : 0;
  $('#kEplUsersNote').textContent = (firstActive >= 0 && nowU)
    ? `${thenU} in week 1 → ${nowU} in the latest full week`
    : 'distinct people, counted once across deployments';
  $('#kEplAgents').textContent = fmtInt(agents);
  $('#kEplAgentsNote').textContent = 'agents that have served real traffic';

  if (rated){
    $('#kEplComplaint').textContent = (100*negC/convs).toFixed(1) + '%';
    $('#kEplComplaintNote').textContent =
      `${fmtInt(negC)} of ${fmtInt(convs)} conversations · ${fmtInt(pos)} 👍 / `
      + `${fmtInt(neg)} 👎 turns from ${fmtInt(rated)} rated (a floor: `
      + `${(100*rated/convs).toFixed(1)}% submitted feedback)`;
  } else {
    $('#kEplComplaint').textContent = '—';
    $('#kEplComplaintNote').textContent = 'no feedback submitted for this selection';
  }

  const complete = E.partialWeek >= 0 ? E.weeks.length - 1 : E.weeks.length;
  $('#cEplNote').textContent =
    `Monday-anchored weeks. ${fmtInt(complete)} complete week`
    + (complete === 1 ? '' : 's')
    + (E.partialWeek >= 0
        ? `, plus ${E.weeks[E.partialWeek]} still in progress (drawn hollow).` : '.')
    + ' People is a distinct count, so it never sums deployments.';

  drawEplWeekly(deps);
  renderEplDeps(deps);
  renderEplCauses(deps);
}

/* ---------- top-level render ---------- */
function render(){
  renderSlice();
  if (state.view === 'adoption') renderAdoption();
  else if (state.view === 'feedback') renderFeedback();
  else if (state.view === 'epl') renderEpl();
}

/* ---------- wiring ---------- */
// All-time count, seeded once, matching dashboard.html -- the badge is a standing
// total rather than a reflection of the current slice.
$('#tabFbCount').textContent = fmtInt(P.fbTurns.length);

// Filter option lists come from the payload, so they can never drift from the data.
for (const pf of P.portfolios){
  const o = document.createElement('forge-option');
  o.value = pf; o.textContent = pf;
  $('#fPortfolio').appendChild(o);
}
for (const pr of P.products){
  const o = document.createElement('forge-option');
  o.value = pr; o.textContent = pr;
  $('#fProduct').appendChild(o);
}
const tenantsUsed = [...new Set(P.fbTurns.map(t => t.tn))]
  .filter(i => i >= 0).map(i => [i, P.tenants[i]])
  .sort((a,b) => a[1].localeCompare(b[1]));
for (const [i,name] of tenantsUsed){
  const o = document.createElement('forge-option');
  o.value = String(i); o.textContent = name;
  $('#fTenant').appendChild(o);
}
for (const l of P.fbLabels){
  const o = document.createElement('forge-option');
  o.value = l; o.textContent = LABEL_TEXT(l);
  $('#fLabel').appendChild(o);
}

// EPL scope. The original was a flat list with leading spaces to fake indentation;
// forge-option-group gives the same two-tier read without the whitespace hack.
// `label` is a PROPERTY on forge-option-group, not an attribute -- setAttribute is
// silently ignored.
$('#tabEplCount').textContent = fmtInt(E.deps.length);
{
  const sel = $('#fEplScope');
  const byType = document.createElement('forge-option-group');
  byType.label = 'By deployment type';
  for (const t of E.typeOrder){
    const members = E.deps.filter(d => d.type === t);
    if (!members.length) continue;
    const o = document.createElement('forge-option');
    o.value = 'type:' + t;
    o.textContent = `${t} (${members.length})`;
    byType.appendChild(o);
  }
  if (byType.children.length) sel.appendChild(byType);

  // Individual deployments, busiest first, so the list is useful rather than alphabetical.
  const each = document.createElement('forge-option-group');
  each.label = 'Individual deployments';
  for (const [i,d] of E.deps.map((d,i) => [i,d]).sort((a,b) => b[1].convs - a[1].convs)){
    const o = document.createElement('forge-option');
    o.value = 'dep:' + i;
    o.textContent = `${d.name} — ${fmtInt(d.convs)} convs`;
    each.appendChild(o);
  }
  if (each.children.length) sel.appendChild(each);

  sel.addEventListener('change', e => {
    const v = e.target.value ?? 'all';
    eplState.scope = v === 'all' ? '' : v;   // '' is "everything shipped" internally
    renderEpl();
  });
}

const reset = () => { state.limit = 40; };
$('#fRange').addEventListener('change', e => { state.range = e.target.value || 'all'; reset(); render(); });
$('#fPortfolio').addEventListener('change', e => { state.portfolio = e.target.value ?? ''; reset(); render(); });
$('#fProduct').addEventListener('change', e => { state.product = e.target.value ?? ''; reset(); render(); });
$('#fFocus').addEventListener('forge-button-toggle-group-change', e => {
  state.focus = hasVal(e.detail, 'focus'); reset(); render(); });
$('#fSandbox').addEventListener('forge-button-toggle-group-change', e => {
  state.sandbox = hasVal(e.detail, 'sandbox'); reset(); render(); });
$('#fTenant').addEventListener('change', e => { state.tenant = e.target.value ?? ''; reset(); render(); });
$('#fLabel').addEventListener('change', e => { state.label = e.target.value ?? ''; reset(); render(); });
$('#fSev').addEventListener('change', e => { state.sev = e.target.value ?? ''; reset(); render(); });
$('#fQuery').addEventListener('input', e => { state.q = e.target.value; reset(); render(); });
$('#fAgent').addEventListener('input', e => { state.agentQuery = e.target.value; renderAgents(); });
const agentTenants = [...new Set(P.agents.map(a => a.tenant).filter(Boolean))].sort();
for (const t of agentTenants){
  const o = document.createElement('forge-option');
  o.value = t; o.textContent = t;
  $('#fAgentTenant').appendChild(o);
}
$('#fAgentTenant').addEventListener('change', e => {
  state.agentTenant = e.target.value ?? ''; renderAgents(); });
$('#fCommented').addEventListener('forge-button-toggle-group-change', e => {
  state.commented = hasVal(e.detail, 'commented'); reset(); render(); });
$('#fbMore').addEventListener('click', () => { state.limit += 40; renderFeedback(); });

// The group's change detail is a value or an array of values depending on
// single/multi selection; normalise rather than assume.
function hasVal(detail, v){
  return Array.isArray(detail) ? detail.includes(v) : detail === v;
}

// Legend chips toggle series visibility. Never let the last series be hidden --
// an empty chart with a full legend reads as broken data.
$('#lg1').addEventListener('forge-chip-select', e => {
  const k = e.target.value;
  state.hiddenSeries.has(k) ? state.hiddenSeries.delete(k) : state.hiddenSeries.add(k);
  const total = $('#lg1').querySelectorAll('forge-chip').length;
  if (state.hiddenSeries.size >= total) state.hiddenSeries.delete(k);
  renderAdoption();
});

// Chart/table toggles.
for (const g of document.querySelectorAll('[data-toggle]')){
  g.addEventListener('forge-button-toggle-group-change', e => {
    const id = g.dataset.toggle;
    const showTable = hasVal(e.detail, 'table');
    $('#'+id).classList.toggle('hidden', !showTable);
    g.closest('forge-card').querySelector('svg.chart')
      .classList.toggle('hidden', showTable);
  });
}

const VIEWS = ['adoption', 'feedback', 'epl'];
$('#tabs').addEventListener('forge-tab-bar-change', e => {
  state.view = VIEWS[e.detail.index];
  $('#viewAdoption').classList.toggle('hidden', state.view !== 'adoption');
  $('#viewFeedback').classList.toggle('hidden', state.view !== 'feedback');
  $('#viewEpl').classList.toggle('hidden', state.view !== 'epl');
  // The tenant filter is meaningful only on the feedback tab.
  $('#fTenant').classList.toggle('hidden', state.view !== 'feedback');
  // The EPL tab ignores the global filters by design. dashboard.html leaves them
  // visible with a note; hiding them removes the chance of someone changing a control
  // mid-presentation and wondering why nothing moved. Same numbers either way.
  const eplView = state.view === 'epl';
  $('#globalFilters').classList.toggle('hidden', eplView);
  $('#slicechip').classList.toggle('hidden', eplView);
  // Charts need a laid-out container to measure, so a hidden view is never drawn --
  // switching re-renders on the way in.
  render();
});

$('#themeBtn').addEventListener('click', () => {
  const sheet = $('#darkCss');
  const dark = sheet.disabled;          // about to be enabled
  // A stylesheet shipped `disabled` is not fetched until first enabled, so on the very
  // first toggle the tokens still resolve to the outgoing theme. The charts read their
  // colours from those tokens, so repainting too early bakes in the wrong palette.
  // Hook the sheet's own load event for that first fetch.
  if (dark && !sheet.sheet) sheet.addEventListener('load', render, {once:true});
  sheet.disabled = !dark;
  $('#themeBtn').setAttribute('aria-pressed', String(dark));
  // Deliberately setTimeout and not requestAnimationFrame: rAF is throttled to a
  // standstill in a background or non-visible tab, which silently skips the repaint
  // (and makes this untestable under browser automation). A task always runs.
  setTimeout(render, 0);
  setTimeout(render, 150);   // after the new sheet has certainly applied
});

/* Redraw when a chart's card changes width. Observing the card rather than the SVG
   avoids a feedback loop, since drawing mutates the SVG's own children. */
let resizeT = 0;
const ro = new ResizeObserver(() => {
  clearTimeout(resizeT);
  resizeT = setTimeout(render, 60);   // a task, not a frame -- see the theme handler
});
for (const s of document.querySelectorAll('svg.chart')) ro.observe(s.closest('forge-card'));

// Defaults that mirror dashboard.html: sandbox included, comment filter on.
customElements.whenDefined('forge-button-toggle').then(() => {
  const sb = $('#fSandbox forge-button-toggle');
  if (sb) sb.selected = true;
  const cm = $('#fCommented forge-button-toggle');
  if (cm) cm.selected = true;
});

// Forge components must upgrade before the cards have their real width, otherwise
// the first chart draw measures an unstyled box and the bars land in the wrong place.
Promise.all(['forge-card','forge-tab-bar','forge-select','forge-table','forge-chip']
  .map(t => customElements.whenDefined(t)))
  .then(() => setTimeout(render, 0));

render();
window.__render = render;   // handles for verify_forge_poc.js
window.__state = state;
window.__aggregate = aggregate;
window.__eplState = eplState;
window.__renderEpl = renderEpl;
</script>
</body>
</html>
"""


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    payload = poc_payload(conn)
    html = HTML.replace("__PAYLOAD__", json.dumps(payload, separators=(",", ":")))
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT.name}  ({len(html.encode()) / 1024:,.0f} KB, "
          f"{len(payload['fbTurns']):,} turns, {len(payload['agents']):,} agents, "
          f"{len(payload['facts']):,} fact rows)")


if __name__ == "__main__":
    main()
