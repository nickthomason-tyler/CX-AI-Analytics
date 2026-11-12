// Parity check for the Forge dashboard against the shipping dashboard.html.
//
//   python3 -m http.server 8765
//   node verify_forge_poc.js          # exits non-zero on any mismatch
//
// DESIGN: this compares the two pages' EMBEDDED PAYLOADS, not frozen numbers.
// An earlier version asserted hard-coded KPI strings captured from a live page;
// the first nightly data refresh invalidated all nine of them, which is noise, not
// signal -- the numbers were supposed to move. Re-implementing the KPI maths here
// would be worse still: it would only prove this file agrees with itself.
//
// Payload identity is the right invariant. The Forge page's JS is a verbatim port of
// dashboard.html's, so if both pages start from the same data, their KPIs cannot
// disagree. That holds across every refresh without re-baselining. Rendered-value
// equality is checked in the browser instead (see BROWSER CHECK at the bottom).
//
// The Forge payload deliberately differs in two ways, both asserted rather than
// waved past: q/r/why are truncated for page weight, and epl.complaints carries two
// extra columns (cid, turn) that build_epl does not select.

const URL_POC = 'http://localhost:8765/dashboard-forge.html';
const SHIPPING_FILE = `${__dirname}/dashboard.html`;

const fs = require('fs');

const fmt = n => n.toLocaleString('en-US');
const fmtPct = v => (v >= 100 ? v.toFixed(1) : Number(v.toPrecision(3)).toString()) + '%';
const fmtCompact = n => (n >= 1000 ? Math.round(n / 1000).toLocaleString('en-US') + 'K'
                                  : n.toLocaleString('en-US'));

// Every <forge-icon name="..."> in the generated page must have a matching
// IconRegistry.define entry, or it renders as empty space with no error.
function iconsRegistered() {
  let entry;
  try { entry = require('fs').readFileSync(`${__dirname}/forge-entry.js`, 'utf8'); }
  catch { return false; }
  // Scope the lookup to the define() argument list. Checking the whole file would be
  // satisfied by a leftover `import` of an icon that is no longer registered -- which
  // is exactly the state that renders a blank icon.
  const m = entry.match(/IconRegistry\.define\(\s*\[([^\]]*)\]/);
  if (!m) return false;
  const registered = m[1].split(',').map(s => s.trim()).filter(Boolean);
  const used = [...require('fs').readFileSync(`${__dirname}/dashboard-forge.html`, 'utf8')
    .matchAll(/forge-icon[^>]*name="([a-z0-9_]+)"|setAttribute\('name',\s*'([a-z0-9_]+)'\)/g)]
    .map(m2 => m2[1] || m2[2]).filter(Boolean);
  const toCamel = n => 'tylIcon' + n.split('_')
    .map(p => p[0].toUpperCase() + p.slice(1)).join('');
  const missing = [...new Set(used)].filter(n => !registered.includes(toCamel(n)));
  if (missing.length) console.log(`      unregistered icons: ${missing.join(', ')}`);
  return used.length > 0 && missing.length === 0;
}

function extractPayload(html) {
  const m = html.match(/<script id="payload" type="application\/json">([\s\S]*?)<\/script>/);
  if (!m) throw new Error('payload script block not found');
  return JSON.parse(m[1]);
}

const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
// Order-insensitive comparison: several payload arrays are built from SQL whose
// ORDER BY does not fully disambiguate ties.
const sameMultiset = (a, b) =>
  a.length === b.length &&
  eq([...a].map(x => JSON.stringify(x)).sort(), [...b].map(x => JSON.stringify(x)).sort());
const pick = (o, keys) => Object.fromEntries(keys.map(k => [k, o[k]]));

(async () => {
  let html;
  try {
    const res = await fetch(URL_POC);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    html = await res.text();
  } catch (e) {
    console.error(`Could not fetch ${URL_POC}: ${e.message}`);
    console.error('Start a server first:  python3 -m http.server 8765');
    process.exit(2);
  }

  const P = extractPayload(html);
  let bad = 0;

  let S = null;
  try { S = extractPayload(fs.readFileSync(SHIPPING_FILE, 'utf8')); }
  catch (e) { console.error(`Could not read shipping payload: ${e.message}`); }

  console.log(`forge payload:    ${fmt(P.fbTurns.length)} turns, `
    + `${fmt(P.fbAgents.length)} scorecard agents, ${fmt(P.facts.length)} fact rows`);
  if (S) console.log(`shipping payload: ${fmt(S.fbTurns.length)} turns, `
    + `${fmt(S.fbAgents.length)} scorecard agents, ${fmt(S.facts.length)} fact rows`);
  console.log(`generated:        ${P.generated}`);

  // ---- payload identity: refresh-proof, no frozen numbers -------------------
  if (!S) {
    bad++;
    console.log('\nFAIL  shipping payload unavailable — run build_dashboard.py');
  } else {
    console.log('\npayload parity vs dashboard.html:');
    // Copied through verbatim by build_forge_poc, so these must be byte-identical.
    const verbatim = ['days', 'facts', 'products', 'portfolios', 'prodPortfolio',
                      'prodFocus', 'portfolioSlot', 'userTuples', 'agentTuples',
                      'agents', 'errors', 'dormant', 'builtAgents', 'wfRuns',
                      'wfAdoption', 'gaps', 'seriesLight', 'seriesDark', 'tenants',
                      'fbCounts', 'fbAgents', 'fbLabels'];
    // NOT `generated`: it is a render timestamp, and the two generators run seconds
    // apart in the same refresh. Comparing it reports drift where there is none.
    // Both being from the same refresh is what matters, so check same-day instead.
    {
      const sameDay = (P.generated || '').slice(0, 10) === (S.generated || '').slice(0, 10);
      if (!sameDay) bad++;
      console.log(`${sameDay ? 'ok  ' : 'FAIL'}  generated same day `
        + `(forge ${P.generated} / shipping ${S.generated})`);
    }
    for (const k of verbatim) {
      const ok = eq(P[k], S[k]);
      if (!ok) bad++;
      const n = Array.isArray(S[k]) ? ` (${fmt(S[k].length)})` : '';
      console.log(`${ok ? 'ok  ' : 'FAIL'}  ${k}${n} identical`);
    }

    // fbTurns: same rows, but q/r/why are truncated in the Forge payload, so compare
    // only the fields that are carried through untouched.
    const TURN_KEYS = ['d', 'p', 'a', 'tn', 'c', 'l', 's', 'f', 'cf'];
    const tOk = sameMultiset(P.fbTurns.map(t => pick(t, TURN_KEYS)),
                             S.fbTurns.map(t => pick(t, TURN_KEYS)));
    if (!tOk) bad++;
    console.log(`${tOk ? 'ok  ' : 'FAIL'}  fbTurns (${fmt(S.fbTurns.length)}) same rows `
      + `on untruncated fields`);

    // Truncation must actually be truncation -- never a change of content.
    const clipOk = P.fbTurns.every((t, i) => {
      const s = S.fbTurns.find(x => x.cid === t.cid && x.t === t.t) || S.fbTurns[i];
      if (!s) return false;
      return ['q', 'r', 'why'].every(k => {
        const a = t[k] || '', b = s[k] || '';
        return a === b || (a.endsWith('…') && b.startsWith(a.slice(0, -1).trimEnd()));
      });
    });
    if (!clipOk) bad++;
    console.log(`${clipOk ? 'ok  ' : 'FAIL'}  q/r/why are prefixes of the originals`);

    // EPL: deps identical; complaints same rows, plus the two enrichment columns.
    const eplOk = eq(P.epl.deps, S.epl.deps) && eq(P.epl.weeks, S.epl.weeks)
      && eq(P.epl.facts, S.epl.facts) && eq(P.epl.excluded, S.epl.excluded)
      && P.epl.partialWeek === S.epl.partialWeek;
    if (!eplOk) bad++;
    console.log(`${eplOk ? 'ok  ' : 'FAIL'}  epl deps/weeks/facts/excluded identical `
      + `(${P.epl.deps.length} deployments)`);

    const C_KEYS = ['dep', 'a', 'd', 'l', 's', 'f', 'c', 'q'];
    const cOk = sameMultiset(P.epl.complaints.map(c => pick(c, C_KEYS)),
                             S.epl.complaints.map(c => pick(c, C_KEYS)));
    if (!cOk) bad++;
    console.log(`${cOk ? 'ok  ' : 'FAIL'}  epl complaints (${S.epl.complaints.length}) `
      + `same rows despite the cid re-query`);
  }

  // Structural assertions: the markup must actually use Forge components rather
  // than having quietly fallen back to plain HTML.
  const required = ['forge-scaffold', 'forge-app-bar', 'forge-tab-bar', 'forge-table',
                   'forge-select', 'forge-card', 'forge-badge', 'forge-text-field',
                   'forge-chip-set', 'forge-chip', 'forge-button-toggle-group',
                   'forge-icon-button', 'forge-option-group', 'forge-inline-message',
                   'forge-icon'];
  console.log('');
  for (const tag of required) {
    // Components may be authored as literal markup or built with createElement
    // (chips, option groups and badges are all generated from the payload).
    const ok = html.includes(`<${tag}`) || html.includes(`createElement('${tag}')`);
    if (!ok) bad++;
    console.log(`${ok ? 'ok  ' : 'FAIL'}  uses ${tag}`);
  }

  // The adoption maths is ported verbatim from dashboard.html, so the check that
  // matters is that it is genuinely ported and not paraphrased. Assert the exact
  // source lines survive; a reworded copy is how the KPI bugs got in last time.
  console.log('');
  const ported = [
    ['distinct-count guard',
     'Distinct counts must come from presence tuples, never from summing.'],
    ['submission-rate denominator',
     'conversations actually held, from the run'],
    ['tenant-blank rationale', "the run facts don't carry"],
    ['fixed portfolio slots', "A portfolio's slot is fixed for the life of the dashboard"],
    ['nice-scale 1/2/5 rule', 'Steps restricted to 1/2/5'],
    ['single axis unit', 'an axis reading 12K / 10K / 8,000 mixes units'],
    ['label-collision drop', "Colliding end-labels read as noise"],
    ['commented denominator', 'uncommented downvotes are skipped by'],
  ];
  for (const [name, needle] of ported) {
    const ok = html.includes(needle);
    if (!ok) bad++;
    console.log(`${ok ? 'ok  ' : 'FAIL'}  ported verbatim: ${name}`);
  }

  // Every $('#id') must resolve to an element in the markup. A missing one throws at
  // module scope and takes the whole page down -- which is how a forgotten tab badge
  // silently killed every tab, not just its own.
  {
    const body = html.split('<script id="payload"')[0];
    const ids = new Set([...body.matchAll(/\bid="([A-Za-z0-9_-]+)"/g)].map(m => m[1]));
    const refs = new Set([...html.matchAll(/\$\('#([A-Za-z0-9_-]+)'\)/g)].map(m => m[1]));
    const dangling = [...refs].filter(r => !ids.has(r));
    if (dangling.length) bad++;
    console.log(`\n${dangling.length ? 'FAIL' : 'ok  '}  every $('#id') resolves`
      + (dangling.length ? `  dangling: ${dangling.join(', ')}` : ` (${refs.size} refs)`));
  }

  // The conversation id is the only route from a truncated excerpt back to the full
  // transcript, so it must be present and NEVER truncated. An earlier payload trim
  // silently dropped it along with the turn index.
  console.log('');
  const turns = P.fbTurns;
  const withCid = turns.filter(t => t.cid);
  const fullUuid = turns.filter(t => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(t.cid || ''));
  const withTurn = turns.filter(t => t.t != null);
  const truncatedCid = turns.filter(t => (t.cid || '').includes('…'));
  const detail = [
    [`cid present on all ${fmt(turns.length)} turns`, withCid.length === turns.length],
    ['every cid is a full 36-char uuid', fullUuid.length === withCid.length && withCid.length > 0],
    ['no cid was truncated', truncatedCid.length === 0],
    ['turn index present', withTurn.length === turns.length],
    ['answer text carried (t.r)', turns.some(t => t.r)],
    ['question text carried (t.q)', turns.some(t => t.q)],
    ['classifier rationale carried (t.why)', turns.some(t => t.why)],
    ['fix location carried (t.f)', turns.some(t => t.f)],
    ['copy affordance in markup', html.includes('function cidButton') && html.includes('copyText')],
    // forge-icon renders nothing for an unregistered name and does not error, so the
    // registration is worth asserting rather than assuming.
    ['icons registered in forge-entry.js', iconsRegistered()],
  ];
  for (const [name, ok] of detail) {
    if (!ok) bad++;
    console.log(`${ok ? 'ok  ' : 'FAIL'}  ${name}`);
  }

  // Current totals, derived from the payload rather than frozen. Informational: the
  // numbers move on every refresh by design, so they are printed for eyeballing, not
  // asserted. Payload identity above is what actually gates.
  console.log('\ncurrent slice (derived, not asserted):');
  {
    const runs = P.facts.reduce((a, r) => a + r[2], 0);
    const cost = P.facts.reduce((a, r) => a + r[4], 0);
    const fails = P.facts.reduce((a, r) => a + r[6], 0);
    const neg = P.fbTurns.length;
    const commented = P.fbTurns.filter(t => t.c).length;
    const pos = P.fbCounts.reduce((a, r) => a + r[3], 0);
    console.log(`      agent runs        ${fmt(runs)} over ${fmt(P.days.length)} days`);
    console.log(`      token cost        $${fmt(Math.round(cost))}`);
    console.log(`      failure rate      ${(100 * fails / (runs || 1)).toFixed(2)}%`);
    console.log(`      thumbs-down       ${fmt(neg)} (${fmt(commented)} commented)`);
    console.log(`      negative share    ${(100 * neg / (neg + pos || 1)).toFixed(1)}%`
      + ` of ${fmt(neg + pos)} rated turns`);
    console.log(`      EPL deployments   ${P.epl.deps.length}`
      + ` · ${P.epl.complaints.length} complaints`
      + ` · ${P.epl.weeks.length} weeks`);
  }

  // BROWSER CHECK -- the one thing this file cannot do headlessly is confirm the two
  // pages RENDER the same strings. Paste into the console on each page and diff:
  //
  //   [$('#kRuns'),$('#kUsers'),$('#kCost'),$('#kFail'),$('#kAgents')]
  //     .map(e => e.textContent).join(' | ')
  //
  // On the Forge page, drive filter states with window.__state + window.__render(),
  // and EPL scopes with window.__eplState.scope + window.__renderEpl().

  // The EPL payload must be self-contained -- it carries its own weeks/facts/tuples.
  console.log('');
  const E = P.epl || {};
  const eplChecks = [
    ['epl payload present', !!P.epl],
    ['deployments present', (E.deps || []).length > 0],
    ['weeks present', (E.weeks || []).length > 0],
    ['presence tuples carried (users/agents/convs)',
     !!(E.userTuples && E.agentTuples && E.convTuples)],
    ['complaints carried', (E.complaints || []).length > 0],
    // Was a hard-coded 41. A golden count silently goes stale on every ingest (it
    // was already wrong at 43 before anyone noticed), and it never expressed the
    // actual intent: that the POC's cid/turn-index re-query neither drops nor
    // duplicates rows. deps[].neg is build_epl's own per-deployment thumbs-down
    // TURN count over the same scope, so it must equal the complaint row count
    // exactly -- and it stays true as data arrives.
    ['complaint rows match the deployment rollup (cid re-query lost/dupd nothing)',
     (E.complaints || []).length ===
       (E.deps || []).reduce((a, d) => a + (d.neg || 0), 0)],
    // build_epl selects neither conversation_id nor turn_index; the POC re-reads the
    // view to add them, so assert the enrichment actually landed on every row.
    ['every EPL complaint has a full uuid cid',
     (E.complaints || []).length > 0 && (E.complaints || []).every(c =>
       /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(c.cid || ''))],
    ['no EPL cid truncated',
     (E.complaints || []).every(c => !(c.cid || '').includes('…'))],
    ['every EPL complaint has a turn index',
     (E.complaints || []).every(c => c.t != null)],
    ['partialWeek index present', E.partialWeek != null],
    ['excluded list carried', Array.isArray(E.excluded)],
    // The label map must be the explicit one; a mechanical un-snake-case turns
    // "kb_gap" into "Kb gap" rather than "Knowledge gap".
    ['explicit label map, not un-snake-cased',
     html.includes("kb_gap:'Knowledge gap'") &&
     html.includes("wrong_ownership_claim:'Wrong ownership / jurisdiction'")],
  ];
  for (const [name, ok] of eplChecks) {
    if (!ok) bad++;
    console.log(`${ok ? 'ok  ' : 'FAIL'}  ${name}`);
  }

  console.log(bad ? `\n${bad} check(s) failed` : '\nall checks passed');
  process.exit(bad ? 1 : 0);
})();
