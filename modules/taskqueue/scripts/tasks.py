"""
Example task definitions for the demo and integration tests.

Import this module with --import-module scripts.tasks when starting workers.
"""

import math
import time

from taskqueue.worker.worker import task


@task()
def add(x, y):
    return x + y


@task()
def multiply(x, y):
    return x * y


@task()
def slow_sum(items, delay=0.1):
    """Simulate I/O-bound work."""
    time.sleep(delay)
    return sum(items)


@task()
def compute_primes(n):
    """Find all primes up to n using the Sieve of Eratosthenes."""
    sieve = bytearray([1]) * (n + 1)
    sieve[0] = sieve[1] = 0
    for i in range(2, int(math.sqrt(n)) + 1):
        if sieve[i]:
            sieve[i * i :: i] = bytearray(len(sieve[i * i :: i]))
    return [i for i, v in enumerate(sieve) if v]


@task(name="scripts.tasks.failing_task")
def failing_task(message="intentional failure"):
    raise ValueError(message)


@task()
def echo(value):
    return value
