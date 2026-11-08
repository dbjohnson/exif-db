// /duplicates: groups that might be copies, one at a time with keys to decide, and the groups hidden
// automatically, a page at a time.  Decisions save in the background, so the next group shows at once

const encodePath = path => path.split('/').map(encodeURIComponent).join('/');
const photoUrl = (api, photo) => `/api/${api}${encodePath(photo.path)}${photo.rotation ? `?rotate=${photo.rotation}` : ''}`;
const plural = (count, noun) => `${count.toLocaleString()} ${noun}${count === 1 ? '' : 's'}`;
const STATUS_NAMES = {approved: 'Approved', unreviewed: 'Unreviewed', rejected: 'Hidden'};
// how many groups ahead to load the pictures of
const PRELOAD = 2;
const HIDDEN_PAGE = 30;
const HIDDEN_MARGIN = 800;

// ---- what's shown about each copy

function formatTaken(value) {
  const [date, time] = value.split(' ');
  const [year, month, day] = date.split(':').map(Number);
  const [hour, minute] = (time || '0:0').split(':').map(Number);
  return new Date(year, month - 1, day, hour, minute).toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'});
}

function formatBytes(bytes) {
  return bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.round((bytes || 0) / 1e3)} KB`;
}

function fields(photo) {
  const pixels = (photo.width || 0) * (photo.height || 0);
  return [
    ['Size', photo.width ? `${photo.width.toLocaleString()} × ${photo.height.toLocaleString()} (${(pixels / 1e6).toFixed(1)} MP)` : 'Unknown'],
    ['File', `${photo.type || 'Unknown'}, ${formatBytes(photo.size)}`],
    ['Taken', photo.taken ? formatTaken(photo.taken) : photo.dated && photo.date ? `${formatTaken(photo.date)}, set by hand` : 'No date of its own'],
    ['Camera', photo.camera_name || (photo.screenshot ? 'None: looks like a screenshot' : 'None')],
    ['Folder', photo.folder],
    ['Name', photo.name],
    ['Status', `${photo.hidden_as_copy ? 'Hidden as a copy' : STATUS_NAMES[photo.status] || photo.status}${photo.favorite ? ', favorite' : ''}`]
  ];
}

// each copy's details, with whatever isn't the same for all of them picked out
function detailLists(photos) {
  const all = photos.map(photo => fields(photo));
  return all.map(list => {
    const element = document.createElement('dl');
    list.forEach(([name, value], row) => {
      const term = document.createElement('dt');
      term.textContent = name;
      const detail = document.createElement('dd');
      detail.textContent = value;
      detail.classList.toggle('differs', all.some(other => other[row][1] !== value));
      element.append(term, detail);
    });
    return element;
  });
}

// ---- one group at a time

const REVIEW = document.getElementById('review');
// one photo and what may be copies of it, as the library's viewer opens this, in a frame of its own
const PHOTO = REVIEW?.dataset.photo;
const EMBEDDED = REVIEW?.hasAttribute('data-embed');
// what the duplicate check made of each one next to this photo
const VERDICTS = {
  same: 'Same photo',
  possible: 'May be the same',
  different: 'Found different',
  export: 'Full-size export'
};
// for the one photo: how many more look alike, which the check found different, and whether to show them
let photoFound = null;
let everything = false;

let queue = [];
let position = 0;
// which copies of the group showing are marked to keep
let marked = new Set();
// decisions that can be taken back, latest last: the group, where it was, and its saved result
const undoable = [];

const current = () => queue[position];

function defaultMarks(group) {
  return new Set([group.best]);
}

function preload() {
  for (let ahead = 1; ahead <= PRELOAD && ahead < queue.length; ahead += 1) {
    for (const photo of queue[(position + ahead) % queue.length].photos) {
      new Image().src = photoUrl('image', photo);
    }
  }
}

function renderReview() {
  const copies = document.getElementById('review-copies');
  const count = document.getElementById('review-count');
  const keep = document.getElementById('review-keep');
  const different = document.getElementById('review-different');
  document.getElementById('review-undo').disabled = !undoable.length;
  const group = current();
  for (const id of ['review-back', 'review-skip']) {
    const button = document.getElementById(id);
    if (button) {
      button.disabled = queue.length < 2;
    }
  }
  if (PHOTO) {
    renderMore();
  }
  if (!group) {
    count.textContent = PHOTO ? '' : 'none left';
    copies.replaceChildren(Object.assign(document.createElement('p'), {
      className: 'no-images',
      textContent: PHOTO ? nothingForPhoto() : 'Nothing left to look at.'
    }));
    keep.disabled = different.disabled = true;
    keep.textContent = 'Keep';
    return;
  }
  count.textContent = PHOTO
    ? `${group.photos.find(photo => photo.this)?.name || ''} and ${plural(group.photos.length - 1, 'other')}`
    : `${(position + 1).toLocaleString()} of ${plural(queue.length, 'group')} to look at`;
  const lists = detailLists(group.photos);
  // up to four in a row; more go in rows of three, with smaller pictures to fit two rows on the screen
  const copyCount = group.photos.length;
  copies.style.setProperty('--columns', copyCount <= 4 ? copyCount : 3);
  copies.classList.toggle('two-rows', copyCount > 4);
  copies.replaceChildren(...group.photos.map((photo, index) => {
    const kept = marked.has(photo.id);
    const copy = document.createElement('section');
    copy.className = `dupe-copy ${kept ? 'marked-keep' : 'marked-hide'}`;
    const label = document.createElement('div');
    label.className = 'dupe-label';
    const key = document.createElement('kbd');
    key.textContent = index + 1;
    const state = document.createElement('span');
    state.className = 'dupe-state';
    state.textContent = kept ? 'Keep' : 'Hide';
    label.append(key, state);
    if (photo.this) {
      label.append(Object.assign(document.createElement('span'), {className: 'dupe-tag', textContent: 'This photo'}));
    } else if (PHOTO) {
      label.append(Object.assign(document.createElement('span'), {
        className: 'dupe-tag',
        textContent: VERDICTS[photo.verdict] || (photo.in_group ? 'Grouped with it' : 'Not compared')
      }));
    }
    if (photo.best) {
      const badge = document.createElement('span');
      badge.className = 'dupe-better';
      badge.textContent = 'Best copy';
      badge.title = 'More detail, or else from a camera, not a screenshot, with a date, in a named folder';
      label.append(badge);
    }
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'dupe-picture';
    toggle.title = kept ? `Mark to hide (Shift+${index + 1})` : `Mark to keep (Shift+${index + 1})`;
    toggle.setAttribute('aria-pressed', kept);
    const image = document.createElement('img');
    image.alt = photo.name;
    image.src = photoUrl('image', photo);
    toggle.append(image);
    toggle.onclick = () => toggleMark(photo.id);
    const original = document.createElement('a');
    original.className = 'dupe-original';
    original.href = `/api/original${encodePath(photo.path)}`;
    original.target = '_blank';
    original.rel = 'noopener';
    original.textContent = 'Original';
    label.append(original);
    const only = document.createElement('button');
    only.type = 'button';
    only.className = 'control dupe-only';
    only.textContent = 'Keep only this';
    only.title = `Hide the others (${index + 1})`;
    only.onclick = () => keepOnly(photo.id);
    copy.append(label, toggle, only, lists[index]);
    return copy;
  }));
  const keeping = group.photos.filter(photo => marked.has(photo.id)).length;
  const hiding = group.photos.length - keeping;
  keep.disabled = !keeping;
  keep.textContent = !keeping ? 'Mark one to keep'
    : !hiding ? 'Keep all of them'
    : `Keep ${keeping}, hide ${hiding}`;
  different.disabled = false;
  preload();
}

function show(index) {
  position = queue.length ? (index + queue.length) % queue.length : 0;
  marked = current() ? defaultMarks(current()) : new Set();
  renderReview();
  // a phone is often scrolled down to the details, and the next group starts at its pictures
  const top = document.getElementById('review-copies').getBoundingClientRect().top;
  if (top < 0) {
    window.scrollBy(0, top - 8);
  }
}

function step(direction) {
  if (queue.length > 1) {
    show(position + direction);
  }
}

function toggleMark(photoId) {
  marked.has(photoId) ? marked.delete(photoId) : marked.add(photoId);
  renderReview();
}

// takes the group showing off the queue, saves the decision, and moves on to the next
function decide(url, body, message) {
  const group = current();
  const index = position;
  const kept = body.keep || body.photo_ids;
  // what leaves the library: those showing that aren't kept
  const hidden = group.showing_ids.filter(id => !kept.includes(id));
  const entry = {group, index, hidden, saved: postJson(url, body)};
  undoable.push(entry);
  queue.splice(index, 1);
  show(Math.min(index, queue.length - 1));
  entry.saved.then(
    () => {
      tellLibrary({hidden});
      showToast(message, undo);
    },
    error => {
      console.log(error);
      undoable.splice(undoable.indexOf(entry), 1);
      putBack(entry);
      showToast(`Couldn't save: ${error.message}`);
    }
  );
}

// the library page this is open over, which takes hidden photos off its timeline
function tellLibrary(change) {
  if (EMBEDDED && window.parent !== window) {
    window.parent.postMessage({duplicates: change}, window.location.origin);
  }
}

function closeReview() {
  tellLibrary({close: true});
}

function keepIds(ids) {
  const group = current();
  if (!group || !ids.length) {
    return;
  }
  // the best of those kept first, as that one takes over what the hidden ones had
  const ordered = group.photos.map(photo => photo.id).filter(id => ids.includes(id));
  const hiding = group.photos.length - ordered.length;
  if (!hiding) {
    notDuplicates();
    return;
  }
  decide(
    '/api/duplicates/keep',
    {keep: ordered, photo_ids: group.photo_ids},
    `Kept ${ordered.length === 1 ? 'one copy' : `${ordered.length} copies`}, hid ${hiding}`
  );
}

function keepOnly(photoId) {
  keepIds([photoId]);
}

function keepMarked() {
  keepIds([...marked]);
}

// only those showing: any hidden here are copies of something else, whatever these turn out to be
function notDuplicates() {
  const group = current();
  if (group) {
    decide('/api/duplicates/show', {photo_ids: group.showing_ids}, 'Not duplicates: all kept');
  }
}

function putBack(entry) {
  const index = Math.min(entry.index, queue.length);
  queue.splice(index, 0, entry.group);
  show(index);
}

async function undo() {
  const entry = undoable.pop();
  if (!entry) {
    return;
  }
  hideToast();
  putBack(entry);
  try {
    const {previous} = await entry.saved;
    await postJson('/api/duplicates/restore', {previous});
    tellLibrary({shown: entry.hidden});
  } catch (error) {
    console.log(error);
    showToast(`Couldn't undo: ${error.message}`);
  }
}

async function loadReview() {
  try {
    const url = PHOTO
      ? `/api/duplicates/photo?${new URLSearchParams({photo_id: PHOTO, everything})}`
      : '/api/duplicates/review';
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error('Status: ' + response.status);
    }
    const found = await response.json();
    if (PHOTO) {
      photoFound = found;
      queue = found.group ? [found.group] : [];
    } else {
      queue = found;
    }
  } catch (error) {
    document.getElementById('review-copies').textContent = `Couldn't load these: ${error.message}`;
    return;
  }
  show(0);
}

function nothingForPhoto() {
  if (undoable.length) {
    return 'Done.';
  }
  if (photoFound && !photoFound.checked) {
    return "This photo hasn't been checked for duplicates yet, which happens after each scan.";
  }
  return photoFound?.more ? 'Nothing the duplicate check thinks may be a copy of this photo.' : 'Nothing else in the library looks like this photo.';
}

// those that look alike but that the check compared and found different, which may still be wanted
function renderMore() {
  const more = document.getElementById('review-more');
  more.hidden = everything || !photoFound?.more || undoable.length > 0;
  more.textContent = `Also show ${plural(photoFound?.more || 0, 'photo')} that look${photoFound?.more === 1 ? 's' : ''} alike but aren't copies to hide`;
}

function showEverything() {
  everything = true;
  loadReview();
}

document.addEventListener('keydown', event => {
  if (!REVIEW || event.ctrlKey || event.metaKey || event.altKey) {
    return;
  }
  if (event.key === 'Escape' && EMBEDDED) {
    closeReview();
    return;
  }
  if (event.target.closest('input, textarea, select')) {
    return;
  }
  const group = current();
  const digit = /^Digit([1-9])$/.exec(event.code);
  if (digit && group) {
    const photo = group.photos[Number(digit[1]) - 1];
    if (photo) {
      event.preventDefault();
      event.shiftKey ? toggleMark(photo.id) : keepOnly(photo.id);
    }
    return;
  }
  if (event.shiftKey) {
    return;
  }
  const actions = {
    Enter: keepMarked,
    n: notDuplicates,
    ...(PHOTO ? {} : {ArrowRight: () => step(1), ArrowLeft: () => step(-1)}),
    u: undo,
    z: undo
  };
  const action = actions[event.key.length === 1 ? event.key.toLowerCase() : event.key];
  if (action) {
    // Enter on a focused button would press it as well
    event.preventDefault();
    action();
  }
});

// ---- the groups hidden automatically

let hiddenLoaded = 0;
let hiddenTotal = null;
let hiddenLoading = false;

function hiddenTile(group, photo) {
  const tile = document.createElement('figure');
  tile.className = `duplicate-tile${photo.best ? ' kept' : ''}${photo.showing ? '' : ' hidden-copy'}`;
  const link = document.createElement('a');
  link.href = photoUrl('image', photo);
  link.target = '_blank';
  link.rel = 'noopener';
  const image = document.createElement('img');
  image.src = photoUrl('thumb', photo);
  image.loading = 'lazy';
  image.decoding = 'async';
  image.alt = photo.name;
  image.onerror = () => image.remove();
  link.append(image);
  const caption = document.createElement('figcaption');
  const line = text => Object.assign(document.createElement('span'), {textContent: text});
  if (photo.best) {
    caption.append(Object.assign(document.createElement('strong'), {className: 'duplicate-badge', textContent: 'Best copy'}));
  } else if (!photo.showing) {
    caption.append(Object.assign(document.createElement('span'), {className: 'duplicate-badge', textContent: 'Hidden'}));
  }
  caption.append(
    line(photo.name),
    line(photo.folder),
    line([
      photo.width && photo.height ? `${photo.width} × ${photo.height}` : null,
      photo.type,
      photo.screenshot ? 'screenshot' : null
    ].filter(Boolean).join(' · '))
  );
  if (!(photo.best && photo.showing)) {
    const actions = document.createElement('span');
    actions.className = 'duplicate-actions';
    const button = (text, act) => {
      const element = document.createElement('button');
      element.textContent = text;
      element.onclick = act;
      return element;
    };
    actions.append(button('Keep this instead', event => hiddenDecide(event.target, '/api/duplicates/keep', {keep: [photo.id], photo_ids: group.photo_ids}, `Kept ${photo.name} instead`)));
    if (!photo.showing) {
      actions.append(button('Show this too', event => hiddenDecide(event.target, '/api/duplicates/show', {photo_ids: [photo.id]}, `${photo.name} shows again`)));
    }
    caption.append(actions);
  }
  tile.append(link, caption);
  return tile;
}

function hiddenGroup(group) {
  const element = document.createElement('div');
  element.className = 'duplicate-group';
  const copies = document.createElement('div');
  copies.className = 'duplicate-copies';
  copies.append(...group.photos.map(photo => hiddenTile(group, photo)));
  element.append(copies);
  return element;
}

// a group's place on the page after a change, which may be that there's nothing left to show of it
function replaceGroup(element, group) {
  const replacement = group ? hiddenGroup(group) : document.createElement('div');
  element.replaceWith(replacement);
  return replacement;
}

async function hiddenDecide(button, url, body, message) {
  const element = button.closest('.duplicate-group');
  button.disabled = true;
  try {
    const {previous, group} = await postJson(url, body);
    let shown = replaceGroup(element, group);
    showToast(message, async () => {
      try {
        const restored = await postJson('/api/duplicates/restore', {previous});
        shown = replaceGroup(shown, restored.group);
      } catch (error) {
        showToast(`Couldn't undo: ${error.message}`);
      }
    });
  } catch (error) {
    console.log(error);
    button.disabled = false;
    showToast(`Couldn't save: ${error.message}`);
  }
}

async function loadHidden() {
  const more = document.getElementById('hidden-more');
  if (hiddenLoading || (hiddenTotal !== null && hiddenLoaded >= hiddenTotal)) {
    return;
  }
  hiddenLoading = true;
  more.textContent = 'Loading…';
  try {
    const response = await fetch(`/api/duplicates/hidden?${new URLSearchParams({offset: hiddenLoaded, limit: HIDDEN_PAGE})}`);
    if (!response.ok) {
      throw new Error('Status: ' + response.status);
    }
    const {total, groups} = await response.json();
    hiddenTotal = total;
    hiddenLoaded += groups.length;
    document.getElementById('hidden-groups').append(...groups.map(hiddenGroup));
    more.hidden = hiddenLoaded >= total || !groups.length;
    more.textContent = '';
  } catch (error) {
    more.textContent = `Couldn't load more: ${error.message}`;
    return;
  } finally {
    hiddenLoading = false;
  }
  // the observer only says when that changes, so a page that didn't reach the screen's end needs another
  if (!more.hidden && more.getBoundingClientRect().top < innerHeight + HIDDEN_MARGIN) {
    loadHidden();
  }
}

if (REVIEW) {
  loadReview();
}
if (document.getElementById('hidden-list')) {
  // the next page as the end of what's loaded comes near the screen
  new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) {
      loadHidden();
    }
  }, {rootMargin: `${HIDDEN_MARGIN}px`}).observe(document.getElementById('hidden-more'));
}
