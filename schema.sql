-- Foundry adoption/usage warehouse.
--
-- Design notes:
--  * product_group lives ONLY on the dimension tables. Facts join to it through
--    views, so re-tagging product ownership never requires re-extraction.
--  * agent_key / project_key are normalised join keys: Foundry emits agent ids
--    both bare ("5bf18a93-...") and prefixed ("agent-5bf18a93-..."), and the two
--    forms refer to the same agent. Always join on the *_key columns.

PRAGMA journal_mode = WAL;

-- ── Dimensions ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dim_project (
    project_id      TEXT PRIMARY KEY,
    name            TEXT,
    description     TEXT,
    tenant_id       TEXT,
    visibility      TEXT,
    owner_email     TEXT,
    member_count    INTEGER,
    agents_count    INTEGER,
    workflows_count INTEGER,
    teams_count     INTEGER,
    evals_count     INTEGER,
    mcp_count       INTEGER,
    created_at      TEXT,
    updated_at      TEXT,
    portfolio       TEXT,
    product         TEXT,
    is_focus        INTEGER,  -- 1 if in taxonomy focus_products (your strategy scope)
    tag_rule        TEXT      -- which rule assigned it (audit trail)
);

CREATE TABLE IF NOT EXISTS dim_agent (
    agent_key     TEXT PRIMARY KEY,   -- normalised: 'agent-' prefix stripped
    agent_id      TEXT,               -- id as returned by the project endpoint
    name          TEXT,
    description   TEXT,
    model         TEXT,
    version       TEXT,
    tenant_id     TEXT,
    -- Persisted because tenant_regex rules need it and 'retag' has no API access:
    -- ~1,400 agents share a handful of names ('Staff Assistant' covers 7 distinct
    -- agents across 7 customer tenants), so the tenant is both the tag input and
    -- the only human-readable way to tell those agents apart in the dashboard.
    tenant_name   TEXT,
    project_id    TEXT,
    project_name  TEXT,
    portfolio     TEXT,
    product       TEXT,
    is_focus      INTEGER,
    tag_rule      TEXT
);

CREATE TABLE IF NOT EXISTS dim_team (
    team_key      TEXT PRIMARY KEY,
    team_id       TEXT,
    name          TEXT,
    description   TEXT,
    project_id    TEXT,
    project_name  TEXT,
    portfolio     TEXT,
    product       TEXT,
    is_focus      INTEGER,
    agent_ids     TEXT                -- JSON array
);

CREATE TABLE IF NOT EXISTS dim_workflow (
    workflow_key  TEXT PRIMARY KEY,
    workflow_id   TEXT,
    name          TEXT,
    description   TEXT,
    project_id    TEXT,
    project_name  TEXT,
    portfolio     TEXT,
    product       TEXT,
    is_focus      INTEGER,
    node_count    INTEGER,
    is_active     INTEGER,
    created_by    TEXT,
    user_name     TEXT,
    created_at    TEXT,
    updated_at    TEXT
);

-- ── Facts ───────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS fact_agent_run (
    run_pk            TEXT PRIMARY KEY,   -- log.id; makes re-ingest idempotent
    run_id            TEXT,
    thread_id         TEXT,
    conversation_id   TEXT,
    agent_key         TEXT,
    agent_id_raw      TEXT,
    agent_name        TEXT,               -- denormalised: agents get renamed
    model_name        TEXT,
    model_provider    TEXT,
    temperature       REAL,
    finish_reason     TEXT,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    total_tokens      INTEGER,
    reasoning_tokens  INTEGER,
    cached_tokens     INTEGER,
    token_cost        REAL,
    status            TEXT,
    error_type_id     TEXT,
    error_message     TEXT,
    user_id           TEXT,
    user_name         TEXT,
    tenant_id         TEXT,
    tenant_name       TEXT,
    started_at        TEXT,
    ended_at          TEXT,
    duration_ms       INTEGER,
    tool_ms           INTEGER,
    tool_call_count   INTEGER,
    tool_names        TEXT,               -- JSON array, deduped
    run_day           TEXT                -- 'YYYY-MM-DD', from started_at
);

CREATE INDEX IF NOT EXISTS ix_agent_run_day   ON fact_agent_run (run_day);
CREATE INDEX IF NOT EXISTS ix_agent_run_agent ON fact_agent_run (agent_key);
CREATE INDEX IF NOT EXISTS ix_agent_run_user  ON fact_agent_run (user_id);
CREATE INDEX IF NOT EXISTS ix_agent_run_stat  ON fact_agent_run (status);

CREATE TABLE IF NOT EXISTS fact_team_run (
    run_pk           TEXT PRIMARY KEY,
    run_id           TEXT,
    thread_id        TEXT,
    conversation_id  TEXT,
    team_key         TEXT,
    team_id_raw      TEXT,
    team_name        TEXT,
    strategy         TEXT,
    model_name       TEXT,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    total_tokens     INTEGER,
    token_cost       REAL,
    status           TEXT,
    error_message    TEXT,
    user_id          TEXT,
    user_name        TEXT,
    tenant_id        TEXT,
    tenant_name      TEXT,
    feedback_type    TEXT,
    feedback_comment TEXT,
    started_at       TEXT,
    ended_at         TEXT,
    duration_ms      INTEGER,
    span_count       INTEGER,
    run_day          TEXT
);

CREATE INDEX IF NOT EXISTS ix_team_run_day  ON fact_team_run (run_day);
CREATE INDEX IF NOT EXISTS ix_team_run_team ON fact_team_run (team_key);

-- Visual-workflow executions. Foundry has no aggregate workflow-run log, so these
-- are collected by fanning out over /api/visual-workflows/{id}/history -- one request
-- per definition in dim_workflow. The payload carries no token usage, cost, or user,
-- so this table is deliberately narrower than fact_agent_run: it can answer "did it
-- run, did it finish, where did it stall" and nothing about spend or adoption breadth.
CREATE TABLE IF NOT EXISTS fact_workflow_run (
    run_pk           TEXT PRIMARY KEY,
    workflow_key     TEXT,
    workflow_id_raw  TEXT,
    workflow_name    TEXT,
    status           TEXT,
    created_at       TEXT,
    completed_at     TEXT,
    suspended_at     TEXT,
    resumed_at       TEXT,
    resumed_by       TEXT,
    error_message    TEXT,
    suspended_node   TEXT,
    suspended_step   TEXT,
    suspended_reason TEXT,
    duration_ms      INTEGER,   -- completed_at - created_at, when both are present
    run_day          TEXT
);

CREATE INDEX IF NOT EXISTS ix_wf_run_day ON fact_workflow_run (run_day);
CREATE INDEX IF NOT EXISTS ix_wf_run_wf  ON fact_workflow_run (workflow_key);

-- Optional (--tool-calls): one row per tool invocation span.
CREATE TABLE IF NOT EXISTS fact_tool_call (
    tool_call_pk  TEXT PRIMARY KEY,
    run_pk        TEXT,
    agent_key     TEXT,
    tool_id       TEXT,
    tool_name     TEXT,
    span_type     TEXT,
    status_code   TEXT,
    duration_ms   INTEGER,
    started_at    TEXT,
    run_day       TEXT
);

CREATE INDEX IF NOT EXISTS ix_tool_call_run  ON fact_tool_call (run_pk);
CREATE INDEX IF NOT EXISTS ix_tool_call_tool ON fact_tool_call (tool_id);

-- ── Conversation feedback (a third grain) ───────────────────────────────────
-- Users rate individual *turns*, not runs and not conversations: one conversation
-- routinely carries several thumbs-down scattered among neutral turns. Neither
-- fact_agent_run (run grain) nor a per-conversation table can express that, so
-- feedback gets its own fact at turn grain.
--
-- Sourcing this tenant-wide has exactly one viable path, and the obvious one is a
-- trap: /api/analytics/feedback and /api/analytics/feedback-trends are scoped to
-- the *calling user*, so they report a handful of rows and look like "the tenant
-- barely gives feedback". The real route is
--   /api/transcripts/conversation_ids?feedback=negative|positive   (discovery)
--   /api/transcripts/{conversationId}                              (turns + comments)
-- and conversation_ids silently ignores limit/offset behind a hard 200-row cap, so
-- discovery has to page by date window. See ingest_feedback() in extract.py.

CREATE TABLE IF NOT EXISTS dim_conversation (
    conversation_id   TEXT PRIMARY KEY,
    conversation_date TEXT,
    agent_key         TEXT,
    agent_id_raw      TEXT,
    agent_name        TEXT,   -- as of discovery; agents get renamed
    turn_count        INTEGER,
    rated_turns       INTEGER,
    negative_turns    INTEGER,
    positive_turns    INTEGER,
    commented_turns   INTEGER,
    discovered_by     TEXT,   -- 'negative', 'positive', or 'both'
    conv_day          TEXT
);

CREATE INDEX IF NOT EXISTS ix_conv_day   ON dim_conversation (conv_day);
CREATE INDEX IF NOT EXISTS ix_conv_agent ON dim_conversation (agent_key);

-- One row per rated turn. NO_ACTION turns are deliberately excluded -- they are
-- the overwhelming majority and carry no signal; dim_conversation.turn_count
-- preserves the denominator for "how much of the conversation was rated".
CREATE TABLE IF NOT EXISTS fact_feedback_turn (
    turn_pk          TEXT PRIMARY KEY,  -- conversation_id || ':' || turn_index
    conversation_id  TEXT,
    turn_index       INTEGER,
    rating           TEXT,              -- THUMBS_UP | THUMBS_DOWN
    comment          TEXT,              -- thumbsDownTextFeedback, when the user typed one
    question         TEXT,
    response         TEXT,
    question_chars   INTEGER,
    response_chars   INTEGER,
    agent_key        TEXT,
    agent_name       TEXT,
    conv_day         TEXT,
    -- Analysis columns. Written only by label_feedback.py, never by extraction,
    -- so a re-extract must not clobber them (see ingest_feedback's upsert).
    label            TEXT,   -- failure category (see LABELS in label_feedback.py)
    severity         TEXT,   -- misleading | unhelpful | minor
    fix_type         TEXT,   -- instructions | knowledge_base | routing | model | none
    label_confidence REAL,
    label_rationale  TEXT,
    labeler_model    TEXT,
    labeled_at       TEXT
);

CREATE INDEX IF NOT EXISTS ix_fb_turn_conv   ON fact_feedback_turn (conversation_id);
CREATE INDEX IF NOT EXISTS ix_fb_turn_rating ON fact_feedback_turn (rating);
CREATE INDEX IF NOT EXISTS ix_fb_turn_day    ON fact_feedback_turn (conv_day);
CREATE INDEX IF NOT EXISTS ix_fb_turn_agent  ON fact_feedback_turn (agent_key);
CREATE INDEX IF NOT EXISTS ix_fb_turn_label  ON fact_feedback_turn (label);

-- Full transcript JSON, kept because judging *why* a turn was downvoted needs the
-- surrounding turns (the complaint is often about context the flagged turn omits).
-- Bulky (~37 KB/conversation) and never embedded in the dashboard -- it backs the
-- labelling pass only.
CREATE TABLE IF NOT EXISTS raw_transcript (
    conversation_id TEXT PRIMARY KEY,
    payload         TEXT,
    fetched_at      TEXT
);

-- ── Ingest bookkeeping (makes the backfill resumable) ───────────────────────

CREATE TABLE IF NOT EXISTS ingest_log (
    source       TEXT,
    day          TEXT,
    api_total    INTEGER,
    rows_written INTEGER,
    fetched_at   TEXT,
    PRIMARY KEY (source, day)
);

-- ── Views: the analysis surface ─────────────────────────────────────────────
-- All views are dropped and recreated on every run: they are pure derivations,
-- so this keeps them honest when the dimension columns change.

-- Facts enriched with ownership. Runs whose agent never got tagged surface as
-- 'Unattributed' rather than silently dropping out of the totals.
DROP VIEW IF EXISTS v_agent_runs;
CREATE VIEW v_agent_runs AS
SELECT
    f.*,
    COALESCE(a.portfolio, 'Non-Product')   AS portfolio,
    COALESCE(a.product,   'Unattributed')  AS product,
    COALESCE(a.is_focus, 0)                AS is_focus,
    a.project_id,
    a.project_name,
    COALESCE(a.name, f.agent_name)         AS agent_name_current,
    substr(f.run_day, 1, 7)                AS run_month
FROM fact_agent_run f
LEFT JOIN dim_agent a ON a.agent_key = f.agent_key;

DROP VIEW IF EXISTS v_team_runs;
CREATE VIEW v_team_runs AS
SELECT
    f.*,
    COALESCE(t.portfolio, 'Non-Product')   AS portfolio,
    COALESCE(t.product,   'Unattributed')  AS product,
    COALESCE(t.is_focus, 0)                AS is_focus,
    t.project_id,
    t.project_name,
    substr(f.run_day, 1, 7)                AS run_month
FROM fact_team_run f
LEFT JOIN dim_team t ON t.team_key = f.team_key;

DROP VIEW IF EXISTS v_workflow_runs;
CREATE VIEW v_workflow_runs AS
SELECT
    f.*,
    COALESCE(w.portfolio, 'Non-Product')  AS portfolio,
    COALESCE(w.product,   'Unattributed') AS product,
    COALESCE(w.is_focus, 0)               AS is_focus,
    w.project_name,
    w.node_count,
    substr(f.run_day, 1, 7)               AS run_month
FROM fact_workflow_run f
LEFT JOIN dim_workflow w ON w.workflow_key = f.workflow_key;

-- Workflow build-vs-run reality check: definitions per product against executions.
DROP VIEW IF EXISTS v_workflow_adoption;
CREATE VIEW v_workflow_adoption AS
SELECT
    w.portfolio,
    w.product,
    w.is_focus,
    COUNT(DISTINCT w.workflow_key)                          AS defined,
    COUNT(DISTINCT f.workflow_key)                          AS ever_ran,
    COUNT(f.run_pk)                                         AS executions,
    SUM(CASE WHEN f.status = 'completed' THEN 1 ELSE 0 END)  AS completed,
    SUM(CASE WHEN f.status = 'suspended' THEN 1 ELSE 0 END)  AS suspended,
    SUM(CASE WHEN f.status = 'failed'    THEN 1 ELSE 0 END)  AS failed
FROM dim_workflow w
LEFT JOIN fact_workflow_run f ON f.workflow_key = w.workflow_key
GROUP BY w.portfolio, w.product;

-- Headline scorecard: one row per product per month.
DROP VIEW IF EXISTS v_product_monthly;
CREATE VIEW v_product_monthly AS
SELECT
    portfolio,
    product,
    MAX(is_focus)                                              AS is_focus,
    run_month,
    COUNT(*)                                                   AS runs,
    COUNT(DISTINCT user_id)                                    AS unique_users,
    COUNT(DISTINCT conversation_id)                            AS conversations,
    COUNT(DISTINCT agent_key)                                  AS active_agents,
    ROUND(SUM(token_cost), 2)                                  AS cost,
    SUM(total_tokens)                                          AS tokens,
    ROUND(AVG(duration_ms) / 1000.0, 2)                        AS avg_seconds,
    SUM(CASE WHEN status <> 'Completed' THEN 1 ELSE 0 END)      AS failed_runs,
    ROUND(100.0 * SUM(CASE WHEN status <> 'Completed' THEN 1 ELSE 0 END)
          / COUNT(*), 2)                                       AS failure_pct,
    ROUND(SUM(token_cost) / NULLIF(COUNT(DISTINCT user_id), 0), 2)
                                                               AS cost_per_user
FROM v_agent_runs
GROUP BY portfolio, product, run_month;

-- Portfolio roll-up: the "us vs. the other portfolios" comparison.
DROP VIEW IF EXISTS v_portfolio_monthly;
CREATE VIEW v_portfolio_monthly AS
SELECT
    portfolio,
    run_month,
    COUNT(*)                        AS runs,
    COUNT(DISTINCT user_id)         AS unique_users,
    COUNT(DISTINCT conversation_id) AS conversations,
    COUNT(DISTINCT product)         AS products_active,
    COUNT(DISTINCT agent_key)       AS active_agents,
    ROUND(SUM(token_cost), 2)       AS cost,
    ROUND(AVG(duration_ms) / 1000.0, 2) AS avg_seconds,
    ROUND(100.0 * SUM(CASE WHEN status <> 'Completed' THEN 1 ELSE 0 END)
          / COUNT(*), 2)            AS failure_pct
FROM v_agent_runs
GROUP BY portfolio, run_month;

-- Relativity: each product's share of total runs and cost, all-time.
DROP VIEW IF EXISTS v_product_share;
CREATE VIEW v_product_share AS
WITH totals AS (
    SELECT COUNT(*) AS all_runs, SUM(token_cost) AS all_cost FROM v_agent_runs
)
SELECT
    r.portfolio,
    r.product,
    MAX(r.is_focus)                                        AS is_focus,
    COUNT(*)                                               AS runs,
    ROUND(100.0 * COUNT(*) / t.all_runs, 2)                AS pct_of_runs,
    ROUND(SUM(r.token_cost), 2)                            AS cost,
    ROUND(100.0 * SUM(r.token_cost) / NULLIF(t.all_cost, 0), 2) AS pct_of_cost,
    COUNT(DISTINCT r.user_id)                              AS unique_users,
    COUNT(DISTINCT r.agent_key)                            AS active_agents,
    ROUND(SUM(r.token_cost) / COUNT(*), 5)                 AS cost_per_run,
    ROUND(100.0 * SUM(CASE WHEN r.status <> 'Completed' THEN 1 ELSE 0 END)
          / COUNT(*), 2)                                   AS failure_pct,
    MIN(r.run_day)                                         AS first_run,
    MAX(r.run_day)                                         AS last_run
FROM v_agent_runs r CROSS JOIN totals t
GROUP BY r.portfolio, r.product;

-- Per-agent leaderboard, all time.
DROP VIEW IF EXISTS v_agent_scorecard;
CREATE VIEW v_agent_scorecard AS
SELECT
    agent_key,
    agent_name_current AS agent_name,
    portfolio,
    product,
    is_focus,
    project_name,
    COUNT(*)                        AS runs,
    COUNT(DISTINCT user_id)         AS unique_users,
    COUNT(DISTINCT conversation_id) AS conversations,
    ROUND(SUM(token_cost), 2)       AS cost,
    ROUND(SUM(token_cost) / COUNT(*), 5) AS cost_per_run,
    ROUND(AVG(duration_ms) / 1000.0, 2)  AS avg_seconds,
    ROUND(100.0 * SUM(CASE WHEN status <> 'Completed' THEN 1 ELSE 0 END)
          / COUNT(*), 2)            AS failure_pct,
    MIN(run_day)                    AS first_run,
    MAX(run_day)                    AS last_run
FROM v_agent_runs
GROUP BY agent_key;

-- Adoption curve: distinct users per product per day.
DROP VIEW IF EXISTS v_daily_active_users;
CREATE VIEW v_daily_active_users AS
SELECT run_day, portfolio, product,
       COUNT(DISTINCT user_id) AS dau,
       COUNT(*)                AS runs
FROM v_agent_runs
GROUP BY run_day, portfolio, product;

-- Built but never run: the abandonment metric.
DROP VIEW IF EXISTS v_dormant_agents;
CREATE VIEW v_dormant_agents AS
SELECT a.agent_key, a.name, a.portfolio, a.product, a.project_name, a.model
FROM dim_agent a
LEFT JOIN fact_agent_run f ON f.agent_key = a.agent_key
WHERE f.run_pk IS NULL;

-- Tagging audit: what is still unattributed, ranked by how much traffic it has.
DROP VIEW IF EXISTS v_tagging_gaps;
CREATE VIEW v_tagging_gaps AS
SELECT
    COALESCE(r.project_name, '(no project)') AS project_name,
    r.agent_name_current                     AS agent_name,
    COUNT(*)                                 AS runs,
    ROUND(SUM(r.token_cost), 2)              AS cost
FROM v_agent_runs r
WHERE r.product = 'Unattributed'
GROUP BY r.project_name, r.agent_name_current
ORDER BY runs DESC;

-- ── Views: conversation feedback ────────────────────────────────────────────

-- One tenant per agent, resolved. dim_agent.tenant_name only covers the agents the
-- transcripts endpoint enumerates (715 of 1,870), so the run facts fill the rest --
-- together they cover every agent that has feedback. An agent serving two tenants
-- resolves to its busiest (exactly one agent in this tenant does).
DROP VIEW IF EXISTS v_agent_tenant;
CREATE VIEW v_agent_tenant AS
WITH from_facts AS (
    SELECT agent_key, tenant_name,
           ROW_NUMBER() OVER (PARTITION BY agent_key
                              ORDER BY COUNT(*) DESC, tenant_name) rn
    FROM fact_agent_run
    WHERE agent_key IS NOT NULL AND tenant_name IS NOT NULL
    GROUP BY agent_key, tenant_name
)
SELECT a.agent_key,
       COALESCE(a.tenant_name, f.tenant_name) AS tenant_name
FROM dim_agent a
LEFT JOIN from_facts f ON f.agent_key = a.agent_key AND f.rn = 1;

-- Rated turns enriched with ownership, so the feedback tab inherits every filter
-- the rest of the dashboard already has. Ownership comes from dim_agent via the
-- conversation's agent, exactly as the run facts do.
DROP VIEW IF EXISTS v_feedback_turns;
CREATE VIEW v_feedback_turns AS
SELECT
    t.*,
    COALESCE(a.portfolio, 'Non-Product')   AS portfolio,
    COALESCE(a.product,   'Unattributed')  AS product,
    COALESCE(a.is_focus, 0)                AS is_focus,
    a.project_name,
    -- The disambiguator. Agent names collide hard ('Staff Assistant' is 7 different
    -- agents in 7 tenants, spanning two products), and most feedback-bearing agents
    -- have no project, so tenant is the only field that tells them apart.
    tn.tenant_name,
    COALESCE(a.name, t.agent_name)         AS agent_name_current,
    c.turn_count,
    substr(t.conv_day, 1, 7)               AS conv_month,
    CASE WHEN t.comment IS NOT NULL AND trim(t.comment) <> '' THEN 1 ELSE 0 END
                                           AS has_comment
FROM fact_feedback_turn t
LEFT JOIN dim_conversation c ON c.conversation_id = t.conversation_id
LEFT JOIN dim_agent a        ON a.agent_key       = t.agent_key
LEFT JOIN v_agent_tenant tn  ON tn.agent_key      = t.agent_key;

-- The metric worth ranking on. Raw negative counts just re-rank agents by volume
-- (ERP Pro is 53% of all traffic, so it "looks worst" by construction), so the
-- denominator -- conversations actually held, from the run facts -- is the point.
-- Conversations are counted from fact_agent_run rather than dim_conversation:
-- dim_conversation only ever contains conversations that received feedback.
DROP VIEW IF EXISTS v_feedback_scorecard;
CREATE VIEW v_feedback_scorecard AS
WITH rated AS (
    SELECT
        agent_key,
        MAX(agent_name_current) AS agent_name,
        MAX(tenant_name)        AS tenant_name,
        MAX(portfolio)          AS portfolio,
        MAX(product)            AS product,
        MAX(is_focus)           AS is_focus,
        COUNT(DISTINCT conversation_id) AS rated_conversations,
        SUM(CASE WHEN rating = 'THUMBS_DOWN' THEN 1 ELSE 0 END) AS negative_turns,
        SUM(CASE WHEN rating = 'THUMBS_UP'   THEN 1 ELSE 0 END) AS positive_turns,
        SUM(CASE WHEN rating = 'THUMBS_DOWN' AND has_comment = 1 THEN 1 ELSE 0 END)
                                AS negative_with_comment,
        MIN(conv_day)           AS first_feedback,
        MAX(conv_day)           AS last_feedback
    FROM v_feedback_turns
    GROUP BY agent_key
),
volume AS (
    SELECT agent_key,
           COUNT(DISTINCT conversation_id) AS conversations,
           COUNT(*)                        AS runs
    FROM fact_agent_run
    GROUP BY agent_key
)
SELECT
    r.agent_key,
    r.agent_name,
    r.tenant_name,
    r.portfolio,
    r.product,
    r.is_focus,
    v.conversations,
    v.runs,
    r.rated_conversations,
    r.negative_turns,
    r.positive_turns,
    r.negative_with_comment,
    -- Share of *rated* turns that were negative: the satisfaction signal.
    ROUND(100.0 * r.negative_turns / NULLIF(r.negative_turns + r.positive_turns, 0), 1)
        AS negative_share_pct,
    -- Negative turns per 100 conversations held: the exposure signal. Low
    -- feedback-submission rates mean this is a floor, never an absolute rate.
    ROUND(100.0 * r.negative_turns / NULLIF(v.conversations, 0), 2)
        AS negatives_per_100_convs,
    ROUND(100.0 * r.rated_conversations / NULLIF(v.conversations, 0), 2)
        AS feedback_rate_pct,
    r.first_feedback,
    r.last_feedback
FROM rated r
LEFT JOIN volume v ON v.agent_key = r.agent_key;

-- Failure themes: the labelling pass's output, cut by product. Unlabelled rows
-- surface as '(unlabelled)' so a partial labelling run can never read as "no
-- problems in this category".
DROP VIEW IF EXISTS v_feedback_themes;
CREATE VIEW v_feedback_themes AS
SELECT
    COALESCE(NULLIF(trim(label), ''), '(unlabelled)') AS label,
    portfolio,
    product,
    conv_month,
    COALESCE(NULLIF(trim(severity), ''), '(unlabelled)') AS severity,
    COALESCE(NULLIF(trim(fix_type), ''), '(unlabelled)') AS fix_type,
    COUNT(*)                                AS negative_turns,
    COUNT(DISTINCT conversation_id)          AS conversations,
    COUNT(DISTINCT agent_key)                AS agents
FROM v_feedback_turns
WHERE rating = 'THUMBS_DOWN'
GROUP BY label, portfolio, product, conv_month, severity, fix_type;

-- Feedback volume over time, tenant-wide. Computed from turn grain, so it does
-- not agree with /api/analytics/feedback-trends -- that endpoint is user-scoped.
DROP VIEW IF EXISTS v_feedback_monthly;
CREATE VIEW v_feedback_monthly AS
SELECT
    conv_month,
    portfolio,
    product,
    SUM(CASE WHEN rating = 'THUMBS_DOWN' THEN 1 ELSE 0 END) AS negative_turns,
    SUM(CASE WHEN rating = 'THUMBS_UP'   THEN 1 ELSE 0 END) AS positive_turns,
    SUM(CASE WHEN rating = 'THUMBS_DOWN' AND has_comment = 1 THEN 1 ELSE 0 END)
                                                            AS negative_with_comment,
    COUNT(DISTINCT conversation_id)                         AS conversations
FROM v_feedback_turns
GROUP BY conv_month, portfolio, product;

-- Labelling progress: how much of the commented negative corpus has been judged.
DROP VIEW IF EXISTS v_labelling_gaps;
CREATE VIEW v_labelling_gaps AS
SELECT
    product,
    COUNT(*)                                                   AS negative_with_comment,
    SUM(CASE WHEN label IS NULL OR trim(label) = '' THEN 1 ELSE 0 END) AS unlabelled
FROM v_feedback_turns
WHERE rating = 'THUMBS_DOWN' AND has_comment = 1
GROUP BY product
ORDER BY unlabelled DESC;
