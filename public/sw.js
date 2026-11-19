/* FitBot PWA installability. All requests remain network-only.
   Do not cache authenticated API calls, private bookings or app shell. */
self.addEventListener("install", () => {
  self.skipWaiting();
});
self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});
