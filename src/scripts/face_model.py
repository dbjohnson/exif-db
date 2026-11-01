"""
Finds faces in a photo and describes each with 512 numbers, using InsightFace's buffalo_l models
through ONNX Runtime.  This is the same work as its FaceAnalysis, without the package, which needs
a compiler and a lot else to install.
"""
import os

import cv2
import numpy as np
import onnxruntime

MODELS = os.getenv('FACE_MODELS', '/opt/face-models')
DETECTOR = 'det_10g.onnx'
RECOGNIZER = 'w600k_r50.onnx'
# which model made an embedding, since those from different models can't be compared
MODEL_NAME = 'buffalo_l'
DETECT_SIZE = 640
MIN_DETECT_SCORE = 0.5
MAX_OVERLAP = 0.4
STRIDES = (8, 16, 32)
# where the eyes, nose and mouth corners go in the square the recognizer looks at
ALIGNED_SIZE = 112
ALIGNED_POINTS = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32
)


def similarity_transform(source, target):
    """
    The rotation, scale and shift that best takes source points to target points (Umeyama, 1991)
    """
    source_mean, target_mean = source.mean(axis=0), target.mean(axis=0)
    source_centered, target_centered = source - source_mean, target - target_mean
    covariance = target_centered.T @ source_centered / len(source)
    u, singular, vt = np.linalg.svd(covariance)
    sign = np.array([1.0, 1.0 if np.linalg.det(covariance) >= 0 else -1.0])
    rotation = u @ np.diag(sign) @ vt
    scale = (singular * sign).sum() / source_centered.var(axis=0).sum()
    matrix = np.zeros((2, 3))
    matrix[:2, :2] = scale * rotation
    matrix[:2, 2] = target_mean - scale * rotation @ source_mean
    return matrix


def keep_separate(boxes, scores):
    """
    Which detections to keep, best first, dropping any that overlap a better one
    """
    x0, y0, x1, y1 = boxes.T
    areas = (x1 - x0 + 1) * (y1 - y0 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        best, rest = order[0], order[1:]
        keep.append(best)
        width = np.maximum(0.0, np.minimum(x1[best], x1[rest]) - np.maximum(x0[best], x0[rest]) + 1)
        height = np.maximum(0.0, np.minimum(y1[best], y1[rest]) - np.maximum(y0[best], y0[rest]) + 1)
        overlap = width * height / (areas[best] + areas[rest] - width * height)
        order = rest[overlap <= MAX_OVERLAP]
    return keep


class FaceModel:
    def __init__(self, threads, models=MODELS):
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        providers = ['CPUExecutionProvider']
        self.detector = onnxruntime.InferenceSession(os.path.join(models, DETECTOR), options, providers=providers)
        self.recognizer = onnxruntime.InferenceSession(os.path.join(models, RECOGNIZER), options, providers=providers)
        self.anchors = {}

    def _anchors(self, stride):
        if stride not in self.anchors:
            cells = DETECT_SIZE // stride
            centers = np.stack(np.mgrid[:cells, :cells][::-1], axis=-1).astype(np.float32) * stride
            self.anchors[stride] = np.stack([centers.reshape(-1, 2)] * 2, axis=1).reshape(-1, 2)
        return self.anchors[stride]

    def detect(self, image):
        """
        Boxes as (x0, y0, x1, y1) in the image's pixels, scores, and 5 landmarks for each
        """
        height, width = image.shape[:2]
        if height / width > 1:
            new_height, new_width = DETECT_SIZE, int(DETECT_SIZE / (height / width))
        else:
            new_width, new_height = DETECT_SIZE, int(DETECT_SIZE * (height / width))
        scale = new_height / height
        canvas = np.zeros((DETECT_SIZE, DETECT_SIZE, 3), dtype=np.uint8)
        canvas[:new_height, :new_width] = cv2.resize(image, (new_width, new_height))
        blob = cv2.dnn.blobFromImage(canvas, 1 / 128, (DETECT_SIZE, DETECT_SIZE), (127.5, 127.5, 127.5), swapRB=True)
        outputs = self.detector.run(None, {self.detector.get_inputs()[0].name: blob})
        boxes, scores, landmarks = [], [], []
        for index, stride in enumerate(STRIDES):
            centers = self._anchors(stride)
            found = outputs[index].ravel() >= MIN_DETECT_SCORE
            if not found.any():
                continue
            distances = outputs[index + 3][found] * stride
            points = outputs[index + 6][found] * stride
            where = centers[found]
            boxes.append(np.stack([
                where[:, 0] - distances[:, 0],
                where[:, 1] - distances[:, 1],
                where[:, 0] + distances[:, 2],
                where[:, 1] + distances[:, 3]
            ], axis=-1))
            scores.append(outputs[index].ravel()[found])
            landmarks.append((points + np.tile(where, (1, 5))).reshape(-1, 5, 2))
        if not boxes:
            return np.empty((0, 4)), np.empty(0), np.empty((0, 5, 2))
        boxes, scores, landmarks = np.vstack(boxes) / scale, np.concatenate(scores), np.vstack(landmarks) / scale
        keep = keep_separate(boxes, scores)
        return boxes[keep], scores[keep], landmarks[keep]

    def describe(self, image, landmarks):
        """
        The recognizer's 512 numbers for a face, as given, since their length says something too
        """
        matrix = similarity_transform(landmarks, ALIGNED_POINTS)
        aligned = cv2.warpAffine(image, matrix, (ALIGNED_SIZE, ALIGNED_SIZE), borderValue=0.0)
        blob = cv2.dnn.blobFromImage(aligned, 1 / 127.5, (ALIGNED_SIZE, ALIGNED_SIZE), (127.5, 127.5, 127.5), swapRB=True)
        return self.recognizer.run(None, {self.recognizer.get_inputs()[0].name: blob.astype(np.float32)})[0].ravel()

    def faces(self, image):
        """
        Every face in an image, with its box as fractions of the image
        """
        height, width = image.shape[:2]
        boxes, scores, landmarks = self.detect(image)
        found = []
        for box, score, points in zip(boxes, scores, landmarks):
            raw = self.describe(image, points)
            length = float(np.linalg.norm(raw))
            found.append({
                'left': float(box[0]) / width,
                'top': float(box[1]) / height,
                'width': float(box[2] - box[0]) / width,
                'height': float(box[3] - box[1]) / height,
                'score': float(score),
                'pixels': float(min(box[2] - box[0], box[3] - box[1])),
                'norm': length,
                'embedding': (raw / length).astype(np.float32)
            })
        return found
