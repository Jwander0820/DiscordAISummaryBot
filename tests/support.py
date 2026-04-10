import importlib
import sys
import types


def install_discord_stub():
    if "discord" in sys.modules:
        return sys.modules["discord"]

    discord_stub = types.ModuleType("discord")

    class TextChannel:
        pass

    class Message:
        pass

    class Guild:
        pass

    class Client:
        pass

    discord_stub.TextChannel = TextChannel
    discord_stub.Message = Message
    discord_stub.Guild = Guild
    discord_stub.Client = Client
    sys.modules["discord"] = discord_stub
    return discord_stub


def reload_module(module_name: str):
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)
