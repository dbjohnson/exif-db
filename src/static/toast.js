// a message that doesn't stop the page, optionally with a button, such as to undo something
const TOAST_MS = 7000;
let toastTimeout;

function hideToast() {
  clearTimeout(toastTimeout);
  document.getElementById('toast')?.remove();
}

function showToast(message, action, actionLabel = 'Undo') {
  hideToast();
  const toast = document.createElement('div');
  toast.id = 'toast';
  toast.className = 'toast';
  toast.setAttribute('role', 'status');
  const text = document.createElement('span');
  text.textContent = message;
  toast.append(text);
  if (action) {
    const button = document.createElement('button');
    button.textContent = actionLabel;
    button.onclick = () => {
      hideToast();
      action();
    };
    toast.append(button);
  }
  document.body.append(toast);
  toastTimeout = setTimeout(hideToast, TOAST_MS);
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    throw new Error('Network response was not ok. Status: ' + response.status);
  }
  return response.json();
}
