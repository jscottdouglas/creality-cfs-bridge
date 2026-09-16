"""CFS control: what may be written, when, and what came back.

The CFS card on the Device tab can change the printer's state, which nothing
else in the bridge's page could do before. Three rules hold it together, and
all three are enforced here, on the server, not in the page:

1. **The printer's own job wins.** While `print_stats.state` is `printing` or
   `paused` every write is refused, with two exceptions: drying, which is a
   cabinet heater and touches no filament path, and editing a slot that is
   neither loaded to the toolhead nor named by the active `colorMatch` map,
   which changes a label on a spool the running job will never reach.
2. **Dry run is still the default.** Every write goes out through
   `CrealityClient`, whose `_send_set` sends nothing at all unless the client
   was built with `dry_run=False`. `CfsWriter` builds a live one only when the
   caller asks for live and the facade was started with `--live`.
3. **Every accepted write is recorded with the values it replaced**, so the
   page can offer to put them back where the protocol allows it. It does not
   always: see `WriteRecord.undoable`.

Nothing in this module opens a socket by itself. `CfsWriter.client_factory` is
the seam the tests replace.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from .logs import get_logger
from .protocol import CrealityClient

log = get_logger()

# Every kind of write the card can ask for.
WRITE_KINDS = ("edit", "clear", "config", "feed", "refresh", "dry")

# Moonraker `print_stats.state` values that mean the printer is committed to a
# job. `complete`, `cancelled`, `error` and `standby` are all free.
BUSY_STATES = ("printing", "paused")

# The two things that stay allowed while a job runs.
ALLOWED_WHILE_PRINTING = ("dry", "edit", "clear")


class WriteRefused(RuntimeError):
    """A write the bridge will not make. The message is shown to the user."""


def print_state(print_stats: Optional[dict], snapshot: Optional[dict] = None) -> str:
    """The printer's job state, preferring Moonraker's word for it.

    Moonraker's `print_stats.state` is the authority: it names `paused`
    separately, which the Creality protocol's `deviceState` does not. When
    Moonraker has not answered, fall back to the LAN socket's own flags and
    report `printing` for anything that is not plainly idle, because refusing a
    write is always the safe way to be wrong.
    """
    state = ((print_stats or {}).get("state") or "").strip().lower()
    if state:
        return state
    if not snapshot:
        return "unknown"
    try:
        device_state = int(float(snapshot.get("deviceState", -1)))
    except (TypeError, ValueError):
        device_state = -1
    if device_state == 0:
        return "standby"
    return "printing"


def is_busy(state: str) -> bool:
    return state in BUSY_STATES or state == "unknown"


def check_write(kind: str, state: str, *, slot=None, active_map=None) -> Optional[str]:
    """Returns the refusal message, or None when the write may go ahead.

    `slot` is the `Slot` an `edit`, `clear`, `feed` or `refresh` targets.
    `active_map` is the set of slot labels the printer's live `colorMatch`
    currently redirects to, which is not the same thing as the loaded slot: a
    two-colour job holds two slots and only one of them is in the toolhead.
    """
    if kind not in WRITE_KINDS:
        return "%r is not a CFS write this bridge knows how to make." % kind
    if not is_busy(state):
        return None

    running = "paused" if state == "paused" else "printing"
    if state == "unknown":
        running = "not answering, so it is treated as printing"

    if kind not in ALLOWED_WHILE_PRINTING:
        return ("The printer is %s. cfsbridge refuses every CFS write except "
                "drying and editing an idle slot while a job is on the bed. "
                "Wait for the job to finish, or use the printer's own screen."
                % running)

    if kind == "dry":
        return None

    # An edit of a slot the running job is using rewrites the label under the
    # filament that is being extruded right now.
    if slot is None:
        return ("The printer is %s, and cfsbridge cannot tell which slot that "
                "edit is for, so it refused it." % running)
    if getattr(slot, "loaded", False):
        return ("Slot %s is the one loaded to the toolhead and the printer is "
                "%s. Editing it now would relabel the filament that is being "
                "printed. Refused." % (slot.label, running))
    labels = {str(x).upper() for x in (active_map or [])}
    if slot.label.upper() in labels or ("T" + slot.label).upper() in labels:
        return ("Slot %s is part of the running job's slot map and the printer "
                "is %s. Refused." % (slot.label, running))
    return None


# -- the write log ---------------------------------------------------------

@dataclass
class WriteRecord:
    """One accepted CFS write, and what it replaced."""

    kind: str
    description: str
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    at: float = field(default_factory=time.time)
    sent: bool = False           # False on a dry run: nothing left the machine
    ok: bool = True
    message: str = ""
    undoable: bool = False
    undo_reason: str = ""
    undone: bool = False

    def as_json(self, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        return {
            "id": self.id,
            "at": self.at,
            "age_s": max(0.0, now - self.at),
            "kind": self.kind,
            "description": self.description,
            "before": self.before,
            "after": self.after,
            "sent": self.sent,
            "ok": self.ok,
            "message": self.message,
            "undoable": self.undoable and self.sent and not self.undone,
            "undo_reason": self.undo_reason,
            "undone": self.undone,
        }


# Why each kind can or cannot be put back.
UNDO_RULES = {
    "edit": (True, ""),
    "clear": (True, ""),
    "config": (True, ""),
    "feed": (False, "A load or an unload is a physical movement of filament. "
                    "The opposite message exists, but sending it is a new "
                    "action, not an undo, so the card makes you press it."),
    "refresh": (False, "Re-reading an RFID tag replaces the slot's material "
                       "with whatever the tag says. There is no message that "
                       "puts the previous reading back; edit the slot instead."),
    "dry": (False, "Drying is stopped with the Stop button, which is its own "
                   "write. There is nothing to undo."),
}


# -- sending ---------------------------------------------------------------

@dataclass
class WriteResult:
    ok: bool
    sent: bool
    dry_run: bool
    message: str
    payload: dict = field(default_factory=dict)
    ack: Optional[dict] = None
    readback: Optional[dict] = None


class CfsWriter:
    """Sends one CFS write and reports what the printer did about it.

    `client_factory(host, dry_run=...)` is the seam: the tests hand in a fake
    that records instead of connecting. The real one is `CrealityClient`, whose
    dry-run mode already refuses to transmit, so a bug here fails closed.
    """

    def __init__(self, host: str, client_factory: Optional[Callable] = None,
                 ack_timeout: float = 8.0, settle: float = 1.0) -> None:
        self.host = host
        self.client_factory = client_factory or CrealityClient
        self.ack_timeout = ack_timeout
        self.settle = settle

    def send(self, description: str, params: dict, reply_key: Optional[str] = None,
             dry_run: bool = True, verify: Optional[Callable] = None) -> WriteResult:
        """Send `params` as a `set`, wait for `reply_key`, then verify.

        `verify(client)` is called after the printer has had `settle` seconds
        to act; it re-reads whatever proves the write landed and returns
        `(ok, message, readback)`. A write with no ack but a verified read-back
        is reported as a success with a note, because the printer's echo is
        advisory and the state is what matters.
        """
        if dry_run:
            client = self.client_factory(self.host, dry_run=True)
            message = client.send_and_wait(description, params, None)[0]
            return WriteResult(
                ok=True, sent=False, dry_run=True,
                message="Dry run: nothing was sent. The bridge would have sent "
                        "%s" % message.as_json(),
                payload=dict(params))

        client = self.client_factory(self.host, dry_run=False)
        try:
            client.connect()
        except Exception as exc:
            return WriteResult(ok=False, sent=False, dry_run=False,
                               message="Could not reach the printer at %s: %s"
                                       % (self.host, exc),
                               payload=dict(params))
        try:
            _, ack = client.send_and_wait(description, params, reply_key,
                                          timeout=self.ack_timeout)
            if verify is None:
                if reply_key and ack is None:
                    return WriteResult(
                        ok=False, sent=True, dry_run=False, payload=dict(params),
                        message="The printer did not answer with %r within %.0f "
                                "seconds. The message was sent; check the "
                                "printer's screen before sending it again."
                                % (reply_key, self.ack_timeout))
                return WriteResult(ok=True, sent=True, dry_run=False, ack=ack,
                                   payload=dict(params),
                                   message="Sent, and the printer acknowledged it."
                                           if ack else "Sent.")
            if self.settle:
                time.sleep(self.settle)
            ok, message, readback = verify(client)
            if ok and ack is None and reply_key:
                message = (message + " (The printer sent no %s echo, but the "
                                     "read-back confirms it.)" % reply_key)
            return WriteResult(ok=ok, sent=True, dry_run=False, ack=ack,
                               readback=readback, message=message,
                               payload=dict(params))
        except Exception as exc:
            return WriteResult(ok=False, sent=True, dry_run=False,
                               payload=dict(params),
                               message="The write was sent but the check after "
                                       "it failed: %s: %s"
                                       % (type(exc).__name__, exc))
        finally:
            try:
                client.close()
            except Exception:
                pass
