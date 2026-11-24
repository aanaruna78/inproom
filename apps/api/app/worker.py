"""`python -m app.worker`: run the source sync worker and scheduler (INPROOM-141 / story INPROOM-25).

Options: --poll SECONDS (idle wait, default 5), --tick SECONDS (scheduler cadence, default 15), --once.
Stops cleanly on SIGTERM/SIGINT after the current job. Safe to run several copies (leases + SKIP LOCKED).
"""

import argparse
import logging
import os
import signal
import socket
import time

from app.sync.worker import run_once, schedule_tick

stopping = False


def _stop(*_args: object) -> None:
    global stopping
    stopping = True


def main() -> None:
    parser = argparse.ArgumentParser(description="INPROOM source sync worker")
    parser.add_argument("--poll", type=float, default=5.0, help="seconds to wait when there is no work")
    parser.add_argument("--tick", type=float, default=15.0, help="seconds between scheduler ticks")
    parser.add_argument("--once", action="store_true", help="process at most one job and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    last_tick = 0.0
    while not stopping:
        if time.monotonic() - last_tick >= args.tick:
            schedule_tick()
            last_tick = time.monotonic()
        did = run_once(worker_id, schedule=False)
        if args.once:
            break
        if did is None:
            time.sleep(args.poll)


if __name__ == "__main__":
    main()
