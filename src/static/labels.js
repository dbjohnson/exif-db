// changing a named place, such as Home, from its page.  Renaming or deleting one leaves the page it's
// done on, so its undo waits in session storage for the page that's arrived at
const LABELS_UNDO = 'labels-undo';

function leaveWithLabelUndo(url, message, previous, returnTo) {
  sessionStorage.setItem(LABELS_UNDO, JSON.stringify({message, previous, returnTo}));
  window.location.href = url;
}

function toggleLabelEdit() {
  const panel = document.getElementById('label-edit');
  panel.hidden = !panel.hidden;
}

async function changePlaceLabel(name) {
  const newName = document.getElementById('label-new-name').value.trim();
  const radius = Number(document.getElementById('label-radius').value);
  if (!newName) {
    return;
  }
  try {
    const {name: saved, url, previous} = await postJson('/api/place-labels/change', {name, new_name: newName, radius});
    leaveWithLabelUndo(url, saved === name ? `Changed ${name}` : `Renamed ${name} to ${saved}`, previous, window.location.href);
  } catch (error) {
    showToast(`Couldn't save: ${error.message}`);
  }
}

async function deletePlaceLabel(button, name) {
  if (!pressedTwice(button, 'Delete name?')) {
    return;
  }
  try {
    const {previous} = await postJson('/api/place-labels/delete', {name});
    leaveWithLabelUndo('/places', `${name} isn't a named place any more`, previous, window.location.href);
  } catch (error) {
    showToast(`Couldn't delete: ${error.message}`);
  }
}

window.addEventListener('DOMContentLoaded', () => {
  const pending = sessionStorage.getItem(LABELS_UNDO);
  if (!pending) {
    return;
  }
  sessionStorage.removeItem(LABELS_UNDO);
  const {message, previous, returnTo} = JSON.parse(pending);
  showToast(message, async () => {
    try {
      await postJson('/api/place-labels/restore', {previous});
      window.location.href = returnTo;
    } catch (error) {
      showToast(`Couldn't undo: ${error.message}`);
    }
  });
});
