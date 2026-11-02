// the map on /people: a dot for each group of faces and each named person's faces, placed by the
// server so that those that look alike are close together.  Dots are pushed apart here, as their
// size depends on the screen.  On a person's "Looks like" page, the same map shows only their groups
// and those that look enough like them
const MAP_MERGE = 'people-map-merge';
const SVG_NS = 'http://www.w3.org/2000/svg';
let mapPerson = null;
let mapResult = null;
// on a person's page, how alike each of mapResult.groups is to the closest of theirs, out of 1
let mapNear = null;
let mapView = null;
// whether someone has zoomed or moved the map since it was drawn, so a newer one shouldn't replace it
let mapMoved = false;

function setMapNote(text, spinning = false) {
  const note = document.getElementById('map-note');
  note.replaceChildren();
  if (spinning) {
    const spinner = document.createElement('span');
    spinner.className = 'spinner';
    spinner.setAttribute('aria-hidden', 'true');
    note.append(spinner, ' ');
  }
  note.append(text);
  note.hidden = !text;
}

function setMapLoading(text, spinning = true) {
  document.getElementById('map-loading').hidden = !text;
  document.getElementById('map-loading-text').textContent = text;
  document.querySelector('#map-loading .spinner').hidden = !spinning;
}

// the map is worked out in the background after each scan and when names change.  An older one is
// shown while a newer one is on its way
async function loadMap() {
  let wait = 3000;
  try {
    const response = await fetch(mapPerson === null ? '/api/people/map' : `/api/people/map?near=${encodeURIComponent(mapPerson)}`);
    if (!response.ok) {
      throw new Error('Status: ' + response.status);
    }
    const {map, near, working, failed} = await response.json();
    if (!mapResult) {
      if (map) {
        mapResult = map;
        mapNear = near;
        drawMap();
      } else if (failed) {
        setMapLoading("Couldn't work out the map. The server's log says why.", false);
        return;
      } else {
        setMapLoading('Placing groups by how alike they look, which takes a minute or two after a restart');
      }
    } else if (!working) {
      if (failed) {
        setMapNote("Couldn't update the map. The server's log says why.");
      } else if (mapMoved) {
        setMapNote('');
        const refresh = document.getElementById('map-refresh');
        refresh.hidden = false;
        refresh.onclick = () => {
          refresh.hidden = true;
          mapResult = map;
          mapNear = near;
          drawMap();
        };
      } else {
        setMapNote('');
        mapResult = map;
        mapNear = near;
        drawMap();
      }
      return;
    }
    if (mapResult && working) {
      setMapNote('Updating for the latest names', true);
    } else if (mapResult) {
      return;
    }
  } catch (error) {
    // the next try may do better
    wait = 5000;
  }
  setTimeout(loadMap, wait);
}

function dotRadius(faces, scale) {
  return Math.max(7, Math.min(32, 2.5 * Math.sqrt(faces))) * scale;
}

// moves overlapping dots apart, a little at a time, as moving one can push it into another
function separate(dots) {
  for (let round = 0; round < 60; round++) {
    let moved = false;
    for (let i = 0; i < dots.length; i++) {
      const a = dots[i];
      for (let j = i + 1; j < dots.length; j++) {
        const b = dots[j];
        const dx = b.cx - a.cx;
        const dy = b.cy - a.cy;
        const needed = a.r + b.r + 2;
        if (Math.abs(dx) >= needed || Math.abs(dy) >= needed) {
          continue;
        }
        const distance = Math.hypot(dx, dy) || 0.01;
        if (distance < needed) {
          const push = (needed - distance) / 2;
          a.cx -= dx / distance * push;
          a.cy -= dy / distance * push;
          b.cx += dx / distance * push;
          b.cy += dy / distance * push;
          moved = true;
        }
      }
    }
    if (!moved) {
      break;
    }
  }
}

function svgElement(name, attributes) {
  const element = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value));
  return element;
}

function setView(svg, view) {
  mapView = view;
  svg.setAttribute('viewBox', `${view.x} ${view.y} ${view.w} ${view.h}`);
}

// the dots to show: on a person's page their groups and those at least as alike as asked for
function mapNodes() {
  if (mapPerson === null) {
    const merged = document.getElementById('map-merge').getAttribute('aria-pressed') === 'true';
    return mapResult[merged ? 'people' : 'groups'];
  }
  const threshold = Number(document.getElementById('map-threshold').value);
  return mapResult.groups
    .map((node, place) => ({...node, alike: mapNear[place], mine: node.name === mapPerson}))
    .filter(node => node.mine || node.alike >= threshold);
}

// where x or y goes across the space, spread over the dots shown, but no more than 4 times as much
// as the whole map is, so a few dots close together stay close together
function across(values, everything, size) {
  const low = Math.min(...values);
  const high = Math.max(...values);
  const range = Math.max(high - low, (Math.max(...everything) - Math.min(...everything)) / 4) || 1;
  const middle = (low + high) / 2;
  return value => ((value - middle) / range + 0.5) * size;
}

function drawMap() {
  const svg = document.getElementById('people-map');
  const layer = document.getElementById('map-dots');
  const nodes = mapNodes();
  layer.replaceChildren();
  mapMoved = false;
  if (mapPerson !== null && !nodes.some(node => node.mine)) {
    setMapLoading(`${mapPerson} has no group of 5 or more faces on the map yet`, false);
    return;
  }
  if (!nodes.length) {
    setMapLoading('No groups big enough to show yet', false);
    return;
  }
  setMapLoading('');
  const width = svg.clientWidth;
  const height = svg.clientHeight;
  const scale = Math.max(0.6, Math.min(1, width / 1200));
  const toX = across(nodes.map(node => node.x), mapResult.groups.map(node => node.x), width);
  const toY = across(nodes.map(node => node.y), mapResult.groups.map(node => node.y), height);
  const dots = nodes.map(node => ({
    ...node,
    cx: toX(node.x),
    cy: toY(node.y),
    r: dotRadius(node.faces, scale)
  }));
  separate(dots);

  // a person split into several groups gets one label, on their biggest
  const labelled = new Set();
  const labels = svgElement('g', {class: 'map-labels'});
  dots.sort((a, b) => b.faces - a.faces).forEach(dot => {
    const link = svgElement('a', {
      href: dot.name ? `/people/${encodeURIComponent(dot.name)}` : `/people/group/${dot.group}`,
      class: dot.mine ? 'map-dot mine' : dot.name ? 'map-dot named' : 'map-dot'
    });
    link.append(
      svgElement('image', {
        href: `/api/face/${dot.face}`,
        x: dot.cx - dot.r,
        y: dot.cy - dot.r,
        width: 2 * dot.r,
        height: 2 * dot.r,
        'clip-path': 'url(#map-circle)',
        preserveAspectRatio: 'xMidYMid slice'
      }),
      svgElement('circle', {cx: dot.cx, cy: dot.cy, r: dot.r})
    );
    link.addEventListener('pointerenter', event => showMapTip(event, dot));
    link.addEventListener('pointermove', moveMapTip);
    link.addEventListener('pointerleave', hideMapTip);
    layer.append(link);
    if (dot.name && !labelled.has(dot.name)) {
      labelled.add(dot.name);
      const label = svgElement('text', {x: dot.cx, y: dot.cy + dot.r + 13 * scale, 'text-anchor': 'middle', 'font-size': 12 * scale});
      label.textContent = dot.name;
      labels.append(label);
    }
  });
  layer.append(labels);

  // the whole map, with the same shape as the space it's shown in so that dots stay round
  const margin = 40 * scale;
  const minX = Math.min(...dots.map(dot => dot.cx - dot.r)) - margin;
  const maxX = Math.max(...dots.map(dot => dot.cx + dot.r)) + margin;
  const minY = Math.min(...dots.map(dot => dot.cy - dot.r)) - margin;
  const maxY = Math.max(...dots.map(dot => dot.cy + dot.r)) + margin;
  const fit = Math.max((maxX - minX) / width, (maxY - minY) / height);
  setView(svg, {
    x: (minX + maxX - width * fit) / 2,
    y: (minY + maxY - height * fit) / 2,
    w: width * fit,
    h: height * fit
  });
}

function showMapTip(event, dot) {
  if (event.pointerType !== 'mouse') {
    return;
  }
  const tip = document.getElementById('map-tip');
  const image = document.createElement('img');
  image.src = `/api/face/${dot.face}`;
  image.alt = '';
  const name = document.createElement('strong');
  name.textContent = dot.name || 'Not named yet';
  const faces = document.createElement('span');
  const count = `${dot.faces.toLocaleString()} face${dot.faces === 1 ? '' : 's'}`;
  faces.textContent = dot.name && dot.group !== null ? `${count} in this group` : count;
  tip.replaceChildren(image, name, faces);
  if (dot.alike !== undefined && !dot.mine) {
    const alike = document.createElement('span');
    alike.textContent = `${Math.round(dot.alike * 100)}% like ${mapPerson}`;
    tip.append(alike);
  }
  tip.hidden = false;
  moveMapTip(event);
}

function moveMapTip(event) {
  const tip = document.getElementById('map-tip');
  const flip = event.clientX + 14 + tip.offsetWidth > window.innerWidth;
  tip.style.left = `${flip ? event.clientX - 14 - tip.offsetWidth : event.clientX + 14}px`;
  tip.style.top = `${event.clientY + 14}px`;
}

function hideMapTip() {
  document.getElementById('map-tip').hidden = true;
}

// zooming by a factor, keeping the point at clientX, clientY where it is
function zoomMap(svg, factor, clientX, clientY) {
  mapMoved = true;
  const box = svg.getBoundingClientRect();
  const x = mapView.x + (clientX - box.left) / box.width * mapView.w;
  const y = mapView.y + (clientY - box.top) / box.height * mapView.h;
  setView(svg, {
    x: x - (x - mapView.x) * factor,
    y: y - (y - mapView.y) * factor,
    w: mapView.w * factor,
    h: mapView.h * factor
  });
}

// dragging pans, two fingers pinch to zoom, and a drag doesn't count as clicking the dot it ends on
function watchMapGestures(svg) {
  const pointers = new Map();
  let dragged = false;
  let start = null;
  let pinch = null;

  svg.addEventListener('pointerdown', event => {
    pointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
    if (pointers.size === 1) {
      dragged = false;
      start = {x: event.clientX, y: event.clientY};
    }
    pinch = null;
  });
  window.addEventListener('pointermove', event => {
    const last = pointers.get(event.pointerId);
    if (!last || !mapView) {
      return;
    }
    const box = svg.getBoundingClientRect();
    if (pointers.size === 1) {
      const dx = event.clientX - last.x;
      const dy = event.clientY - last.y;
      if (dx || dy) {
        dragged = dragged || Math.hypot(event.clientX - start.x, event.clientY - start.y) > 4;
        svg.classList.add('dragging');
        mapMoved = true;
        hideMapTip();
        setView(svg, {...mapView, x: mapView.x - dx / box.width * mapView.w, y: mapView.y - dy / box.height * mapView.h});
      }
    }
    pointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const distance = Math.hypot(a.x - b.x, a.y - b.y);
      if (pinch) {
        zoomMap(svg, pinch / distance, (a.x + b.x) / 2, (a.y + b.y) / 2);
      }
      pinch = distance;
      dragged = true;
    }
  });
  const lift = event => {
    pointers.delete(event.pointerId);
    pinch = null;
    if (!pointers.size) {
      svg.classList.remove('dragging');
    }
  };
  window.addEventListener('pointerup', lift);
  window.addEventListener('pointercancel', lift);
  svg.addEventListener('click', event => {
    if (dragged) {
      event.preventDefault();
      dragged = false;
    }
  }, true);
  svg.addEventListener('wheel', event => {
    if (!mapView) {
      return;
    }
    event.preventDefault();
    zoomMap(svg, Math.exp(event.deltaY * 0.002), event.clientX, event.clientY);
  }, {passive: false});
  const zoomFromMiddle = factor => {
    if (mapView) {
      const box = svg.getBoundingClientRect();
      zoomMap(svg, factor, box.left + box.width / 2, box.top + box.height / 2);
    }
  };
  document.getElementById('map-zoom-in').addEventListener('click', () => zoomFromMiddle(1 / 1.5));
  document.getElementById('map-zoom-out').addEventListener('click', () => zoomFromMiddle(1.5));
}

window.addEventListener('DOMContentLoaded', () => {
  const svg = document.getElementById('people-map');
  if (!svg) {
    return;
  }
  mapPerson = svg.dataset.person ?? null;
  const merge = document.getElementById('map-merge');
  if (merge) {
    // one dot per person unless someone has asked otherwise
    merge.setAttribute('aria-pressed', localStorage.getItem(MAP_MERGE) === '0' ? 'false' : 'true');
    merge.addEventListener('click', () => {
      const merged = merge.getAttribute('aria-pressed') !== 'true';
      merge.setAttribute('aria-pressed', merged ? 'true' : 'false');
      localStorage.setItem(MAP_MERGE, merged ? '1' : '0');
      if (mapResult) {
        drawMap();
      }
    });
  }
  // the map over the whole window, and back.  Not the browser's own full screen, which iPhones only
  // allow for videos
  const full = document.getElementById('map-full');
  const area = document.getElementById('map-area');
  const setFull = on => {
    area.classList.toggle('full', on);
    document.body.classList.toggle('map-full', on);
    full.setAttribute('aria-pressed', on ? 'true' : 'false');
    full.textContent = on ? 'Exit full page' : 'Full page';
    hideMapTip();
    if (mapResult) {
      drawMap();
    }
  };
  full.addEventListener('click', () => setFull(!area.classList.contains('full')));
  window.addEventListener('keydown', event => {
    if (event.key === 'Escape' && area.classList.contains('full')) {
      setFull(false);
    }
  });
  const threshold = document.getElementById('map-threshold');
  if (threshold) {
    let drawing = null;
    threshold.addEventListener('input', () => {
      document.getElementById('map-threshold-value').textContent = `${Math.round(threshold.value * 100)}%`;
      cancelAnimationFrame(drawing);
      drawing = requestAnimationFrame(() => mapResult && drawMap());
    });
  }
  watchMapGestures(svg);
  let resizing = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizing);
    resizing = setTimeout(() => mapResult && drawMap(), 200);
  });
  loadMap();
});
