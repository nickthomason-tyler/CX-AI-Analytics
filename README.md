# Foundry Analytics Warehouse (Phase 0)

Pulls Foundry execution logs into a local SQLite warehouse so adoption, cost, and
reliability can be sliced by **Tyler portfolio and product** — a dimension Foundry itself
does not have.

```bash
python3 extract.py dims                       # projects/agents/teams/workflows + ownership tags
python3 extract.py facts --since 2025-01-01   # backfill runs (resumable, ~30 min)
python3 extract.py facts --incremental        # daily top-up (seconds)
python3 extract.py workflows                  # visual-workflow executions (fan-out over definitions)
python3 extract.py retag                      # re-apply product_taxonomy.json, no API calls
python3 extract.py feedback --since 2026-01-01 # thumbs up/down turns + written complaints
python3 extract.py status                     # row counts + validation against the live API
python3 label_feedback.py                     # classify complaints into a failure taxonomy
python3 build_dashboard.py                    # render dashboard.html
```

Daily refresh:

```bash
python3 extract.py facts --incremental \
  && python3 extract.py feedback \
  && python3 label_feedback.py \
  && python3 build_dashboard.py
```

Credentials are read from the Foundry plugin's `foundry.local.md` (same source as
`foundry-api.sh`), or `FOUNDRY_API_KEY` / `FOUNDRY_BASE_URL` / `FOUNDRY_TENANT_SLUG`.

## Why it extracts everything, not just your agents

Log rows carry **no project and no product-team field** — only `agent.id`. Ownership is
therefore a *join*, not a filter. Extracting only one team's agents would make
cross-team comparison impossible without a second full backfill, and the marginal cost
of "everything" is near zero: tenant-wide traffic is ~2,000 runs/day, so the daily
incremental is 2–4 API pages. Filter in the dashboard, not in the extractor.

## Ownership: Tyler portfolio > product

Foundry has no product-team field, and its "projects" are overwhelmingly personal
sandboxes (`Jane Doe - project`, `Sam's Foundry Work`). Ownership is therefore
curated in `product_taxonomy.json`, which encodes the **Tyler Product Architecture
Guide** hierarchy (portfolio > application suite > application > module > feature). We
tag at the *application* level and roll up to portfolio.

Rules assign a **product**; the portfolio is looked up from the canonical
`portfolios` table, so the two can never disagree. First match wins.

| Field | Matches against | Notes |
|---|---|---|
| `project_ids` | exact project id | Most precise — prefer this |
| `project_regex` | project name | |
| `agent_regex` | agent name | Catches product agents living in personal sandboxes |
| `tenant_regex` | tenant name | |

Products in `focus_products` get `is_focus = 1` — the EPL-strategy scope from the guide
(EPL, ESR, EAM, Environmental Health, State Regulatory, Cannabis Licensing, Application
Platform, Payments, Resident AI Assistant, Appointment Scheduling, Engagement Builder,
Data & Insights, Identity). That flag is how the dashboard defaults to "my scope" while
keeping every other portfolio available for comparison.

Two non-product buckets exist deliberately, so internal work doesn't pollute product
numbers or vanish from them: **Internal Sandbox** (individually-owned scratch projects)
and **Platform Engineering / Internal Tooling**. Both sit under portfolio `Non-Product`.

**Rule ordering matters** and encodes real disambiguations:
- Cannabis Licensing is checked *before* the EPL regexes — it's a Regulatory product, not EPL.
- ESR is checked before EPL, so `ESR and EPL Orchestrator` lands in Service Requests.
- School ERP Pro is checked before ERP Pro, or the Schools product would be stolen by ERP.
- Internal Sandbox is last, so a product-named agent inside a personal project still
  tags to its product.

`Enterprise Environmental Health` appears under both Civic and Health & Human Services
in the guide. The loader detects this, resolves to the first-listed portfolio (Civic),
and prints the collision on every `dims`/`retag` run rather than hiding it.

### Iterating

```bash
python3 extract.py retag                      # no API calls, seconds
sqlite3 foundry_analytics.db "SELECT * FROM v_tagging_gaps LIMIT 20;"
```

`v_tagging_gaps` ranks what's still `Unattributed` **by run volume**, so you fix the
buckets that actually move the numbers instead of chasing dormant agents. Because
`portfolio`/`product` live only on dimension tables and facts reach them through views,
re-tagging never requires re-extraction.

#### `retag` must stay idempotent — check it

Some rules match on `tenant_regex` (School ERP Pro's `SEPRO`, Enterprise ERP's), and
~1,400 agents reach a product **only** via tenant: no project, and no fact rows either if
they never ran. `retag` has no API access, so if the tenant isn't in the database the rule
cannot fire and the agent silently falls to `Unattributed`. That was a real bug — `retag`
used to undo tags `dims` had just assigned, losing 19 agents (11 Enterprise ERP, 6 School
ERP Pro) per run while `status` still looked healthy. `dim_agent.tenant_name` now persists
the tenant and `retag` reads it, with the run facts as fallback.

After editing `product_taxonomy.json`, prove the round-trip:

```bash
sqlite3 foundry_analytics.db "CREATE TABLE _s AS SELECT agent_key, product FROM dim_agent;"
python3 extract.py retag
sqlite3 foundry_analytics.db "SELECT COUNT(*) FROM _s s JOIN dim_agent a USING(agent_key)
  WHERE IFNULL(s.product,'') <> IFNULL(a.product,'');"   # must be 0
sqlite3 foundry_analytics.db "DROP TABLE _s;"
```

#### Rule ORDER encodes intent, in both directions

Two blocks sit deliberately at the extremes of the file, for opposite reasons.

**At the top: rules that must beat a project claim.** An agent inherits whatever product
owns its project, and several personal projects are inside EPL's `project_ids` list — so
`Raleigh NC Website Assistant` in a personal sandbox project reported as EPL while the same
agent in the Raleigh NC tenant was correctly Resident AI Assistant. Same failure mode as
the warehouse's own classifier agent. These rules AND the agent name against a
personal-project shape so they only reach into scratch space.

**At the bottom: name-only fallbacks that must NOT beat a project claim.** The ERP side
ships agents under bare, unprefixed names (`Staff Assistant`, `Report Assistant`,
`Recruiting Assistant`, `User Administration Inquiry ...`), so a copy dropped into a demo
or preview tenant loses its project link and has nothing left to identify it. Sitting
last, these can only claim what nothing else wanted — the Unattributed pile — and cannot
re-home the many ERP agents legitimately in `Internal Sandbox` personal projects.

That placement is load-bearing, and getting it wrong is silent. An `^EAM\b` rule placed
next to the `EAM Project` rule *also* pulled 803 runs of EAM development out of
`Internal Sandbox`, which would have treated EAM's scratch work differently from
Enterprise ERP's own — `Enterprise ERP Payroll Document Architect` stays in
`Internal Sandbox` when it lives in a personal project. Moving the rule to the bottom
block fixed it. **After adding a rule, diff by `was → now`, not just by final counts**:
totals looked plausible while 803 runs moved for the wrong reason.

Every pattern in the bottom block is justified by a sibling of the *same agent name* that
IS attributed via a project called "Enterprise ERP" — not by guessing from the name. That
standard exists because guessing from the name is how `Staff Assistant` got read as EPL.

**EAM rolls up to Enterprise ERP** (confirmed 2026-08-10). `Enterprise Asset Management`
remains in the portfolio table but now carries **zero runs**; its four "EAM Project"
agents report as Enterprise ERP. Flip the `product` string on that rule to undo — it is a
one-word change and needs no re-extraction.

**Rule 0 must stay first.** This warehouse's own agents (`Feedback Failure Classifier`,
`Feedback Synthesis Analyst`) live in a personal sandbox project, which the EPL `project_ids`
rule would otherwise claim — and one `label_feedback.py` pass logs ~1,000 runs in a day,
which showed up as EPL adoption growing +433% instead of +208%. Rules are first-match-wins
and the general internal-harness rule sits after the product rules, so it cannot catch
them. Add any new warehouse-side agent to rule 0.

## Schema

Facts are one row per run, keyed on the log id, so re-ingesting a day is idempotent.

- `fact_agent_run` — tokens in/out/cached, `token_cost`, status, error type, duration,
  tool-call count and names, user, tenant, conversation
- `fact_team_run` — team runs, including inline feedback
- `fact_workflow_run` — visual-workflow executions (status, suspend point, duration)
- `fact_tool_call` — one row per tool span (opt-in via `--tool-calls`)
- `fact_feedback_turn` — **one row per rated conversation turn**: thumbs up/down, the
  user's written complaint, the question and response it was about, plus the
  classifier's `label` / `severity` / `fix_type`
- `dim_conversation` — one row per conversation that received feedback, with turn counts
- `raw_transcript` — full transcript JSON for those conversations (backs the classifier;
  never embedded in the dashboard)
- `dim_project` / `dim_agent` / `dim_team` / `dim_workflow` — with `portfolio`, `product`,
  `is_focus`, `tag_rule`
- `ingest_log` — per-day watermarks; makes the backfill resumable

Ready-made views:

| View | Answers |
|---|---|
| `v_agent_runs` / `v_team_runs` | Facts joined to ownership — the base for ad-hoc queries |
| `v_product_share` | **Relativity**: each product's % of all runs and all cost, with `$/run` and failure rate |
| `v_product_monthly` | Per-product monthly trend (runs, users, cost, latency, failures) |
| `v_portfolio_monthly` | Same, rolled up to portfolio |
| `v_agent_scorecard` | Per-agent leaderboard, all time |
| `v_daily_active_users` | Adoption curve by product |
| `v_dormant_agents` | Built but never run — the abandonment metric |
| `v_workflow_runs` | Workflow executions joined to ownership |
| `v_workflow_adoption` | Workflow definitions vs. executions per product |
| `v_tagging_gaps` | Unattributed traffic, ranked by volume |
| `v_feedback_turns` | Rated turns joined to ownership — the base for feedback queries |
| `v_feedback_scorecard` | **Per-agent negative rate**, against conversations actually held |
| `v_feedback_themes` | Failure category × product × month × severity × fix |
| `v_feedback_monthly` | Thumbs up/down volume over time |
| `v_labelling_gaps` | How much of the complaint corpus is still unclassified |

`dims` drops and rebuilds the dimension tables every run (they are fully derived from
the API), so taxonomy column changes need no migration. Facts are never touched.

## Gotchas encoded here

1. **Agent ids come in two forms.** Foundry emits both `5bf18a93-…` and
   `agent-5bf18a93-…` for the same agent. Always join on `agent_key` (prefix stripped),
   never the raw id.
2. **Rows are ~6 KB of repeated payload.** Every log row re-sends the agent's full
   instructions and every tool's JSON schema. A naive pull of all 321 K rows is ~21 GB;
   the extractor strips both on ingest and the resulting DB is a few hundred MB. There
   is no `fields`/projection parameter — I tested four spellings, all ignored.
3. **`/api/agent-logs` supports `startDate`, `endDate`, `agentId`, `status`** (camelCase
   only — `start_date` and `agent_id` are silently ignored and return unfiltered totals,
   which is an easy way to draw a very wrong conclusion).
4. **`limit=1` is a free `COUNT(*)`.** The `total` field respects filters, so a one-row
   request answers "how many runs for agent X in August" for ~nothing. Used for month
   probing and for `status` validation.
5. **`/foundry/v1/projects` caps `limit` at 500**; `/api/agent-logs` accepts 1000.
6. **The pre-aggregated analytics endpoints disagree with each other**
   (`usage-details` reported 660 users / 86,856 conversations vs
   `summary-statistics` 525 / 77,151 over near-identical windows). Compute from the
   facts instead so numbers are defensible.
7. **`/api/analytics/stats/usage-details` and `/advanced/summary-statistics` are async** —
   they return `{status:"computing", jobId}` and need the same URL requested again.
   `FoundryClient.get_async_job` handles it.
8. **Tenant token metering is off** (`/api/tenants/current/token-usage` →
   `enabled:false`, `tokensUsed:0`). Cost must be summed from `metadata.tokenCost`.
9. **`/api/analytics/user-activity` returns 403** — admin-scoped. Not used.
10. **`/api/agent-logs/filters/agents` returns an empty list** — looks broken; the agent
    dimension is built from `/foundry/v1/projects/{id}/agents` plus
    `/api/transcripts/agents` as a fallback instead.
11. **~230 agents appear in the logs but in no enumerable project** (deleted since, or in
    a tenant this key can't list) — 16,934 runs, 5% of all traffic. A `LEFT JOIN` to
    `dim_agent` silently drops these to `Unattributed` no matter how good the rules are.
    `seed_agents_from_facts()` creates dim rows from the agent name recorded on the fact
    rows; it runs at the end of both `dims` and `retag`. **Always sanity-check the join,
    not just the tagging rules** — the first tagging pass looked like a rules problem and
    was 27% a coverage problem.
12. **An incremental resume must re-fetch its last day.** The day the backfill ended was
    partial, and the `ingest_log` watermark marks it done — so a naive resume filter skips
    it and the warehouse silently stops advancing while `status` still looks healthy. The
    fix is `refetch_days`; re-ingest is idempotent (primary key on the log id) so it costs
    only time. Check `extract.py status` — a growing `delta` against the API means the
    incremental isn't landing.
13. **Tag by run volume, not agent count.** `dim_agent` includes 472 agents (26%) that
    have never produced a run, so per-agent tallies badly misrepresent where traffic
    actually is. `v_tagging_gaps` deliberately ranks by runs.

## The dashboard

`build_dashboard.py` emits **one self-contained `dashboard.html`** (~620 KB) — no
server, no dependencies, no network. Aggregates are embedded as JSON and every filter
recomputes client-side, so the file can be mailed or dropped in SharePoint and still work.

**Filters** (one row, scoping everything below): date range · portfolio · product ·
*Focus only* (the `is_focus` scope) · *Incl./Excl. non-product* (sandbox + platform work) ·
light/dark.

**Views:** hero run count + 5 stat tiles · runs per month by portfolio (multi-line) ·
distinct users per month · share of runs by product · cost per run · failure rate ·
top error classes · sortable agent leaderboard · **visual workflow executions** ·
**workflows built vs. ever run** · dormant agents · unattributed traffic.

Three things that are easy to get wrong and are handled deliberately:

1. **Distinct counts are not additive.** Summing per-product user counts double-counts
   anyone who used two products. The payload embeds `(user × day × product)` and
   `(agent × day × product)` presence tuples (~7.5 K and ~7.9 K rows) so users and
   active agents are **exact under every filter combination**, not approximated.
2. **Colour follows the portfolio, not its rank.** Slots are fixed from all-time volume
   in Python (`portfolioSlot`) and never recomputed per slice — otherwise filtering
   repaints the survivors and a reader who learned "ERP is blue" is misled. The long
   tail is a permanent `Other`, itself a stable identity.
3. **Conversation counts are deliberately absent.** `COUNT(DISTINCT conversation_id)`
   summed over day × product overcounts conversations spanning either boundary
   (~89.9 K vs the true ~86.9 K). Rather than publish a number that's quietly wrong,
   the tile shows tokens and runs/month.

The palette is the dataviz skill's validated reference instance, slots 1–6, checked with
`validate_palette.js` in **both** modes (light: worst adjacent CVD ΔE 9.1, normal-vision
19.6; dark: 8.4 / 19.3 — all gates pass). Light mode raises a sub-3:1 contrast WARN on
three hues, which obligates relief: every chart ships a **Table** toggle. This is a
brand-neutral analytics palette, not Tyler brand colours — for client-facing output use
the `tyler-branded-pdf` skill instead.

## Visual workflows (the third fact table)

There is **no aggregate workflow-run log**. Agent runs live at `/api/agent-logs` and team
runs at `/api/team-logs`, but workflow executions are only reachable per-workflow via
`/api/visual-workflows/{workflowId}/history`. `extract.py workflows` fans out over
`dim_workflow` — one request per definition, so **cost scales with definitions, not
executions** (424 requests to collect 22 rows). It runs automatically as part of `facts`;
`--skip-workflows` opts out.

```bash
python3 extract.py workflows          # ~424 requests, about a minute
```

The response envelope is `{success, workflowId, workflowName, count, executions[]}`, and
each execution carries `id, workflowId, status, createdAt, completedAt, suspendedAt,
resumedAt, resumedBy, error, suspendedStep{nodeId, stepName, reason}`. Note what is
**absent**: no token usage, no cost, no user. `fact_workflow_run` is therefore
deliberately narrower than `fact_agent_run` — it can answer *did it run, did it finish,
where did it stall*, and nothing about spend or adoption breadth. That's also why
workflows get their own dashboard panel instead of joining the cost and user charts.

Views: `v_workflow_runs` (facts + ownership) and `v_workflow_adoption` (definitions vs.
executions per product — the build-vs-run reality check).

**What closing the gap actually showed:** 22 executions across 6 of 424 definitions
(1.4% ever ran), and **all 22 are `suspended` — none completed, failed, or was ever
resumed.** Every one is parked at a human-in-the-loop step ("Waiting for user to add
file", "High risk score detected… requiring manual examiner review", "Ask: What Type of
Help?"). Visual workflows aren't failing here; they're being abandoned mid-flight at the
point a human is needed. 7 definitions returned an error (likely deleted) and are counted
and reported, not silently dropped.

## Negative feedback (the fourth fact table)

The question this answers: **every time a user leaves negative feedback with a comment,
what did they say, and what pattern do the comments add up to?**

### Sourcing it: the obvious endpoints are a trap

`/api/analytics/feedback` and `/api/analytics/feedback-trends` are **scoped to the calling
user**. They return your own handful of rows and look authoritative — using them
tenant-wide reports ~6 pieces of feedback for the whole tenant instead of the real
several thousand. There is no parameter to widen them.

The tenant-wide path is the transcripts route, in two phases:

```
GET /api/transcripts/conversation_ids?feedback=negative|positive   # discovery
GET /api/transcripts/{conversationId}                             # ratings + comments
```

- `feedback` takes **`negative` / `positive`** — *not* the `THUMBS_DOWN` / `THUMBS_UP`
  values the transcript body uses. Sending the latter is a 400.
- **`limit` and `offset` are silently ignored**, and every response truncates at **200
  rows**. `startDate` / `endDate` *do* work, so `discover_feedback_conversations()` pages
  by date window: request a window, and if it returns exactly 200, split it and request
  each half. A single day that saturates cannot be split further — those are collected
  and reported as an explicit undercount warning rather than passed over.
- Discovery returns **no comment text** — only conversation ids. The ratings and comments
  cost one transcript request each (~37 KB), which is why the walk is thousands of
  requests long and commits in batches of 50.
- Foundry **throttles hard (429)** once the fan-out widens. `foundry_client` waits these
  out on a separate, generous budget instead of consuming the retry count, because a
  fan-out that gave up would report throttled conversations as *missing data* — which
  reads identically to "these conversations were deleted".

### Feedback is per *turn* — a grain the run facts cannot express

Users rate individual turns, and one conversation routinely carries several thumbs-down
scattered among neutral ones (a real example: 6 downvotes across 13 turns). So
`fact_feedback_turn` is turn-grained, and `NO_ACTION` turns are excluded — they are the
overwhelming majority and carry no signal. `dim_conversation.turn_count` keeps the
denominator so *"3 of 13 turns were downvoted"* stays answerable.

`conversation_id` already exists on `fact_agent_run`, so feedback inherits portfolio,
product, user, model and cost by join — no new taxonomy work.

### Tenant is what tells two agents apart

Agent names collide badly, and the collisions are not cosmetic: **`Staff Assistant` is
seven distinct agents across seven tenants, and they span two different products** (one
sits in Enterprise ERP, the rest are Unattributed). Almost every feedback-bearing agent
has no project either, so `project_name` cannot separate them. Tenant can.

`v_feedback_scorecard` was always keyed on `agent_key`, so the *rows* were right — they
just rendered as seven identical lines reading "Staff Assistant". The feedback tab now
carries a **tenant filter**, a **Tenant column** on the scorecard, and a tenant pill on
every complaint; the "Negative rate by agent" chart qualifies a bar's label with its
tenant only when that name is ambiguous among the bars actually drawn.

Two things this forced:

- `dim_agent.tenant_name` is now persisted, and `v_agent_tenant` resolves one tenant per
  agent (`dim_agent` first, busiest tenant in the run facts as fallback). Together they
  cover every agent that has feedback; `dim_agent` alone covers only 49 of 131.
- `fbCounts` in the dashboard payload is grouped by tenant as well as day and product.
  Positives live only in that fact, so without the tenant column a filtered view would
  show one tenant's thumbs-down against **every** tenant's thumbs-up — which reads as a
  collapse in satisfaction rather than a filter.

The feedback-submission-rate KPI **blanks itself** under a tenant filter. Its denominator
is conversations held, which comes from the run facts — grouped by day and product only,
with no tenant column. Narrowing the numerator alone would understate the rate by roughly
the tenant's share of traffic, so it shows `—` and says why.

## The EPL solutions tab

Built for the Quarterly Product Strategy Meeting: *what has shipped, who is using it
week over week, and how is it performing.* It is **presented from**, which drives most
of its design decisions.

`EPL_DEPLOYMENTS` in `build_dashboard.py` is the curated list of shipped deployments and
their type (Support / Private preview / Customer). Foundry records a tenant name and
nothing about whether that tenant is a customer, a preview or a sandbox, so this
mapping cannot be derived — it was confirmed directly on 2026-08-10.

**Two tenant names mean the opposite of what they look like.** `City of Encinitas, CA
(Test)` and `Coral Gables, FL (Train)` are the environments those customers actually
work in, so they are real customer usage, not probing. Do not filter them out as test
traffic. `City of AI, ME` is a private preview where users from many client
organisations shared one environment, which makes its user count the best multi-client
evidence available — but note it is a **mixed-product tenant**: its `Staff Assistant`
(1,540 runs) is Enterprise ERP, not EPL, confirmed from conversation content (0 EPL
keywords against 52 ERP ones, one user referring to "the Munis screen"). Only the
EPL-tagged agents there belong to EPL.

**Encinitas is listed twice, on purpose.** The customer cut over from
`City of Encinitas, CA (Test)` to the production tenant `City of Encinitas, CA` on
**2026-08-14** — (Test)'s last run and production's first run are the same day — so
both are Customer and both are listed. Drop either and the adoption curve breaks at
the cutover: (Test) alone reads as a customer that went dormant on 08-14, production
alone as one that only started last week. The production tenant is also
**mixed-product** (its `Resident AI Assistant`, 440 runs, is Tyler One), so it is safe
in this tab only because every query scopes on `product = EPL_PRODUCT` as well as
`tenant_name` — the same trap as `City of AI, ME`.

Adding it was two steps, and the second is the one that is easy to miss: the tenant's
agents are invisible to `extract.py dims`, because this API key cannot enumerate that
tenant's projects. They arrive only through `seed_agents_from_facts`, i.e. via
`extract.py retag` (every Encinitas agent's `tag_rule` ends in `+from_facts`).
`refresh.sh` runs neither `dims` nor `retag` in either mode, so a brand-new tenant's
agents sit in **Unattributed** — with runs, and invisible to the EPL tab — until
someone runs `retag` by hand. Encinitas production sat that way for three days.

Design decisions worth keeping:

- **The tab ignores the global date / portfolio / product filters**, and hides both the
  filter bar and the slice chip while it is open. A stray filter silently restating the
  numbers mid-presentation is the failure mode being designed out.
- **Conversations, users and agents all ship as `(week, deployment, index)` presence
  tuples.** Every one of them overcounts if aggregated naively: users sum to 70 against
  a true 53 (the same person appears in several deployments), and conversations sum to
  379 against a true 366 (10 conversations span a week boundary). Weekly rows are
  labelled "conversations **active**" for that reason, and totals always come from
  `eplDistinct()` over the full range — never from summing the bars.
- **Weeks are Monday-anchored with explicit arithmetic**, not sqlite's `weekday 1`,
  which resolves to *today* when today is already a Monday and so files Mondays into
  the previous week.
- **The final week is drawn hollow and the users line stops at the last complete week.**
  A part-week charted as a whole one invents a collapse in usage.
- **The week axis is trimmed to the weeks the current selection was live**
  (`eplWeekSpan()`), and the caption states that span. `E.weeks` covers every shipped
  deployment, which is right for "all" and wrong for one: scoped to EPL CX (live
  2026-07-10) the full axis drew sixteen empty weeks — four months of blank chart —
  before the first bar. Only the empty head and tail are dropped. **Interior gaps stay**:
  a dormant week inside a live run is real (2026-06-22 is one), and squeezing it out
  would make the timeline read as tighter than it was.
- **Each deployment row opens to the agents behind it** (`E.depAgents`, one entry per
  `agent_key`). Runs, convs and rated/👍/👎 reconcile exactly to the row above — no
  conversation in this data touches two agents inside one tenant, and
  `v_feedback_turns` pins each agent to a single tenant — so the rollup is a true sum
  there. **Users is the exception and is never totalled**: summing the sub-rows gives
  103 against a true 53. Where two agent records share a display name inside one
  deployment (a copy, or a rebuild under a new id — `City of AI, ME` has two
  `EPL Staff Assistant`), the id fragment is appended, but only where it is needed to
  tell them apart.
- **A deployment with no feedback shows `—`, never 0%.** Four of nine have never
  received a rating; "0% complaints" would be a claim the data cannot support.
- **Feedback is carried at two grains and they are never mixed.** `pos`/`neg` are rated
  **turns**; `posC`/`negC` are the **conversations** those turns sit in; `rated` is
  conversations with any feedback. The complaint *rate* uses `negC`, because the claim
  is a share *of conversations*. This was wrong until 2026-08-11: it divided 41 complaint
  turns by 379 conversations and reported **10.8% against a true 7.4%** — Encinitas read
  **70.8% against 45.8%** — because ten conversations carry 2+ complaint turns and one
  City of AI conversation carries four. It was caught from the table not adding up
  (9 👍 + 5 👎 against 13 rated on EPL CX). The columns are now labelled
  `Rated convs` / `👍 turns` / `👎 turns` so the grains are legible and the addition is
  no longer invited. **`rated` is not `posC + negC`**: 7 shipped conversations contain
  both a 👍 and a 👎, so those two overlap rather than partition — the verifier asserts
  `posC + negC - overlap == rated`. The fix list below the table stays at turn grain and
  says so ("41 thumbs-down turns across 28 conversations"), because two complaints in one
  conversation are two things to fix.
- **Deployments that are not shipped are listed in the tab**, not silently dropped —
  `Tyler Technologies` alone is 4,380 runs of dev sandbox, larger than every shipped
  deployment combined.

### Verifying the dashboard

```bash
python3 build_dashboard.py && node verify_dashboard.js dashboard.html
```

`verify_dashboard.js` pulls the payload *and the functions themselves* back out of the
built HTML and runs them in node, so it tests shipped code rather than a
re-implementation. It asserts the aggregation invariants against SQL truth — distinct
users, distinct conversations, per-deployment scoping, and that summing the weekly bars
really does overcount (so a future edit cannot quietly go back to summing them). It
caught the 379-vs-366 conversation bug.

It cannot catch rendering faults. For those, view the file in a browser — but note
**`file://` URLs are blocked by the Chrome extension**, so serve it:

```bash
python3 -m http.server 8765 --bind 127.0.0.1    # NOT 3000 (excalidraw uses it)
```

Two chart bugs only ever showed up visually: the users line ran into the partial week and
drew a collapse that was just "the week isn't over", and the dual axes were divided by
different step counts so the right-hand one stopped at 4 instead of reaching 0. Neither
was detectable from the data. The axis-trim bug was a third — the numbers were right, the
chart was four-fifths empty. `verify_dashboard.js` now asserts the span invariants
(`lo`/`hi` land on live weeks and no live week falls outside them) for every deployment,
which is checkable from the data even though "it looks empty" is not.

On framing: the headline metric is **"X% of conversations produced a complaint,"** stated
as a floor with the submission rate beside it. The tempting alternative — "97%
positive/neutral" — treats everyone who never rated as satisfied, and the same EPL CX
data also supports "5 of 15 raters were unhappy." The floor framing survives someone
recomputing it in the room — which is the whole point, and is also how the grain bug
above got caught. **If the words say "of conversations," the numerator has to be
conversations.**

### Rate, not count

Raw negative counts just re-rank agents by traffic volume (ERP Pro is ~53% of all runs, so
it "looks worst" by construction). `v_feedback_scorecard` therefore reports:

- `negative_share_pct` — thumbs-down as a share of *rated* turns (the satisfaction signal)
- `negatives_per_100_convs` — against conversations **actually held**, from the run facts,
  not against conversations that happened to get rated (the exposure signal)
- `feedback_rate_pct` — how often anyone rates at all, which is **~1–2%**. Every rate here
  is a floor, never an absolute quality measure.

The dashboard's agent-rate chart requires 250+ conversations, or a three-conversation
pilot with one downvote tops the chart at 33 per 100.

### Classifying the complaints

`label_feedback.py` judges each commented thumbs-down into a fixed taxonomy — `label`
(what broke), `severity` (`misleading` / `unhelpful` / `minor`), `fix_type` (where the
correction belongs: `knowledge_base` / `instructions` / `routing` / `model` / `none`),
plus a confidence and a one-line rationale.

The judge is a dedicated Foundry agent, **Feedback Failure Classifier**
(`ef725813-6f63-4826-9f69-087854995635`, backed up in
`foundry-agents/agents/feedback-failure-classifier/`), so this needs no credential beyond
the Foundry key. Two facts about `POST /api/agents/{id}/generate` shape the code:

- `structuredOutput` **must be wrapped**: `{"structuredOutput": {"schema": {...}}}`.
  Passing the bare JSON Schema returns **HTTP 500**.
- Some inputs make structured output **500 deterministically** while the identical prompt
  succeeds without it (2 turns in ~990, both short questions carrying lists of record ids).
  `classify()` therefore retries unconstrained on a 500 and parses the object out of the
  prose — the model returns the right answer either way, just fenced in a code block.
  Without that fallback those rows are silently lost.
- the per-request **`instructions` override is silently ignored** — the agent always runs
  its stored prompt. That is why the taxonomy lives in the agent's own instructions, and
  why *editing the taxonomy means updating that agent*, not this script. (Also note
  agents are created via `POST /api/configurable-agents`, not `/api/agents`, and the
  response nests the agent under an `agent` key.)

Labels are written to `fact_feedback_turn` and **never touched by `extract.py`** — the
feedback upsert updates only the extraction columns, so re-extracting a conversation does
not discard classification work. Re-runs only pick up still-unlabelled rows, so an
interrupted pass is safe to resume.

```bash
python3 label_feedback.py --limit 50        # a batch, to sanity-check first
python3 label_feedback.py --report          # summarise what's classified (no API calls)
python3 label_feedback.py --brief "ERP Pro" # dump one product's corpus to read yourself
```

**Use 2 workers, not more.** A single `/generate` call takes ~7 s, so more concurrency
looks like the obvious win — it isn't. Foundry's rate limiter punishes width: at 6 workers
throughput briefly tripled, then collapsed to **zero** with every worker parked in backoff
and no open sockets at all, while a serial caller went straight through. 2 workers sustains
~850 turns/hour, so ~900 complaints is about an hour. The client's 429 backoff is
deliberately shallow (1.5 s → 20 s cap) for the same reason: a steep curve keeps workers
asleep long after the limiter has cleared.

### Narrative briefs

Counts tell you *how much*; they don't tell you what to change. `--synthesize` writes a
per-product improvement brief — dominant themes with verbatim quotes, a recommended-changes
table, and an explicit "what this feedback cannot tell you" section — into
`feedback-briefs/<product>.md`.

```bash
python3 label_feedback.py --synthesize              # top products by complaint volume
python3 label_feedback.py --synthesize "ERP Pro"    # just one
```

The writer is a second dedicated agent, **Feedback Synthesis Analyst**
(create it in your tenant and save its id to `agents/feedback-synthesis-analyst/metadata.json`,
the same shape as the classifier's). Its instructions specifically require it to flag
when **one internal reviewer dominates a sample** — with a ~1–2% submission rate that is
the most common way this data misleads, and several complaints in an identical voice
usually means one tester doing a review pass, not a groundswell of user pain.

### Reading it in the dashboard

`dashboard.html` gains a **Negative feedback** tab sharing the existing date, portfolio,
product and scope filters. It carries the per-complaint text (comment, question, and a
truncated response) for every thumbs-down, searchable and filterable by category and
severity — roughly 1 MB. Full transcripts are *not* embedded; at ~37 KB each they would
run to tens of MB, so they stay in SQLite for the classifier.

One subtlety: the dashboard's day domain is the **union** of run days and feedback days.
Feedback typically runs a day or two ahead of the last ingested run day, and a
run-day-only domain silently drops the newest complaints from the default "all time"
slice — exactly the ones worth reading.
