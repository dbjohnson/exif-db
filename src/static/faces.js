// drawing where faces are found over a photo showing at any size

// a box as a fraction of a photo, once that photo has been turned clockwise by this many degrees
function turnedBox(box, degrees) {
  const {Left: left, Top: top, Width: width, Height: height} = box;
  switch ((degrees + 360) % 360) {
    case 90:
      return {Left: 1 - top - height, Top: left, Width: height, Height: width};
    case 180:
      return {Left: 1 - left - width, Top: 1 - top - height, Width: width, Height: height};
    case 270:
      return {Left: top, Top: 1 - left - width, Width: height, Height: width};
    default:
      return box;
  }
}

// a layer over the part of an image with object-fit: contain that the photo actually fills
function coverPhoto(layer, image) {
  // a resize can arrive while the page is still loading
  if (!image?.naturalWidth || !image.clientWidth) {
    return;
  }
  const scale = Math.min(image.clientWidth / image.naturalWidth, image.clientHeight / image.naturalHeight);
  const width = image.naturalWidth * scale;
  const height = image.naturalHeight * scale;
  layer.style.left = `${image.offsetLeft + (image.clientWidth - width) / 2}px`;
  layer.style.top = `${image.offsetTop + (image.clientHeight - height) / 2}px`;
  layer.style.width = `${width}px`;
  layer.style.height = `${height}px`;
}

function makeFaceBox(box, label) {
  const element = document.createElement('button');
  element.type = 'button';
  element.className = 'face-box';
  element.style.left = `${box.Left * 100}%`;
  element.style.top = `${box.Top * 100}%`;
  element.style.width = `${box.Width * 100}%`;
  element.style.height = `${box.Height * 100}%`;
  if (label) {
    const text = document.createElement('span');
    text.className = 'face-box-label';
    // inside the photo: a face at the very top has nowhere above it for a label
    text.classList.toggle('below', box.Top < 0.08);
    text.textContent = label;
    element.append(text);
  }
  return element;
}
