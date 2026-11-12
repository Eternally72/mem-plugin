"""Keep the historical API command while adding a single local startup command."""

import sys


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "start":
        from mem_plugin.bootstrap.runtime import main as run

        return run(args[1:])
    from mem_plugin.bootstrap.api import main as api

    return api(args)
