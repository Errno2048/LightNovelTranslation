"""Small public CLI for private configuration and reusable skill resources."""
from __future__ import annotations

import argparse
import json

from .errors import ConfigurationError, TranslationError
from .skill import export_skill
from .user_config import init_user_config, user_config_path, user_config_status


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="lightnovel-translate")
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="Initialize or check reusable private settings")
    actions = config.add_subparsers(dest="action", required=True)
    for name in ("init", "check", "path"):
        action = actions.add_parser(name)
        action.add_argument("--directory", help="Override ~/.lightnovel_translation")
    skill = commands.add_parser("skill", help="Export the bundled agent skill")
    exports = skill.add_subparsers(dest="action", required=True)
    exports.add_parser("export").add_argument("destination")
    args = parser.parse_args(argv)
    try:
        if args.command == "skill":
            result = export_skill(args.destination)
        elif args.action == "init":
            result = init_user_config(args.directory)
        elif args.action == "check":
            result = user_config_status(args.directory)
        else:
            result = {"path": str(user_config_path(args.directory))}
    except (ConfigurationError, TranslationError, OSError):
        # Do not format raw parser/OS errors that could embed configuration data.
        print(json.dumps({"ok": False, "error": "configuration_or_resource_error"}))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if args.action != "check" or result["generation_ready"] else 1
