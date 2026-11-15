// Zooming into the photo in a viewer.  With a mouse, the wheel zooms around the pointer, a
// double-click zooms in or back out, and dragging moves the photo.  On a touch screen, pinching
// zooms, a double tap zooms in or back out, and one finger moves the photo.  The photo is never
// smaller than it fits, nor moved further than its edges.  Once zoomed, the image shows the photo
// at full size, from fullUrl(), so there's more to see than the copy for the screen has
const ZOOM_MAX = 8;
const ZOOM_DOUBLE = 2.5;
// how close together, in time and place, two taps are to make a double tap
const DOUBLE_TAP_MS = 300;
const DOUBLE_TAP_PX = 30;

function photoZoom(viewer, image, {fullUrl, changed = () => {}}) {
  let scale = 1;
  let x = 0;
  let y = 0;
  // counts photos, so that a full size copy arriving late isn't put on the next one
  let showing = 0;
  let fullLoaded = false;
  let fullLoading = false;
  let pinch = null;
  let pan = null;
  let lastTap = null;
  let lastTouch = 0;

  // where the photo sits in the image's box before zooming, as object-fit: contain puts it
  function fitted() {
    const room = {width: image.clientWidth, height: image.clientHeight};
    const fit = Math.min(room.width / image.naturalWidth, room.height / image.naturalHeight) || 1;
    const width = image.naturalWidth * fit;
    const height = image.naturalHeight * fit;
    return {room, width, height, left: (room.width - width) / 2, top: (room.height - height) / 2};
  }

  // a photo smaller than the screen stays in the middle, and a bigger one leaves no gap at an edge
  function keepInView() {
    const box = fitted();
    const along = (offset, size, room, start) => size * scale <= room
      ? (room - size * scale) / 2 - start * scale
      : Math.min(-start * scale, Math.max(room - (start + size) * scale, offset));
    x = along(x, box.width, box.room.width, box.left);
    y = along(y, box.height, box.room.height, box.top);
  }

  function loadFull() {
    const url = fullUrl();
    if (fullLoaded || fullLoading || !url) {
      return;
    }
    fullLoading = true;
    const photo = showing;
    const full = new Image();
    full.onload = () => {
      fullLoading = false;
      if (photo === showing) {
        fullLoaded = true;
        image.src = full.src;
      }
    };
    full.onerror = () => {
      fullLoading = false;
    };
    full.src = url;
  }

  function show(animate = false) {
    image.classList.toggle('zoom-animating', animate);
    image.style.transform = scale > 1 ? `translate(${x}px, ${y}px) scale(${scale})` : '';
    viewer.classList.toggle('zoomed', scale > 1);
    if (scale > 1) {
      loadFull();
    }
    changed(scale);
  }

  // a point on the screen as a point in the image's box, before zooming
  function inImage(clientX, clientY) {
    const box = viewer.getBoundingClientRect();
    return {x: clientX - box.left - image.offsetLeft, y: clientY - box.top - image.offsetTop};
  }

  // zooms to a scale keeping the photo under a point where it is
  function zoomAt(point, to, animate = false) {
    if (!image.naturalWidth) {
      return;
    }
    to = Math.min(ZOOM_MAX, Math.max(1, to));
    x = point.x - (point.x - x) * to / scale;
    y = point.y - (point.y - y) * to / scale;
    scale = to;
    keepInView();
    show(animate);
  }

  function toggleAt(point) {
    zoomAt(point, scale > 1 ? 1 : ZOOM_DOUBLE, true);
  }

  // on the photo itself, or a face on it, rather than the buttons around it
  const onPhoto = target => target === image || target.closest?.('#viewer-boxes');

  image.addEventListener('transitionend', () => image.classList.remove('zoom-animating'));
  // a dragged photo would otherwise be picked up, as a file to drop somewhere
  image.addEventListener('dragstart', event => event.preventDefault());

  viewer.addEventListener('wheel', event => {
    if (!onPhoto(event.target) || !image.naturalWidth) {
      return;
    }
    event.preventDefault();
    // lines or pages from a mouse wheel, pixels from a trackpad, whose pinch comes with ctrlKey
    const pixels = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 400 : 1);
    zoomAt(inImage(event.clientX, event.clientY), scale * Math.exp(-pixels * (event.ctrlKey ? 0.01 : 0.002)));
  }, {passive: false});

  viewer.addEventListener('dblclick', event => {
    // a double tap has already been dealt with, and some browsers send a dblclick for it too
    if (!onPhoto(event.target) || Date.now() - lastTouch < 800) {
      return;
    }
    toggleAt(inImage(event.clientX, event.clientY));
  });

  viewer.addEventListener('pointerdown', event => {
    if (event.pointerType !== 'mouse' || event.button !== 0 || scale <= 1 || !onPhoto(event.target)) {
      return;
    }
    event.preventDefault();
    pan = {startX: event.clientX, startY: event.clientY, x, y};
    viewer.classList.add('panning');
    // on the photo, so that a double-click after dragging is still on it
    image.setPointerCapture(event.pointerId);
  });
  viewer.addEventListener('pointermove', event => {
    if (event.pointerType !== 'mouse' || !pan) {
      return;
    }
    x = pan.x + event.clientX - pan.startX;
    y = pan.y + event.clientY - pan.startY;
    keepInView();
    show();
  });
  const endDrag = event => {
    if (event.pointerType === 'mouse' && pan) {
      pan = null;
      viewer.classList.remove('panning');
    }
  };
  viewer.addEventListener('pointerup', endDrag);
  viewer.addEventListener('pointercancel', endDrag);

  const middle = touches => ({
    x: (touches[0].clientX + touches[1].clientX) / 2,
    y: (touches[0].clientY + touches[1].clientY) / 2
  });
  const apart = touches => Math.hypot(touches[0].clientX - touches[1].clientX, touches[0].clientY - touches[1].clientY);

  function startTouchPan(touch) {
    pan = scale > 1 ? {startX: touch.clientX, startY: touch.clientY, x, y} : null;
  }

  viewer.addEventListener('touchstart', event => {
    lastTouch = Date.now();
    if (!image.naturalWidth || (!pinch && !onPhoto(event.target))) {
      return;
    }
    if (event.touches.length === 2) {
      const at = middle(event.touches);
      pinch = {distance: apart(event.touches), scale, point: inImage(at.x, at.y), x, y};
      pan = null;
      lastTap = null;
      image.classList.remove('zoom-animating');
    } else if (event.touches.length === 1) {
      startTouchPan(event.touches[0]);
    }
  }, {passive: true});

  viewer.addEventListener('touchmove', event => {
    if (pinch && event.touches.length === 2) {
      const at = middle(event.touches);
      const now = inImage(at.x, at.y);
      const to = Math.min(ZOOM_MAX, Math.max(1, pinch.scale * apart(event.touches) / pinch.distance));
      // the spot that was between the fingers stays between them, wherever they go
      x = now.x - (pinch.point.x - pinch.x) * to / pinch.scale;
      y = now.y - (pinch.point.y - pinch.y) * to / pinch.scale;
      scale = to;
      keepInView();
      show();
    } else if (pan && event.touches.length === 1) {
      x = pan.x + event.touches[0].clientX - pan.startX;
      y = pan.y + event.touches[0].clientY - pan.startY;
      keepInView();
      show();
    }
  }, {passive: true});

  viewer.addEventListener('touchend', event => {
    lastTouch = Date.now();
    if (pinch) {
      // lifting one finger of two carries on as moving the photo with the other
      if (event.touches.length === 1) {
        pinch = null;
        startTouchPan(event.touches[0]);
      } else if (!event.touches.length) {
        pinch = null;
      }
      return;
    }
    const touch = event.changedTouches[0];
    const moved = pan && Math.hypot(touch.clientX - pan.startX, touch.clientY - pan.startY) > DOUBLE_TAP_PX;
    pan = null;
    if (event.touches.length || moved || !onPhoto(event.target)) {
      lastTap = null;
      return;
    }
    const tap = {time: Date.now(), x: touch.clientX, y: touch.clientY};
    if (lastTap && tap.time - lastTap.time < DOUBLE_TAP_MS && Math.hypot(tap.x - lastTap.x, tap.y - lastTap.y) < DOUBLE_TAP_PX) {
      lastTap = null;
      toggleAt(inImage(tap.x, tap.y));
    } else {
      lastTap = tap;
    }
  }, {passive: true});

  // Safari's own pinch, which would zoom the whole page instead
  viewer.addEventListener('gesturestart', event => event.preventDefault());

  return {
    // for a new photo, or the same one turned, enhanced or shown from its raw file
    reset() {
      showing += 1;
      fullLoaded = false;
      fullLoading = false;
      pinch = null;
      pan = null;
      scale = 1;
      x = 0;
      y = 0;
      show();
    },
    // after the window changes size, when the photo fits differently
    refit() {
      if (scale > 1) {
        keepInView();
        show();
      }
    },
    zoomed: () => scale > 1,
    showingFull: () => fullLoaded,
    zoomBy(factor) {
      const box = fitted();
      zoomAt({x: box.room.width / 2, y: box.room.height / 2}, scale * factor, true);
    },
    fit() {
      zoomAt({x: 0, y: 0}, 1, true);
    }
  };
}
