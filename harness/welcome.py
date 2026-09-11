"""First turn of a freshly created bot: the welcome prompt.

A bot added through the app (or the `create_bot` tool, which POSTs the same
route) used to sit silent until the user typed something. Now creation queues
one user-lane inbox message tagged ``origin="welcome"`` and the bot opens the
conversation itself.

* **Recipe install** — the soul, skills and starting memories are already
  seeded, so the prompt tells the bot to finish setting itself up (paused
  routines, core memories), introduce the job, and ask for the one thing it
  needs to start on the recipe's first task.
* **Manual create** — the prompt carries what the user typed (title, role,
  description) and tells the bot to read its job from that and drive the
  chat: propose the first concrete step, or ask one question. A bot with
  no description has no job yet, so it asks what the user wants it for.

The prompt is the bot's instruction, not a person talking: it is hidden
from the 1:1 transcript (`agent/history.py`), never fanned out as a user
bubble (`harness/server.py`), and the Mac client skips the ``[Welcome``
head the same way it skips dream prompts. The bot's reply is an ordinary
bot message. Callers opt out with ``add_bot(welcome=False)`` (``harness
import`` and duplicates never send one, they pass ``start=False``).
"""

from __future__ import annotations

from agent import messaging
from harness.paths import HarnessPaths

#: head of every welcome prompt; clients and history hide user lines with it.
WELCOME_HEAD = "[Welcome"

#: a description this long is a drafted soul (bot builder, `create_bot`),
#: already in the system prompt — the prompt points at it instead of
#: repeating it.
_DESCRIPTION_MAX = 600

#: what a bot with no description asks (mirrors the manual-create chat).
NO_JOB_OPTIONS = ("A standing job", "Something in progress", "Spare pair of hands")


def is_welcome_prompt(text: str) -> bool:
    return (text or "").lstrip().startswith(WELCOME_HEAD)


def _own_description(title: str, name: str, description: str) -> str:
    """The description the user actually wrote, or "" when it is only the name.

    Both the Mac app and `Orchestrator.add_bot` fill an empty description with
    the display name, so a description equal to the title/name means none.
    """
    desc = " ".join((description or "").split())
    if not desc:
        return ""
    trivial = {(title or "").strip().lower(), (name or "").strip().lower()}
    if desc.lower() in trivial:
        return ""
    return desc


def welcome_prompt(
    *,
    title: str,
    name: str = "",
    role: str = "",
    description: str = "",
    recipe: dict | None = None,
) -> str:
    """The first user-lane message a new bot receives."""
    label = (title or name or "bot").strip()
    if recipe:
        return _recipe_prompt(label, recipe)
    return _manual_prompt(label, name, role, description)


def _manual_prompt(label: str, name: str, role: str, description: str) -> str:
    desc = _own_description(label, name, description)
    role_line = " ".join((role or "").split())
    if role_line.lower() in {label.lower(), (name or "").lower()}:
        role_line = ""
    lines = [
        f"{WELCOME_HEAD} — you were just created]",
        f'Your user just made you in the app and named you "{label}".',
    ]
    if role_line:
        lines.append(f"Role they gave you: {role_line}")
    if desc:
        if len(desc) > _DESCRIPTION_MAX:
            lines.append(
                "They wrote a full description of who you are; it is already loaded "
                "as your soul above, so read your job from that."
            )
        else:
            lines.append(f'Their description of you: "{desc}"')
    lines.append(
        "This is the first thing they will see from you, so open the conversation "
        "yourself. Greet them briefly (by name if you know it)."
    )
    if desc or role_line:
        lines.append(
            "Read your job from the name and description above: say in one or two "
            "lines what you take it to be, then drive the chat — propose the first "
            "concrete thing you can do for it, or ask the single question you need "
            "before starting. If a choice would move things along, put it in one "
            "ask_user_choice with at most four options. Do not write memories or "
            "save routines until they have confirmed the job."
        )
    else:
        options = " / ".join(f'"{o}"' for o in NO_JOB_OPTIONS)
        lines.append(
            "They gave no description, so you do not have a job yet. Ask what they "
            f"want you around for with one ask_user_choice offering {options}, "
            "and wait for the answer."
        )
    lines.append("Keep it to two or three short sentences plus the question, if any.")
    return "\n".join(lines)


def _recipe_prompt(label: str, recipe: dict) -> str:
    rname = str(recipe.get("name") or label).strip()
    first_task = " ".join(str(recipe.get("first_task") or "").split())
    plugins = [str(p).strip() for p in (recipe.get("plugins") or []) if str(p).strip()]
    never = [str(n).strip() for n in (recipe.get("never") or []) if str(n).strip()]
    lines = [
        f'{WELCOME_HEAD} — you were just installed from the "{rname}" recipe]',
        f'Your user picked this recipe in the app and you are now "{label}". Your '
        "soul, skills and starting memories are already in place. Finish setting "
        "yourself up, then introduce yourself:",
        "1. Save the recurring jobs your skills describe as routines, paused — "
        "check what already exists first, and nothing runs until they turn one on.",
        "2. Write the core memories you will need that are not remembered yet "
        "(what you track, where your workspace lives, the rules you never break).",
        "3. Introduce yourself in two or three sentences: what you do, what the "
        "paused routines are for"
        + (", and that you never " + "; ".join(never) if never else "")
        + ".",
    ]
    ask = "4. Finish with the one thing you need from them to start"
    if first_task:
        ask += f' on your first task: "{first_task}"'
    ask += "."
    if plugins:
        ask += (
            f" This job uses the {', '.join(plugins)} plugin"
            f"{'s' if len(plugins) > 1 else ''}; if one is not connected yet, say so "
            "and ask them to connect it in Plugins rather than working around it."
        )
    lines.append(ask)
    lines.append(
        "Do not invent data, prices, locations or accounts, and do not run the job "
        "itself yet — they have not asked."
    )
    return "\n".join(lines)


def queue_welcome(paths: HarnessPaths, bot: str, text: str) -> str:
    """Drop the welcome into the bot's inbox as a user-lane turn.

    User lane, not background: this is the bot's first user-facing chat, so it
    may ask questions and its cards must render like any typed send.
    """
    msg = messaging.Msg(to=bot, frm="user", text=text, origin=messaging.ORIGIN_WELCOME)
    messaging.send(paths, msg)
    return msg.id
