// A failed page load leaves a browser error page, which has no way to try again, and the frame
// has no browser controls to do it with.  This answers those loads with a page that keeps trying
// instead, including after a reboot where the frame comes up before the server does
const RETRY_SECS = 15;
const REQUEST_TIMEOUT_MS = 30000;

const retryPage = () => new Response(`<!DOCTYPE html>
<html>
  <head>
    <title>Exif DB</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script>setTimeout(() => window.location.reload(), ${RETRY_SECS * 1000});</script>
  </head>
  <body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#000;color:#666;font:24px Arial,sans-serif">
    Can't reach the photo server. Trying again…
  </body>
</html>`, {headers: {'Content-Type': 'text/html; charset=utf-8'}});

async function loadPage(request) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(request, {signal: controller.signal});
    // anything else is an answer: retrying won't change a page that doesn't exist
    return response.status >= 500 ? retryPage() : response;
  } catch (error) {
    return retryPage();
  } finally {
    clearTimeout(timeout);
  }
}

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  // only pages: everything else already handles its own failures
  const path = new URL(event.request.url).pathname;
  if (event.request.mode === 'navigate' && !path.startsWith('/api/')) {
    event.respondWith(loadPage(event.request));
  }
});
