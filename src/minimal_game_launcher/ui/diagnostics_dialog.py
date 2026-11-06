"""Diagnostic report dialog with summary and collapsible technical details."""

from __future__ import annotations

import logging
import re
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from minimal_game_launcher.models import Game  # noqa: E402
from minimal_game_launcher.ui.lifecycle import DialogSignalOwner  # noqa: E402
from minimal_game_launcher.services.diagnostics import (  # noqa: E402
    CheckStatus,
    DiagnosticCheck,
    DiagnosticReport,
    DiagnosticsService,
)

LOGGER = logging.getLogger(__name__)

_STATUS_ICONS = {
    CheckStatus.OK: "emblem-ok-symbolic",
    CheckStatus.INFO: "dialog-information-symbolic",
    CheckStatus.WARNING: "dialog-warning-symbolic",
    CheckStatus.PROBLEM: "dialog-error-symbolic",
    CheckStatus.UNKNOWN: "dialog-question-symbolic",
}


class DiagnosticsDialog(Adw.Dialog, DialogSignalOwner):
    """Run diagnostics off the main thread and present a local report.

    The dialog only reads state. Export writes the user-selected report file and
    never triggers network access.
    """

    def __init__(
        self,
        parent: Gtk.Window | None,
        game: Game,
        service: DiagnosticsService,
    ) -> None:
        super().__init__()
        self._parent = parent
        self._game = game
        self._service = service
        self._report: DiagnosticReport | None = None
        self._redact_paths = True
        self._generation = 0
        self._check_rows: list[Gtk.Widget] = []

        self.set_content_width(720)
        self.set_content_height(680)
        self.set_can_close(True)
        self.set_child(self._build_ui())
        self._init_dialog_lifecycle()
        self._regenerate()

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(
                title="Diagnostics",
                subtitle=self._game.name,
            )
        )
        close = Gtk.Button(label="Close")
        close.add_css_class("flat")
        self.connect_owned(close, "clicked", self._on_close_clicked)
        header.pack_start(close)

        self._export_button = Gtk.Button.new_from_icon_name(
            "document-save-symbolic"
        )
        self._export_button.set_tooltip_text("Export diagnostic report")
        self._export_button.set_sensitive(False)
        self.connect_owned(
            self._export_button, "clicked", self._on_export_clicked
        )
        header.pack_end(self._export_button)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        summary_group = Adw.PreferencesGroup(title="Summary")
        page.add(summary_group)
        self._summary_row = Adw.ActionRow(title="Checking…")
        self._summary_row.set_subtitle(
            "Read-only checks. No files are modified and the game is not "
            "started."
        )
        self._summary_spinner = Gtk.Spinner()
        self._summary_spinner.set_valign(Gtk.Align.CENTER)
        self._summary_row.add_prefix(self._summary_spinner)
        self._summary_icon = Gtk.Image()
        self._summary_icon.set_valign(Gtk.Align.CENTER)
        self._summary_icon.set_visible(False)
        self._summary_row.add_prefix(self._summary_icon)
        summary_group.add(self._summary_row)

        self._checks_group = Adw.PreferencesGroup(title="Checks")
        page.add(self._checks_group)

        report_group = Adw.PreferencesGroup(title="Report")
        page.add(report_group)
        paths_row = Adw.ActionRow(
            title="Show full paths in report",
            subtitle="Home-directory paths are shown as ~ by default.",
        )
        self._paths_switch = Gtk.Switch()
        self._paths_switch.set_valign(Gtk.Align.CENTER)
        self._paths_switch.set_active(False)
        self.connect_owned(
            self._paths_switch,
            "notify::active",
            self._on_path_visibility_changed,
        )
        paths_row.add_suffix(self._paths_switch)
        paths_row.set_activatable_widget(self._paths_switch)
        report_group.add(paths_row)

        self._export_row = Adw.ActionRow(title="Export")
        self._export_row.set_subtitle(
            "Export writes a local plain-text report. Nothing is uploaded."
        )
        report_group.add(self._export_row)

        return toolbar

    def _regenerate(self) -> None:
        """Start one background run and ignore results from older runs."""
        self._generation += 1
        generation = self._generation
        self._report = None
        self._export_button.set_sensitive(False)
        self._show_checking()
        redact_paths = self._redact_paths

        def worker() -> None:
            try:
                report = self._service.generate(
                    self._game, redact_paths=redact_paths
                )
            except Exception as error:  # The service isolates checks too.
                LOGGER.exception("Diagnostics failed for %s", self._game.name)
                GLib.idle_add(
                    self._apply_failure,
                    generation,
                    type(error).__name__,
                    str(error),
                )
                return
            GLib.idle_add(self._apply_report, generation, report)

        threading.Thread(
            target=worker,
            name=f"diagnostics-{self._game.id[:8]}",
            daemon=True,
        ).start()

    def _show_checking(self) -> None:
        self._summary_spinner.set_visible(True)
        self._summary_spinner.start()
        self._summary_icon.set_visible(False)
        self._summary_row.set_title("Running read-only checks…")
        self._summary_row.set_subtitle(
            "No files are modified and the game is not started."
        )
        self._clear_check_rows()

    def _clear_check_rows(self) -> None:
        for row in self._check_rows:
            self._checks_group.remove(row)
        self._check_rows = []

    def _apply_report(
        self, generation: int, report: DiagnosticReport
    ) -> bool:
        if generation != self._generation:
            return GLib.SOURCE_REMOVE
        self._report = report
        self._clear_check_rows()
        self._summary_spinner.stop()
        self._summary_spinner.set_visible(False)
        self._summary_icon.set_visible(True)
        self._summary_icon.set_from_icon_name(
            _STATUS_ICONS[report.overall_status]
        )
        self._summary_row.set_title(report.summary_line())
        self._summary_row.set_subtitle(
            "Generated locally. Read-only checks; no fixes were applied."
        )
        for check in report.checks:
            expander = self._build_check_row(check)
            self._checks_group.add(expander)
            self._check_rows.append(expander)
        self._export_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _apply_failure(
        self, generation: int, error_name: str, message: str
    ) -> bool:
        if generation != self._generation:
            return GLib.SOURCE_REMOVE
        self._clear_check_rows()
        self._summary_spinner.stop()
        self._summary_spinner.set_visible(False)
        self._summary_icon.set_visible(True)
        self._summary_icon.set_from_icon_name(
            _STATUS_ICONS[CheckStatus.UNKNOWN]
        )
        self._summary_row.set_title("Diagnostics could not be completed")
        self._summary_row.set_subtitle(
            "No changes were made. Try again or check the launcher log."
        )
        row = Adw.ActionRow(title="Unexpected error")
        row.set_subtitle(f"{error_name}: {message}")
        row.set_subtitle_selectable(True)
        row.set_subtitle_lines(0)
        self._checks_group.add(row)
        self._check_rows.append(row)
        return GLib.SOURCE_REMOVE

    def _build_check_row(self, check: DiagnosticCheck) -> Adw.ExpanderRow:
        expander = Adw.ExpanderRow(title=check.title)
        expander.set_subtitle(check.summary)
        expander.set_subtitle_lines(0)
        expander.set_title_lines(0)
        icon = Gtk.Image.new_from_icon_name(_STATUS_ICONS[check.status])
        icon.set_tooltip_text(check.status.label)
        expander.add_prefix(icon)

        has_detail = bool(check.suggestion or check.details)
        expander.set_enable_expansion(has_detail)
        if not has_detail:
            return expander

        if check.suggestion:
            suggestion = Adw.ActionRow(title="Suggestion")
            suggestion.set_subtitle(check.suggestion)
            suggestion.set_subtitle_selectable(True)
            suggestion.set_subtitle_lines(0)
            expander.add_row(suggestion)
        for detail in check.details:
            row = Adw.ActionRow(title=detail)
            row.set_title_lines(0)
            row.add_css_class("monospace")
            expander.add_row(row)
        return expander

    def _on_path_visibility_changed(
        self, switch: Gtk.Switch, _pspec: object
    ) -> None:
        self._redact_paths = not switch.get_active()
        self._regenerate()

    def _on_export_clicked(self, _button: Gtk.Button) -> None:
        if self._report is None:
            return
        dialog = Gtk.FileDialog()
        dialog.set_title("Export diagnostic report")
        dialog.set_initial_name(self._default_filename())
        parent = self.get_root() or self._parent
        dialog.save(parent, None, self._on_export_finished)

    def _on_export_finished(
        self, dialog: Gtk.FileDialog, result: object
    ) -> None:
        try:
            selected = dialog.save_finish(result)
        except GLib.Error:
            return
        path = selected.get_path()
        if not path:
            self._export_row.set_subtitle("The selected location is unusable.")
            return
        try:
            if self._report is None:
                raise OSError("No diagnostic report is available.")
            self._report.save(path)
        except OSError as error:
            self._export_row.set_subtitle(f"Report could not be saved: {error}")
            return
        self._export_row.set_subtitle(f"Saved to {path}")

    def _default_filename(self) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", self._game.name).strip("-")
        return f"{slug or 'game'}-diagnostics.txt"

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _release_dialog_references(self) -> None:
        # Invalidate any worker result that has not reached the main loop yet.
        self._generation += 1
        self._report = None
        self._check_rows = []
