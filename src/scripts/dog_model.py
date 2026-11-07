"""
Finds dogs and cats in a photo with YOLOX, describes each with DINOv2, and works out which dogs
already named the dogs are.  Cats are only kept as examples of what dogs aren't: a black cat can
otherwise pass for a black dog.  Both models run through ONNX Runtime, like the face models, and are fetched by
fetch_dog_models.py.  Imports nothing from the api package, like people_db.
"""
import os
import re
import time
from collections import Counter

import cv2
import numpy as np
import onnxruntime

import people_db

MODELS = os.getenv('DOG_MODELS', '/opt/dog-models')
DETECTOR = 'yolox_m.onnx'
DESCRIBER = 'dinov2_small.onnx'
DETECT_SIZE = 640
# where YOLOX's list of things it knows has these
KINDS = {16: 'dog', 15: 'cat'}
DOG = 'dog'
MIN_DETECT_SCORE = 0.2
MAX_OVERLAP = 0.45
STRIDES = (8, 16, 32)
DESCRIBE_SIZE = 224
# a little room around each dog, and the size its picture is first brought down to, as when the
# dogs already named were described
CROP_MARGIN = 0.05
CROP_SIZE = 448
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
EMBEDDING_SIZE = 384
# a dog is named only when its dog's model is this sure, and only in photos from around when that dog
# was being photographed: from half a year before its first named photo to a year after its last,
# leaving out the first and last 1% in case of a wrong date.  Dogs from outside those times are what
# each dog's model learns isn't them.  Checked by eye on 2026-11-02: 60 named photos of each of four
# dogs were all right, and about 5 to 10% of those named with less than 0.97 weren't
NAME_PROBABILITY = 0.9
MIN_NAMED = 5
MONTHS_BEFORE = 6
MONTHS_AFTER = 12
DATE_PERCENTILE = 1
# a name given to faces mostly inside dogs is a dog's, as for dogs named before they were found here
DOG_NAME_SHARE = 0.5
DOG_NAME_FACES = 3
REGULARISATION = 1.0


class DogModel:
    def __init__(self, threads=2, models=MODELS):
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        providers = ['CPUExecutionProvider']
        self.detector = onnxruntime.InferenceSession(os.path.join(models, DETECTOR), options, providers=providers)
        self.describer = onnxruntime.InferenceSession(os.path.join(models, DESCRIBER), options, providers=providers)
        grids, strides = [], []
        for stride in STRIDES:
            cells = DETECT_SIZE // stride
            ys, xs = np.meshgrid(np.arange(cells), np.arange(cells), indexing='ij')
            grids.append(np.stack((xs, ys), axis=2).reshape(-1, 2))
            strides.append(np.full((cells * cells, 1), stride))
        self.grid = np.concatenate(grids).astype(np.float32)
        self.strides = np.concatenate(strides).astype(np.float32)

    def _boxes(self, image):
        # YOLOX takes the photo scaled to fit, padded with grey, as unnormalised BGR
        height, width = image.shape[:2]
        scale = min(DETECT_SIZE / height, DETECT_SIZE / width)
        padded = np.full((DETECT_SIZE, DETECT_SIZE, 3), 114, dtype=np.uint8)
        resized = cv2.resize(image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_LINEAR)
        padded[:resized.shape[0], :resized.shape[1]] = resized
        output = self.detector.run(None, {self.detector.get_inputs()[0].name: padded.transpose(2, 0, 1)[None].astype(np.float32)})[0][0]
        centres = (output[:, :2] + self.grid) * self.strides
        sizes = np.exp(output[:, 2:4]) * self.strides
        # each place is whichever thing it most looks like, as with the detector the dogs already
        # named were found with
        classes = output[:, 5:].argmax(axis=1)
        scores = output[:, 4] * output[np.arange(len(output)), 5 + classes]
        found = []
        for number, kind in KINDS.items():
            keep = (classes == number) & (scores >= MIN_DETECT_SCORE)
            corners = np.concatenate([centres[keep] - sizes[keep] / 2, sizes[keep]], axis=1) / scale
            chosen = cv2.dnn.NMSBoxes(corners.tolist(), scores[keep].tolist(), MIN_DETECT_SCORE, MAX_OVERLAP)
            for place in np.array(chosen, dtype=int).reshape(-1):
                left, top, box_width, box_height = corners[place]
                right, bottom = min(width, left + box_width), min(height, top + box_height)
                left, top = max(0.0, left), max(0.0, top)
                if right - left >= 2 and bottom - top >= 2:
                    found.append((kind, left, top, right, bottom, float(scores[keep][place])))
        return found

    def _picture(self, image, left, top, right, bottom):
        height, width = image.shape[:2]
        margin_x, margin_y = (right - left) * CROP_MARGIN, (bottom - top) * CROP_MARGIN
        crop = image[
            int(max(0, top - margin_y)):int(min(height, bottom + margin_y)),
            int(max(0, left - margin_x)):int(min(width, right + margin_x))
        ]
        shrink = min(1.0, CROP_SIZE / max(crop.shape[:2]))
        if shrink < 1:
            crop = cv2.resize(crop, None, fx=shrink, fy=shrink, interpolation=cv2.INTER_AREA)
        scale = DESCRIBE_SIZE / max(crop.shape[:2])
        crop = cv2.resize(crop, (max(1, int(crop.shape[1] * scale)), max(1, int(crop.shape[0] * scale))))
        square = np.full((DESCRIBE_SIZE, DESCRIBE_SIZE, 3), 128, dtype=np.uint8)
        y, x = (DESCRIBE_SIZE - crop.shape[0]) // 2, (DESCRIBE_SIZE - crop.shape[1]) // 2
        square[y:y + crop.shape[0], x:x + crop.shape[1]] = crop
        return ((square[:, :, ::-1].astype(np.float32) / 255 - MEAN) / STD).transpose(2, 0, 1)

    def animals(self, image):
        """
        The dogs and cats in a photo, with where each is as a fraction of the photo and 384 numbers
        describing it
        """
        height, width = image.shape[:2]
        boxes = self._boxes(image)
        if not boxes:
            return []
        pictures = np.stack([self._picture(image, *box[1:5]) for box in boxes])
        # the first of what DINOv2 gives back sums up the whole picture
        described = self.describer.run(None, {'pixel_values': pictures})[0][:, 0]
        described /= np.linalg.norm(described, axis=1, keepdims=True)
        return [
            {
                'kind': kind,
                # plain numbers, as SQLite keeps numpy's as bytes
                'left': float(left / width),
                'top': float(top / height),
                'width': float((right - left) / width),
                'height': float((bottom - top) / height),
                'score': score,
                'pixels': float(min(right - left, bottom - top)),
                'embedding': embedding.astype(np.float32)
            }
            for (kind, left, top, right, bottom, score), embedding in zip(boxes, described)
        ]


def animal_id(photo_id, animal):
    # a dog's is the id of the face it becomes; a cat's can't be the same as a dog's in the same place
    face_id = people_db.face_id(photo_id, animal)
    return face_id if animal['kind'] == DOG else f"{animal['kind']}-{face_id}"


def save_photo(connection, photo, found):
    """
    Saves the animals found in a photo, and that it's been looked at, and returns the dogs' ids
    """
    with connection:
        connection.executemany(
            """
            INSERT OR IGNORE INTO animals (id, kind, photo_id, path, rotation, left, top, width, height, score, pixels, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    animal_id(photo['id'], dog),
                    dog['kind'],
                    people_db.to_db(photo['id']),
                    people_db.to_db(photo['path']),
                    photo['rotation'],
                    dog['left'], dog['top'], dog['width'], dog['height'], dog['score'], dog['pixels'],
                    dog['embedding'].tobytes()
                )
                for dog in found
            ]
        )
        connection.execute(
            'INSERT OR REPLACE INTO animals_scanned (photo_id, animals, at) VALUES (?, ?, ?)',
            (people_db.to_db(photo['id']), len(found), int(time.time()))
        )
    return [animal_id(photo['id'], dog) for dog in found if dog['kind'] == DOG]


def _month(photo_id):
    # photos' ids end with when they were taken, where that's known; some cameras' dates are nonsense
    match = re.search(rb'\|(\d{4}):(\d{2})', photo_id)
    if match and 1980 <= int(match[1]) <= 2100 and 1 <= int(match[2]) <= 12:
        return int(match[1]) * 12 + int(match[2]) - 1
    return None


def _fit(examples, labels):
    """
    Logistic regression with the two sides weighted equally, by Newton's method: the chance each of
    a set of descriptions is of this dog comes from the returned function
    """
    rows = np.hstack([examples, np.ones((len(examples), 1), dtype=np.float32)]).astype(np.float64)
    weights = np.where(labels, len(labels) / (2 * labels.sum()), len(labels) / (2 * (~labels).sum()))
    penalty = np.full(rows.shape[1], 1 / REGULARISATION)
    penalty[-1] = 0
    coefficients = np.zeros(rows.shape[1])
    for _ in range(50):
        chances = 1 / (1 + np.exp(-(rows @ coefficients)))
        gradient = rows.T @ (weights * (chances - labels)) + penalty * coefficients
        hessian = (rows * (weights * chances * (1 - chances))[:, None]).T @ rows + np.diag(penalty + 1e-9)
        step = np.linalg.solve(hessian, gradient)
        coefficients -= step
        if np.abs(step).max() < 1e-6:
            break
    return lambda described: 1 / (1 + np.exp(-(np.hstack([described, np.ones((len(described), 1))]) @ coefficients)))


def _inside(face, dog):
    centre_x, centre_y = face[0] + face[2] / 2, face[1] + face[3] / 2
    return dog[0] <= centre_x <= dog[0] + dog[2] and dog[1] <= centre_y <= dog[1] + dog[3]


def _who_each_animal_is(connection):
    """
    Every animal found, with what's known of who it is: a dog's own name once it's been given one, or
    failing that the name of a face inside it, if that name is a dog's.  None for a dog not decided
    on, and '' for one that's been taken out of the running, as every cat is
    """
    animals = connection.execute(
        """
        SELECT animals.id, animals.photo_id, animals.rotation, animals.left, animals.top, animals.width,
            animals.height, animals.embedding, faces.id IS NOT NULL, names.name, animals.kind
        FROM animals LEFT JOIN faces ON faces.id = animals.id LEFT JOIN names ON names.face_id = animals.id
        """
    ).fetchall()
    dogs_in_photo = {}
    for place, row in enumerate(animals):
        if row[10] == DOG:
            dogs_in_photo.setdefault(row[1], []).append(place)
    faces = connection.execute(
        """
        SELECT faces.photo_id, faces.rotation, faces.left, faces.top, faces.width, faces.height, names.name
        FROM faces JOIN names ON names.face_id = faces.id
        WHERE names.name <> ? AND faces.id NOT IN (SELECT id FROM animals)
            -- someone tagged by hand has no box to be inside a dog's
            AND faces.width > 0
        """,
        (people_db.NOBODY,)
    ).fetchall()
    inside, named_faces = [], Counter()
    for photo_id, rotation, *box, name in faces:
        named_faces[name] += 1
        around = [
            place for place in dogs_in_photo.get(photo_id, ())
            if animals[place][2] == rotation and _inside(box, animals[place][3:7])
        ]
        if around:
            # the smallest, when dogs overlap
            inside.append((min(around, key=lambda place: animals[place][5] * animals[place][6]), name))
    inside_counts = Counter(name for _, name in inside)
    dog_names = {row[9] for row in animals if row[10] == DOG and row[9]} | {
        name for name, count in inside_counts.items()
        if count >= DOG_NAME_FACES and count >= DOG_NAME_SHARE * named_faces[name]
    }
    who = [
        people_db.NOBODY if row[10] != DOG else row[9] if row[8] else None
        for row in animals
    ]
    for place, name in inside:
        if who[place] is None and name in dog_names:
            who[place] = name
    return animals, who, dog_names


def name_new_dogs(connection, dog_ids):
    """
    Names those of these dogs that are clearly a dog already named, and returns how many.  Each dog's
    model is worked out afresh from every dog with a name, so that names given or changed by hand
    count
    """
    animals, who, dog_names = _who_each_animal_is(connection)
    if not animals or not dog_ids:
        return 0
    described = np.frombuffer(b''.join(row[7] for row in animals), dtype=np.float32).reshape(-1, EMBEDDING_SIZE)
    months = np.array([np.nan if (month := _month(row[1])) is None else month for row in animals])
    dated = ~np.isnan(months)
    labels = np.array(['' if name is None else name for name in who], dtype=object)
    decided = np.array([name is not None for name in who])
    wanted = set(dog_ids)
    new = np.array([row[0] in wanted and row[10] == DOG and not row[8] for row in animals])
    if not new.any():
        return 0
    chances = {}
    for name in sorted(dog_names):
        mine = labels == name
        if mine.sum() < MIN_NAMED or not (mine & dated).any():
            continue
        first = np.percentile(months[mine & dated], DATE_PERCENTILE) - MONTHS_BEFORE
        last = np.percentile(months[mine & dated], 100 - DATE_PERCENTILE) + MONTHS_AFTER
        around = ~dated | ((months >= first) & (months <= last))
        not_mine = (decided & ~mine) | ~around
        if not not_mine.any():
            continue
        model = _fit(described[mine | not_mine], mine[mine | not_mine])
        chances[name] = np.where(around[new], model(described[new]), 0)
    if not chances:
        return 0
    names = list(chances)
    table = np.stack([chances[name] for name in names], axis=1)
    rows = [animals[place] for place in np.flatnonzero(new)]
    named_in_photo = {}
    for photo_id, name in connection.execute(
        'SELECT faces.photo_id, names.name FROM faces JOIN names ON names.face_id = faces.id WHERE names.name <> ?',
        (people_db.NOBODY,)
    ):
        named_in_photo.setdefault(photo_id, set()).add(name)
    # the surest first, as nobody is in a photo twice
    order = sorted(range(len(rows)), key=lambda place: -table[place].max())
    chosen = []
    for place in order:
        best = int(table[place].argmax())
        name, row = names[best], rows[place]
        if table[place, best] < NAME_PROBABILITY or name in named_in_photo.get(row[1], ()):
            continue
        named_in_photo.setdefault(row[1], set()).add(name)
        chosen.append((row, name))
    if not chosen:
        return 0
    with connection:
        connection.executemany(
            """
            INSERT OR IGNORE INTO faces (id, photo_id, path, rotation, left, top, width, height, score, pixels, norm, embedding)
            SELECT id, photo_id, path, rotation, left, top, width, height, ?, pixels, 0, ? FROM animals WHERE id = ?
            """,
            [(people_db.DOG_FACE_SCORE, np.zeros(people_db.EMBEDDING_SIZE, dtype=np.float32).tobytes(), row[0]) for row, _ in chosen]
        )
    people_db.record_names(connection, [(row[0], name) for row, name in chosen])
    return len(chosen)
