// The website of an export: every photo by month, newest first, people to narrow them to, and a
// viewer.  Everything comes from data.js, and every address is relative, so the folder works
// wherever it's put: S3, any other static host, or opened straight from a disk.  With a password,
// data.js and the photos are in a folder named after it, which unlock.js works out
(function () {
  const site = window.EXPORT_SITE;
  // where data.js and the photos are, from this page: '' or the password's folder and a slash
  let base = '';
  let data = null;
  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  const UNDATED = 'undated';
  // people chosen to narrow the photos to: each one chosen narrows them further, as in the library
  const chosen = new Set();
  let shown = [];
  let current = -1;
  // whether the viewer was opened from a tile, so closing it can go back instead of adding a step
  let openedFromList = false;

  const element = id => document.getElementById(id);
  const escape = text => String(text).replace(/[&<>"']/g, character => `&#${character.charCodeAt(0)};`);
  // a file's path as an address, each part on its own, so a name with a space or # still works
  const address = path => path.split('/').map(encodeURIComponent).join('/');
  const thumbUrl = photo => `${base}site/thumbs/${photo.key}.jpg`;
  const largeUrl = photo => `${base}site/large/${photo.key}.jpg`;
  const monthKey = photo => photo.date ? photo.date.slice(0, 7) : UNDATED;
  const monthLabel = key => key === UNDATED ? 'Undated' : `${MONTHS[Number(key.slice(5, 7)) - 1]} ${key.slice(0, 4)}`;
  const count = (number, word) => `${number.toLocaleString()} ${word}${number === 1 ? '' : 's'}`;

  function names(photo) {
    return photo.people.map(number => data.people[number].name);
  }

  function summary() {
    const years = data.photos.filter(photo => photo.date).map(photo => photo.date.slice(0, 4));
    const first = years.length ? years[years.length - 1] : null;
    const last = years.length ? years[0] : null;
    const span = !first ? '' : first === last ? ` from ${first}` : ` from ${first} to ${last}`;
    return `${count(data.photos.length, 'photo')}${span}. Exported ${data.made}.`;
  }

  function renderPeople() {
    const people = element('people');
    // when every photo has everyone, such as with one person, there's nothing to narrow to
    people.hidden = data.people.every(person => person.photos === data.photos.length);
    people.innerHTML = data.people.map((person, number) => `
      <button type="button" class="person" data-person="${number}" aria-pressed="${chosen.has(number)}">
        ${person.face ? `<img src="${escape(base + person.face)}" alt="">` : '<span class="no-face" aria-hidden="true"></span>'}
        <span class="person-name">${escape(person.name)}</span>
        <span class="person-count">${person.photos.toLocaleString()}</span>
      </button>`).join('');
  }

  function renderList() {
    shown = data.photos.filter(photo => [...chosen].every(number => photo.people.includes(number)));
    const months = [];
    shown.forEach((photo, index) => {
      const key = monthKey(photo);
      if (!months.length || months[months.length - 1].key !== key) {
        months.push({key, photos: []});
      }
      months[months.length - 1].photos.push({photo, index});
    });
    element('months').innerHTML = months.map(month => `
      <section class="month" id="month-${month.key}" data-year="${month.key === UNDATED ? UNDATED : month.key.slice(0, 4)}">
        <h2>${monthLabel(month.key)} <span class="month-count">${month.photos.length.toLocaleString()}</span></h2>
        <div class="grid">${month.photos.map(({photo, index}) => `
          <a class="tile" href="#photo=${photo.key}" data-index="${index}" title="${escape(photo.when || 'Undated')}">
            <img src="${thumbUrl(photo)}" loading="lazy" decoding="async" alt="${escape(names(photo).join(', '))}">
          </a>`).join('')}
        </div>
      </section>`).join('');
    const years = [...new Set(months.map(month => month.key === UNDATED ? UNDATED : month.key.slice(0, 4)))];
    element('years').innerHTML = years.map(year => `<button type="button" data-year="${year}">${year === UNDATED ? 'Undated' : year}</button>`).join('');
    element('years').hidden = years.length < 2;
    element('empty').hidden = shown.length > 0;
    watchYears();
  }

  // the year being looked at is picked out in the row of years: that of the last month whose
  // heading has reached the row
  let yearPending = false;
  function markYear() {
    yearPending = false;
    const row = element('years');
    const below = row.getBoundingClientRect().bottom + 4;
    let year = null;
    for (const section of document.querySelectorAll('.month')) {
      if (section.getBoundingClientRect().top > below) {
        break;
      }
      year = section.dataset.year;
    }
    year = year || (document.querySelector('.month') || {dataset: {}}).dataset.year;
    row.querySelectorAll('button').forEach(button => {
      const on = button.dataset.year === year;
      if (on && !button.classList.contains('current') && row.scrollWidth > row.clientWidth) {
        // on a phone the years run off the side, so the one being looked at is kept in the middle
        row.scrollLeft = button.offsetLeft - row.clientWidth / 2 + button.offsetWidth / 2;
      }
      button.classList.toggle('current', on);
    });
  }

  function watchYears() {
    if (!yearPending) {
      yearPending = true;
      requestAnimationFrame(markYear);
    }
  }

  function step(by) {
    if (current < 0 || !shown.length) {
      return;
    }
    show((current + by + shown.length) % shown.length);
    history.replaceState(null, '', `#photo=${shown[current].key}`);
  }

  function show(index) {
    current = index;
    const photo = shown[index];
    const image = element('viewer-image');
    // the thumbnail is already loaded, so something shows straight away
    image.src = thumbUrl(photo);
    image.alt = names(photo).join(', ');
    const large = new Image();
    large.onload = () => {
      if (shown[current] === photo) {
        image.src = large.src;
      }
    };
    large.src = largeUrl(photo);
    // and the ones either side, for moving on
    [index - 1, index + 1].forEach(other => {
      if (shown[other]) {
        new Image().src = largeUrl(shown[other]);
      }
    });
    element('viewer-when').textContent = photo.when || 'Undated';
    element('viewer-detail').textContent = [names(photo).join(', '), photo.place].filter(Boolean).join(' · ');
    const download = element('viewer-download');
    download.href = base + address(photo.file);
    download.setAttribute('download', photo.file.split('/').pop());
    const viewer = element('viewer');
    viewer.hidden = false;
    document.body.classList.add('viewing');
    viewer.querySelector('.viewer-step.prev').hidden = shown.length < 2;
    viewer.querySelector('.viewer-step.next').hidden = shown.length < 2;
  }

  function hide() {
    const viewer = element('viewer');
    if (viewer.hidden) {
      return;
    }
    viewer.hidden = true;
    document.body.classList.remove('viewing');
    element('viewer-image').removeAttribute('src');
    // so the list is where the viewer got to
    const tile = document.querySelector(`.tile[data-index="${current}"]`);
    if (tile) {
      const box = tile.getBoundingClientRect();
      if (box.top < 0 || box.bottom > window.innerHeight) {
        tile.scrollIntoView({block: 'center'});
      }
    }
    current = -1;
  }

  function close() {
    if (openedFromList) {
      openedFromList = false;
      history.back();
    } else {
      history.replaceState(null, '', location.pathname + location.search);
      hide();
    }
  }

  // the address names the photo open, so a link to it opens it
  function followAddress() {
    const match = location.hash.match(/^#photo=([0-9a-f]+)$/);
    if (!match) {
      openedFromList = false;
      hide();
      return;
    }
    let index = shown.findIndex(photo => photo.key === match[1]);
    if (index < 0 && chosen.size && data.photos.some(photo => photo.key === match[1])) {
      // a photo the people chosen leave out
      chosen.clear();
      renderPeople();
      renderList();
      index = shown.findIndex(photo => photo.key === match[1]);
    }
    if (index < 0) {
      hide();
      return;
    }
    show(index);
  }

  // every photo, and the site, as one zip, once the host says it's there and how big it is.  A copy
  // unzipped from it doesn't have it, and one opened from a disk can't ask
  function offerZip() {
    if (!data.zip) {
      return;
    }
    const link = element('download-all');
    link.href = base + encodeURIComponent(data.zip);
    link.setAttribute('download', data.zip);
    fetch(link.href, {method: 'HEAD'}).then(response => {
      if (!response.ok) {
        return;
      }
      const bytes = Number(response.headers.get('Content-Length'));
      if (bytes) {
        const size = bytes < 1e9 ? `${Math.max(1, Math.round(bytes / 1e6))} MB` : `${(bytes / 1e9).toFixed(1)} GB`;
        element('download-all-label').textContent = `Download all (zip, ${size})`;
      }
      link.hidden = false;
    }).catch(() => {});
  }

  function setUp() {
    shown = data.photos;
    element('album').hidden = false;
    element('summary').textContent = summary();
    offerZip();
    renderPeople();
    renderList();

    element('people').addEventListener('click', event => {
      const button = event.target.closest('.person');
      if (!button) {
        return;
      }
      const number = Number(button.dataset.person);
      chosen.has(number) ? chosen.delete(number) : chosen.add(number);
      renderPeople();
      renderList();
    });
    element('show-all').addEventListener('click', () => {
      chosen.clear();
      renderPeople();
      renderList();
    });
    element('years').addEventListener('click', event => {
      const button = event.target.closest('button');
      const section = button && document.querySelector(`.month[data-year="${button.dataset.year}"]`);
      if (section) {
        section.scrollIntoView();
      }
    });
    element('months').addEventListener('click', event => {
      if (event.target.closest('.tile') && !event.metaKey && !event.ctrlKey && !event.shiftKey) {
        openedFromList = true;
      }
    });
    element('viewer-close').addEventListener('click', close);
    element('viewer-prev').addEventListener('click', () => step(-1));
    element('viewer-next').addEventListener('click', () => step(1));
    element('viewer-image').addEventListener('click', event => {
      // a tap on the left or right of the photo moves along
      const box = event.currentTarget.getBoundingClientRect();
      step(event.clientX < box.left + box.width / 2 ? -1 : 1);
    });
    document.addEventListener('keydown', event => {
      if (element('viewer').hidden || event.metaKey || event.ctrlKey || event.altKey) {
        return;
      }
      if (event.key === 'ArrowLeft') {
        step(-1);
      } else if (event.key === 'ArrowRight') {
        step(1);
      } else if (event.key === 'Escape') {
        close();
      } else {
        return;
      }
      event.preventDefault();
    });
    let touch = null;
    element('viewer').addEventListener('touchstart', event => {
      touch = event.touches.length === 1 ? {x: event.touches[0].clientX, y: event.touches[0].clientY} : null;
    }, {passive: true});
    element('viewer').addEventListener('touchend', event => {
      if (!touch) {
        return;
      }
      const dx = event.changedTouches[0].clientX - touch.x;
      const dy = event.changedTouches[0].clientY - touch.y;
      touch = null;
      if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) {
        step(dx < 0 ? 1 : -1);
      } else if (dy > 80 && Math.abs(dy) > Math.abs(dx)) {
        // pulled down, as a phone's own photos close
        close();
      }
    });
    window.addEventListener('scroll', watchYears, {passive: true});
    window.addEventListener('resize', watchYears);
    window.addEventListener('hashchange', followAddress);
    followAddress();
  }

  // data.js from a folder, or false if there's nothing there, as for a wrong password
  function load(folder) {
    return new Promise(resolve => {
      window.EXPORT = undefined;
      const script = document.createElement('script');
      script.src = `${folder}site/data.js?v=${site.version}`;
      script.onload = () => resolve(Boolean(window.EXPORT));
      script.onerror = () => {
        script.remove();
        resolve(false);
      };
      document.head.append(script);
    });
  }

  function open(folder) {
    base = folder;
    data = window.EXPORT;
    setUp();
  }

  // the folder a password opened is remembered in this browser, so it's only asked for once
  const REMEMBERED = site.lock && `exportFolder:${site.lock.salt}`;
  function remembered() {
    try {
      return localStorage.getItem(REMEMBERED);
    } catch (error) {
      return null;
    }
  }

  function remember(folder) {
    try {
      folder ? localStorage.setItem(REMEMBERED, folder) : localStorage.removeItem(REMEMBERED);
    } catch (error) {
      // a private window: asked for again next time
    }
  }

  function askForPassword() {
    const form = element('lock');
    const input = element('lock-password');
    const error = element('lock-error');
    form.hidden = false;
    input.focus();
    form.addEventListener('submit', async event => {
      event.preventDefault();
      error.textContent = 'Opening…';
      error.classList.remove('wrong');
      // a moment for that to show, as working out the folder holds up the page
      await new Promise(resolve => setTimeout(resolve, 30));
      const folder = `${window.exportFolder(input.value, site.lock.salt, site.lock.iterations)}/`;
      if (await load(folder)) {
        remember(folder);
        form.hidden = true;
        open(folder);
      } else {
        error.textContent = "That isn't the password.";
        error.classList.add('wrong');
        input.select();
      }
    });
  }

  async function start() {
    if (!site.lock) {
      if (await load('')) {
        open('');
      }
      return;
    }
    const lockAgain = element('lock-again');
    lockAgain.hidden = false;
    lockAgain.addEventListener('click', () => {
      remember(null);
      location.replace(location.pathname + location.search);
    });
    const folder = remembered();
    if (folder && await load(folder)) {
      open(folder);
      return;
    }
    remember(null);
    askForPassword();
  }

  start();
})();
