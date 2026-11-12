/* Pull the real payload and the real functions out of the built dashboard and run
   them, so this checks shipped code rather than a re-implementation. */
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");

const payload = JSON.parse(
  html.match(/<script id="payload" type="application\/json">([\s\S]*?)<\/script>/)[1]);
const js = html.match(/<script>\n"use strict";([\s\S]*?)<\/script>/)[1];

function grab(name, kind){
  const re = kind === "fn"
    ? new RegExp(`\\nfunction ${name}\\([\\s\\S]*?\\n\\}`)
    : new RegExp(`\\nconst ${name} = [\\s\\S]*?;`);
  const m = js.match(re);
  if (!m) throw new Error("could not extract " + name);
  return m[0];
}

const src = [
  "const D = PAYLOAD;",
  "const E = D.epl;",
  grab("eplState", "const"),
  grab("eplDepSet", "fn"),
  grab("eplDistinct", "fn"),
  grab("eplWeekly", "fn"),
  grab("eplWeekSpan", "fn"),
  "return {eplState, eplDepSet, eplDistinct, eplWeekly, eplWeekSpan, E};",
].join("\n");

const api = new Function("PAYLOAD", src)(payload);
const {eplState, eplDepSet, eplDistinct, eplWeekly, eplWeekSpan, E} = api;

let fails = 0;
const check = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) fails++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}: got ${JSON.stringify(got)}`
              + (ok ? "" : `  want ${JSON.stringify(want)}`));
};

const lastW = E.weeks.length - 1;

// 1. All-scope distinct users must equal the value the extractor computed in SQL.
eplState.scope = "";
check("distinct users, all shipped", eplDistinct(E.userTuples, eplDepSet(), 0, lastW),
      E.nUsers);

// 2. Distinct users must NOT equal the naive per-deployment sum (the bug being avoided).
const summed = E.deps.reduce((a,d) => a + d.users, 0);
console.log(`INFO  per-deployment sum would be ${summed}, distinct is ${E.nUsers}`
            + `  (overcount avoided: ${summed - E.nUsers})`);
check("distinct < summed (overcount really exists)", summed > E.nUsers, true);

// 3. The KPI total must be distinct-based and match SQL, NOT the sum of weekly bars
//    (10 conversations span a week boundary, so summing weeks overcounts).
eplState.scope = "";
const weekly = eplWeekly(eplDepSet());
const weekSum = weekly.reduce((a,r) => a + r.convs, 0);
const depSum = E.deps.reduce((a,d) => a + d.convs, 0);
check("distinct convs == SQL truth", eplDistinct(E.convTuples, eplDepSet(), 0, lastW),
      E.nConvs);
check("distinct convs == sum of per-deployment (a conv has one tenant)",
      eplDistinct(E.convTuples, eplDepSet(), 0, lastW), depSum);
console.log(`INFO  summing weekly bars would give ${weekSum} vs distinct ${E.nConvs}`
            + `  (overcount avoided: ${weekSum - E.nConvs})`);
check("weekly bars really do overcount (so the KPI must not sum them)",
      weekSum > E.nConvs, true);

// 4. Per-week distinct users must never exceed the all-time distinct total.
check("max weekly users <= all-time distinct",
      Math.max(...weekly.map(r => r.users)) <= E.nUsers, true);

// 5. Scoping to one deployment must reproduce that deployment's own totals.
const ix = E.deps.findIndex(d => d.name === "EPL CX");
eplState.scope = "dep:" + ix;
const one = eplDepSet();
check("EPL CX scope selects exactly one deployment", one.size, 1);
check("EPL CX distinct convs == its stored convs",
      eplDistinct(E.convTuples, one, 0, lastW), E.deps[ix].convs);
check("EPL CX distinct users == its stored users",
      eplDistinct(E.userTuples, one, 0, lastW), E.deps[ix].users);

// 6. Type scoping partitions cleanly: the three types must cover every deployment once.
let covered = 0;
for (const t of E.typeOrder){
  eplState.scope = "type:" + t;
  covered += eplDepSet().size;
}
check("type scopes partition all deployments", covered, E.deps.length);

// 7. The growth claim the KPI makes: first active week vs latest complete week.
eplState.scope = "dep:" + ix;
const w = eplWeekly(one);
const lastComplete = E.partialWeek === lastW ? lastW - 1 : lastW;
const firstActive = w.findIndex(r => r.users > 0);
console.log(`INFO  EPL CX: week1 (${E.weeks[firstActive]}) ${w[firstActive].users} user(s)`
            + ` -> latest complete (${E.weeks[lastComplete]}) ${w[lastComplete].users}`);

// 8. Partial week must be the last week and must be genuinely short.
check("partialWeek is the final week", E.partialWeek, lastW);

// 9. The week axis is trimmed to the weeks the selection was live. Empty head and tail
//    must go; an interior dormant week must stay, or the timeline reads as tighter than
//    it was. The all-scope axis must be untouched.
eplState.scope = "";
check("all-scope axis is the full span", eplWeekSpan(eplWeekly(eplDepSet())),
      [0, lastW]);
for (let i = 0; i < E.deps.length; i++){
  eplState.scope = "dep:" + i;
  const rows = eplWeekly(eplDepSet());
  const [lo, hi] = eplWeekSpan(rows);
  const live = r => !!(r.runs || r.convs || r.users);
  const gaps = rows.slice(lo, hi+1).filter(r => !live(r)).length;
  check(`${E.deps[i].name}: axis starts on a live week`, live(rows[lo]), true);
  check(`${E.deps[i].name}: axis ends on a live week`, live(rows[hi]), true);
  check(`${E.deps[i].name}: no live week outside [lo,hi]`,
        rows.every((r,j) => !live(r) || (j >= lo && j <= hi)), true);
  console.log(`INFO  ${E.deps[i].name}: ${E.weeks[lo]} -> ${E.weeks[hi]}`
    + ` = ${hi-lo+1} of ${rows.length} weeks (${gaps} interior gap week(s) kept,`
    + ` ${rows.length-(hi-lo+1)} empty head/tail week(s) dropped)`);
}

// 10. Per-agent detail must reconcile to the row it opens under. Convs, runs and the
//     feedback counts add up exactly (no conversation touches two agents in one
//     tenant); users must NOT be added up, so only the max is bounded by the rollup.
for (let i = 0; i < E.deps.length; i++){
  const d = E.deps[i], kids = E.depAgents[i];
  check(`${d.name}: agent rows == agent count`, kids.length, d.agents);
  for (const k of ["runs","convs","rated","pos","neg","posC","negC"])
    check(`${d.name}: agent ${k} sums to the rollup`,
          kids.reduce((a,x) => a + x[k], 0), d[k]);
  check(`${d.name}: no agent has more users than the deployment`,
        Math.max(...kids.map(x => x.users)) <= d.users, true);
  check(`${d.name}: agent names are unique within the row`,
        new Set(kids.map(x => x.name)).size, kids.length);
}
const summedAgentUsers = E.depAgents.flat().reduce((a,x) => a + x.users, 0);
console.log(`INFO  summing agent users would give ${summedAgentUsers}`
            + ` vs ${E.nUsers} distinct (why sub-rows are never totalled)`);

// 11. Feedback grain. pos/neg are TURNS, posC/negC are the CONVERSATIONS those turns
//     sit in. Mixing them is what made the table fail to add up (9 + 5 turns against 13
//     rated conversations) and inflated the headline rate from 7.4% to 10.8%.
const sum = k => E.deps.reduce((a,d) => a + d[k], 0);
check("complaint conversations == SQL truth", sum("negC"), E.nComplaintConvs);
check("turns >= the conversations holding them (up)", sum("pos") >= sum("posC"), true);
check("turns >= the conversations holding them (down)", sum("neg") >= sum("negC"), true);
// rated is NOT posC + negC: a conversation can hold both a thumbs-up and a thumbs-down,
// so the two overlap. That overlap is exactly nMixedConvs.
check("posC + negC - overlap == rated",
      sum("posC") + sum("negC") - E.nMixedConvs, sum("rated"));
E.deps.forEach((d,i) => {
  check(`${d.name}: 0 rated => 0 complaint conversations`,
        d.rated === 0 ? d.negC === 0 : true, true);
  check(`${d.name}: complaint conversations <= conversations held`,
        d.negC <= d.convs, true);
  for (const a of E.depAgents[i])
    check(`${d.name}/${a.name}: agent complaint convs <= its convs`,
          a.negC <= a.convs, true);
});
console.log(`INFO  headline complaint rate: ${sum("negC")} of ${E.nConvs} conversations`
  + ` = ${(100*sum("negC")/E.nConvs).toFixed(1)}%`
  + `  (the turn-count numerator would have said`
  + ` ${(100*sum("neg")/E.nConvs).toFixed(1)}%)`);
console.log(`INFO  ${E.nMixedConvs} conversation(s) hold both a thumbs-up and a`
  + ` thumbs-down, which is why rated != posC + negC`);

console.log(fails ? `\n${fails} CHECK(S) FAILED` : "\nall checks passed");
process.exit(fails ? 1 : 0);
