// a bar across the top while the next page is on its way, as the NAS takes a few seconds over some.
// Only for a page that's slow to come, so quick ones don't flash it
(function () {
  const SHOW_AFTER_MS = 250;
  // a link that turns out to be a download leaves this page where it is
  const GIVE_UP_MS = 30000;
  let showing = null;
  let giving_up = null;

  function start() {
    clearTimeout(showing);
    clearTimeout(giving_up);
    showing = setTimeout(() => document.documentElement.classList.add('navigating'), SHOW_AFTER_MS);
    giving_up = setTimeout(stop, GIVE_UP_MS);
  }

  function stop() {
    clearTimeout(showing);
    clearTimeout(giving_up);
    document.documentElement.classList.remove('navigating');
  }

  // after everything else has had its say, so a link something else handled doesn't count
  window.addEventListener('click', event => {
    const link = event.target.closest && event.target.closest('a[href]');
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    // getAttribute, as the map's links are SVG, whose href isn't a string
    const target = link.getAttribute('target');
    if ((target && target !== '_self') || link.hasAttribute('download')) {
      return;
    }
    const url = new URL(link.getAttribute('href'), location.href);
    if (url.origin !== location.origin || (url.pathname === location.pathname && url.search === location.search && url.hash)) {
      return;
    }
    start();
  });
  // coming back to a page from the browser's history brings it back as it was left
  window.addEventListener('pageshow', stop);
})();
