"""Ownership bookkeeping for widget signal handlers.

A GObject signal keeps a reference to every connected handler, and a handler
that is a bound method keeps its instance alive. Child widgets are owned by
their parent, so ``child.connect("clicked", self._on_clicked)`` creates the
cycle ``parent -> child -> handler -> parent``. PyGObject cannot hand that
cycle back to the garbage collector, so a discarded widget (a rebuilt card or a
closed dialog) would otherwise stay alive forever.

Widgets that are meant to be discarded therefore record their own handler IDs
and disconnect them when they are permanently disposed. Only handlers owned by
the widget are tracked; handlers connected by a longer-lived owner (for
example the main window listening to a card's ``launch-requested`` signal) are
left to that owner.
"""

from __future__ import annotations

import logging

from gi.repository import GObject

LOGGER = logging.getLogger(__name__)


class SignalOwner:
    """Mixin that remembers handler IDs so they can be disconnected later.

    The mixin must be listed after the GObject base class, for example
    ``class Card(Gtk.FlowBoxChild, SignalOwner)``. Cleanup is idempotent: a
    disconnected or already collected handler is skipped.
    """

    _owned_signals: list[tuple[GObject.Object, int]] | None = None

    def connect_owned(
        self,
        instance: GObject.Object,
        detailed_signal: str,
        handler,
        *args,
    ) -> int:
        """Connect ``handler`` and remember it as owned by this object."""
        handler_id = instance.connect(detailed_signal, handler, *args)
        if self._owned_signals is None:
            self._owned_signals = []
        self._owned_signals.append((instance, handler_id))
        return handler_id

    def disconnect_owned_signals(self) -> None:
        """Disconnect every handler recorded by :meth:`connect_owned`.

        Safe to call more than once and safe from inside a handler that is
        itself being disconnected: GObject permits disconnecting a handler
        during its own emission, and the emission has already captured what it
        needs.
        """
        owned = self._owned_signals
        self._owned_signals = []
        for instance, handler_id in owned or ():
            try:
                if instance.handler_is_connected(handler_id):
                    instance.disconnect(handler_id)
            except (TypeError, RuntimeError):
                # The instance or handler is already gone.
                LOGGER.debug(
                    "Skipping disconnect of stale handler %s", handler_id
                )


class DialogSignalOwner(SignalOwner):
    """Signal owner that releases its handlers when the dialog closes.

    A dialog owns the widgets below it, and those widgets own every handler
    connected to them. A bound method or a lambda that mentions the dialog
    therefore closes the cycle ``dialog -> widget -> handler -> dialog``, which
    PyGObject never returns to the garbage collector. Call
    :meth:`_init_dialog_lifecycle` at the end of ``__init__`` and connect
    dialog-owned handlers through :meth:`connect_owned`.
    """

    def _init_dialog_lifecycle(self) -> None:
        self.connect_owned(self, "closed", self._on_lifecycle_closed)

    def _on_lifecycle_closed(self, _dialog: GObject.Object) -> None:
        self.disconnect_owned_signals()
        self._release_dialog_references()

    def _release_dialog_references(self) -> None:
        """Drop child-widget and callback references held by the dialog.

        Subclasses extend this and must stay safe when called twice.
        """
