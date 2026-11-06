"""GTK dialogs for game-data cleanup and orphan inspection.

All filesystem and metadata decisions belong to ``CleanupService`` and
``GameManager``. These dialogs only present the exact targets, require explicit
confirmation, and show partial results without hiding them.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402
from minimal_game_launcher.ui.lifecycle import DialogSignalOwner  # noqa: E402

from minimal_game_launcher.errors import LauncherError  # noqa: E402
from minimal_game_launcher.models import Game  # noqa: E402
from minimal_game_launcher.services.cleanup import (  # noqa: E402
    CleanupOutcome,
    CleanupReport,
    CleanupService,
    ResourceTarget,
)
from minimal_game_launcher.services.game_manager import GameManager  # noqa: E402
from minimal_game_launcher.services.ownership import (  # noqa: E402
    OwnershipStatus,
    ResourceKind,
    SizeEstimate,
)

_KIND_LABEL = {
    ResourceKind.INSTALLATION: "Game installation",
    ResourceKind.PREFIX: "Wine prefix",
    ResourceKind.SHARED_PREFIX: "Shared Wine prefix",
    ResourceKind.INSTALLER_SOURCE: "Installer source",
}

_STATUS_LABEL = {
    OwnershipStatus.MANAGED: "MGL-managed",
    OwnershipStatus.EXTERNAL: "External path",
    OwnershipStatus.MISSING: "Missing",
    OwnershipStatus.INVALID: "Invalid or unverified path",
}


class CleanupDialog(Adw.Dialog, DialogSignalOwner):
    """Let the user remove a library entry and optionally managed files.

    The library record is selected by default, which preserves every file and
    keeps metadata-only removal the safe default. Every resource must be chosen
    explicitly, and unsafe targets are disabled with an explanation.
    """

    __gsignals__ = {
        "finished": (GObject.SignalFlags.RUN_FIRST, None, (object, bool)),
    }

    def __init__(
        self,
        parent: Gtk.Window,
        game: Game,
        cleanup: CleanupService,
        games: GameManager,
    ) -> None:
        super().__init__()
        self._parent = parent
        self.game = game
        self._cleanup = cleanup
        self._games = games
        self._targets = cleanup.targets_for_game(game)
        self._target_rows: dict[ResourceKind, Adw.SwitchRow] = {}
        self._active_result: CleanupResultDialog | None = None
        self._active_alert: Adw.AlertDialog | None = None

        self.set_content_width(680)
        self.set_content_height(620)
        self.set_can_close(True)
        self.set_child(self._build_ui())
        self._init_dialog_lifecycle()

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(title=f"Remove {self.game.name}")
        )
        close = Gtk.Button(label="Cancel")
        close.add_css_class("flat")
        self.connect_owned(close, "clicked", self._on_close_clicked)
        header.pack_start(close)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        library = Adw.PreferencesGroup(title="Library")
        page.add(library)
        self._record_row = Adw.SwitchRow(title="Remove library entry")
        self._record_row.set_subtitle(
            "Removes only the database record when no managed data is selected."
        )
        self._record_row.set_active(True)
        self.connect_owned(
            self._record_row, "notify::active", self._on_selection_changed
        )
        library.add(self._record_row)

        if self._targets:
            managed = Adw.PreferencesGroup(
                title="Managed data",
                description=(
                    "Only verified MGL-managed paths can be deleted. External "
                    "installations, external prefixes, and the original ISO are "
                    "never offered for deletion."
                ),
            )
            page.add(managed)
            for target in self._targets:
                row = self._build_target_row(target)
                self._target_rows[target.kind] = row
                managed.add(row)

        safety = Adw.PreferencesGroup(title="Safety")
        page.add(safety)
        for title, subtitle in (
            (
                "Original ISO",
                "Disc images are references, never MGL-owned data. Cleanup "
                "never deletes an ISO.",
            ),
            (
                "External data",
                "Paths outside the configured MGL-managed roots are shown for "
                "context only and cannot be selected.",
            ),
            (
                "Running games",
                "Managed data cannot be deleted while its game is starting or "
                "running, or while an installer session is active.",
            ),
        ):
            row = Adw.ActionRow(title=title)
            row.set_subtitle(subtitle)
            safety.add(row)

        toolbar.add_bottom_bar(self._build_action_bar())
        return toolbar

    def _build_target_row(self, target: ResourceTarget) -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=_KIND_LABEL[target.kind])
        details = [
            f"{target.game_name or 'Orphaned data'} ({target.game_id})",
            str(target.path),
            f"{_STATUS_LABEL[target.status]} · {_size_text(target.estimate)}",
        ]
        if target.unavailable_reason:
            details.append(f"Cannot delete: {target.unavailable_reason}")
        if target.installer_active:
            details.append("An active or deferred installer session uses this data.")
        if target.running:
            details.append("The game is starting or running.")
        row.set_subtitle("\n".join(details))
        row.set_subtitle_lines(4)
        row.set_tooltip_text(str(target.path))
        if not target.can_delete:
            row.set_sensitive(False)
            row.set_active(False)
        self.connect_owned(
            row, "notify::active", self._on_selection_changed
        )
        return row

    def _build_action_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_top(8)
        bar.set_margin_bottom(8)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.set_halign(Gtk.Align.END)

        self._summary = Gtk.Label(label="")
        self._summary.add_css_class("dim-label")
        self._summary.set_hexpand(True)
        self._summary.set_xalign(0.0)
        bar.append(self._summary)

        self._continue_button = Gtk.Button(label="Continue")
        self._continue_button.add_css_class("suggested-action")
        self.connect_owned(
            self._continue_button, "clicked", self._on_continue
        )
        bar.append(self._continue_button)

        self._update_summary()
        return bar

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_result_closed(
        self, _dialog: Adw.Dialog, report: CleanupReport, record_removed: bool
    ) -> None:
        self._finish_with_result(report, record_removed)

    def _release_dialog_references(self) -> None:
        self._active_result = None
        self._active_alert = None
        self._target_rows.clear()

    # ------------------------------------------------------------------
    # Selection and confirmation

    def _selected_targets(self) -> list[ResourceTarget]:
        return [
            target
            for target in self._targets
            if target.can_delete
            and self._target_rows[target.kind].get_active()
        ]

    def _on_selection_changed(
        self, _row: Adw.SwitchRow, _pspec: object
    ) -> None:
        if hasattr(self, "_summary"):
            self._update_summary()

    def _update_summary(self) -> None:
        selected = self._selected_targets()
        record = self._record_row.get_active()
        if not selected and not record:
            text = "Select the library entry or at least one managed resource."
        elif not selected:
            text = "Only the library record will be removed. All files stay on disk."
        else:
            count = len(selected)
            text = f"{count} managed resource{'s' if count != 1 else ''} selected."
            if record:
                text += " The library entry is removed after successful deletion."
            else:
                text += " The library entry will be kept."
        self._summary.set_text(text)
        self._continue_button.set_sensitive(bool(selected) or record)

    def confirmation_body(self) -> str:
        """Return the exact confirmation text (also used by tests)."""
        record = self._record_row.get_active()
        selected = self._selected_targets()
        lines: list[str] = []
        if record:
            lines.append(
                f"• Library entry: {self.game.name} ({self.game.id})"
            )
        for target in selected:
            lines.append(
                f"• {_KIND_LABEL[target.kind]}: {target.path}\n"
                f"  {_STATUS_LABEL[target.status]}, "
                f"{_size_text(target.estimate)}"
            )
        if not selected:
            lines.append(
                "\nNo files will be deleted. The executable, installation "
                "directory, Wine prefix, installer source, and ISO remain on disk."
            )
        return "\n".join(lines)

    def _on_continue(self, _button: Gtk.Button) -> None:
        selected = self._selected_targets()
        record = self._record_row.get_active()
        if not selected and not record:
            return
        heading = (
            "Remove managed data?"
            if selected
            else "Remove from library?"
        )
        dialog = Adw.AlertDialog(
            heading=heading,
            body=self.confirmation_body(),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance(
            "remove", Adw.ResponseAppearance.DESTRUCTIVE
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        self._active_alert = dialog
        dialog.choose(self._parent, None, self._on_confirmed, selected, record)

    def _on_confirmed(
        self,
        dialog: Adw.AlertDialog,
        result: object,
        selected: list[ResourceTarget],
        record: bool,
    ) -> None:
        self._active_alert = None
        try:
            response = dialog.choose_finish(result)
        except GLib.Error:
            return
        if response != "remove":
            return
        self._perform(selected, record)

    def _perform(
        self, selected: list[ResourceTarget], record: bool
    ) -> None:
        try:
            report = self._cleanup.delete_targets(selected, authorized=True)
        except LauncherError as error:
            self._show_error(str(error))
            return
        except Exception as error:
            self._show_error(f"Cleanup could not be completed: {error}")
            return

        record_removed = False
        record_error: str | None = None
        if record and report.all_deleted:
            try:
                self._games.remove_game(self.game.id)
                record_removed = True
            except LauncherError as error:
                record_error = str(error)
            except Exception as error:
                record_error = f"The library entry could not be removed: {error}"

        if not selected and record_error is None:
            self.emit("finished", report, record_removed)
            self.close()
            return

        result_dialog = CleanupResultDialog(
            report,
            record_selected=record,
            record_removed=record_removed,
            record_error=record_error,
        )
        result_dialog.connect(
            "closed",
            self._on_result_closed,
            report,
            record_removed,
        )
        self._active_result = result_dialog
        result_dialog.present(self._parent)

    def _finish_with_result(
        self, report: CleanupReport, record_removed: bool
    ) -> None:
        self._active_result = None
        self.emit("finished", report, record_removed)
        self.close()

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog(heading="Cleanup failed", body=message)
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        self._active_alert = dialog
        dialog.choose(self._parent, None, self._on_error_closed)

    def _on_error_closed(
        self, dialog: Adw.AlertDialog, result: object
    ) -> None:
        self._active_alert = None
        try:
            dialog.choose_finish(result)
        except GLib.Error:
            pass


class OrphanCleanupDialog(Adw.Dialog, DialogSignalOwner):
    """Inspect managed-root orphans on demand and delete explicit selections."""

    __gsignals__ = {
        "finished": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, parent: Gtk.Window, cleanup: CleanupService) -> None:
        super().__init__()
        self._parent = parent
        self._cleanup = cleanup
        self._scan = cleanup.scan_orphans()
        self._target_rows: dict[Adw.SwitchRow, ResourceTarget] = {}
        self._active_result: CleanupResultDialog | None = None
        self._active_alert: Adw.AlertDialog | None = None

        self.set_content_width(720)
        self.set_content_height(660)
        self.set_can_close(True)
        self.set_child(self._build_ui())
        self._init_dialog_lifecycle()

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(title="Unused managed data")
        )
        close = Gtk.Button(label="Close")
        close.add_css_class("flat")
        self.connect_owned(close, "clicked", self._on_close_clicked)
        header.pack_start(close)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        if not self._scan.candidates and not self._scan.issues:
            status = Adw.StatusPage()
            status.set_icon_name("object-select-symbolic")
            status.set_title("No unused managed data")
            status.set_description(
                "Every directory under the MGL-managed roots is associated with "
                "a library game or an installer session."
            )
            toolbar.set_content(status)
            return toolbar

        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        candidates = Adw.PreferencesGroup(
            title="Orphan candidates",
            description=(
                "These directories are verified MGL-managed paths but no current "
                "game or active installer session references them. Nothing is "
                "deleted until you select it and confirm."
            ),
        )
        page.add(candidates)
        for target in self._scan.candidates:
            row = Adw.SwitchRow(title=_KIND_LABEL[target.kind])
            row.set_subtitle(
                f"{target.path}\n{target.game_id} · {_size_text(target.estimate)}"
            )
            row.set_subtitle_lines(3)
            row.set_tooltip_text(str(target.path))
            self.connect_owned(
                row, "notify::active", self._on_selection_changed
            )
            candidates.add(row)
            self._target_rows[row] = target

        if self._scan.issues:
            issues = Adw.PreferencesGroup(
                title="Not removable",
                description=(
                    "These entries are not safe deletion candidates. They are "
                    "reported only and cannot be selected."
                ),
            )
            page.add(issues)
            for issue in self._scan.issues:
                row = Adw.ActionRow(title=_KIND_LABEL[issue.kind])
                row.set_subtitle(f"{issue.path}\n{issue.reason}")
                row.set_subtitle_lines(3)
                row.set_sensitive(False)
                issues.add(row)

        toolbar.add_bottom_bar(self._build_action_bar())
        return toolbar

    def _build_action_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_top(8)
        bar.set_margin_bottom(8)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.set_halign(Gtk.Align.END)

        self._summary = Gtk.Label(label="")
        self._summary.add_css_class("dim-label")
        self._summary.set_hexpand(True)
        self._summary.set_xalign(0.0)
        bar.append(self._summary)

        self._delete_button = Gtk.Button(label="Delete selected")
        self._delete_button.add_css_class("destructive-action")
        self.connect_owned(
            self._delete_button, "clicked", self._on_delete_clicked
        )
        bar.append(self._delete_button)
        self._update_summary()
        return bar

    def selected_targets(self) -> list[ResourceTarget]:
        """Return the candidates whose switches are active."""
        return [
            target
            for row, target in self._target_rows.items()
            if row.get_active()
        ]

    def _on_selection_changed(
        self, _row: Adw.SwitchRow, _pspec: object
    ) -> None:
        if hasattr(self, "_summary"):
            self._update_summary()

    def _update_summary(self) -> None:
        selected = self.selected_targets()
        if selected:
            self._summary.set_text(
                f"{len(selected)} orphan resource"
                f"{'s' if len(selected) != 1 else ''} selected."
            )
        else:
            self._summary.set_text("Select one or more candidates to delete.")
        self._delete_button.set_sensitive(bool(selected))

    def confirmation_body(self) -> str:
        """Return the exact target list shown before deletion."""
        lines = []
        for target in self.selected_targets():
            lines.append(
                f"• {_KIND_LABEL[target.kind]}: {target.path}\n"
                f"  {_STATUS_LABEL[target.status]}, "
                f"{_size_text(target.estimate)}"
            )
        return "\n".join(lines) or "No targets selected."

    def _on_delete_clicked(self, _button: Gtk.Button) -> None:
        selected = self.selected_targets()
        if not selected:
            return
        dialog = Adw.AlertDialog(
            heading="Delete selected orphan data?",
            body=self.confirmation_body(),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Delete")
        dialog.set_response_appearance(
            "remove", Adw.ResponseAppearance.DESTRUCTIVE
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        self._active_alert = dialog
        dialog.choose(
            self._parent, None, self._on_confirmed, selected
        )

    def _on_confirmed(
        self,
        dialog: Adw.AlertDialog,
        result: object,
        selected: list[ResourceTarget],
    ) -> None:
        self._active_alert = None
        try:
            response = dialog.choose_finish(result)
        except GLib.Error:
            return
        if response != "remove":
            return
        try:
            report = self._cleanup.delete_targets(selected, authorized=True)
        except LauncherError as error:
            self._show_error(str(error))
            return
        except Exception as error:
            self._show_error(f"Cleanup could not be completed: {error}")
            return

        result_dialog = CleanupResultDialog(report)
        result_dialog.connect("closed", self._on_result_closed, report)
        self._active_result = result_dialog
        result_dialog.present(self._parent)

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_result_closed(
        self, _dialog: Adw.Dialog, report: CleanupReport
    ) -> None:
        self._finish(report)

    def _release_dialog_references(self) -> None:
        self._active_result = None
        self._active_alert = None
        self._target_rows.clear()

    def _finish(self, report: CleanupReport) -> None:
        self._active_result = None
        self.emit("finished", report)
        self.close()

    def _show_error(self, message: str) -> None:
        dialog = Adw.AlertDialog(heading="Cleanup failed", body=message)
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        self._active_alert = dialog
        dialog.choose(self._parent, None, self._on_error_closed)

    def _on_error_closed(
        self, dialog: Adw.AlertDialog, result: object
    ) -> None:
        self._active_alert = None
        try:
            dialog.choose_finish(result)
        except GLib.Error:
            pass


class CleanupResultDialog(Adw.Dialog, DialogSignalOwner):
    """Show successes, skips, partial failures, and remaining data."""

    def __init__(
        self,
        report: CleanupReport,
        *,
        record_selected: bool = False,
        record_removed: bool = False,
        record_error: str | None = None,
    ) -> None:
        super().__init__()
        self.report = report
        self.record_selected = record_selected
        self.record_removed = record_removed
        self.record_error = record_error
        self.set_content_width(640)
        self.set_content_height(520)
        self.set_can_close(True)
        self.set_child(self._build_ui())
        self._init_dialog_lifecycle()

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Cleanup results"))
        close = Gtk.Button(label="Close")
        self.connect_owned(close, "clicked", self._on_close_clicked)
        header.pack_start(close)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        record = Adw.PreferencesGroup(title="Library")
        page.add(record)
        if self.record_removed:
            row = Adw.ActionRow(title="Library entry removed")
            row.set_subtitle("All selected managed data was removed.")
        elif self.record_selected:
            row = Adw.ActionRow(title="Library entry kept")
            row.set_subtitle(
                self.record_error
                or "Data removal was incomplete, so the record was kept."
            )
        else:
            row = Adw.ActionRow(title="Library entry kept")
            row.set_subtitle("The library record was not selected for removal.")
        record.add(row)

        results = Adw.PreferencesGroup(title="Selected data")
        page.add(results)
        if not self.report.outcomes:
            row = Adw.ActionRow(title="No files were selected")
            row.set_subtitle("Only library metadata was handled.")
            results.add(row)
        else:
            for outcome in self.report.outcomes:
                results.add(self._build_outcome_row(outcome))

        return toolbar

    def _build_outcome_row(self, outcome: CleanupOutcome) -> Adw.ActionRow:
        target = outcome.target
        title = _KIND_LABEL[target.kind]
        if outcome.deleted:
            row = Adw.ActionRow(title=title)
            row.set_subtitle(
                f"Removed: {target.path}\n"
                f"{outcome.removed_entries} filesystem entries removed."
            )
            row.set_subtitle_lines(3)
            return row

        row = Adw.ActionRow(title=title)
        details = [str(target.path)]
        if outcome.skipped_reason:
            details.append(f"Skipped: {outcome.skipped_reason}")
        else:
            details.append("Removal failed or was incomplete.")
            details.extend(outcome.errors)
        if outcome.remaining is not None:
            details.append(
                f"Remaining: {_size_text(outcome.remaining)}"
            )
        row.set_subtitle("\n".join(details))
        row.set_subtitle_lines(5)
        return row

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def summary_text(self) -> str:
        """Return a plain-text result summary (also used by tests)."""
        lines: list[str] = []
        if self.record_removed:
            lines.append("Library entry removed.")
        elif self.record_selected:
            lines.append(
                "Library entry kept: "
                + (self.record_error or "data removal was incomplete.")
            )
        else:
            lines.append("Library entry kept.")
        for outcome in self.report.outcomes:
            target = outcome.target
            if outcome.deleted:
                lines.append(
                    f"Removed {_KIND_LABEL[target.kind]}: {target.path}"
                )
            elif outcome.skipped_reason:
                lines.append(
                    f"Skipped {_KIND_LABEL[target.kind]}: "
                    f"{outcome.skipped_reason}"
                )
            else:
                errors = "; ".join(outcome.errors) or "incomplete"
                lines.append(
                    f"Failed {_KIND_LABEL[target.kind]}: {errors}"
                )
                if outcome.remaining is not None:
                    lines.append(
                        f"Remaining: {_size_text(outcome.remaining)}"
                    )
        return "\n".join(lines)


def _size_text(estimate: SizeEstimate | None) -> str:
    if estimate is None:
        return "Size unavailable"
    text = _human_size(estimate.total_bytes)
    files = f"{estimate.file_count} files"
    if estimate.symlink_count:
        files += f", {estimate.symlink_count} links"
    if not estimate.complete:
        return f"At least {text} ({files}); estimate incomplete"
    return f"{text} ({files})"


def _human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


__all__ = [
    "CleanupDialog",
    "CleanupResultDialog",
    "OrphanCleanupDialog",
]
