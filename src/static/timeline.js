// A long list of months, newest first, for photos or movies.  Every month takes its full height from
// the start, worked out from how many it has, and fills in when it comes near the screen.  So going
// to a date is only scrolling there, the scrollbar covers everything, and scrolling either way from
// anywhere carries on as usual.
//
// months are {key, label, year, count, days}: label null for a list with no heading, and year null
// for one with no year above it, such as Undated.  load(month) gives a month's items, and
// render(month, items) the element showing them.  monthActions(month, heading) and
// yearActions(year, heading) can add buttons to headings
class Timeline {
  // aspect is a tile's height for its width.  headings says whether years and months have them:
  // without, each day's heading says its year instead, and a month only has one if it has a label
  constructor(container, months, {load, render, tileWidth = 140, aspect = 1, gap = 4, dayHeight = 44, headings = true, monthActions, yearActions}) {
    this.container = container;
    this.load = load;
    this.render = render;
    this.tileWidth = tileWidth;
    this.aspect = aspect;
    this.gap = gap;
    this.dayHeight = dayHeight;
    this.headings = headings;
    this.monthActions = monthActions;
    this.yearActions = yearActions;
    this.entries = new Map();
    this.years = new Map();
    this.width = 0;
    // the browser's own way of keeping the view still would fight this one, which works everywhere
    document.documentElement.style.overflowAnchor = 'none';
    this.observer = new IntersectionObserver(seen => {
      for (const entry of seen) {
        if (entry.isIntersecting) {
          this.ensure(entry.target.dataset.key);
        }
      }
    }, {rootMargin: '1500px 0px'});
    this.setMonths(months);
    let resizing = null;
    window.addEventListener('resize', () => {
      clearTimeout(resizing);
      resizing = setTimeout(() => this.resized(), 200);
    });
  }

  // how tall a month is likely to be before it's loaded
  estimate(month) {
    const width = this.container.clientWidth || window.innerWidth;
    const columns = Math.max(1, Math.floor((width + this.gap) / (this.tileWidth + this.gap)));
    const size = (width - this.gap * (columns - 1)) / columns;
    // each day starts a row of its own, which on average leaves half a row empty
    const days = Math.max(month.days || 1, 1);
    const rows = Math.ceil(month.count / columns + days / 2);
    return rows * (size * this.aspect + this.gap) + days * this.dayHeight;
  }

  yearHeading(year) {
    const heading = document.createElement('h2');
    heading.className = 'timeline-year';
    const label = document.createElement('span');
    label.textContent = year;
    const count = document.createElement('span');
    count.className = 'timeline-count';
    heading.append(label, count);
    this.yearActions?.(year, heading);
    this.years.set(year, {heading, count});
    return heading;
  }

  section(month) {
    const section = document.createElement('section');
    section.className = 'timeline-month';
    section.dataset.key = month.key;
    const body = document.createElement('div');
    body.className = 'timeline-body';
    body.style.height = `${this.estimate(month)}px`;
    let count = null;
    if (month.label && (this.headings || month.key === 'undated')) {
      const heading = document.createElement('h3');
      heading.className = 'timeline-month-heading';
      const label = document.createElement('span');
      label.textContent = month.label;
      count = document.createElement('span');
      count.className = 'timeline-count';
      heading.append(label, count);
      this.monthActions?.(month, heading);
      section.append(heading);
    }
    section.append(body);
    return {month, section, body, count, state: 'empty', loading: null};
  }

  // lays out months, keeping those already there with what they've loaded: a change of date can
  // bring a month a list didn't have
  setMonths(months) {
    this.months = months;
    const keys = new Set(months.map(month => month.key));
    for (const [key, entry] of this.entries) {
      if (!keys.has(key)) {
        this.observer.unobserve(entry.section);
        entry.section.remove();
        this.entries.delete(key);
      }
    }
    let previous = null;
    for (const month of months) {
      let entry = this.entries.get(month.key);
      if (entry) {
        entry.month = month;
        if (entry.state === 'empty') {
          entry.body.style.height = `${this.estimate(month)}px`;
        }
      } else {
        entry = this.section(month);
        this.entries.set(month.key, entry);
        this.observer.observe(entry.section);
      }
      // a year's heading goes before its first month
      const year = month.year;
      const startsYear = this.headings && year && (!previous || previous.year !== year);
      const before = previous ? this.entries.get(previous.key).section.nextSibling : this.container.firstChild;
      if (startsYear) {
        const heading = this.years.get(year)?.heading || this.yearHeading(year);
        if (heading !== before) {
          this.container.insertBefore(heading, before);
        }
        if (heading.nextSibling !== entry.section) {
          this.container.insertBefore(entry.section, heading.nextSibling);
        }
      } else if (entry.section !== before) {
        this.container.insertBefore(entry.section, before);
      }
      previous = month;
    }
    this.counted();
  }

  // how many each month and year has, given how many are left in each loaded month
  counted(left = () => null) {
    const years = new Map();
    for (const month of this.months) {
      const entry = this.entries.get(month.key);
      const count = left(month.key) ?? month.count;
      if (entry.count) {
        entry.count.textContent = count.toLocaleString();
      }
      entry.section.hidden = count === 0 && entry.state === 'loaded';
      if (month.year) {
        years.set(month.year, (years.get(month.year) || 0) + count);
      }
    }
    for (const [year, {heading, count}] of this.years) {
      const total = years.get(year) || 0;
      count.textContent = total.toLocaleString();
      heading.hidden = !total;
    }
  }

  // where the sticky header ends, which is the top of what can be seen
  top() {
    const header = document.querySelector('.app-header');
    return header ? header.getBoundingClientRect().bottom : 0;
  }

  ensure(key) {
    const entry = this.entries.get(key);
    if (!entry) {
      return Promise.resolve();
    }
    if (entry.state !== 'empty') {
      return entry.loading || Promise.resolve();
    }
    entry.state = 'loading';
    entry.loading = (async () => {
      try {
        this.fill(entry, await this.load(entry.month));
      } catch (error) {
        console.log(error);
        // tried again the next time it comes into view
        entry.state = 'empty';
        entry.loading = null;
      }
    })();
    return entry.loading;
  }

  fill(entry, items) {
    // what's being looked at stays where it is: the month at the top, unless it's this one, whose
    // photos are what's arriving
    const showing = this.showing();
    const anchor = showing && showing.key !== entry.month.key ? this.entries.get(showing.key).section : null;
    const before = anchor?.getBoundingClientRect().top;
    entry.body.replaceChildren(this.render(entry.month, items));
    entry.body.style.height = '';
    entry.state = 'loaded';
    if (anchor) {
      const moved = anchor.getBoundingClientRect().top - before;
      if (moved) {
        window.scrollBy(0, moved);
      }
    }
  }

  // loads a month again, such as after photos' dates have changed
  async reload(key) {
    const entry = this.entries.get(key);
    if (!entry || entry.state === 'empty') {
      return;
    }
    await entry.loading;
    entry.state = 'empty';
    entry.body.style.height = `${entry.body.getBoundingClientRect().height}px`;
    await this.ensure(key);
  }

  scrollTo(key) {
    const entry = this.entries.get(key);
    if (!entry) {
      return;
    }
    // a year's first month starts with the year's heading
    const heading = entry.section.previousElementSibling;
    const target = heading?.classList.contains('timeline-year') ? heading : entry.section;
    window.scrollTo(0, window.scrollY + target.getBoundingClientRect().top - this.top());
    this.ensure(key);
  }

  // the month at the top of what can be seen, not counting the last few pixels of the one before
  showing() {
    const top = this.top() + 8;
    for (const month of this.months) {
      const entry = this.entries.get(month.key);
      if (!entry.section.hidden && entry.section.getBoundingClientRect().bottom > top) {
        return month;
      }
    }
    return this.months[this.months.length - 1] || null;
  }

  resized() {
    const width = this.container.clientWidth;
    if (width === this.width) {
      return;
    }
    this.width = width;
    const showing = this.showing();
    const offset = showing ? this.entries.get(showing.key).section.getBoundingClientRect().top : 0;
    for (const entry of this.entries.values()) {
      if (entry.state === 'empty') {
        entry.body.style.height = `${this.estimate(entry.month)}px`;
      }
    }
    // the same month stays where it was
    if (showing) {
      window.scrollBy(0, this.entries.get(showing.key).section.getBoundingClientRect().top - offset);
    }
  }
}

// going to a year and month, from two lists: the years there are, and their months
function dateJump(years, months, timeline, chosen) {
  const yearSelect = document.getElementById(years);
  const monthSelect = document.getElementById(months);
  // short, so that the date fits in a phone's header beside the buttons
  const NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  let present = new Map();
  let hasUndated = false;
  let filledYear = null;

  function fill() {
    present = datedYears(timeline);
    hasUndated = timeline.months.some(month => month.key === 'undated');
    yearSelect.replaceChildren(...[...present.keys()].map(year => {
      const option = document.createElement('option');
      option.value = year;
      option.textContent = year;
      return option;
    }));
    filledYear = null;
  }

  // a year's months, newest first, and then Undated, which isn't in any year
  function fillMonths(year) {
    filledYear = year;
    const has = new Set((present.get(year) || []).map(month => month.key));
    const options = NAMES.map((name, index) => {
      const key = `${year}-${String(index + 1).padStart(2, '0')}`;
      const option = document.createElement('option');
      option.value = key;
      option.textContent = name;
      option.disabled = !has.has(key);
      return option;
    }).reverse();
    if (hasUndated) {
      const undated = document.createElement('option');
      undated.value = 'undated';
      undated.textContent = 'Undated';
      options.push(undated);
    }
    monthSelect.replaceChildren(...options);
  }

  // follows along with scrolling
  function show(month) {
    if (!month) {
      return;
    }
    if (month.key === 'undated') {
      if (!filledYear) {
        fillMonths(yearSelect.value);
      }
      monthSelect.value = 'undated';
      return;
    }
    if (!month.year) {
      return;
    }
    if (yearSelect.value !== month.year || filledYear !== month.year) {
      yearSelect.value = month.year;
      fillMonths(month.year);
    }
    monthSelect.value = month.key;
  }

  yearSelect.addEventListener('change', () => {
    const year = yearSelect.value;
    fillMonths(year);
    // the newest month of the year
    const first = present.get(year)?.[0];
    if (first) {
      chosen(first.key);
    }
  });
  monthSelect.addEventListener('change', () => chosen(monthSelect.value));
  fill();
  return {
    show,
    refill: fill
  };
}

// the dated months, by year, newest first
function datedYears(timeline) {
  const years = new Map();
  for (const month of timeline.months) {
    if (month.key === 'undated' || !month.year) {
      continue;
    }
    if (!years.has(month.year)) {
      years.set(month.year, []);
    }
    years.get(month.year).push(month);
  }
  return years;
}

// ---- narrowing a list, which loads the page again with the change in its address

function filterTo(changes) {
  const url = new URL(window.location.href);
  for (const [key, value] of Object.entries(changes)) {
    if (value) {
      url.searchParams.set(key, value);
    } else {
      url.searchParams.delete(key);
    }
  }
  window.location.href = url;
}

// a list's filters as a query, where tags and people can each be more than one
function filterParams(filters) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    for (const each of Array.isArray(value) ? value : [value]) {
      params.append(key, each);
    }
  }
  return params;
}

// adds a tag or person to what a list is narrowed to, or takes one off
function changeFilter(kind, name, add) {
  const url = new URL(window.location.href);
  const values = url.searchParams.getAll(kind).filter(value => value !== name);
  if (add) {
    values.push(name);
  }
  url.searchParams.delete(kind);
  values.forEach(value => url.searchParams.append(kind, value));
  window.location.href = url;
}

// the × on a tag or person, and the lists for adding one, wherever they are
document.addEventListener('click', event => {
  const remove = event.target.closest?.('.filter-remove');
  if (remove) {
    changeFilter(remove.dataset.kind, remove.dataset.name, false);
  }
});
document.addEventListener('change', event => {
  if (event.target.matches?.('select[data-filter-add]') && event.target.value) {
    const [kind, ...rest] = event.target.value.split(':');
    changeFilter(kind, rest.join(':'), true);
  }
});

function setPlace(place) {
  filterTo({place_kind: place?.kind, place_key: place?.key});
}

// places to choose from as their names are typed: named places, towns, states, countries and folders
function placeFilter(input) {
  const list = document.createElement('datalist');
  list.id = `${input.id}-places`;
  input.setAttribute('list', list.id);
  input.after(list);
  const found = new Map();
  let waiting = null;
  const suggest = async () => {
    try {
      const response = await fetch(`/api/places?${new URLSearchParams({q: input.value})}`);
      const places = response.ok ? await response.json() : [];
      found.clear();
      list.replaceChildren(...places.slice(0, 30).map(place => {
        const label = place.kind === 'folder' ? `${place.label} (folder)` : place.detail ? `${place.label}, ${place.detail}` : place.label;
        found.set(label, place);
        const option = document.createElement('option');
        option.value = label;
        return option;
      }));
    } catch (error) {
      console.log(error);
    }
  };
  input.addEventListener('input', () => {
    const chosen = found.get(input.value);
    if (chosen) {
      setPlace(chosen);
      return;
    }
    clearTimeout(waiting);
    waiting = setTimeout(suggest, 200);
  });
  input.addEventListener('focus', () => {
    if (!input.value) {
      suggest();
    }
  });
}

// ---- the sidebar on a wide screen

// years down the side, and the months of the one showing, for going to a date
function sideDates(yearsId, monthsId, timeline, chosen) {
  const yearsList = document.getElementById(yearsId);
  const monthsGrid = document.getElementById(monthsId);
  const NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  // Undated isn't in any year, so it goes under the months
  const undated = document.createElement('button');
  undated.type = 'button';
  undated.className = 'side-undated';
  undated.textContent = 'Undated';
  undated.onclick = () => chosen('undated');
  monthsGrid.after(undated);
  let present = new Map();
  let shownYear = null;

  function fill() {
    present = datedYears(timeline);
    const undatedMonth = timeline.months.find(month => month.key === 'undated');
    undated.hidden = !undatedMonth;
    if (undatedMonth) {
      undated.title = `${undatedMonth.count.toLocaleString()} with no usable date`;
    }
    yearsList.replaceChildren(...[...present].map(([year, months]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.year = year;
      button.textContent = year;
      button.title = `${months.reduce((sum, month) => sum + month.count, 0).toLocaleString()} in ${year}`;
      // the newest month of the year
      button.onclick = () => chosen(months[0].key);
      return button;
    }));
    shownYear = null;
  }

  function fillMonths(year) {
    shownYear = year;
    const has = new Set((present.get(year) || []).map(month => month.key));
    monthsGrid.replaceChildren(...NAMES.map((name, index) => {
      const key = `${year}-${String(index + 1).padStart(2, '0')}`;
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.key = key;
      button.textContent = name;
      button.disabled = !has.has(key);
      button.onclick = () => chosen(key);
      return button;
    }));
  }

  function show(month) {
    if (!month) {
      return;
    }
    undated.toggleAttribute('aria-current', month.key === 'undated');
    if (month.key === 'undated') {
      for (const button of [...yearsList.children, ...monthsGrid.children]) {
        button.removeAttribute('aria-current');
      }
      return;
    }
    const year = month.year;
    if (!year) {
      return;
    }
    if (year !== shownYear) {
      fillMonths(year);
      for (const button of yearsList.children) {
        const current = button.dataset.year === year;
        button.toggleAttribute('aria-current', current);
        if (current) {
          // within the sidebar, which scrolls by itself
          const side = yearsList.closest('.library-sidebar');
          const top = button.offsetTop - side.offsetTop;
          if (top < side.scrollTop || top > side.scrollTop + side.clientHeight - button.offsetHeight) {
            side.scrollTop = top - side.clientHeight / 3;
          }
        }
      }
    }
    for (const button of monthsGrid.children) {
      button.toggleAttribute('aria-current', button.dataset.key === month.key);
    }
  }

  fill();
  return {
    show,
    refill: fill
  };
}

// the sidebar's place
function sideFilters() {
  const place = document.getElementById('side-place');
  if (place) {
    placeFilter(place);
  }
}

// how tall the sticky header is, which the sidebar sticks beneath
function noteHeaderHeight() {
  const header = document.querySelector('.app-header');
  if (header) {
    document.documentElement.style.setProperty('--header-height', `${header.offsetHeight}px`);
  }
}
window.addEventListener('DOMContentLoaded', noteHeaderHeight);
window.addEventListener('resize', noteHeaderHeight);
