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

// a delete that asks with a second press rather than stopping the page with an alert
const SECOND_PRESS_MS = 4000;
const armedButtons = new Map();

function disarmButton(button) {
  const armed = armedButtons.get(button);
  if (armed) {
    clearTimeout(armed.timeout);
    button.innerHTML = armed.content;
    button.classList.remove('confirming');
    armedButtons.delete(button);
  }
}

function pressedTwice(button, prompt = 'Delete?') {
  if (armedButtons.has(button)) {
    disarmButton(button);
    return true;
  }
  armedButtons.set(button, {
    content: button.innerHTML,
    timeout: setTimeout(() => disarmButton(button), SECOND_PRESS_MS)
  });
  button.classList.add('confirming');
  button.textContent = prompt;
  return false;
}
