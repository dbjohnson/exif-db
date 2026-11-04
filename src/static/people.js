// changing a person, from either of their pages.  Both changes leave the page they're made on, so
// their undo waits in session storage for the page they arrive at
const PEOPLE_UNDO = 'people-undo';

function togglePersonEdit() {
  const panel = document.getElementById('person-edit');
  panel.hidden = !panel.hidden;
}

function leaveWithUndo(url, message, previous, returnTo) {
  sessionStorage.setItem(PEOPLE_UNDO, JSON.stringify({message, previous, returnTo}));
  window.location.href = url;
}

async function renamePerson(name) {
  const typed = document.getElementById('person-new-name').value;
  if (!typed.trim() || typed.trim() === name) {
    return;
  }
  try {
    const known = [...document.querySelectorAll('#names option')].map(option => option.value);
    const {name: saved, previous} = await postJson('/api/people/rename', {name, new_name: typed});
    leaveWithUndo(
      `/people/${encodeURIComponent(saved)}`,
      known.includes(saved) ? `Joined ${name} with ${saved}` : `Renamed ${name} to ${saved}`,
      previous,
      `/people/${encodeURIComponent(name)}`
    );
  } catch (error) {
    showToast(`Couldn't rename: ${error.message}`);
  }
}

async function removePerson(button, name) {
  if (!pressedTwice(button, 'Remove?')) {
    return;
  }
  try {
    const {previous} = await postJson('/api/people/remove', {name});
    leaveWithUndo('/people', `Removed ${name}`, previous, `/people/${encodeURIComponent(name)}`);
  } catch (error) {
    showToast(`Couldn't remove: ${error.message}`);
  }
}

// marking someone as family, or not, from their Edit panel or from People
async function setFamily(name, family) {
  await postJson('/api/people/family', {name, family});
}

async function toggleFamily(button, name) {
  const family = button.getAttribute('aria-pressed') !== 'true';
  try {
    await setFamily(name, family);
    button.setAttribute('aria-pressed', family ? 'true' : 'false');
    showToast(family ? `${name} is family` : `${name} isn't family`);
  } catch (error) {
    showToast(`Couldn't save: ${error.message}`);
  }
}

// someone's mom and dad, from their Edit panel, and the relatives that follow from everyone's
const RELATIVE_HEADINGS = {children: 'Children', siblings: 'Brothers and sisters', partners: 'Has children with'};
const PARENT_SLOTS = ['mom', 'dad'];

function showFamily(family) {
  PARENT_SLOTS.forEach(slot => {
    document.getElementById(`person-${slot}`).value = family.parents[slot] || '';
  });
  const list = document.getElementById('person-relatives-list');
  list.replaceChildren();
  Object.entries(RELATIVE_HEADINGS).forEach(([kind, heading]) => {
    if (!family[kind].length) {
      return;
    }
    const row = document.createElement('div');
    row.className = 'person-relatives-row';
    const label = document.createElement('span');
    label.className = 'person-relatives-heading';
    label.textContent = heading;
    row.append(label);
    family[kind].forEach(relative => {
      const chip = document.createElement('a');
      chip.className = 'person-relative';
      chip.href = `/people/${encodeURIComponent(relative.name)}`;
      chip.textContent = relative.name;
      if (relative.through) {
        const through = document.createElement('span');
        through.className = 'person-relative-through';
        through.textContent = `through ${relative.through}`;
        chip.append(through);
      }
      row.append(chip);
    });
    list.append(row);
  });
}

async function setParent(slot, parent) {
  const person = document.getElementById('person-relatives').dataset.person;
  const {previous, family} = await postJson('/api/people/parent', {name: person, slot, parent});
  showFamily(family);
  return previous;
}

async function parentTyped(input) {
  const person = document.getElementById('person-relatives').dataset.person;
  const slot = input.dataset.slot;
  const parent = input.value.trim() || null;
  if (parent && ![...document.querySelectorAll('#names option')].some(option => option.value === parent)) {
    showToast(`Nobody is called ${parent} yet`);
    return;
  }
  try {
    const previous = await setParent(slot, parent);
    if (previous === parent) {
      return;
    }
    showToast(parent ? `${parent} is ${person}'s ${slot}` : `Cleared ${person}'s ${slot}`, async () => {
      try {
        await setParent(slot, previous);
      } catch (error) {
        showToast(`Couldn't undo: ${error.message}`);
      }
    });
  } catch (error) {
    showToast(`Couldn't save: ${error.message}`);
  }
}

window.addEventListener('DOMContentLoaded', () => {
  const relatives = document.getElementById('person-relatives');
  if (!relatives) {
    return;
  }
  showFamily(JSON.parse(relatives.dataset.family));
  PARENT_SLOTS.forEach(slot => {
    const input = document.getElementById(`person-${slot}`);
    input.addEventListener('change', () => parentTyped(input));
  });
});

window.addEventListener('DOMContentLoaded', () => {
  const pending = sessionStorage.getItem(PEOPLE_UNDO);
  if (!pending) {
    return;
  }
  sessionStorage.removeItem(PEOPLE_UNDO);
  const {message, previous, returnTo} = JSON.parse(pending);
  showToast(message, async () => {
    try {
      await postJson('/api/people/faces/restore', {previous});
      window.location.href = returnTo;
    } catch (error) {
      showToast(`Couldn't undo: ${error.message}`);
    }
  });
});
