"""Check real discord.py registration without importing integrations or secrets."""
import ast
from pathlib import Path
import unittest

try:
    import discord
    from discord import app_commands
    from discord.ext import commands
except ImportError:
    discord = app_commands = commands = None


@unittest.skipUnless(app_commands and hasattr(app_commands, "Command"), "requires real discord.py")
class CommandHistoryLimitTests(unittest.TestCase):
    def test_registered_ranges_and_defaults_match_all_four_commands(self):
        root = Path(__file__).resolve().parents[1]
        cases = (
            ("summary_cog.py", "SummaryCog", {"summarize": 5000, "magic_summarize": 5000, "deep_summary": 10000}),
            ("conversation_cog.py", "ConversationCog", {"ask_about_conversation": 1000}),
        )
        for filename, class_name, defaults in cases:
            tree = ast.parse((root / "discord_bot" / "cogs" / filename).read_text(encoding="utf-8"))
            node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
            namespace = {
                "__name__": "command_registration_test", "discord": discord,
                "app_commands": app_commands, "commands": commands, "MAX_HISTORY_MESSAGES": 20000,
            }
            # Execute the original class/annotations/decorators; callbacks do not run.
            exec(compile(ast.Module(body=[node], type_ignores=[]), filename, "exec"), namespace)
            for name, default in defaults.items():
                with self.subTest(command=name):
                    parameter = next(p for p in getattr(namespace[class_name], name).parameters if p.name == "len_msg")
                    self.assertEqual((parameter.min_value, parameter.max_value, parameter.default), (1, 20000, default))


if __name__ == "__main__":
    unittest.main()
