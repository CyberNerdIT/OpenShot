"""
 @file
 @brief Haram Filter submenu helpers for project file context menus.
 @author OpenShot Studios

 @section LICENSE

 Copyright (c) 2008-2026 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
 dedicated to delivering high quality video editing and animation solutions
 to the world.

 OpenShot Video Editor is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 OpenShot Video Editor is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with OpenShot Library.  If not, see <http://www.gnu.org/licenses/>.
 """

from classes.app import get_app
from .menu import StyledContextMenu, add_bound_action


def _filterable_files(win):
    """Return selected files that the Haram Filter can process."""
    service = getattr(win, "haram_filter_service", None)
    files = [f for f in (win.selected_files() or []) if f]
    if not service:
        return []
    return [f for f in files if service.is_filterable(f)]


def populate_haram_filter_menu(win, filter_menu):
    """Populate a Haram Filter submenu for the current file selection."""
    filter_menu.clear()
    selected_files = _filterable_files(win)
    if not selected_files:
        return None

    _ = get_app()._tr
    service = getattr(win, "haram_filter_service", None)
    has_active = bool(service and any(
        service.get_active_job_for_file(getattr(f, "id", None))
        for f in selected_files
    ))

    win.actionHaramFilterApply.setEnabled(not has_active)
    win.actionHaramFilterCancel.setEnabled(has_active)
    if has_active:
        add_bound_action(
            filter_menu, win, "actionHaramFilterCancel", _("Cancel"),
            "actionHaramFilterCancel_trigger", enabled=has_active,
        )
        return filter_menu

    add_bound_action(
        filter_menu, win, "actionHaramFilterApply", _("Filter Skin (Blur)"),
        "actionHaramFilterApply_trigger", enabled=not has_active,
    )
    return filter_menu


def add_haram_filter_menu(win, menu):
    """Add the Haram Filter submenu for the current file selection."""
    selected_files = _filterable_files(win)
    if not selected_files:
        return None

    filter_menu = StyledContextMenu(title=get_app()._tr("Haram Filter"), parent=menu)
    populate_haram_filter_menu(win, filter_menu)
    menu.addMenu(filter_menu)
    return filter_menu
