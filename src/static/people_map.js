// the map on /people: a dot for each group of faces and each named person's faces, placed by the
// server so that those that look alike are close together.  Dots are pushed apart here, as their
// size depends on the screen.  On a person's "Looks like" page, the same map shows only their groups
// and those that look enough like them
const MAP_MERGE = 'people-map-merge';
// lines only go to dots at least this alike, leaving out the weakest matches, which the map had
// no reason to put nearby: those under 20% were a median of 30% of the map's width apart.  Unrelated
// faces' averages are 10 to 25% alike, so lines between 20 and 30% (15% apart) are often strangers
const MAP_LINK_ALIKE = 0.2;
// the smallest unnamed group shown, chosen with a slider.  Most groups are small: of about 900 with
// 5 or more faces, 330 have 10 or more and 45 have 50 or more
const MAP_SIZES = [5, 8, 10, 15, 20, 30, 50, 75, 100, 200];
const MAP_MIN_FACES = 'people-map-min-faces';

function minFaces() {
  return MAP_SIZES[Number(document.getElementById('map-size').value)];
}
const SVG_NS = 'http://www.w3.org/2000/svg';
let mapPerson = null;
let mapResult = null;
// on a person's page, how alike each of mapResult.groups is to the closest of theirs, out of 1
let mapNear = null;
// who to draw touching, as [name, name, how hard]: parents and children, and siblings less
let mapFamily = [];
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
    const {map, near, working, failed, family} = await response.json();
    mapFamily = family || [];
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
      setMapNote('Updating for the latest names and relatives', true);
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

// how hard relatives are pulled towards touching, and how far overlapping dots are pushed apart,
// each round, for some rounds and then for some more only pushing.  Pushing each overlapping pair
// fully apart in turn, as this used to, shuffled a crowded family: with 45 relatives, 13 of 49
// parents and children touched.  Small steps for all dots at once and this pull made it 30, and
// a stronger pull or more rounds did no better
const FAMILY_PULL = 0.5;
const OVERLAP_PUSH = 0.5;
const FAMILY_ROUNDS = 60;
const SPREAD_ROUNDS = 40;

// moves overlapping dots apart, while pulling relatives, given as [dot, dot, how hard], together
function spreadOut(dots, links) {
  const count = dots.length;
  const x = Float64Array.from(dots, dot => dot.cx);
  const y = Float64Array.from(dots, dot => dot.cy);
  const r = Float64Array.from(dots, dot => dot.r);
  const moveX = new Float64Array(count);
  const moveY = new Float64Array(count);
  const cell = 2 * Math.max(...r) + 2;
  // only dots in the same or a neighbouring square of a grid can touch.  Squares are numbered, as
  // building text keys each round took most of the time
  const columns = 1 << 16;
  const grid = new Map();
  for (let round = 0; round < FAMILY_ROUNDS + SPREAD_ROUNDS; round++) {
    const pulling = round < FAMILY_ROUNDS;
    moveX.fill(0);
    moveY.fill(0);
    grid.clear();
    for (let i = 0; i < count; i++) {
      const key = Math.floor(y[i] / cell) * columns + Math.floor(x[i] / cell);
      const square = grid.get(key);
      if (square) {
        square.push(i);
      } else {
        grid.set(key, [i]);
      }
    }
    let worst = 0;
    for (let i = 0; i < count; i++) {
      const gx = Math.floor(x[i] / cell);
      const gy = Math.floor(y[i] / cell);
      for (let row = gy - 1; row <= gy + 1; row++) {
        for (let column = gx - 1; column <= gx + 1; column++) {
          const square = grid.get(row * columns + column);
          if (!square) {
            continue;
          }
          for (const j of square) {
            if (j <= i) {
              continue;
            }
            const dx = x[j] - x[i];
            const dy = y[j] - y[i];
            const distance = Math.hypot(dx, dy) || 0.01;
            const overlap = r[i] + r[j] + 2 - distance;
            if (overlap > 0) {
              worst = Math.max(worst, overlap);
              const push = OVERLAP_PUSH * overlap / 2 / distance;
              moveX[i] -= dx * push;
              moveY[i] -= dy * push;
              moveX[j] += dx * push;
              moveY[j] += dy * push;
            }
          }
        }
      }
    }
    if (pulling) {
      for (const [i, j, strength] of links) {
        const dx = x[j] - x[i];
        const dy = y[j] - y[i];
        const distance = Math.hypot(dx, dy) || 0.01;
        const gap = distance - r[i] - r[j] - 2;
        if (gap > 0) {
          const pull = FAMILY_PULL * strength * gap / 2 / distance;
          moveX[i] += dx * pull;
          moveY[i] += dy * pull;
          moveX[j] -= dx * pull;
          moveY[j] -= dy * pull;
        }
      }
    } else if (worst < 0.5) {
      break;
    }
    for (let i = 0; i < count; i++) {
      x[i] += moveX[i];
      y[i] += moveY[i];
    }
  }
  dots.forEach((dot, i) => {
    dot.cx = x[i];
    dot.cy = y[i];
  });
}

// mapFamily as places in dots, going by each person's biggest dot
function familyLinks(dots) {
  const biggest = new Map();
  dots.forEach((dot, index) => {
    if (dot.name && !(biggest.has(dot.name) && dots[biggest.get(dot.name)].faces >= dot.faces)) {
      biggest.set(dot.name, index);
    }
  });
  return mapFamily
    .filter(([first, second]) => biggest.has(first) && biggest.has(second))
    .map(([first, second, strength]) => [biggest.get(first), biggest.get(second), strength]);
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
function mapSource() {
  if (mapPerson !== null) {
    return mapResult.groups;
  }
  const merged = document.getElementById('map-merge').getAttribute('aria-pressed') === 'true';
  return mapResult[merged ? 'people' : 'groups'];
}

function mapNodes() {
  // each keeps its place in the list, which is what its closest dots are given by.  Named people
  // are never too small to show
  const smallest = minFaces();
  const source = mapSource()
    .map((node, index) => ({...node, index}))
    .filter(node => node.name || node.faces >= smallest);
  if (mapPerson === null) {
    return source;
  }
  const threshold = Number(document.getElementById('map-threshold').value);
  return source
    .map(node => ({...node, alike: mapNear[node.index], mine: node.name === mapPerson}))
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

// lines from a dot to the most alike of those shown, each with how alike they are
function drawLinks(links, dot, drawn, scale) {
  links.replaceChildren();
  dot.near.forEach(([index, alike]) => {
    const other = drawn.get(index);
    if (!other || alike < MAP_LINK_ALIKE) {
      return;
    }
    // stronger for those more alike
    const strength = (alike - MAP_LINK_ALIKE) / (1 - MAP_LINK_ALIKE);
    links.append(svgElement('line', {
      x1: dot.cx,
      y1: dot.cy,
      x2: other.cx,
      y2: other.cy,
      'stroke-width': 1.5 + 3 * strength,
      'stroke-opacity': 0.5 + 0.5 * strength
    }));
    const label = svgElement('text', {
      x: dot.cx + (other.cx - dot.cx) * 0.6,
      y: dot.cy + (other.cy - dot.cy) * 0.6 + 4 * scale,
      'text-anchor': 'middle',
      'font-size': 12 * scale
    });
    label.textContent = `${Math.round(alike * 100)}%`;
    links.append(label);
  });
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
  const everything = mapSource();
  const toX = across(nodes.map(node => node.x), everything.map(node => node.x), width);
  const toY = across(nodes.map(node => node.y), everything.map(node => node.y), height);
  const dots = nodes.map(node => ({
    ...node,
    cx: toX(node.x),
    cy: toY(node.y),
    r: dotRadius(node.faces, scale)
  }));
  spreadOut(dots, familyLinks(dots));
  const drawn = new Map(dots.map(dot => [dot.index, dot]));
  const links = svgElement('g', {class: 'map-links'});

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
    link.addEventListener('pointerenter', event => {
      showMapTip(event, dot);
      if (event.pointerType === 'mouse') {
        drawLinks(links, dot, drawn, scale);
      }
    });
    link.addEventListener('pointermove', moveMapTip);
    link.addEventListener('pointerleave', () => {
      hideMapTip();
      links.replaceChildren();
    });
    layer.append(link);
    if (dot.name && !labelled.has(dot.name)) {
      labelled.add(dot.name);
      const label = svgElement('text', {x: dot.cx, y: dot.cy + dot.r + 13 * scale, 'text-anchor': 'middle', 'font-size': 12 * scale});
      label.textContent = dot.name;
      labels.append(label);
    }
  });
  layer.append(links, labels);

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
  if (dot.near.length && dot.near[0][1] < MAP_LINK_ALIKE) {
    const alone = document.createElement('span');
    alone.textContent = `Nothing else here is ${Math.round(MAP_LINK_ALIKE * 100)}% or more alike`;
    tip.append(alone);
  }
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
  const size = document.getElementById('map-size');
  const sizeShown = () => {
    document.getElementById('map-size-value').textContent = minFaces();
  };
  const stored = MAP_SIZES.indexOf(Number(localStorage.getItem(MAP_MIN_FACES)));
  size.value = stored >= 0 ? stored : 0;
  sizeShown();
  let filtering = null;
  size.addEventListener('input', () => {
    sizeShown();
    localStorage.setItem(MAP_MIN_FACES, minFaces());
    cancelAnimationFrame(filtering);
    filtering = requestAnimationFrame(() => mapResult && drawMap());
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
