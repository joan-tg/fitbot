const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const html = fs.readFileSync("public/index.html", "utf8");
const js = fs.readFileSync("public/app.js", "utf8");
const css = fs.readFileSync("public/styles.css", "utf8");

function extract(name, bindings = {}) {
  const match = js.match(new RegExp("function " + name + "\\([^)]*\\) \\{[\\s\\S]*?\\n  \\}"));
  assert.ok(match, "Function missing: " + name);
  return vm.runInNewContext("(" + match[0] + ")", bindings);
}
function el() {
  return {
    hidden: false,
    disabled: false,
    textContent: "",
    title: "",
    dataset: {},
    attributes: {},
    setAttribute(key,value) { this.attributes[key] = value; },
  };
}

const ids = ["environment-indicator", "environment-label", "booking-mode-card", "booking-mode-title", "booking-mode-detail", "schedule-button", "dashboard", "auth-panel", "logout-button", "refresh-button", "toggle-detail"];
const elements = Object.fromEntries(ids.map(id=>[id,el()]));
const $ = id => { assert.ok(elements[id], "missing element " + id); return elements[id]; };
const state = {environment:"production",bookingMode:"live"};
const renderEnvironment = extract("renderEnvironment", {$,state});
const renderMode = extract("renderMode", {$,state,renderEnvironment});
const showDashboard = extract("showDashboard", {$,renderEnvironment});
renderMode();
showDashboard(true);
assert.equal($("environment-indicator").hidden, true, "Hide normal Production badge");
assert.equal($("booking-mode-card").dataset.mode, "live");
assert.equal($("schedule-button").disabled,false);
assert.equal($("toggle-detail").hidden,false,"Header detail toggle available after sign-in");
assert.equal($("logout-button").hidden,false,"Header logout available after sign-in");

state.bookingMode="simulation";
renderMode();
assert.match($("booking-mode-title").textContent,/Sin reservas reales/);
assert.match($("booking-mode-detail").textContent,/DRY_RUN=true/);
assert.equal($("booking-mode-card").dataset.mode,"simulation");

state.environment="preview";
state.bookingMode="live";
renderMode();
assert.equal($("environment-indicator").hidden,false);
assert.equal($("environment-label").textContent,"PREVIEW");
assert.equal($("schedule-button").disabled,true);
assert.equal($("booking-mode-card").dataset.mode,"preview");

state.environment="unknown";
renderMode();
assert.equal($("environment-indicator").hidden,false);
assert.equal($("environment-label").textContent,"ENTORNO SIN VERIFICAR");
showDashboard(false);
assert.equal($("environment-indicator").hidden,true);
assert.equal($("toggle-detail").hidden,true);

const topbar = html.match(/<header class="topbar">[\s\S]*?<\/header>/)?.[0];
assert.ok(topbar,"Topbar missing");
assert.ok(topbar.includes('id="toggle-detail"'),"Details switch must be in header");
assert.ok(topbar.includes('id="logout-button"'),"Logout must be in header");
assert.ok(!html.includes('id="toggle-detail" class="outline-button"'),"Old wide details switch still exists");
assert.ok(html.includes("Programa reservas de StudioCrossFit"));
assert.ok(html.includes('class="muted detailed-only"'),"Keep detailed explanatory copy");
assert.ok(html.includes('class="simple-only"'),"Simple heading missing");
assert.ok(css.includes('#dashboard[data-view="simple"] .quota-month-controls{display:flex'),"Inline quota selector missing");
assert.ok(css.includes('#dashboard[data-view="simple"] #quota-month{display:none}'),"Redundant month status should be hidden");
assert.ok(css.includes('.mode-card[data-mode="live"]{display:none}'),"Normal live status should not clutter simple mode");
assert.ok(css.includes('.stat-card[data-stat="simulated"]'),"Simulation stat should be hidden in simple mode");
assert.ok(css.includes('.stat-card[data-stat="incidents"][data-empty="true"]'),"Zero incidents should be hidden");
assert.ok(css.includes('.detail-toggle-label{display:none}'),"Compact details icon not configured");
assert.ok(css.includes('.logout-label{display:none}'),"Compact logout icon not configured");
console.log("Simple view: environment, DRY_RUN, permissions and compact layout checks passed");
