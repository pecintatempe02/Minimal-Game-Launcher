"""GTK dialogs for installing a game from an ISO image or an executable.

The dialog is a thin presentation layer. Mounting, copying, verification,
discovery, ownership, and persistence belong to ``InstallerManager``. The
installer runs through the existing ``LaunchManager`` with the game's stable
WINEPREFIX; no dependency or DXVK setup is performed here.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

from minimal_game_launcher.errors import LauncherError  # noqa: E402
from minimal_game_launcher.models import (  # noqa: E402
    Game,
    InstallerSession,
    InstallerSourceKind,
    InstallerStatus,
)
from minimal_game_launcher.services.game_manager import GameManager  # noqa: E402
from minimal_game_launcher.services.executable_discovery import (  # noqa: E402
    ExecutableCandidate,
)
from minimal_game_launcher.services.installer_manager import (  # noqa: E402
    CandidateKind,
    InstallerCandidate,
    InstallerManager,
    PendingRemovalReport,
)
from minimal_game_launcher.services.ownership import ResourceKind  # noqa: E402
from minimal_game_launcher.services.launch_manager import (  # noqa: E402
    LaunchManager,
)
from minimal_game_launcher.ui.lifecycle import DialogSignalOwner  # noqa: E402

LOGGER = logging.getLogger(__name__)

_ISO_PATTERNS = ("*.iso", "*.ISO", "*.img", "*.IMG")
_EXE_PATTERNS = ("*.exe", "*.EXE")


def _source_label(kind: InstallerSourceKind) -> str:
    """Return the short noun used in dialog text for a source kind."""
    return "ISO" if kind is InstallerSourceKind.ISO else "executable"


def _source_kind_label(kind: InstallerSourceKind) -> str:
    """Return the user-facing source description."""
    return "ISO image" if kind is InstallerSourceKind.ISO else "installer executable"


class InstallerDialog(Adw.Dialog, DialogSignalOwner):
    """Guide one installation through staging, install, and registration.

    The dialog is source-aware: an ISO session mounts and copies an image,
    while an executable session copies the one selected installer file. Both
    share the same session, prefix, launch, discovery, and registration paths.
    """

    __gsignals__ = {
        "installed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(
        self,
        parent: Gtk.Window,
        installer_manager: InstallerManager,
        game_manager: GameManager,
        launch_manager: LaunchManager,
        *,
        session: InstallerSession | None = None,
        source_kind: InstallerSourceKind = InstallerSourceKind.ISO,
    ) -> None:
        super().__init__()
        self._parent = parent
        self._installers = installer_manager
        self._games = game_manager
        self._launch_manager = launch_manager
        self._session = session
        self._source_kind = (
            session.source_kind if session is not None else source_kind
        )
        self._source_path: str | None = (
            session.source_path if session else None
        )
        self._destination_mode = self._initial_destination_mode(session)
        self._external_destination: str | None = (
            session.install_directory
            if session
            and session.install_directory
            and not session.install_directory_owned
            else None
        )
        self._candidates: dict[Gtk.ListBoxRow, InstallerCandidate] = {}
        self._other_rows: list[Adw.ActionRow] = []
        self._selected_candidate: InstallerCandidate | None = None
        self._discovery_rows: dict[Gtk.ListBoxRow, ExecutableCandidate] = {}
        self._selected_executable: ExecutableCandidate | None = None
        self._active_file_dialog: Gtk.FileDialog | None = None
        self._active_error: Adw.AlertDialog | None = None
        self._closed = False
        self._launch_finished_id = 0
        self._launch_failed_id = 0
        self._page = "setup"

        self.set_content_width(700)
        self.set_content_height(720)
        self.set_can_close(True)
        self.set_child(self._build_ui())
        self._init_dialog_lifecycle()
        self._apply_destination_mode()

        self._launch_finished_id = self._launch_manager.connect(
            "launch-finished", self._on_launch_finished
        )
        self._launch_failed_id = self._launch_manager.connect(
            "launch-failed", self._on_launch_failed
        )

        if self._session is not None:
            GLib.idle_add(self._resume_session)

    @property
    def _source_kind_is_iso(self) -> bool:
        """Return whether this dialog stages from an ISO image."""
        return self._source_kind is InstallerSourceKind.ISO

    # ------------------------------------------------------------------
    # Construction

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(
                title=f"Install from {_source_label(self._source_kind)}",
                subtitle=(
                    "Read-only mount"
                    if self._source_kind_is_iso
                    else "Managed copy of one executable"
                ),
            )
        )
        close = Gtk.Button(label="Close")
        close.add_css_class("flat")
        self.connect_owned(close, "clicked", self._on_close_clicked)
        header.pack_start(close)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(120)
        self._stack.add_named(self._build_setup_page(), "setup")
        self._stack.add_named(self._build_candidates_page(), "candidates")
        self._stack.add_named(self._build_finish_page(), "finish")
        toolbar.set_content(self._stack)
        toolbar.add_bottom_bar(self._build_action_bar())
        return toolbar

    def _build_setup_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        source = Adw.PreferencesGroup(
            title=_source_kind_label(self._source_kind),
            description=(
                "The image is mounted read-only without root privileges and "
                "its complete contents are copied to MGL staging. The original "
                "ISO is never modified or deleted."
                if self._source_kind_is_iso
                else "The selected Windows installer is copied into MGL-managed "
                "staging. Only this file is copied; companion .cab, .bin, or "
                "other data files beside it are not. The original file is kept."
            ),
        )
        page.add(source)

        self._iso_row = Adw.ActionRow(
            title=_source_kind_label(self._source_kind)
        )
        self._iso_row.set_subtitle(
            self._source_path
            or f"No {_source_kind_label(self._source_kind)} selected"
        )
        self._iso_row.set_subtitle_selectable(True)
        choose_iso = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        choose_iso.set_tooltip_text(
            f"Choose {_source_kind_label(self._source_kind)}"
        )
        choose_iso.set_valign(Gtk.Align.CENTER)
        self.connect_owned(choose_iso, "clicked", self._choose_source)
        self._iso_row.add_suffix(choose_iso)
        source.add(self._iso_row)

        details = Adw.PreferencesGroup(title="Game")
        page.add(details)
        self._name_row = Adw.EntryRow(title="Game name")
        if self._session is not None:
            self._name_row.set_text(self._session.game_name)
        details.add(self._name_row)

        destination = Adw.PreferencesGroup(
            title="Installation destination",
            description=(
                "The installer's own default writes into the game's Wine "
                "prefix and needs no games folder. The MGL-managed destination "
                "is the only path MGL may treat as owned. External destinations "
                "stay non-owned and are never deleted by MGL."
            ),
        )
        page.add(destination)

        self._destination_row = Adw.ComboRow(title="Destination type")
        self._destination_row.set_model(
            Gtk.StringList.new(
                [
                    "Wine prefix (installer default)",
                    "MGL-managed game directory",
                    "External folder",
                ]
            )
        )
        self.connect_owned(
            self._destination_row,
            "notify::selected",
            self._on_destination_mode_changed,
        )
        destination.add(self._destination_row)

        self._external_row = Adw.ActionRow(title="External folder")
        self._external_row.set_subtitle(
            self._external_destination or "No folder selected"
        )
        self._external_row.set_subtitle_selectable(True)
        choose_folder = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        choose_folder.set_tooltip_text("Choose external folder")
        choose_folder.set_valign(Gtk.Align.CENTER)
        self.connect_owned(
            choose_folder, "clicked", self._choose_external_destination
        )
        self._external_row.add_suffix(choose_folder)
        self._external_row.set_visible(False)
        destination.add(self._external_row)

        notes = Adw.PreferencesGroup(title="Safety")
        page.add(notes)
        for title, subtitle in self._safety_notes():
            row = Adw.ActionRow(title=title)
            row.set_subtitle(subtitle)
            notes.add(row)
        return page

    def _safety_notes(self) -> tuple[tuple[str, str], ...]:
        """Return the source-specific safety notes shown on the setup page."""
        if self._source_kind_is_iso:
            return (
                (
                    "Full contents",
                    "Every file and directory is copied, not only setup.exe. The "
                    "copy is verified before the ISO is unmounted.",
                ),
                (
                    "No automatic installer",
                    "Nothing is executed until you select an installer and press "
                    "Run installer.",
                ),
                (
                    "No automatic dependencies",
                    "DXVK, Winetricks components, and runtimes are not installed.",
                ),
                (
                    "Keep the disc",
                    "The original ISO is kept. Some games may still require the "
                    "disc or image at runtime.",
                ),
            )
        return (
            (
                "Copied into MGL staging",
                "The selected executable is copied into the game's MGL-managed "
                "installer directory and verified. The original file stays where "
                "it is.",
            ),
            (
                "Not a safety check",
                "A Windows MZ signature does not establish that the file is "
                "safe, authentic, or a working installer. MGL does not scan it.",
            ),
            (
                "No automatic installer",
                "Nothing is executed until you press Run installer.",
            ),
            (
                "No companion files",
                "Only the selected file is copied. Standalone installers that "
                "need companion .cab, .bin, or data files are not supported in "
                "this mode; use an ISO instead.",
            ),
            (
                "No automatic dependencies",
                "DXVK, Winetricks components, and runtimes are not installed.",
            ),
        )

    def _build_candidates_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        summary = Adw.PreferencesGroup(title="Staged copy")
        page.add(summary)
        self._staging_row = Adw.ActionRow(title="Staging directory")
        self._staging_row.set_subtitle_selectable(True)
        summary.add(self._staging_row)
        self._destination_summary = Adw.ActionRow(title="Install destination")
        self._destination_summary.set_subtitle_selectable(True)
        summary.add(self._destination_summary)

        candidates = Adw.PreferencesGroup(
            title="Installer candidates",
            description=(
                "Discovery is name- and content-based and does not guarantee "
                "installer compatibility. Select one explicitly."
            ),
        )
        page.add(candidates)
        self._candidate_list = Gtk.ListBox()
        self._candidate_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._candidate_list.add_css_class("boxed-list")
        self.connect_owned(
            self._candidate_list, "row-selected", self._on_candidate_selected
        )
        candidates.add(self._candidate_list)

        self._other_group = Adw.PreferencesGroup(
            title="Other executables",
            description=(
                "These entries are shown for context only. They are not run by "
                "this installer step."
            ),
        )
        page.add(self._other_group)
        return page

    def _build_finish_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        self._finish_group = Adw.PreferencesGroup(title="Installation")
        page.add(self._finish_group)
        self._finish_row = Adw.ActionRow(title="Installer finished")
        self._finish_row.set_subtitle(
            "Select the actual game executable to add it to the library."
        )
        self._finish_group.add(self._finish_row)

        discovery = Adw.PreferencesGroup(
            title="Game executable",
            description=(
                "Read-only scan of the installation folder and the Wine "
                "prefix. Nothing is executed automatically; confirm one "
                "candidate to register it."
            ),
        )
        page.add(discovery)
        self._discovery_status = Adw.ActionRow(
            title="Looking for game executables…"
        )
        self._discovery_status.set_subtitle(
            "Searching the installation folder and the Wine prefix."
        )
        discovery.add(self._discovery_status)
        self._discovery_list = Gtk.ListBox()
        self._discovery_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._discovery_list.add_css_class("boxed-list")
        self.connect_owned(
            self._discovery_list,
            "row-selected",
            self._on_discovery_selected,
        )
        discovery.add(self._discovery_list)

        kept = Adw.PreferencesGroup(title="Preserved files")
        page.add(kept)
        self._kept_row = Adw.ActionRow(title="Installer source")
        self._kept_row.set_subtitle_selectable(True)
        kept.add(self._kept_row)
        original_row = Adw.ActionRow(
            title=(
                "Original ISO"
                if self._source_kind_is_iso
                else "Original installer"
            )
        )
        original_row.set_subtitle(
            self._source_path or f"No {_source_kind_label(self._source_kind)}"
        )
        original_row.set_subtitle_selectable(True)
        kept.add(original_row)
        note = Adw.ActionRow(title="Nothing is deleted automatically")
        note.set_subtitle(
            "Installer files remain available until you explicitly choose to "
            "remove them in a later cleanup step."
        )
        kept.add(note)
        return page

    def _build_action_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bar.set_margin_top(8)
        bar.set_margin_bottom(8)
        bar.set_margin_start(12)
        bar.set_margin_end(12)

        self._progress = Gtk.ProgressBar()
        self._progress.set_show_text(True)
        self._progress.set_visible(False)
        bar.append(self._progress)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        self._defer_button = Gtk.Button(label="Finish later")
        self._defer_button.set_visible(False)
        self.connect_owned(
            self._defer_button, "clicked", self._on_defer_clicked
        )
        buttons.append(self._defer_button)

        self._manual_button = Gtk.Button(label="Choose another executable…")
        self._manual_button.set_visible(False)
        self.connect_owned(
            self._manual_button, "clicked", self._choose_game_executable
        )
        buttons.append(self._manual_button)

        self._primary_button = Gtk.Button(label="Mount and copy")
        self._primary_button.add_css_class("suggested-action")
        self.connect_owned(
            self._primary_button, "clicked", self._on_primary_clicked
        )
        buttons.append(self._primary_button)
        bar.append(buttons)
        return bar

    # ------------------------------------------------------------------
    # Setup actions

    def _choose_source(self, _button: Gtk.Button) -> None:
        kind = _source_kind_label(self._source_kind)
        dialog = Gtk.FileDialog(title=f"Select {kind}", modal=True)
        file_filter = Gtk.FileFilter()
        if self._source_kind_is_iso:
            file_filter.set_name("Disc images (*.iso, *.img)")
            patterns = _ISO_PATTERNS
        else:
            file_filter.set_name("Windows installers (*.exe)")
            patterns = _EXE_PATTERNS
        for pattern in patterns:
            file_filter.add_pattern(pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(file_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(file_filter)
        if self._source_path:
            folder = Path(self._source_path).expanduser().parent
            if folder.is_dir():
                dialog.set_initial_folder(Gio.File.new_for_path(str(folder)))
        self._active_file_dialog = dialog
        dialog.open(self._parent, None, self._on_source_selected)

    def _on_source_selected(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult
    ) -> None:
        self._active_file_dialog = None
        try:
            selected = dialog.open_finish(result)
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._show_error(error.message)
            return
        path = selected.get_path()
        if not path:
            self._show_error(
                f"Only local {_source_kind_label(self._source_kind)} files "
                "can be selected."
            )
            return
        self._source_path = path
        self._iso_row.set_subtitle(path)

    def _choose_external_destination(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Select installation folder", modal=True)
        if self._external_destination:
            folder = Path(self._external_destination).expanduser()
            if folder.is_dir():
                dialog.set_initial_folder(Gio.File.new_for_path(str(folder)))
        self._active_file_dialog = dialog
        dialog.select_folder(
            self._parent, None, self._on_external_destination_selected
        )

    def _on_external_destination_selected(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult
    ) -> None:
        self._active_file_dialog = None
        try:
            selected = dialog.select_folder_finish(result)
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._show_error(error.message)
            return
        path = selected.get_path()
        if not path:
            self._show_error("Only local folders can be selected.")
            return
        self._external_destination = path
        self._external_row.set_subtitle(path)

    @staticmethod
    def _initial_destination_mode(session: InstallerSession | None) -> str:
        if session is None:
            return "wine"
        if session.install_directory_owned:
            return "managed"
        if session.install_directory:
            return "external"
        return "wine"

    def _apply_destination_mode(self) -> None:
        selected = {"wine": 0, "managed": 1, "external": 2}.get(
            self._destination_mode, 0
        )
        self._destination_row.set_selected(selected)
        self._external_row.set_visible(self._destination_mode == "external")

    def _on_destination_mode_changed(
        self, _row: Adw.ComboRow, _pspec: object
    ) -> None:
        selected = self._destination_row.get_selected()
        self._destination_mode = {0: "wine", 1: "managed", 2: "external"}.get(
            selected, "wine"
        )
        self._external_row.set_visible(self._destination_mode == "external")

    def _on_primary_clicked(self, _button: Gtk.Button) -> None:
        if self._page == "setup":
            self._start_prepare()
        elif self._page == "candidates":
            self._run_installer()
        else:
            self._register_selected_executable()

    def _set_page(self, name: str) -> None:
        self._page = name
        self._stack.set_visible_child_name(name)
        self._defer_button.set_visible(name in {"candidates", "finish"})
        self._defer_button.set_label(
            "Finish later" if name == "candidates" else "Register later"
        )
        self._manual_button.set_visible(name == "finish")
        if name == "setup":
            self._primary_button.set_label(
                "Mount and copy" if self._source_kind_is_iso else "Copy installer"
            )
        elif name == "candidates":
            self._primary_button.set_label("Run installer")
        else:
            self._primary_button.set_label("Register selected executable")
            self._primary_button.set_sensitive(
                self._selected_executable is not None
            )

    # ------------------------------------------------------------------
    # Staging

    def _start_prepare(self) -> None:
        if not self._source_path:
            self._show_error(
                f"Choose an {_source_kind_label(self._source_kind)} first."
            )
            return
        name = self._name_row.get_text().strip()
        if not name:
            self._show_error(
                f"Enter a game name before staging the "
                f"{_source_kind_label(self._source_kind)}."
            )
            return
        if self._destination_mode == "external" and not self._external_destination:
            self._show_error("Choose an external installation folder.")
            return
        if self._session is None:
            try:
                if self._source_kind_is_iso:
                    self._session = self._installers.create_session(
                        self._source_path, name
                    )
                else:
                    self._session = self._installers.create_executable_session(
                        self._source_path, name
                    )
            except LauncherError as error:
                self._show_error(str(error))
                return
            except Exception as error:
                LOGGER.exception("Could not create installer session")
                self._show_error(f"The installer session could not be created: {error}")
                return
        else:
            self._session = replace(self._session, game_name=name)

        try:
            self._installers.resolve_destination(
                self._session,
                managed=self._destination_mode == "managed",
                external_path=(
                    self._external_destination
                    if self._destination_mode == "external"
                    else None
                ),
                create=False,
                wine_default=self._destination_mode == "wine",
            )
        except LauncherError as error:
            self._show_error(str(error))
            return

        self._session = self._installers.save_session(
            replace(
                self._session,
                status=InstallerStatus.PENDING,
                message=(
                    "Mounting the ISO and copying its contents…"
                    if self._source_kind_is_iso
                    else "Copying the installer executable into managed staging…"
                ),
            )
        )
        self._primary_button.set_sensitive(False)
        self._defer_button.set_sensitive(False)
        self._progress.set_visible(True)
        self._progress.set_fraction(0.0)
        self._progress.set_text("Preparing…")
        session = self._session
        thread = threading.Thread(
            target=self._prepare_worker,
            args=(session,),
            name=(
                f"iso-stage-{session.game_id[:8]}"
                if self._source_kind_is_iso
                else f"exe-stage-{session.game_id[:8]}"
            ),
            daemon=True,
        )
        thread.start()

    def _prepare_worker(self, session: InstallerSession) -> None:
        try:
            prepared = self._installers.prepare(
                session, progress=self._on_progress
            )
        except Exception as error:
            LOGGER.exception("Installer staging failed for %s", session.game_id)
            GLib.idle_add(self._on_prepare_failed, session, str(error))
            return
        GLib.idle_add(self._on_prepared, session, prepared)

    def _on_progress(self, text: str, fraction: float) -> None:
        GLib.idle_add(self._update_progress, text, fraction)

    def _update_progress(self, text: str, fraction: float) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._progress.set_fraction(fraction)
        self._progress.set_text(text)
        return GLib.SOURCE_REMOVE

    def _on_prepare_failed(
        self, session: InstallerSession, message: str
    ) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._primary_button.set_sensitive(True)
        self._defer_button.set_sensitive(True)
        self._progress.set_visible(False)
        self._session = self._installers.save_session(
            replace(
                session,
                status=InstallerStatus.FAILED,
                message=message,
            )
        )
        self._show_error(message)
        return GLib.SOURCE_REMOVE

    def _on_prepared(
        self, session: InstallerSession, prepared: InstallerSession
    ) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        try:
            destination = self._installers.resolve_destination(
                prepared,
                managed=self._destination_mode == "managed",
                external_path=(
                    self._external_destination
                    if self._destination_mode == "external"
                    else None
                ),
                create=self._destination_mode != "wine",
                wine_default=self._destination_mode == "wine",
            )
            candidates = self._installers.discover(prepared)
        except LauncherError as error:
            self._on_prepare_failed(prepared, str(error))
            return GLib.SOURCE_REMOVE

        self._session = self._installers.save_session(
            replace(
                prepared,
                install_directory=(
                    str(destination.path) if destination.path else None
                ),
                install_directory_owned=destination.owned,
                status=InstallerStatus.STAGED,
                message=(
                    "ISO contents staged and verified. Select an installer."
                    if prepared.source_kind_is_iso
                    else "Installer executable staged and verified. "
                    "Review it, then run the installer."
                ),
            )
        )
        self._staging_row.set_subtitle(self._session.staging_directory)
        self._destination_summary.set_subtitle(
            (
                f"{destination.path} "
                f"({'MGL-managed' if destination.owned else 'external'})"
            )
            if destination.path
            else (
                "Wine prefix (installer default): "
                f"{self._installers.prefix_path(prepared)}"
            )
        )
        self._populate_candidates(candidates)
        self._progress.set_visible(False)
        self._primary_button.set_sensitive(True)
        self._defer_button.set_sensitive(True)
        self._set_page("candidates")
        return GLib.SOURCE_REMOVE

    def _populate_candidates(self, candidates: list[InstallerCandidate]) -> None:
        for row in list(self._candidates):
            self._candidate_list.remove(row)
        self._candidates = {}
        self._selected_candidate = None

        installers = [
            candidate
            for candidate in candidates
            if candidate.kind is CandidateKind.INSTALLER
        ]
        others = [
            candidate
            for candidate in candidates
            if candidate.kind is CandidateKind.GAME
        ]

        if installers:
            for candidate in installers:
                action = Adw.ActionRow(title=Path(candidate.relative_path).name)
                action.set_subtitle(
                    f"{candidate.relative_path}\n{candidate.reason}"
                )
                action.set_subtitle_lines(2)
                action.set_tooltip_text(candidate.relative_path)
                self._candidate_list.append(action)
                self._candidates[action] = candidate
        else:
            action = Adw.ActionRow(
                title=(
                    "No installer-like executable found"
                    if self._source_kind_is_iso
                    else "The staged installer could not be found"
                )
            )
            action.set_subtitle(
                "You can finish later and inspect the staged files manually. "
                "Nothing is executed automatically."
            )
            action.set_activatable(False)
            action.set_selectable(False)
            self._candidate_list.append(action)

        for row in self._other_rows:
            self._other_group.remove(row)
        self._other_rows = []
        for candidate in others[:50]:
            row = Adw.ActionRow(title=Path(candidate.relative_path).name)
            row.set_subtitle(f"{candidate.relative_path}\n{candidate.reason}")
            row.set_subtitle_lines(2)
            self._other_group.add(row)
            self._other_rows.append(row)
        self._other_group.set_visible(bool(others))

        if not self._source_kind_is_iso and len(self._candidates) == 1:
            # The user already chose this file; preselect it but never run it.
            self._candidate_list.select_row(next(iter(self._candidates)))

        self._set_page("candidates")

    def _on_candidate_selected(
        self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None
    ) -> None:
        self._selected_candidate = self._candidates.get(row) if row else None

    # ------------------------------------------------------------------
    # Installer execution and completion

    def _run_installer(self) -> None:
        if self._session is None or self._selected_candidate is None:
            self._show_error("Select an installer candidate first.")
            return
        try:
            self._session = self._installers.save_session(
                self._installers.select_installer(
                    self._session, self._selected_candidate.relative_path
                )
            )
            game = self._installers.installer_game(self._session)
        except LauncherError as error:
            self._show_error(str(error))
            return

        # Persist INSTALLING before the process can report back, so a finish or
        # failure signal always lands on a session that already says "installing".
        previous = self._session
        self._session = self._installers.save_session(
            replace(
                previous,
                status=InstallerStatus.INSTALLING,
                message=f"Installer started: {previous.installer_relative_path}",
            )
        )
        try:
            started = self._launch_manager.launch(game)
        except Exception as error:
            self._restore_session(previous, str(error))
            raise
        if not started:
            self._restore_session(
                previous,
                "The installer did not start because the game is already "
                "starting or running.",
            )
            self._show_error(
                "This game is already starting or running, so the installer "
                "was not started."
            )
            return
        self._primary_button.set_sensitive(False)
        self._defer_button.set_sensitive(False)
        self._progress.set_visible(True)
        self._progress.set_fraction(0.65)
        self._progress.set_text(
            "Installer running in its own Wine prefix…"
        )

    def _restore_session(self, previous: InstallerSession, message: str) -> None:
        """Put a session back to its pre-launch status and explain why."""
        self._session = self._installers.save_session(
            replace(previous, message=message)
        )

    def _on_launch_finished(
        self, _manager: LaunchManager, game_id: str, exit_code: int
    ) -> None:
        if self._session is None or game_id != self._session.game_id:
            return
        self._session = self._installers.save_session(
            replace(
                self._session,
                status=InstallerStatus.INSTALLER_FINISHED,
                message=f"Installer exited with code {exit_code}.",
            )
        )
        if self._closed:
            return
        self._progress.set_visible(False)
        self._finish_row.set_subtitle(
            f"The installer exited with code {exit_code}. Choose the game "
            "executable that was installed."
        )
        self._kept_row.set_subtitle(self._session.staging_directory)
        self._set_page("finish")
        self._start_executable_discovery()

    def _on_launch_failed(
        self, _manager: LaunchManager, game_id: str, message: str
    ) -> None:
        if self._session is None or game_id != self._session.game_id:
            return
        self._session = self._installers.save_session(
            replace(
                self._session,
                status=InstallerStatus.FAILED,
                message=message,
            )
        )
        if self._closed:
            return
        self._progress.set_visible(False)
        self._primary_button.set_sensitive(True)
        self._defer_button.set_sensitive(True)
        self._show_error(message, heading="Installer could not start")

    def _on_defer_clicked(self, _button: Gtk.Button) -> None:
        if self._session is None:
            self.close()
            return
        if self._page == "finish":
            # Registration is optional. Closing here changes neither the game
            # record nor the session status, so a later resume can still finish.
            self.close()
            return
        self._session = self._installers.save_session(
            replace(
                self._session,
                status=InstallerStatus.DEFERRED,
                message="Installation deferred; staged files are preserved.",
            )
        )
        self.close()

    # ------------------------------------------------------------------
    # Executable discovery and registration

    def _start_executable_discovery(self) -> None:
        if self._session is None:
            return
        self._discovery_status.set_title("Looking for game executables…")
        self._discovery_status.set_subtitle(
            "Searching the installation folder and the Wine prefix."
        )
        self._clear_discovery_rows()
        session = self._session
        thread = threading.Thread(
            target=self._discovery_worker,
            args=(session,),
            name=f"executable-discovery-{session.game_id[:8]}",
            daemon=True,
        )
        thread.start()

    def _discovery_worker(self, session: InstallerSession) -> None:
        try:
            candidates = self._installers.discover_game_executables(session)
        except Exception as error:
            LOGGER.exception(
                "Executable discovery failed for %s", session.game_id
            )
            GLib.idle_add(self._on_discovery_failed, session.game_id, str(error))
            return
        GLib.idle_add(self._on_discovery_ready, session.game_id, candidates)

    def _on_discovery_failed(self, game_id: str, message: str) -> bool:
        if self._closed or self._session is None or game_id != self._session.game_id:
            return GLib.SOURCE_REMOVE
        self._discovery_status.set_title("No executables could be found")
        self._discovery_status.set_subtitle(
            f"The scan could not finish: {message}. Choose one manually."
        )
        return GLib.SOURCE_REMOVE

    def _on_discovery_ready(
        self, game_id: str, candidates: list[ExecutableCandidate]
    ) -> bool:
        if self._closed or self._session is None or game_id != self._session.game_id:
            return GLib.SOURCE_REMOVE
        self._clear_discovery_rows()
        if not candidates:
            self._discovery_status.set_title("No likely game executable found")
            self._discovery_status.set_subtitle(
                "Nothing was selected automatically. Choose the installed "
                "executable manually."
            )
            return GLib.SOURCE_REMOVE

        self._discovery_status.set_title(
            f"{len(candidates)} possible executable"
            f"{'' if len(candidates) == 1 else 's'} found"
        )
        self._discovery_status.set_subtitle(
            "Candidates are ranked by location and name. Select one to "
            "register; nothing is launched."
        )
        for candidate in candidates:
            row = Adw.ActionRow(title=candidate.path.name)
            row.set_subtitle(
                f"{candidate.relative_path} · {candidate.location}\n"
                f"{candidate.reason}"
            )
            row.set_subtitle_lines(2)
            row.set_tooltip_text(str(candidate.path))
            if candidate.recommended:
                badge = Gtk.Label(label="Recommended")
                badge.add_css_class("accent")
                badge.set_valign(Gtk.Align.CENTER)
                row.add_suffix(badge)
            self._discovery_list.append(row)
            self._discovery_rows[row] = candidate
        self._primary_button.set_sensitive(False)
        return GLib.SOURCE_REMOVE

    def _clear_discovery_rows(self) -> None:
        for row in list(self._discovery_rows):
            self._discovery_list.remove(row)
        self._discovery_rows = {}
        self._selected_executable = None
        if self._page == "finish":
            self._primary_button.set_sensitive(False)

    def _on_discovery_selected(
        self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None
    ) -> None:
        self._selected_executable = self._discovery_rows.get(row) if row else None
        self._primary_button.set_sensitive(self._selected_executable is not None)

    def _register_selected_executable(self) -> None:
        if self._session is None or self._selected_executable is None:
            self._show_error("Select one discovered executable first.")
            return
        self._register_executable(self._selected_executable.path)

    def _choose_game_executable(self, _button: Gtk.Button | None = None) -> None:
        if self._session is None:
            self.close()
            return
        dialog = Gtk.FileDialog(title="Select installed game executable", modal=True)
        file_filter = Gtk.FileFilter()
        file_filter.set_name("Windows executables (*.exe)")
        file_filter.add_pattern("*.exe")
        file_filter.add_pattern("*.EXE")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(file_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(file_filter)
        initial = self._executable_dialog_folder()
        if initial is not None:
            dialog.set_initial_folder(Gio.File.new_for_path(str(initial)))
        self._active_file_dialog = dialog
        dialog.open(self._parent, None, self._on_game_executable_selected)

    def _executable_dialog_folder(self) -> Path | None:
        if self._session is None:
            return None
        candidates = [
            self._session.install_directory,
            str(self._installers.prefix_path(self._session) / "drive_c"),
            self._session.staging_directory,
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_dir():
                return Path(candidate)
        return None

    def _on_game_executable_selected(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult
    ) -> None:
        self._active_file_dialog = None
        try:
            selected = dialog.open_finish(result)
        except GLib.Error as error:
            if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self._show_error(error.message)
            return
        path = selected.get_path()
        if not path:
            self._show_error("Only local game executables can be selected.")
            return
        self._register_executable(Path(path))

    def _register_executable(self, path: Path) -> None:
        if self._session is None:
            return
        existing = self._games.get_game(self._session.game_id)
        draft = self._installers.installation_draft(
            self._session, path, existing
        )
        try:
            game = self._games.register_installation(
                self._session.game_id,
                draft,
                install_directory=self._session.install_directory,
                install_directory_owned=self._session.install_directory_owned,
                installer_source_directory=self._session.staging_directory,
                installer_source_owned=True,
                wine_prefix_path=str(self._installers.prefix_path(self._session)),
                wine_prefix_shared=self._session.wine_prefix_shared,
            )
        except LauncherError as error:
            self._show_error(str(error), heading="Game could not be registered")
            return
        except OSError as error:
            self._show_error(
                f"The game could not be registered: {error}",
                heading="Game could not be registered",
            )
            return

        self._session = self._installers.save_session(
            replace(
                self._session,
                status=InstallerStatus.INSTALLED,
                message=f"Registered game executable: {game.executable}",
            )
        )
        self.emit("installed", game)
        self.close()

    # ------------------------------------------------------------------
    # Resume

    def _resume_session(self) -> bool:
        if self._session is None or self._closed:
            return GLib.SOURCE_REMOVE
        session = self._session
        if session.status is InstallerStatus.INSTALLED:
            self._finish_row.set_subtitle(
                "This installation is already registered in the library."
            )
            self._primary_button.set_sensitive(False)
            self._set_page("finish")
            return GLib.SOURCE_REMOVE
        if session.status is InstallerStatus.INSTALLER_FINISHED:
            self._finish_row.set_subtitle(
                session.message
                or "The installer has finished. Choose the game executable."
            )
            self._kept_row.set_subtitle(session.staging_directory)
            self._set_page("finish")
            self._start_executable_discovery()
            return GLib.SOURCE_REMOVE

        staging = Path(session.staging_directory)
        source = staging / "source"
        if not source.is_dir():
            self._finish_row.set_subtitle(
                "No staged contents exist yet. "
                + (
                    "Mount and copy the ISO to continue this session."
                    if session.source_kind_is_iso
                    else "Copy the installer executable to continue this session."
                )
            )
            self._set_page("setup")
            return GLib.SOURCE_REMOVE

        if self._launch_manager.is_active(session.game_id):
            self._primary_button.set_sensitive(False)
            self._defer_button.set_sensitive(False)
            self._progress.set_visible(True)
            self._progress.set_fraction(0.65)
            self._progress.set_text("Installer is still running…")
            self._set_page("candidates")
            return GLib.SOURCE_REMOVE

        self._primary_button.set_sensitive(False)
        self._defer_button.set_sensitive(False)
        self._progress.set_visible(True)
        self._progress.set_fraction(0.2)
        self._progress.set_text("Verifying staged copy…")
        thread = threading.Thread(
            target=self._resume_worker,
            args=(session,),
            name=(
                f"iso-resume-{session.game_id[:8]}"
                if session.source_kind_is_iso
                else f"exe-resume-{session.game_id[:8]}"
            ),
            daemon=True,
        )
        thread.start()
        return GLib.SOURCE_REMOVE

    def _resume_worker(self, session: InstallerSession) -> None:
        try:
            self._installers.verify_staged(session)
            candidates = self._installers.discover(session)
        except Exception as error:
            LOGGER.exception("Installer resume failed for %s", session.game_id)
            GLib.idle_add(self._on_resume_failed, session, str(error))
            return
        GLib.idle_add(self._on_resume_ready, session, candidates)

    def _on_resume_failed(
        self, session: InstallerSession, message: str
    ) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._session = self._installers.save_session(
            replace(session, status=InstallerStatus.FAILED, message=message)
        )
        self._primary_button.set_sensitive(True)
        self._defer_button.set_sensitive(True)
        self._progress.set_visible(False)
        self._show_error(
            "The staged copy could not be verified. Nothing was deleted or "
            f"re-copied.\n\n{message}",
            heading="Staged copy needs attention",
        )
        return GLib.SOURCE_REMOVE

    def _on_resume_ready(
        self,
        session: InstallerSession,
        candidates: list[InstallerCandidate],
    ) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._staging_row.set_subtitle(session.staging_directory)
        if session.install_directory:
            self._destination_summary.set_subtitle(
                f"{session.install_directory} "
                f"({'MGL-managed' if session.install_directory_owned else 'external'})"
            )
        elif session.installer_relative_path:
            self._destination_summary.set_subtitle(
                "Wine prefix (installer default): "
                f"{self._installers.prefix_path(session)}"
            )
        else:
            self._destination_summary.set_subtitle("Not selected yet")
        self._populate_candidates(candidates)
        self._progress.set_visible(False)
        self._primary_button.set_sensitive(True)
        self._defer_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Helpers

    def _show_error(
        self, message: str, *, heading: str | None = None
    ) -> None:
        if heading is None:
            heading = (
                "ISO installation"
                if self._source_kind_is_iso
                else "Installer executable installation"
            )
        dialog = Adw.AlertDialog(heading=heading, body=message)
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        self._active_error = dialog
        dialog.choose(self, None, self._on_error_closed)

    def _on_error_closed(
        self, dialog: Adw.AlertDialog, result: Gio.AsyncResult
    ) -> None:
        self._active_error = None
        try:
            dialog.choose_finish(result)
        except GLib.Error:
            pass

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _release_dialog_references(self) -> None:
        self._closed = True
        if self._launch_finished_id:
            self._launch_manager.disconnect(self._launch_finished_id)
            self._launch_finished_id = 0
        if self._launch_failed_id:
            self._launch_manager.disconnect(self._launch_failed_id)
            self._launch_failed_id = 0
        self._candidates.clear()
        self._discovery_rows.clear()
        self._other_rows = []
        self._active_file_dialog = None
        self._active_error = None


class PendingInstallsDialog(Adw.Dialog, DialogSignalOwner):
    """List resumable installer sessions and allow removing them.

    Listing and removal never touch files until the user confirms. Removal
    offers session-only deletion or session plus managed files: installer
    staging, the managed destination when it is owned, and the Wine prefix.
    External destinations are reported and left untouched.
    """

    __gsignals__ = {
        "resume-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "removed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(
        self,
        installer_manager: InstallerManager,
        *,
        is_game_active: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__()
        self._installers = installer_manager
        self._is_game_active = is_game_active or (lambda _game_id: False)
        self._sessions: list[InstallerSession] = []
        self._active_alert: Adw.AlertDialog | None = None
        self.set_content_width(620)
        self.set_content_height(560)
        self.set_can_close(True)
        self.set_child(self._build_ui())
        self._init_dialog_lifecycle()

    def _build_ui(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Pending installs"))
        close = Gtk.Button(label="Close")
        close.add_css_class("flat")
        self.connect_owned(close, "clicked", self._on_close_clicked)
        header.pack_start(close)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        self._sessions = self._installers.list_sessions()
        if not self._sessions:
            toolbar.set_content(self._build_empty_page())
            return toolbar

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Installer sessions",
            description=(
                "Resuming verifies the staged copy without mounting an ISO or "
                "copying it again. Removing either deletes the session only or "
                "also deletes the staged files, the managed destination, and "
                "the Wine prefix. Original installer files are never removed."
            ),
        )
        page.add(group)
        for session in self._sessions:
            row = Adw.ActionRow(title=session.game_name)
            subtitle = (
                f"{session.status.value.replace('_', ' ')} · "
                f"{_source_kind_label(session.source_kind)}: "
                f"{Path(session.source_path).name}\n{session.message}"
            )
            if session.status is InstallerStatus.FAILED:
                subtitle += (
                    "\nFailed sessions keep their staged files. Resume to retry, "
                    "or remove the session to delete them."
                )
            row.set_subtitle(subtitle)
            row.set_subtitle_lines(3)

            remove = Gtk.Button(label="Remove")
            remove.add_css_class("destructive-action")
            remove.set_valign(Gtk.Align.CENTER)
            remove.set_tooltip_text("Delete this pending install and its files")
            self.connect_owned(
                remove,
                "clicked",
                self._on_remove_clicked,
                session,
            )
            row.add_suffix(remove)

            resume = Gtk.Button(label="Resume")
            resume.set_valign(Gtk.Align.CENTER)
            self.connect_owned(
                resume,
                "clicked",
                self._on_resume_clicked,
                session,
            )
            row.add_suffix(resume)

            group.add(row)
        toolbar.set_content(page)
        return toolbar

    def _build_empty_page(self) -> Adw.StatusPage:
        status = Adw.StatusPage()
        status.set_icon_name("media-optical-symbolic")
        status.set_title("No pending installs")
        status.set_description(
            "ISO and installer-executable installations that are deferred or "
            "waiting for a game executable appear here."
        )
        return status

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self.close()

    def _on_remove_clicked(
        self, _button: Gtk.Button, session: InstallerSession
    ) -> None:
        self._confirm_remove(session)

    def _on_resume_clicked(
        self, _button: Gtk.Button, session: InstallerSession
    ) -> None:
        self._resume(session)

    def _release_dialog_references(self) -> None:
        """Drop session rows and alert references once the dialog closes."""
        self._active_alert = None
        self._sessions = []

    def _resume(self, session: InstallerSession) -> None:
        self.emit("resume-requested", session)
        self.close()

    # ------------------------------------------------------------------
    # Removal

    def confirmation_body(self, session: InstallerSession) -> str:
        """Return the exact confirmation text (also used by tests)."""
        lines = [
            f"Pending install: {session.game_name}",
            f"{_source_kind_label(session.source_kind)}: "
            f"{session.source_path} (kept)",
        ]
        for kind, path in self._installers.session_resources(session):
            lines.append(f"• {_RESOURCE_LABEL[kind]}: {path}")
        if session.install_directory and not session.install_directory_owned:
            lines.append(
                f"• External folder kept: {session.install_directory}"
            )
        lines.append(
            "\nRemove files and session deletes the files listed above and the "
            "session record. Remove session only keeps every file on disk and "
            "deletes just the session record. Missing files never block either "
            "choice."
        )
        return "\n".join(lines)

    def _confirm_remove(self, session: InstallerSession) -> None:
        dialog = Adw.AlertDialog(
            heading="Remove this pending install?",
            body=self.confirmation_body(session),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("session", "Remove session only")
        dialog.add_response("all", "Remove files and session")
        dialog.set_response_appearance(
            "all", Adw.ResponseAppearance.DESTRUCTIVE
        )
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        self._active_alert = dialog
        dialog.choose(self, None, self._on_remove_confirmed, session)

    def _on_remove_confirmed(
        self,
        dialog: Adw.AlertDialog,
        result: Gio.AsyncResult,
        session: InstallerSession,
    ) -> None:
        self._active_alert = None
        try:
            response = dialog.choose_finish(result)
        except GLib.Error:
            return
        if response not in ("session", "all"):
            return
        thread = threading.Thread(
            target=self._remove_worker,
            args=(session, response == "all"),
            name=f"pending-remove-{session.game_id[:8]}",
            daemon=True,
        )
        thread.start()

    def _remove_worker(
        self, session: InstallerSession, delete_files: bool
    ) -> None:
        try:
            report = self._installers.remove_pending_install(
                session,
                authorized=True,
                game_running=self._is_game_active(session.game_id),
                delete_files=delete_files,
                delete_session=False,
            )
        except LauncherError as error:
            GLib.idle_add(self._on_remove_failed, session, str(error))
            return
        except Exception as error:
            LOGGER.exception("Pending install removal failed for %s", session.game_id)
            GLib.idle_add(
                self._on_remove_failed,
                session,
                f"The pending install could not be removed: {error}",
            )
            return
        GLib.idle_add(self._on_removed, session, report)

    def _on_remove_failed(self, session: InstallerSession, message: str) -> bool:
        self._show_result(
            heading="Pending install not removed",
            body=f"{session.game_name}\n{message}",
        )
        return GLib.SOURCE_REMOVE

    def _on_removed(
        self, session: InstallerSession, report: PendingRemovalReport
    ) -> bool:
        if not report.session_removed and not report.errors:
            if self._installers.discard_pending_session(session):
                report = replace(report, session_removed=True)
        if report.session_removed:
            self.emit("removed", session)
            self._refresh_sessions()
        self._show_result(
            heading=(
                "Pending install removed"
                if report.complete
                else "Pending install partly removed"
            ),
            body=removal_report_text(report),
        )
        return GLib.SOURCE_REMOVE

    def _refresh_sessions(self) -> None:
        self._sessions = self._installers.list_sessions()
        self.set_child(self._build_ui())

    def _show_result(self, *, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        self._active_alert = dialog
        dialog.choose(self, None, self._on_result_closed)

    def _on_result_closed(
        self, dialog: Adw.AlertDialog, result: Gio.AsyncResult
    ) -> None:
        self._active_alert = None
        try:
            dialog.choose_finish(result)
        except GLib.Error:
            pass


_RESOURCE_LABEL = {
    ResourceKind.INSTALLER_SOURCE: "Installer staging",
    ResourceKind.PREFIX: "Wine prefix",
    ResourceKind.SHARED_PREFIX: "Shared Wine prefix",
    ResourceKind.INSTALLATION: "Managed game folder",
}


def removal_report_text(report: PendingRemovalReport) -> str:
    """Describe a removal result as plain text (also used by tests)."""
    lines: list[str] = [f"{report.game_name}"]
    for item in report.items:
        label = _RESOURCE_LABEL.get(item.kind, item.kind.value)
        if item.retained:
            lines.append(f"Kept (external): {label} — {item.path}")
        elif item.missing:
            lines.append(f"Already missing: {label} — {item.path}")
        elif item.deleted:
            lines.append(f"Deleted: {label} — {item.path}")
        else:
            detail = "; ".join(item.errors) or "removal failed"
            lines.append(f"Failed: {label} — {item.path} ({detail})")
    if report.session_removed:
        lines.append("The pending install record was removed.")
    else:
        lines.append(
            "The pending install record was kept so you can retry."
        )
    if report.files_kept:
        lines.append("Every staged file was kept on disk.")
    elif report.missing and report.session_removed:
        lines.append(
            "Missing files did not block removal."
        )
    return "\n".join(lines)


__all__ = [
    "InstallerDialog",
    "PendingInstallsDialog",
    "removal_report_text",
]
