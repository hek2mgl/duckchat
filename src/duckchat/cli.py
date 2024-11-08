"""
CLI based AI chat application.
"""

import argparse
import atexit
import code
import json
import logging
import os
import re
import readline
import subprocess
import sys
import textwrap

import requests
from google_speech import Speech
from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.text import Text

from duckchat import __version__

# Dictionary mapping model aliases to their respective identifiers.
models = {
    "gpt-4o-mini": "gpt-4o-mini",
    "claude-3-haiku": "claude-3-haiku-20240307",
    "llama": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
    "mixtral": "mistralai/Mixtral-8x7B-Instruct-v0.1",
}


def create_argparser():
    """
    Create and configure the argument parser for the CLI.

    Returns:
        argparse.ArgumentParser: Configured argument parser.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        help="the model to be used for the chat. The list of "
        "available models can be printed with the --list-models option",
        default="gpt-4o-mini",
    )

    parser.add_argument(
        "--list-models",
        help="list the models that can be used with the --model option and "
        "then exit the program. Do not start a chat",
        action="store_true",
    )

    parser.add_argument(
        "-f",
        "--file",
        help="append contents of file to prompt",
    )

    parser.add_argument(
        "--debug",
        help="print debug output",
        action="store_true",
    )

    parser.add_argument(
        "-s",
        "--one-shot",
        metavar="PROMPT",
        help="execute the prompt and exit",
    )

    parser.add_argument(
        "--tts",
        action="store_true",
        help="enable text-to-speech (experimental)",
    )

    parser.add_argument(
        "--tts-lang",
        help="text-to-speech language. (en for English, de for German)"
        " default is en",
        default="en",
    )

    parser.add_argument(
        "--tts-rate",
        help="rate of the text-to-speech voice",
        type=float,
        default=1.1,
    )

    parser.add_argument(
        "--version",
        action="version",
        version=__version__,
    )

    parser.add_argument(
        "--print-file",
        help="(try to) extract a file from the AI answer. this works when the"
        " answer contains exactly one code block. intended for AI-driven"
        " in-place editing of a file (experimental)",
        action="store_true",
    )

    return parser


class Output(Console):
    """
    Custom output class for handling console interactions.

    Inherits from rich.console.Console to provide enhanced output formatting.
    """

    def hello(self, args):
        """Print a welcome message to the console."""
        self.print(
            """[cyan]
 ____             _       ____ _           _
|  _ \\ _   _  ___| | __  / ___| |__   __ _| |_
| | | | | | |/ __| |/ / | |   | '_ \\ / _` | __|
| |_| | |_| | (__|   <  | |___| | | | (_| | |_
|____/ \\__,_|\\___|_|\\_\\  \\____|_| |_|\\__,_|\\__|

                   """
        )
        self.print(f"[cyan]Welcome to DuckChat! Your assistant is {args.model}")
        self.print("---")

    def print_models(self):
        """Print the available models to the console.

        Args:
            models (dict): A dictionary of model aliases and their names.
        """
        for model_alias, model_name in models.items():
            print(model_alias, model_name)

    def error(self, message):
        """Print an error message to the console.

        Args:
            message (str): The error message to display.
        """
        self.print("[red]EE:[/] " + str(message))

    def print_answer(self, response, chat, args):
        """Process and print the AI's response to the console.

        Args:
            response (requests.Response): The response object from the AI.
            chat (Chat): The Chat instance managing the conversation.
            args (argparse.Namespace): The parsed command-line arguments.
        """
        buffer = ""
        for line in response.iter_lines():
            line = line.decode("utf8").strip()
            chat.log.debug("read line %s", line)
            if not line:
                continue
            if line == "data: [DONE]":
                break

            if not line.startswith("data: "):
                raise ValueError(f"Bad format: {line}")

            data = json.loads(line.replace("data: ", ""))
            msg = data.get("message", "")
            buffer += msg

        chat.messages.append({"content": msg, "role": "assistant"})

        new_buffer = []
        printing = False
        for ln, line in enumerate(buffer.split("\n")):
            if "```" in line and not printing and args.print_file:
                printing = True
                continue

            if "```" in line and printing and args.print_file:
                printing = False
                continue

            if printing or not args.print_file:
                new_buffer.append(line)

        Output().print(f"🤖 [cyan]{chat.model}[default]: ", end="")

        # Prevent printing an extra empty line when the model responds
        # with a single line answer
        if len(new_buffer) > 1:
            Output().print(Markdown("\n".join(new_buffer)))
        else:
            print(new_buffer[0])

        if args.tts:
            speak("\n".join(new_buffer), args.tts_lang, args.tts_rate)

    def print_cmd_help(self):
        self.print(textwrap.dedent("""
            Available commands:

            listmodels - print available models
            setmodel [MODEL] - set the model. with no argument, print the current model
            help - display this help
        """))


class Chat:
    """
    Class to manage the chat session with the AI model.

    Attributes:
        base_url (str): The base URL for the AI service.
        model (str): The model identifier to use for the chat.
        messages (list): List of messages exchanged in the chat.
        vqd (str): VQD token for session management.
        session (requests.Session): HTTP session for making requests.
        log (logging.Logger): Logger for debugging and information.
    """

    def __init__(self, url, model):
        """
        Initialize the Chat instance.

        Args:
            url (str): The base URL for the AI service.
            model (str): The model identifier to use for the chat.
        """
        self.base_url = url
        self.model = model
        self.messages = []
        self.vqd = None
        self.session = None
        self.log = logging.getLogger("chat")

    def setup(self):
        """
        Set up the chat session by initializing the HTTP session and retrieving the VQD token.

        Raises:
            Exception: If the VQD token cannot be retrieved.
        """
        self.log.debug("Initializing")
        self.log.debug("Create session")
        self.session = requests.Session()
        headers = {
            "x-vqd-accept": "1",
        }
        url = self.base_url + "/status"
        self.log.debug("Getting vqd from %s", url)
        response = self.session.get(url, headers=headers)
        response.raise_for_status()

        self.vqd = response.headers.get("x-vqd-4")
        self.log.debug("Received vqd: %s", self.vqd)
        if not self.vqd:
            raise RuntimeError("Failed to retrieve VQD")
        self.log.debug("Initialized. Ok")

    def prompt(
        self,
        msg,
    ):
        """
        Send a message to the AI model and receive a response.

        Args:
            msg (str): The message to send to the AI.

        Returns:
            requests.Response: The response object from the AI.

        Raises:
            requests.exceptions.HTTPError: If the request to the AI service fails.
        """
        self.messages.append({"content": msg, "role": "user"})
        url = self.base_url + "/chat"

        headers = {
            "x-vqd-4": self.vqd,
            "Content-Type": "application/json",
            "Accept": "text/plain",
        }

        payload = json.dumps(
            {
                "model": self.model,
                "messages": self.messages,
            }
        )

        response = self.session.post(
            url,
            headers=headers,
            data=payload,
        )

        response.raise_for_status()
        loggers = [logging.getLogger(name) for name in logging.root.manager.loggerDict]
        headers = dict(zip(response.headers.keys(), response.headers.values()))
        self.log.debug("response headers %s", json.dumps(headers, indent=2))
        new_vqd = response.headers["x-vqd-4"]
        self.log.debug("vqd changed: %s -> %s", self.vqd, new_vqd)
        self.vqd = new_vqd
        return response


def init_logging(args):
    """
    Initialize logging for the application.

    Configures the logging level based on the provided command-line arguments.
    If the debug flag is set, the logging level is set to DEBUG; otherwise, it defaults to WARNING.

    Args:
        args (argparse.Namespace): Parsed command-line arguments containing the debug flag.

    Returns:
        logging.Logger: The configured logger instance.
    """
    log = logging.getLogger()
    if args.debug:
        logging.basicConfig()
        log.setLevel(logging.DEBUG)
    return log


class HistoryConsole(code.InteractiveConsole):
    """
    Derived from an example in the Python documentation
    see: https://docs.python.org/3/library/readline.html
    """

    def __init__(
        self,
        locals=None,
        filename="<console>",
        histfile=os.path.expanduser("~/.duckchat-history"),
    ):
        code.InteractiveConsole.__init__(self, locals, filename)
        self.init_len = 0
        self.init_history(histfile)

    def init_history(self, histfile):
        readline.parse_and_bind("tab: complete")
        if hasattr(readline, "read_history_file"):
            try:
                readline.read_history_file(histfile)
                self.init_len = readline.get_current_history_length()
            except FileNotFoundError:
                pass
            atexit.register(self.save_history, histfile)

    def save_history(self, histfile):
        # create empty file if it doens't exist
        # see: https://stackoverflow.com/a/12654798/171318
        open(histfile, "a").close()
        new_len = readline.get_current_history_length()
        readline.set_history_length(1000)
        readline.append_history_file(new_len - self.init_len, histfile)

    def read_prompt(self):
        """
        Prompt the user for input until a non-empty response is received.

        Continuously asks the user for input until they provide a non-empty string.
        Displays an error message if the input is empty.

        Returns:
            str: The user's input prompt.
        """
        while True:
            username = os.environ.get("USER", "me")
            prompt_prefix = f"🦆 \033[33m{username}\033[0m: "
            prompt = self.raw_input(prompt=prompt_prefix)
            if not prompt:
                Output().print("[dark_red]Your prompt is empty!")
                continue
            Output().print("---")
            return prompt


def readfile(filename):
    """
    Read the contents of a file.

    Opens the specified file and returns its contents as a string.

    Args:
        filename (str): The path to the file to be read.

    Returns:
        str: The contents of the file.
    """
    with open(filename, encoding="utf8") as open_file:
        return open_file.read()


def passthru(command):
    # Execute the command and display the output
    process = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = process.communicate()

    # Print the output
    if stdout:
        print(stdout.decode(), end="")
    if stderr:
        print(stderr.decode(), end="")


def spawn_shell(command_line):
    if command_line == "!":
        subprocess.run(["bash", "-li"])
    else:
        subprocess.run(
            ["bash", "-c", command_line[1:]],
            env={"PS2": "(exit to return) >"},
        )



def run_cmd(command_line, chat):
    command_line = command_line[1:]
    words = re.split(" +", command_line)
    command = words[0]

    if len(words) > 1:
        args = words[1:]
    else:
        args = []

    # if command == "m":
    print(command, args)

    if command == "newhist":
        chat.messages = []
    elif command == "listmodels":
        Output().print_models()
    elif command == "setmodel":
        if len(args):
            chat.model = models[args[0]]
        else:
            Output().print(chat.model)
    elif command == "help":
        Output().print_cmd_help()


def speak(text, lang, rate):
    """
    :param lang: en=English de=German
    """
    speech = Speech(text, lang)
    speech.rate = rate
    speech.play()


def main():
    """
    Main entry point for the CLI chat application.

    Parses command-line arguments, initializes logging, and manages the chat session.
    If the --list-models option is provided, it lists available models and exits.
    Otherwise, it sets up the chat and enters a loop to handle user prompts and AI responses.
    """
    args = create_argparser().parse_args()
    init_logging(args)
    output = Output()
    cnsl = HistoryConsole()

    if args.list_models:
        output.print_models()
        return 0

    if args.model not in models:
        output.error(
            f"Model '{args.model}' is not available. "
            "List available models with --list-models"
        )
        return 1

    chat = Chat(
        "https://duckduckgo.com/duckchat/v1",
        models[args.model],
    )

    output.hello(args)
    chat.setup()

    try:
        while True:
            if args.one_shot:
                prompt = args.one_shot
                if args.file:
                    prompt += " " + readfile(args.file)
            else:
                prompt = cnsl.read_prompt()
                if prompt == "exit":
                    Output().print("bye!")
                    break

            if prompt.startswith("!"):
                spawn_shell(prompt)
                if args.one_shot:
                    break
                continue

            if prompt.startswith(":"):
                run_cmd(prompt, chat)
                if args.one_shot:
                    break
                continue

            Output().print_answer(chat.prompt(prompt), chat, args)
            if args.one_shot:
                break
            Output().print("---")

    except (KeyboardInterrupt, EOFError):
        print("")
        return 1

    except requests.exceptions.HTTPError as err:
        Output().error(err)
        Output().error(err.response.text)

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
