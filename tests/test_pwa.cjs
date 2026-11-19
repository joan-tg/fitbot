const assert = require("node:assert/strict");
const fs = require("node:fs");

const html = fs.readFileSync("public/index.html", "utf8");
const app = fs.readFileSync("public/app.js", "utf8");
const worker = fs.readFileSync("public/sw.js", "utf8");
const manifest = JSON.parse(fs.readFileSync("public/manifest.webmanifest", "utf8"));

assert.equal(manifest.short_name, "FitBot");
assert.equal(manifest.start_url, "/");
assert.equal(manifest.scope, "/");
assert.equal(manifest.display, "standalone");
assert.equal(manifest.theme_color, "#101827");
assert.equal(manifest.lang, "es");
assert.match(html, /<link rel="manifest" href="\/manifest.webmanifest">/);
assert.match(app, /navigator\.serviceWorker\.register\("\/sw\.js"\)/);
assert.ok(!worker.includes("fetch"), "Avoid intercepting or caching authenticated requests");
assert.ok(!worker.includes("caches"), "Avoid storing private bookings offline");

for (const size of [192, 512]) {
  const filename = `public/icons/fitbot-${size}.png`;
  const bytes = fs.readFileSync(filename);
  assert.equal(bytes.subarray(0, 8).toString("hex"), "89504e470d0a1a0a");
  assert.equal(bytes.readUInt32BE(16), size);
  assert.equal(bytes.readUInt32BE(20), size);
  assert.ok(manifest.icons.some(icon =>
    icon.sizes === `${size}x${size}` &&
    icon.type === "image/png" &&
    icon.src === `/icons/fitbot-${size}.png` &&
    icon.purpose.includes("maskable")
  ));
}
console.log("FitBot Android PWA: manifest, 192/512 launcher icons and network-only worker OK");
