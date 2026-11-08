// Offline evidence checklist, not a substitute for the human device rehearsal.
const fs = require("node:fs");
const platforms = ["chromeos-chrome","chromeos-pwa","windows-chrome","windows-edge","macos-chrome"];
const cases = [
  ...platforms.flatMap(p => ["cc","codex"].map(provider => p+"/"+provider)),
  ...Array.from({length:5},(_,i) => "entry/online-"+(i+1)),
  "entry/two-hosts","entry/offline-deny-expire","security/second-user","security/revoke-tabs",
  "continuity/slow-duplicate","continuity/30s-gap","home/30-jobs","scope/direct-api","rollback/staging",
];
function template(revision) {
  if (!/^[0-9a-f]{40}$/.test(revision)) throw Error("Supply the full candidate Git SHA");
  return {revision,runs:cases.map(id => ({id,status:"NOT_RUN",environment:"",runId:"",seconds:null}))};
}
function evaluate(record,revision) {
  template(revision);
  if (record.revision!==revision || !Array.isArray(record.runs)) throw Error("Wrong candidate or record format");
  const seen=new Set(), issues=[], evidence=new Set();
  let fast=0;
  for (const r of record.runs) {
    if (!cases.includes(r?.id) || seen.has(r.id)) throw Error("Unknown or duplicate case");
    seen.add(r.id);
    if (r.status!=="PASS") { issues.push(r.id+": not passed"); continue; }
    if (!/^(ChromeOS Chrome|Windows Chrome|Windows Edge|macOS Chrome) [0-9]+(?:\.[0-9]+){1,3}$/.test(r.environment) ||
        !/^qa-[0-9a-f]{8}$/.test(r.runId) || evidence.has(r.runId) ||
        !Number.isFinite(r.seconds) || r.seconds<=0) {
      issues.push(r.id+": missing device version, unique run reference or duration"); continue;
    }
    const platform=r.id.split("/")[0];
    const required={"chromeos-chrome":"ChromeOS Chrome","chromeos-pwa":"ChromeOS Chrome",
      "windows-chrome":"Windows Chrome","windows-edge":"Windows Edge","macos-chrome":"macOS Chrome"}[platform];
    if (required && !r.environment.startsWith(required+" ")) issues.push(r.id+": wrong device");
    evidence.add(r.runId);
    if (r.id.startsWith("entry/online-") && r.seconds<=60) fast++;
  }
  for (const id of cases) if (!seen.has(id)) issues.push(id+": missing");
  if (fast<4) issues.push("entry: only "+fast+"/5 within 60s");
  return issues;
}
if (require.main===module) {
  try {
    const [action,sha,file]=process.argv.slice(2);
    if (action==="init") console.log(JSON.stringify(template(sha),null,2));
    else if (action==="check") {
      const issues=evaluate(JSON.parse(fs.readFileSync(file,"utf8")),sha);
      if (issues.length) { console.error("NO-GO\n"+issues.join("\n")); process.exitCode=1; }
      else console.log("Evidence matrix complete; verify the referenced runs before release.");
    } else throw Error("Use init <sha> or check <sha> <record.json>");
  } catch (e) { console.error(e.message); process.exitCode=2; }
}
module.exports={template,evaluate};
