// the map on /people: a dot for each group of faces and each named person's faces, placed by the
// server so that those that look alike are close together.  Dots are pushed apart here, as their
// size depends on the screen
const MAP_MERGE = 'people-map-merge';
const SVG_NS = 'http://www.w3.org/2000/svg';
let mapResult = null;
let mapView = null;

async function loadMap() {
  const note = document.getElementById('map-note');
  try {
    const response = await fetch('/api/people/map');
    if (!response.ok) {
      throw new Error('Status: ' + response.status);
    }
    const {map, failed} = await response.json();
    if (map) {
      mapResult = map;
      note.hidden = true;
      drawMap();
      return;
    }
    if (failed) {
      note.textContent = "Couldn't work out the map. The server's log says why.";
      return;
    }
  } catch (error) {
    // the next try may do better
  }
  setTimeout(loadMap, 3000);
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

function drawMap() {
  const svg = document.getElementById('people-map');
  const layer = document.getElementById('map-dots');
  const merged = document.getElementById('map-merge').checked;
  const nodes = mapResult[merged ? 'people' : 'groups'];
  layer.replaceChildren();
  if (!nodes.length) {
    const note = document.getElementById('map-note');
    note.textContent = 'No groups big enough to show yet.';
    note.hidden = false;
    return;
  }
  const width = svg.clientWidth;
  const height = svg.clientHeight;
  const scale = Math.max(0.6, Math.min(1, width / 1200));
  const xs = nodes.map(node => node.x);
  const ys = nodes.map(node => node.y);
  const [left, right, top, bottom] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const dots = nodes.map(node => ({
    ...node,
    cx: (node.x - left) / (right - left || 1) * width,
    cy: (node.y - top) / (bottom - top || 1) * height,
    r: dotRadius(node.faces, scale)
  }));
  separate(dots);

  // a person split into several groups gets one label, on their biggest
  const labelled = new Set();
  const labels = svgElement('g', {class: 'map-labels'});
  dots.sort((a, b) => b.faces - a.faces).forEach(dot => {
    const link = svgElement('a', {
      href: dot.name ? `/people/${encodeURIComponent(dot.name)}` : `/people/group/${dot.group}`,
      class: dot.name ? 'map-dot named' : 'map-dot'
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
  const merge = document.getElementById('map-merge');
  merge.checked = localStorage.getItem(MAP_MERGE) === '1';
  merge.addEventListener('change', () => {
    localStorage.setItem(MAP_MERGE, merge.checked ? '1' : '0');
    if (mapResult) {
      drawMap();
    }
  });
  watchMapGestures(svg);
  let resizing = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizing);
    resizing = setTimeout(() => mapResult && drawMap(), 200);
  });
  loadMap();
});
