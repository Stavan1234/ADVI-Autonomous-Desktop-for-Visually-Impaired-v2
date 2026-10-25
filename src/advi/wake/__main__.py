"""python -m advi.wake [run | install | uninstall | status | stop]

run        listen for "wake up ADVI" (the default; started at sign-in); restarts its worker after a crash
install    start the listener at every Windows sign-in, and start it now
uninstall  stop starting it at sign-in, and stop the running listener
status     show whether it is installed and running
stop       stop the running listener (it starts again at the next sign-in, or with ADVI)
"""

from __future__ import annotations

import sys


def cli(argv: list[str]) -> int:
    command = argv[0] if argv else "run"
    if command == "run":
        from advi.wake.listener import supervise

        return supervise()
    if command == "worker":  # started by the supervisor, with its process id
        from advi.wake.listener import worker

        return worker(int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else None)
    import advi.core.config  # noqa: F401  (loads .env, for ADVI_WAKE)
    from advi.wake import autostart
    from advi.wake.presence import app_running, wake_listener_running

    if command == "install":
        autostart.install()
        started = autostart.ensure_running()
        print(f"Wake word installed: it starts at every Windows sign-in{' and is listening now' if started else ''}.")
        return 0
    if command == "uninstall":
        removed = autostart.uninstall()
        stopped = autostart.stop_running()
        print("Wake word removed from Windows sign-in." if removed else "Wake word was not installed.")
        if stopped:
            print("The running wake listener was stopped.")
        return 0
    if command == "stop":  # used before updating packages: the listener keeps their DLLs open
        stopped = autostart.stop_running()
        print(f"Stopped {stopped} wake listener process(es)." if stopped else "The wake listener was not running.")
        return 0
    if command == "status":
        print(f"Starts at sign-in: {'yes' if autostart.installed() else 'no'}")
        print(f"Listening now:     {'yes' if wake_listener_running() else 'no'}")
        print(f"ADVI running:      {'yes' if app_running() else 'no'}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
