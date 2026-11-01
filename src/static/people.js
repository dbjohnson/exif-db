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
