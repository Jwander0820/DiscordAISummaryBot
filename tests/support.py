import importlib
import sys
import types


def install_discord_stub():
    if "discord" in sys.modules:
        return sys.modules["discord"]

    discord_stub = types.ModuleType("discord")
    ext_stub = types.ModuleType("discord.ext")
    commands_stub = types.ModuleType("discord.ext.commands")
    app_commands_stub = types.ModuleType("discord.app_commands")
    errors_stub = types.ModuleType("discord.errors")
    ui_stub = types.ModuleType("discord.ui")

    class TextChannel:
        pass

    class Message:
        pass

    class Guild:
        pass

    class Client:
        pass

    class Interaction:
        pass

    class Member:
        pass

    class Embed:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.title = kwargs.get("title")
            self.description = kwargs.get("description")
            self.url = kwargs.get("url")

        def set_author(self, **kwargs):
            self.author = kwargs

        def set_image(self, **kwargs):
            self.image = kwargs

    class File:
        def __init__(self, fp=None, filename=None, spoiler=False, description=None):
            self.fp = fp
            self.filename = filename
            self.spoiler = spoiler
            self.description = description

    class AllowedMentions:
        @staticmethod
        def none():
            return None

    class ButtonStyle:
        gray = "gray"
        link = "link"

    class DiscordException(Exception):
        pass

    class Forbidden(DiscordException):
        pass

    class HTTPException(DiscordException):
        pass

    class NotFound(DiscordException):
        pass

    class InteractionResponded(DiscordException):
        pass

    class View:
        def __init__(self, *args, **kwargs):
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    class Button:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.label = kwargs.get("label")
            self.url = kwargs.get("url")

    def ui_button(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    class Cog:
        @classmethod
        def listener(cls, *args, **kwargs):
            def decorator(func):
                return func

            if args and callable(args[0]) and not kwargs:
                return args[0]
            return decorator

    class Bot:
        pass

    def identity_decorator(*args, **kwargs):
        def decorator(func):
            return func

        if args and callable(args[0]) and not kwargs:
            return args[0]
        return decorator

    def choices(**_kwargs):
        return identity_decorator

    class Choice:
        def __init__(self, *, name, value):
            self.name = name
            self.value = value

    def get(iterable, **attrs):
        for item in iterable:
            if all(getattr(item, key, None) == value for key, value in attrs.items()):
                return item
        return None

    discord_stub.TextChannel = TextChannel
    discord_stub.Message = Message
    discord_stub.Guild = Guild
    discord_stub.Client = Client
    discord_stub.Interaction = Interaction
    discord_stub.Member = Member
    discord_stub.Embed = Embed
    discord_stub.File = File
    discord_stub.AllowedMentions = AllowedMentions
    discord_stub.ButtonStyle = ButtonStyle
    discord_stub.Forbidden = Forbidden
    discord_stub.HTTPException = HTTPException
    discord_stub.NotFound = NotFound
    discord_stub.InteractionResponded = InteractionResponded
    discord_stub.ui = ui_stub
    discord_stub.utils = types.SimpleNamespace(get=get)

    ui_stub.View = View
    ui_stub.Button = Button
    ui_stub.button = ui_button

    errors_stub.Forbidden = Forbidden
    errors_stub.HTTPException = HTTPException
    errors_stub.NotFound = NotFound
    errors_stub.InteractionResponded = InteractionResponded

    commands_stub.Cog = Cog
    commands_stub.Bot = Bot

    app_commands_stub.command = identity_decorator
    app_commands_stub.choices = choices
    app_commands_stub.default_permissions = identity_decorator
    app_commands_stub.rename = identity_decorator
    app_commands_stub.describe = identity_decorator
    app_commands_stub.autocomplete = identity_decorator
    app_commands_stub.Choice = Choice

    sys.modules["discord"] = discord_stub
    sys.modules["discord.ext"] = ext_stub
    sys.modules["discord.ext.commands"] = commands_stub
    sys.modules["discord.app_commands"] = app_commands_stub
    sys.modules["discord.errors"] = errors_stub
    sys.modules["discord.ui"] = ui_stub
    return discord_stub


def reload_module(module_name: str):
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)
