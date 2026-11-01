"""
Works out which faces look alike across the whole of a people.db, from scratch.  Meant for a fast
computer, after the first scan of a library, or if the way of grouping changes: it compares every
face with every other, and after that the server only adds new faces to these groups.  Decisions
aren't touched, since they belong to faces rather than groups.

Each face is linked to its closest faces, and then each repeatedly takes on the group most of its
links are in (Chinese whispers).  Linking everything alike into one group would run people together,
through a chain of faces that each look like the next: on a library of 83,000 faces that made one
group of 37,000.  This also needs about 12GB of memory.

Usage: cluster_people.py <people.db> [--similarity 0.5] [--links 200] [--rounds 20]
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'api'))
import people_db

BATCH = 2000


def closest_faces(embeddings, links, similarity):
    """
    For each face, which others it's most like and how much, or -1 for fewer than that many links
    """
    count = len(embeddings)
    neighbours = np.full((count, links), -1, dtype=np.int32)
    weights = np.zeros((count, links), dtype=np.float32)
    for start in range(0, count, BATCH):
        part = embeddings[start:start + BATCH] @ embeddings.T
        part[np.arange(len(part)), np.arange(start, start + len(part))] = -1
        nearest = np.argpartition(-part, links, axis=1)[:, :links]
        values = np.take_along_axis(part, nearest, axis=1)
        keep = values >= similarity
        neighbours[start:start + len(part)] = np.where(keep, nearest, -1)
        weights[start:start + len(part)] = np.where(keep, values, 0)
    return neighbours, weights


def whispers(neighbours, weights, rounds, seed=1):
    # links go both ways: a face being on another's list is reason enough to be like it
    count = len(neighbours)
    edges = [[] for _ in range(count)]
    for face in range(count):
        for other, weight in zip(neighbours[face], weights[face]):
            if other >= 0:
                edges[face].append((int(other), float(weight)))
                edges[int(other)].append((face, float(weight)))
    labels = list(range(count))
    order = list(range(count))
    rng = random.Random(seed)
    for _ in range(rounds):
        rng.shuffle(order)
        changed = 0
        for face in order:
            votes = {}
            for other, weight in edges[face]:
                votes[labels[other]] = votes.get(labels[other], 0.0) + weight
            if votes:
                best = max(votes, key=votes.get)
                if best != labels[face]:
                    labels[face] = best
                    changed += 1
        if not changed:
            break
    return np.array(labels)


def load_usable(connection):
    rows = connection.execute(
        f'SELECT id, embedding FROM faces WHERE score >= {people_db.MIN_SCORE} AND pixels >= {people_db.MIN_PIXELS}'
    ).fetchall()
    ids = [row[0] for row in rows]
    embeddings = np.empty((len(rows), people_db.EMBEDDING_SIZE), dtype=np.float32)
    for place, row in enumerate(rows):
        embeddings[place] = people_db.embedding_from_db(row[1])
    return ids, embeddings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('database')
    parser.add_argument('--similarity', type=float, default=people_db.CLUSTER_SIMILARITY)
    parser.add_argument('--links', type=int, default=200)
    parser.add_argument('--rounds', type=int, default=20)
    args = parser.parse_args()
    connection = people_db.connect(args.database)
    ids, embeddings = load_usable(connection)
    print(f"{len(ids):,} faces good enough to compare", flush=True)
    neighbours, weights = closest_faces(embeddings, args.links, args.similarity)
    labels = whispers(neighbours, weights, args.rounds)
    groups = {label: number for number, label in enumerate(np.unique(labels), 1)}
    with connection:
        connection.execute('UPDATE faces SET cluster = NULL')
        connection.executemany(
            'UPDATE faces SET cluster = ? WHERE id = ?',
            [(groups[label], face_id) for label, face_id in zip(labels.tolist(), ids)]
        )
    sizes = np.sort(np.bincount(labels))[::-1]
    print(f"{len(groups):,} groups, largest {sizes[:8].tolist()}, {int((sizes == 1).sum()):,} on their own", flush=True)


if __name__ == '__main__':
    main()
