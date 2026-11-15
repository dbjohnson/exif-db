"""
Finds the faces in a video by looking at frames a few seconds apart.  Shared by the job that looks at
new videos and the script that looks at them all from a faster computer.  OpenCV reads the video,
whether a file or an address, and turns frames the way the video says to be shown.
"""
import math

import cv2

# up to this many frames, this far apart, spread evenly over the video.  On a sample of 200 videos
# that was 5 seconds a video on a fast computer, and takes about a minute on the NAS
MAX_FRAMES = 12
SECONDS_APART = 3
# a turn someone gave the video in the app, clockwise
TURNS = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}


def frame_times(duration):
    count = max(1, min(MAX_FRAMES, math.ceil(duration / SECONDS_APART)))
    return [(place + 0.5) * duration / count for place in range(count)]


def faces_in_video(model, source, duration, rotation=0):
    """
    The faces in a video's frames, each with 'at', how many seconds in its frame was.  Raises
    ValueError if no frame could be read
    """
    capture = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    if not capture.isOpened():
        raise ValueError(f"Can't open {source}")
    found = []
    frames = 0
    try:
        for at in frame_times(duration):
            capture.set(cv2.CAP_PROP_POS_MSEC, at * 1000)
            ok, frame = capture.read()
            if not ok:
                continue
            frames += 1
            if rotation in TURNS:
                frame = cv2.rotate(frame, TURNS[rotation])
            for face in model.faces(frame):
                face['at'] = round(at, 3)
                found.append(face)
    finally:
        capture.release()
    if not frames:
        raise ValueError(f"Can't read a frame of {source}")
    return found
