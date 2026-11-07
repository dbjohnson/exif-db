// making tags from the library, and changing one from its folder.  Renaming or deleting a tag leaves
// the page it's done on, so its undo waits in session storage for the page that's arrived at
const TAGS_UNDO = 'tags-undo';

const tagUrl = name => `/library/tags/${encodeURIComponent(name)}`;

function leaveWithTagUndo(url, message, previous, returnTo) {
  sessionStorage.setItem(TAGS_UNDO, JSON.stringify({message, previous, returnTo}));
  window.location.href = url;
}

function toggleTagEdit() {
  const panel = document.getElementById('tag-edit');
  panel.hidden = !panel.hidden;
}

async function renameTag(name) {
  const typed = document.getElementById('tag-new-name').value.trim();
  if (!typed || typed === name) {
    return;
  }
  try {
    const known = [...document.querySelectorAll('#tags option')].map(option => option.value);
    const {name: saved, previous} = await postJson('/api/tags/rename', {name, new_name: typed});
    leaveWithTagUndo(
      tagUrl(saved),
      known.includes(saved) ? `Joined ${name} with ${saved}` : `Renamed ${name} to ${saved}`,
      previous,
      tagUrl(name)
    );
  } catch (error) {
    showToast(`Couldn't rename: ${error.message}`);
  }
}

async function deleteTag(button, name) {
  if (!pressedTwice(button, 'Delete tag?')) {
    return;
  }
  try {
    const {previous} = await postJson('/api/tags/delete', {name});
    leaveWithTagUndo('/library/folders', `Deleted the tag ${name}`, previous, tagUrl(name));
  } catch (error) {
    showToast(`Couldn't delete: ${error.message}`);
  }
}

async function createTag(event) {
  event.preventDefault();
  const input = document.getElementById('new-tag');
  if (!input.value.trim()) {
    return;
  }
  try {
    const {name} = await postJson('/api/tags', {name: input.value});
    window.location.href = tagUrl(name);
  } catch (error) {
    showToast(`Couldn't make the tag: ${error.message}`);
  }
}

window.addEventListener('DOMContentLoaded', () => {
  const pending = sessionStorage.getItem(TAGS_UNDO);
  if (!pending) {
    return;
  }
  sessionStorage.removeItem(TAGS_UNDO);
  const {message, previous, returnTo} = JSON.parse(pending);
  showToast(message, async () => {
    try {
      await postJson('/api/tags/restore', {previous});
      window.location.href = returnTo;
    } catch (error) {
      showToast(`Couldn't undo: ${error.message}`);
    }
  });
});
