"""Seeded skill + chat text for Teach a task (learn from a demonstration).

The host owns capture (`harness/teachrec.py`). This module is only the
procedure the bot follows once stills land on the turn, and the user-line
the host sends to invoke it.
"""

SKILL_ID = "learn-from-demonstration"

LEARN_CHAT = "The recording is finished. Learn the task from it. /learn-from-demonstration"

LEARN_BLANK = (
    "The recording is finished but the capture looks unusable ({reason}). "
    "Offer a redo. /learn-from-demonstration"
)

SKILL_MD = """\
---
name: learn-from-demonstration
description: Turn a screen-recorded demonstration on your computer into a reusable skill
when_to_use: a teach recording has finished, the user says the recording is finished, or /learn-from-demonstration. Do not use for ordinary chat, for editing an existing skill by hand, or when no demonstration stills were attached.
tools: propose_skill
---
You are given stills (and sometimes a click/key log) from a screen recording
of the user demonstrating a task on YOUR computer. Turn that demonstration
into a reusable private skill. Work in the open: send a short first message
acknowledging you are watching the demo, and narrate meaningful beats as you
go.

## 1. Look at the evidence

The host already stopped capture and attached the stills to this turn. Do not
try to find, claim, or delete a recording; do not stop capture yourself;
do not invent a video-watching subagent, CDP, Playwright, or a file-reader tool.

Read every attached image, in order. If an `input.json` (or similar) log is
attached, use it for click/key timing — trust the log for coordinates and
order, and the stills for what was on screen. Never open Chrome DevTools,
never scrape cookies, never drive the browser from a shell.

If the stills show an idle desktop, the wrong surface, or nothing useful:
tell the user the recording came out blank, offer a redo, and do **not**
call `propose_skill`.

## 2. Decide what the reusable skill is

Identify the goal, the steps, and which demonstrated values are INPUTS
(the search term, the recipient, the date) versus fixed details. If the
recording clearly establishes a reusable workflow, proceed even if the user
did not say "make a skill". If ambiguity would materially change the skill
(unclear goal, cannot tell inputs from constants), ask concise questions
and wait.

Treat passwords, one-time codes, API keys, financial account numbers, and
private personal details as sensitive: placeholders only. If the
demonstration was mostly entering credentials, say so and do not create a
skill. Note that a credential was entered — never transcribe it.

## 3. Write the skill

Do not stop at a summary or an offer to create one. Call `propose_skill`
with a kebab-case slash name. That tool prompts the user to **edit** the
draft or **save it as a skill** — do not write the file any other way,
and do not skip the prompt.

- name: short kebab-case slash command ("order-groceries")
- description: one line saying when to apply it
- when_to_use: when the user asks for this workflow
- body: the GENERIC, reusable recipe. Parameterize inputs ("search for {item}").
  Prefer stable targets (URLs, labeled buttons and fields) over coordinates.
  Prefer a connector or MCP tool over UI replay when one covers a step; use
  the computer/browser only for steps nothing else supports. Mark
  consequential steps (submitting orders, sending messages, payments) as
  confirm-with-the-user-first. Never embed credentials — sign-in state lives
  in the browser profile, so a step that needs login says "assumes signed in
  to X". Do not encode harness internals: no CDP ports, no playwright
  snippets, no "call computerUse".

## 4. Report

After `propose_skill` returns, say whether they saved or discarded. If they
saved, name the `/command` so they can run it immediately. Offer a dry run;
NEVER run the learned skill unprompted. Offer a schedule (a routine that
@-mentions the workflow) only when the task looks recurring.
"""
