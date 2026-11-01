// a card of what's known about a photo, over whatever page it's opened from
function closeInfo() {
  document.getElementById('info-card')?.remove();
}

function infoSection(title, fields) {
  const section = document.createElement('section');
  const heading = document.createElement('h3');
  heading.textContent = title;
  const list = document.createElement('dl');
  for (const {label, value, href} of fields) {
    const term = document.createElement('dt');
    term.textContent = label;
    const detail = document.createElement('dd');
    if (href) {
      const link = document.createElement('a');
      link.href = href;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = value;
      detail.append(link);
    } else {
      detail.textContent = value;
    }
    list.append(term, detail);
  }
  section.append(heading, list);
  return section;
}

async function showInfo(path) {
  closeInfo();
  const card = document.createElement('aside');
  card.id = 'info-card';
  card.className = 'info-card';
  card.setAttribute('aria-label', 'About this photo');
  const header = document.createElement('div');
  header.className = 'info-card-header';
  const title = document.createElement('h2');
  title.textContent = 'Info';
  const close = document.createElement('button');
  close.className = 'control';
  close.textContent = 'Close';
  close.onclick = closeInfo;
  header.append(title, close);
  card.append(header);
  // stays over the photo, so that going through photos in the viewer isn't what closes it
  card.addEventListener('touchstart', event => event.stopPropagation(), {passive: true});
  document.body.append(card);
  try {
    const response = await fetch(`/api/info?${new URLSearchParams({path})}`);
    if (!response.ok) {
      throw new Error('Status: ' + response.status);
    }
    const info = await response.json();
    if (!card.isConnected) {
      return;
    }
    const body = [];
    if (info.people.length) {
      body.push(infoSection('People', [{label: 'In this photo', value: info.people.join(', ')}]));
    }
    const {location} = info;
    if (location) {
      const coordinates = `${location.lat.toFixed(5)}, ${location.lon.toFixed(5)}`;
      body.push(infoSection('Location', [
        ...[['Near', location.city], ['County', location.county], ['State', location.state], ['Country', location.country]]
          .filter(([, value]) => value)
          .map(([label, value]) => ({label, value})),
        // only ever opened on purpose, since it tells OpenStreetMap where the photo was taken
        {
          label: 'Map',
          value: coordinates,
          href: `https://www.openstreetmap.org/?mlat=${location.lat}&mlon=${location.lon}#map=14/${location.lat}/${location.lon}`
        }
      ]));
    } else {
      const none = document.createElement('p');
      none.className = 'library-hint';
      none.textContent = 'No location recorded.';
      body.push(none);
    }
    body.push(...info.sections.map(section => infoSection(section.title, section.fields)));
    const everything = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = 'All details';
    everything.append(summary, infoSection('', Object.entries(info.all).map(([label, value]) => ({label, value}))));
    body.push(everything);
    card.append(...body);
  } catch (error) {
    const failed = document.createElement('p');
    failed.className = 'library-hint';
    failed.textContent = `Couldn't load this photo's info: ${error.message}`;
    card.append(failed);
  }
}

// first, so that closing this doesn't also close a viewer underneath
window.addEventListener('keydown', event => {
  if (event.key === 'Escape' && document.getElementById('info-card')) {
    closeInfo();
    event.stopImmediatePropagation();
  }
}, true);
