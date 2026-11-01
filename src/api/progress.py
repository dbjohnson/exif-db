import time


# a job's first moments, like starting up, say little about how long the rest will take
MIN_ELAPSED_SECS = 15


def remaining_secs(started, done, total):
    """
    How much longer a job should take at its pace so far, or None if it's too soon to say
    """
    elapsed = time.time() - started
    if done <= 0 or done >= total or elapsed < MIN_ELAPSED_SECS:
        return None
    return round(elapsed / done * (total - done))
