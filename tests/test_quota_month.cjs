const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const js = fs.readFileSync("public/app.js", "utf8");
const html = fs.readFileSync("public/index.html", "utf8");

function extract(name) {
  const match = js.match(new RegExp("function " + name + "\\([^)]*\\) \\{[\\s\\S]*?\\n  \\}"));
  assert.ok(match, "Missing function " + name);
  return vm.runInNewContext("(" + match[0] + ")");
}

const nextMonthKey = extract("nextMonthKey");
assert.equal(nextMonthKey("2026-11"), "2026-12");
assert.equal(nextMonthKey("2026-12"), "2027-01");
assert.equal(nextMonthKey("2027-01"), "2027-02");

assert.ok(html.includes('id="quota-month-select"'), "Missing quota month selector");
assert.ok(js.includes('state.quotaMonth=month'), "Class date should select quota month");
assert.ok(js.includes('const futureMonth=monthKey!==madridMonthKey()'), "Do not reuse current-month live credits");
assert.ok(js.includes('if(futureMonth) {'), "Next month needs separate accounting");
assert.ok(js.includes('source:"future_fitbot"'), "Future counts must be labeled as estimates");
assert.ok(js.includes('saldo AimHarder sin verificar'), "Never claim an unverified balance");
assert.ok(js.includes('state.monthlyCreditsReadOnly||futureMonth'), "Manual current-month credits cannot be edited for future month");
console.log("Monthly quota selector: month rollover, isolation, labels and form guard OK");
