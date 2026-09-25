"""Scoped approvals + epoch-bound refusal memory for the control gate.

One JSON store per bot on the shared volume:

    shared/control/approvals-<bot>.json

Approvals are keyed `(bot, tool_call_id, action, sha256(target))` and are
retired when their scope ends (`end_scope`) or a new user turn begins
(`begin_turn`) — unless flagged `outlives_scope` (a background command whose
output outlasts the tool call). An approval may carry a `resource_path`;
reading exactly that path is implicitly approved, which is how "read the
output file of the command you approved" composes without a second ask.

Epochs: `begin_turn` bumps the bot's epoch once per new user turn. Refusals
are remembered as `sha256(target)@epoch`; a check scoped to epoch E is refused
when that target was refused at epoch >= E — so an approval widened later must
NOT retroactively authorize targets the user already declined. When the
refusal memory exceeds the cap the store degrades CLOSED: the current epoch is
marked saturated and every check scoped to it (or earlier) is refused.

Writes are atomic (`write_atomic`) so readers see the old or new file, never a
torn one. A corrupt store raises — the gate layer (`agent/gate.py`,
`fail_closed`) turns that into a refusal rather than an open gate.

Reimplemented in repo style from grok-bot 0.18's local-tool-permission
controller/machinery.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .fsutil import write_atomic
from .paths import HarnessPaths

#: remembered refusals per bot before the epoch degrades closed
REFUSAL_MEMORY_CAP = 512

VERDICT_ALLOW = "allow"  # an existing approval covers the action
VERDICT_ASK = "ask"  # nothing recorded either way; ask the human
VERDICT_REFUSED = "refused"  # this exact target was refused at this epoch or later
VERDICT_SATURATED = "saturated"  # too many refusals; the whole epoch is closed

#: actions that count as reads for the implicit resource_path approval
READ_ACTIONS = frozenset({"read", "read-file", "read_file"})


def target_key(target: str) -> str:
    """Stable key for an action target (command line, path, URL...)."""
    return hashlib.sha256((target or "").encode("utf-8")).hexdigest()


def _normalize_path(path: str | None) -> str | None:
    if not path:
        return None
    return path.replace("\\", "/").rstrip("/") or "/"


def is_user_chat(conversation: str) -> bool:
    return conversation == "peer:user" or (
        conversation.startswith(("thread:", "room:")) and bool(conversation.split(":", 1)[1])
    )


@dataclass
class Approval:
    """One recorded approval; `target_sha256` stands in for the raw target."""

    id: str
    action: str
    target_sha256: str
    tool_call_id: str = ""
    outlives_scope: bool = False
    resource_path: str | None = None
    epoch: int = 0
    granted: float = field(default_factory=time.time)


class ApprovalStore:
    """Read/write one bot's approvals and refusal memory (atomic JSON file)."""

    def __init__(self, paths: HarnessPaths, bot: str, cap: int = REFUSAL_MEMORY_CAP) -> None:
        self.paths = paths
        self.bot = bot
        self.cap = cap

    @property
    def file(self) -> Path:
        return self.paths.control / f"approvals-{self.bot}.json"

    # -- persistence ------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        if not self.file.is_file():
            return self._empty()
        # A corrupt file raises here on purpose: the gate boundary turns that
        # into a fail-closed refusal instead of silently forgetting refusals.
        data = json.loads(self.file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"approvals store {self.file} is not a JSON object")
        base = self._empty()
        base.update(data)
        if not isinstance(base.get("approvals"), list) or not isinstance(
            base.get("refusals"), dict
        ) or not isinstance(base.get("standing"), list) or not isinstance(
            base.get("credential_versions"), dict
        ):
            raise ValueError(f"approvals store {self.file} has a malformed shape")
        return base

    def _empty(self) -> dict[str, Any]:
        return {
            "bot": self.bot,
            "epoch": 0,
            "saturated_epoch": -1,
            "approvals": [],
            "standing": [],
            "credential_versions": {},
            "refusals": {},
        }

    def _save(self, data: dict[str, Any]) -> None:
        write_atomic(self.file, json.dumps(data, ensure_ascii=False, indent=2))

    @staticmethod
    def _approval(raw: dict[str, Any]) -> Approval:
        return Approval(
            id=str(raw.get("id", "")),
            action=str(raw.get("action", "")),
            target_sha256=str(raw.get("target_sha256", "")),
            tool_call_id=str(raw.get("tool_call_id", "")),
            outlives_scope=bool(raw.get("outlives_scope", False)),
            resource_path=raw.get("resource_path"),
            epoch=int(raw.get("epoch", 0)),
            granted=float(raw.get("granted", 0.0)),
        )

    # -- epochs -----------------------------------------------------------
    def epoch(self) -> int:
        return int(self._load().get("epoch", 0))

    def begin_turn(self) -> int:
        """New user turn: bump the epoch and retire scope-bound approvals."""
        data = self._load()
        data["epoch"] = int(data.get("epoch", 0)) + 1
        data["approvals"] = [a for a in data["approvals"] if a.get("outlives_scope")]
        self._save(data)
        return int(data["epoch"])

    # -- approvals --------------------------------------------------------
    def grant_all(self) -> None:
        """Allow remaining asks for this epoch (the rest of the user turn).

        `begin_turn` bumps the epoch, so the next user message asks again.
        Refusal memory still wins: a target already declined this epoch stays
        refused.
        """
        data = self._load()
        data["allow_all_epoch"] = int(data.get("epoch", 0))
        self._save(data)

    def grant_standing_issue_creation(self, conversation: str, repo: str) -> str:
        """Remember consent for creating issues in one repo in one human chat."""
        if not is_user_chat(conversation):
            raise ValueError("a chat is required for standing permission")
        repo = repo.strip().casefold()
        if len(repo.split("/")) != 2 or not all(repo.split("/")):
            raise ValueError("an owner/repository is required for standing permission")
        data = self._load()
        for row in data["standing"]:
            if row.get("conversation") == conversation and row.get("repo") == repo:
                return str(row["id"])
        permission_id = uuid.uuid4().hex[:12]
        data["standing"].append({
            "id": permission_id,
            "conversation": conversation,
            "tool": "github_create_issue",
            "repo": repo,
            "epoch": int(data.get("epoch", 0)),
            "granted": time.time(),
        })
        self._save(data)
        return permission_id

    def standing_permissions(self, conversation: str) -> list[dict[str, Any]]:
        return [row.copy() for row in self._load()["standing"] if row.get("conversation") == conversation]

    def credential_proposal_token(self, name: str, fingerprint: str) -> str:
        """Opaque, replay-stable card identity; private fingerprints stay here."""
        if not fingerprint:
            raise ValueError("credential version unavailable")
        data = self._load()
        saved = data["credential_versions"].get(name) or {}
        if saved.get("fingerprint") == fingerprint and saved.get("token"):
            return str(saved["token"])
        token = uuid.uuid4().hex
        data["credential_versions"][name] = {"fingerprint": fingerprint, "token": token}
        self._save(data)
        return token

    def grant_routine_credential(self, conversation: str, routine_id: str,
                                 revision: str, name: str, *, source_task: str,
                                 fingerprint: str) -> str:
        """Store one explicit human confirmation; never infer from routine prose."""
        from .secrets import resolve_env_name, valid_secret_name

        if (not is_user_chat(conversation) or not source_task or not routine_id
                or not revision or not fingerprint):
            raise ValueError("routine credential permission needs a current human confirmation")
        if not valid_secret_name(name):
            raise ValueError("invalid credential name")
        name = resolve_env_name(name)
        data = self._load()
        for row in data["standing"]:
            if (row.get("tool") == "use_secret_file" and row.get("conversation") == conversation
                    and row.get("routine_id") == routine_id and row.get("routine_revision") == revision
                    and row.get("credential") == name and row.get("credential_version") == fingerprint):
                return str(row["id"])
        permission_id = uuid.uuid4().hex[:12]
        data["standing"].append({
            "id": permission_id, "conversation": conversation, "tool": "use_secret_file",
            "routine_id": routine_id, "routine_revision": revision, "credential": name,
            "credential_version": fingerprint,
            "source_task": source_task, "epoch": int(data.get("epoch", 0)), "granted": time.time(),
        })
        self._save(data)
        return permission_id

    def revoke_credential(self, name: str) -> None:
        data = self._load()
        data["credential_versions"].pop(name, None)
        data["standing"] = [r for r in data["standing"] if not (
            r.get("tool") == "use_secret_file" and r.get("credential") == name
        )]
        self._save(data)

    def revoke_standing(self, conversation: str, permission_id: str) -> bool:
        data = self._load()
        keep = [row for row in data["standing"] if not (
            row.get("conversation") == conversation and row.get("id") == permission_id
        )]
        if len(keep) == len(data["standing"]):
            return False
        for row in data["standing"]:
            if row.get("conversation") == conversation and row.get("id") == permission_id:
                data["credential_versions"].pop(row.get("credential"), None)
        data["standing"] = keep
        self._save(data)
        return True

    def grant(
        self,
        action: str,
        target: str,
        *,
        tool_call_id: str = "",
        outlives_scope: bool = False,
        resource_path: str | None = None,
    ) -> Approval:
        data = self._load()
        approval = Approval(
            id=uuid.uuid4().hex[:12],
            action=action,
            target_sha256=target_key(target),
            tool_call_id=tool_call_id,
            outlives_scope=outlives_scope,
            resource_path=_normalize_path(resource_path),
            epoch=int(data.get("epoch", 0)),
        )
        data["approvals"].append(asdict(approval))
        self._save(data)
        return approval

    def approvals(self) -> list[Approval]:
        return [self._approval(raw) for raw in self._load()["approvals"]]

    def retire(self, approval_id: str) -> bool:
        data = self._load()
        keep = [a for a in data["approvals"] if a.get("id") != approval_id]
        if len(keep) == len(data["approvals"]):
            return False
        data["approvals"] = keep
        self._save(data)
        return True

    def end_scope(self, tool_call_id: str) -> int:
        """Retire approvals granted for one tool call, unless `outlives_scope`."""
        if not tool_call_id:
            return 0
        data = self._load()
        keep: list[dict[str, Any]] = []
        dropped = 0
        for raw in data["approvals"]:
            if raw.get("tool_call_id") == tool_call_id and not raw.get("outlives_scope"):
                dropped += 1
            else:
                keep.append(raw)
        if dropped:
            data["approvals"] = keep
            self._save(data)
        return dropped

    def approval_covering(
        self,
        action: str,
        target: str,
        *,
        tool_call_id: str | None = None,
        scope_epoch: int | None = None,
    ) -> Approval | None:
        """The live approval that covers `(action, target)` in this scope.

        Exact match on `(action, sha256(target))`, or — for read actions — an
        approval whose `resource_path` is exactly the requested path. An
        approval granted at a later epoch than the scope never applies.
        """
        data = self._load()
        epoch = int(data.get("epoch", 0)) if scope_epoch is None else scope_epoch
        sha = target_key(target)
        wanted_path = _normalize_path(target)
        for raw in data["approvals"]:
            approval = self._approval(raw)
            if approval.epoch > epoch:
                continue  # granted after this scope began; not retroactive
            in_scope = (
                tool_call_id is None
                or not approval.tool_call_id
                or approval.tool_call_id == tool_call_id
                or approval.outlives_scope
            )
            if not in_scope:
                continue
            if approval.action == action and approval.target_sha256 == sha:
                return approval
            if (
                action in READ_ACTIONS
                and approval.resource_path
                and _normalize_path(approval.resource_path) == wanted_path
            ):
                return approval
        return None

    def read_allowed(self, path: str) -> Approval | None:
        """The approval (if any) whose `resource_path` implicitly approves reading `path`."""
        return self.approval_covering("read-file", path)

    # -- refusal memory ---------------------------------------------------
    @staticmethod
    def _refusal_key(action: str, target: str) -> str:
        return f"{action}:{target_key(target)}"

    def record_refusal(self, action: str, target: str) -> None:
        """Remember `sha256(target)@epoch`; degrade CLOSED past the cap."""
        data = self._load()
        epoch = int(data.get("epoch", 0))
        refusals: dict[str, Any] = data["refusals"]
        key = self._refusal_key(action, target)
        refusals[key] = max(int(refusals.get(key, -1)), epoch)
        if len(refusals) > self.cap:
            # Too much to remember item-by-item: mark the epoch saturated so
            # everything scoped to it is refused, then drop the entries that
            # saturation now subsumes.
            data["saturated_epoch"] = max(int(data.get("saturated_epoch", -1)), epoch)
            data["refusals"] = {
                k: e for k, e in refusals.items() if int(e) > int(data["saturated_epoch"])
            }
        self._save(data)

    def refusal_verdict(
        self, action: str, target: str, scope_epoch: int | None = None
    ) -> str | None:
        """`VERDICT_SATURATED`, `VERDICT_REFUSED`, or None for this scope."""
        data = self._load()
        epoch = int(data.get("epoch", 0)) if scope_epoch is None else scope_epoch
        if int(data.get("saturated_epoch", -1)) >= epoch:
            return VERDICT_SATURATED
        refused_at = data["refusals"].get(self._refusal_key(action, target))
        if refused_at is not None and int(refused_at) >= epoch:
            return VERDICT_REFUSED
        return None

    # -- gate decision ----------------------------------------------------
    def check(
        self,
        action: str,
        target: str,
        *,
        tool_call_id: str | None = None,
        scope_epoch: int | None = None,
        conversation: str = "",
        tool_name: str = "",
        repo: str = "",
        routine_scope: tuple[str, str] | None = None,
        credential: str = "",
    ) -> tuple[str, Approval | None]:
        """One-stop decision: `(VERDICT_*, approval-or-None)`.

        Refusal memory is consulted before approvals, so an approval widened
        after the user said no does not override the no within that epoch.
        """
        verdict = self.refusal_verdict(action, target, scope_epoch)
        if verdict is not None:
            return verdict, None
        data = self._load()
        epoch = int(data.get("epoch", 0)) if scope_epoch is None else scope_epoch
        if int(data.get("allow_all_epoch", -1)) >= epoch:
            return VERDICT_ALLOW, None
        if tool_name == "github_create_issue" and conversation and repo:
            for row in data["standing"]:
                if (row.get("conversation") == conversation
                    and row.get("tool") == tool_name
                    and row.get("repo") == repo.strip().casefold()
                    and int(row.get("epoch", 0)) <= epoch):
                    return VERDICT_ALLOW, None
        if tool_name == "use_secret_file" and routine_scope and credential:
            from .secrets import credential_fingerprint

            current_version = credential_fingerprint(credential, self.paths)
            # Remember an observed environment rotation too. Restoring the old
            # bytes later must not revive consent that was invalidated here.
            kept = [row for row in data["standing"] if not (
                row.get("tool") == tool_name and row.get("credential") == credential
                and row.get("credential_version") != current_version
            )]
            if len(kept) != len(data["standing"]):
                data["standing"] = kept
                saved = data["credential_versions"].get(credential) or {}
                if saved.get("fingerprint") != current_version:
                    data["credential_versions"].pop(credential, None)
                self._save(data)
            for row in data["standing"]:
                if (row.get("tool") == tool_name and row.get("credential") == credential
                        and (row.get("routine_id"), row.get("routine_revision")) == routine_scope
                        and current_version and row.get("credential_version") == current_version
                        and int(row.get("epoch", 0)) <= epoch):
                    return VERDICT_ALLOW, None
        approval = self.approval_covering(
            action, target, tool_call_id=tool_call_id, scope_epoch=scope_epoch
        )
        if approval is not None:
            return VERDICT_ALLOW, approval
        return VERDICT_ASK, None
