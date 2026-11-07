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

// ---- changing a photo's date and place from its card

const TOWN_WAIT_MS = 250;
const TOWN_ZOOM = 11;
// how far around a named place counts, in metres
const LABEL_RADII = [100, 250, 500, 1000, 5000];
const DEFAULT_LABEL_RADIUS = 250;
const placeLabelUrl = name => `/places/photos?${new URLSearchParams({kind: 'label', key: name})}`;
// where the map starts for a photo with no place, as photos near each other are often done in turn
const LAST_PLACE = 'last-place';
let leafletLoading = null;

function lastPlace() {
  try {
    return JSON.parse(localStorage.getItem(LAST_PLACE));
  } catch (error) {
    return null;
  }
}

function rememberPlace(point) {
  try {
    localStorage.setItem(LAST_PLACE, JSON.stringify(point));
  } catch (error) {
    // only a convenience
  }
}

function loadLeaflet() {
  if (window.L) {
    return Promise.resolve();
  }
  if (!leafletLoading) {
    const style = document.createElement('link');
    style.rel = 'stylesheet';
    style.href = '/static/vendor/leaflet/leaflet.css';
    document.head.append(style);
    leafletLoading = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = '/static/vendor/leaflet/leaflet.js';
      script.onload = resolve;
      script.onerror = () => {
        leafletLoading = null;
        reject(new Error("Couldn't load the map"));
      };
      document.head.append(script);
    });
  }
  return leafletLoading;
}

// a map to choose a point on, starting at start if there is one.  Its pin moves to each point
// tapped, which picked is given, and to wherever pin is asked to put it
async function placeMap(box, start, picked) {
  await loadLeaflet();
  const map = L.map(box, {worldCopyJump: true}).setView(start ? [start.lat, start.lon] : [20, 0], start ? TOWN_ZOOM : 2);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);
  let marker = null;
  const pin = (lat, lon, zoom) => {
    marker = marker || L.marker([lat, lon]).addTo(map);
    marker.setLatLng([lat, lon]);
    if (zoom) {
      map.setView([lat, lon], zoom);
    }
  };
  map.on('click', event => {
    // a map that's been scrolled round the world gives longitudes past 180
    const lat = event.latlng.lat;
    const lon = L.Util.wrapNum(event.latlng.lng, [-180, 180], true);
    pin(lat, lon);
    picked(lat, lon);
  });
  return {map, pin};
}

// suggests places as their names are typed, and calls picked with one once it's chosen.  Returns
// the list of suggestions, which has to be on the page beside the input: it's put there if the
// input is already, and otherwise the caller has to
function townSearch(input, picked) {
  const list = document.createElement('datalist');
  list.id = `towns-${Math.random().toString(36).slice(2)}`;
  input.setAttribute('list', list.id);
  if (input.parentNode) {
    input.after(list);
  }
  const found = new Map();
  let waiting = null;
  const suggest = async () => {
    try {
      const response = await fetch(`/api/towns?${new URLSearchParams({q: input.value})}`);
      const towns = response.ok ? await response.json() : [];
      found.clear();
      list.replaceChildren(...towns.map(town => {
        found.set(town.label, town);
        const option = document.createElement('option');
        option.value = town.label;
        // shown beside it, so that Home isn't taken for a town
        if (town.named) {
          option.label = 'Named place';
        }
        return option;
      }));
    } catch (error) {
      console.log(error);
    }
  };
  input.addEventListener('input', () => {
    const chosen = found.get(input.value);
    if (chosen) {
      picked(chosen);
      return;
    }
    clearTimeout(waiting);
    waiting = setTimeout(suggest, TOWN_WAIT_MS);
  });
  // the named places, before anything's typed
  input.addEventListener('focus', () => {
    if (!input.value) {
      suggest();
    }
  });
  return list;
}

function infoButton(label, action) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'control';
  button.textContent = label;
  button.onclick = action;
  return button;
}

function infoNote(text) {
  const note = document.createElement('p');
  note.className = 'library-hint';
  note.textContent = text;
  return note;
}

// after a change or its undo: the card shows it, and so does the page, such as by moving the photo
async function infoChanged(path, message, undo) {
  let info = null;
  try {
    const response = await fetch(`/api/info?${new URLSearchParams({path})}`);
    info = response.ok ? await response.json() : null;
  } catch (error) {
    console.log(error);
  }
  if (info) {
    window.dispatchEvent(new CustomEvent('photo-changed', {detail: {path, ...info}}));
  }
  if (document.getElementById('info-card')?.dataset.path === path) {
    showInfo(path);
  }
  if (message) {
    showToast(message, undo && (async () => {
      try {
        await undo();
        await infoChanged(path);
      } catch (error) {
        showToast(`Couldn't undo: ${error.message}`);
      }
    }));
  }
}

function dateEditor(path, info) {
  const editor = document.createElement('div');
  editor.className = 'info-editor';
  editor.hidden = true;
  const input = document.createElement('input');
  input.type = 'datetime-local';
  input.value = info.taken || '';
  input.min = '1902-01-01T00:00';
  input.setAttribute('aria-label', 'Date and time taken');
  const save = infoButton('Save date', async () => {
    if (!input.value) {
      return;
    }
    try {
      const {previous} = await postJson('/api/photo-date', {photo_id: info.photo_id, taken: input.value});
      infoChanged(path, 'Date changed', () => postJson('/api/dates/restore', {previous}));
    } catch (error) {
      showToast(`Couldn't change the date: ${error.message}`);
    }
  });
  editor.append(input, save);
  if (info.date_assigned) {
    editor.append(
      infoButton("Use the photo's own date", async () => {
        try {
          const {previous} = await postJson('/api/dates/own', {photo_ids: [info.photo_id]});
          infoChanged(path, "Back to the photo's own date", () => postJson('/api/dates/restore', {previous}));
        } catch (error) {
          showToast(`Couldn't change the date: ${error.message}`);
        }
      }),
      infoNote(info.own_date ? `Its own date: ${info.own_date}` : 'Its file has no date of its own.')
    );
  }
  return editor;
}

function placeEditor(path, info) {
  const editor = document.createElement('div');
  editor.className = 'info-editor';
  editor.hidden = true;
  const search = document.createElement('input');
  search.type = 'text';
  search.placeholder = 'Named place or town';
  search.setAttribute('aria-label', 'Town');
  const mapBox = document.createElement('div');
  mapBox.className = 'info-map';
  const hint = infoNote('Choose a named place or a town, or tap the map where it was taken.');
  let point = null;
  let map = null;
  const save = infoButton('Save place', async () => {
    if (!point) {
      return;
    }
    try {
      const {previous} = await postJson('/api/locations', {photo_ids: [info.photo_id], ...point});
      rememberPlace(point);
      infoChanged(path, 'Place changed', () => postJson('/api/locations/restore', {previous}));
    } catch (error) {
      showToast(`Couldn't change the place: ${error.message}`);
    }
  });
  save.disabled = true;

  function choose(lat, lon) {
    point = {lat, lon};
    save.disabled = false;
  }

  const suggestions = townSearch(search, town => {
    choose(town.lat, town.lon);
    map?.pin(town.lat, town.lon, TOWN_ZOOM);
  });
  const changes = [save];
  if (info.location) {
    changes.push(infoButton('No place', async () => {
      try {
        const {previous} = await postJson('/api/locations', {photo_ids: [info.photo_id]});
        infoChanged(path, 'Place taken off', () => postJson('/api/locations/restore', {previous}));
      } catch (error) {
        showToast(`Couldn't change the place: ${error.message}`);
      }
    }));
  }
  // given by hand, as a place or as having none
  if (info.location?.set || (!info.location && info.own_location)) {
    changes.push(infoButton("Use the photo's own place", async () => {
      try {
        const {previous} = await postJson('/api/locations', {photo_ids: [info.photo_id], own: true});
        infoChanged(path, "Back to the photo's own place", () => postJson('/api/locations/restore', {previous}));
      } catch (error) {
        showToast(`Couldn't change the place: ${error.message}`);
      }
    }));
  }
  const buttons = document.createElement('div');
  buttons.className = 'info-editor-buttons';
  buttons.append(...changes);
  editor.append(search, suggestions, mapBox, hint, buttons);
  if (info.location?.set || (!info.location && info.own_location)) {
    const own = info.own_location;
    editor.append(infoNote(own ? `Its own place: near ${[own.city, own.state, own.country].filter(Boolean).join(', ')}` : 'Its file has no place of its own.'));
  }

  editor.open = async () => {
    if (map) {
      return;
    }
    try {
      map = await placeMap(mapBox, info.location || info.own_location || lastPlace(), choose);
    } catch (error) {
      mapBox.replaceWith(infoNote(error.message));
      return;
    }
    if (info.location) {
      map.pin(info.location.lat, info.location.lon);
    }
  };
  return editor;
}

// gives the place a photo was taken a name, such as Home, which every photo near it then has
function labelEditor(path, info) {
  const editor = document.createElement('div');
  editor.className = 'info-editor';
  editor.hidden = true;
  const name = document.createElement('input');
  name.type = 'text';
  name.maxLength = 80;
  name.placeholder = 'Name, such as Home';
  name.setAttribute('aria-label', 'Name for this place');
  const radius = document.createElement('select');
  radius.setAttribute('aria-label', 'How far around it counts');
  radius.append(...LABEL_RADII.map(metres => {
    const option = document.createElement('option');
    option.value = metres;
    option.textContent = `Within ${metres < 1000 ? `${metres} m` : `${metres / 1000} km`}`;
    option.selected = metres === DEFAULT_LABEL_RADIUS;
    return option;
  }));
  const label = replace => postJson('/api/place-labels', {
    name: name.value,
    lat: info.location.lat,
    lon: info.location.lon,
    radius: Number(radius.value),
    replace
  });
  const saved = ({name: given, previous}, moved) => infoChanged(
    path,
    moved ? `Moved ${given} here` : `Named this place ${given}`,
    () => postJson('/api/place-labels/restore', {previous})
  );
  const save = infoButton('Save name', async () => {
    if (!name.value.trim()) {
      return;
    }
    try {
      saved(await label(false), false);
    } catch (error) {
      if (!error.message.includes('409')) {
        showToast(`Couldn't name this place: ${error.message}`);
        return;
      }
      showToast(`There's already a place called ${name.value.trim()}`, async () => {
        try {
          saved(await label(true), true);
        } catch (again) {
          showToast(`Couldn't name this place: ${again.message}`);
        }
      }, 'Move it here');
    }
  });
  name.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      save.click();
    }
  });
  editor.append(name, radius, save, infoNote('Every photo taken within this distance will be at this place.'));
  return editor;
}

function editors(path, info) {
  const row = document.createElement('div');
  row.className = 'info-editor-buttons';
  const date = dateEditor(path, info);
  const place = placeEditor(path, info);
  // only a place that isn't named yet, as one that is can be changed from its own page
  const label = info.location && !info.location.label ? labelEditor(path, info) : null;
  const all = [date, place, label].filter(Boolean);
  // one at a time
  const toggle = editor => () => {
    all.filter(other => other !== editor).forEach(other => other.hidden = true);
    editor.hidden = !editor.hidden;
    if (!editor.hidden && editor.open) {
      editor.open();
    }
  };
  row.append(infoButton('Change date', toggle(date)), infoButton('Change place', toggle(place)));
  if (label) {
    row.append(infoButton('Name this place', toggle(label)));
  }
  const wrapper = document.createElement('section');
  wrapper.append(row, ...all);
  return wrapper;
}

async function showInfo(path) {
  closeInfo();
  const card = document.createElement('aside');
  card.id = 'info-card';
  card.className = 'info-card';
  card.dataset.path = path;
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
      body.push(infoSection(location.set ? 'Location, set by hand' : 'Location', [
        ...(location.label ? [{label: 'Place', value: location.label, href: placeLabelUrl(location.label)}] : []),
        ...[['Near', location.city], ['County', location.county], ['State', location.state], ['Country', location.country]]
          .filter(([, value]) => value)
          .map(([label, value]) => ({label, value})),
        {
          label: 'Map',
          value: coordinates,
          href: `/places/map?${new URLSearchParams({lat: location.lat, lon: location.lon})}`
        }
      ]));
    } else {
      const none = document.createElement('p');
      none.className = 'library-hint';
      none.textContent = info.own_location
        ? 'Set to have no location.'
        : info.location_read ? 'The photo has no location in it.' : "Location not read yet: this happens at the end of each scan.";
      body.push(none);
    }
    if (info.photo_id && typeof postJson === 'function') {
      body.push(editors(path, info));
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
