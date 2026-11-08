const test=require("node:test"), assert=require("node:assert/strict");
const {template,evaluate}=require("../../scripts/console-w08.cjs");
test("W08 requires complete evidence for exactly the release candidate and real device matrix", () => {
  const sha="a".repeat(40), record=template(sha);
  assert.ok(evaluate(record,sha).length>1);
  record.runs.forEach((r,i) => Object.assign(r,{status:"PASS",runId:"qa-"+i.toString(16).padStart(8,"0"),
    seconds:r.id==="entry/online-2"?61:60, environment:(r.id.startsWith("windows-edge")?"Windows Edge":
      r.id.startsWith("windows-chrome")?"Windows Chrome":r.id.startsWith("chromeos")?"ChromeOS Chrome":"macOS Chrome")+" 142.0"}));
  assert.deepEqual(evaluate(record,sha),[]);
  assert.throws(()=>evaluate(record,"b".repeat(40)),/Wrong candidate/);
  record.runs.find(r=>r.id==="entry/online-1").seconds=61;
  assert.match(evaluate(record,sha).join(),/3\/5/);
  record.runs[0].environment="macOS Chrome 142.0";
  assert.match(evaluate(record,sha).join(),/wrong device/);
  record.runs.pop(); assert.match(evaluate(record,sha).join(),/missing/);
  record.runs.push(record.runs[0]); assert.throws(()=>evaluate(record,sha),/duplicate/);
});
