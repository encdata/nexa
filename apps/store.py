import nexapip as tg

from shell import app_store, file_actions, file_associations, palette
from shell.context_menu import ContextMenu
from shell.icons import load
from shell.text_direction import force_ltr
from shell.text_prompt import TextPrompt


class AppStoreApp:
    app_id = "store"
    name = "Nexa Store"
    icon = "apps"
    build_threaded = True

    def build_with_services(self, activity, parent, context, services):
        self.services = services
        return self.build(activity, parent, context)

    def build(self, activity, parent, context=None):
        self.activity = activity
        self.tab = "discover"
        self.catalogs = []
        self.errors = []
        self.installed = app_store.installed_apps()
        self.actions = {}
        self.busy = False
        self.closed = False
        self.pending_install = None
        self.pending_remove = None
        self.catalog_cards = {}
        self.tab_buttons = {}

        self.root = tg.LinearLayout(activity, parent, vertical=True)
        self.root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        force_ltr(self.root)
        self.root.setbackgroundcolor(palette.window_content)

        header = tg.LinearLayout(activity, self.root, vertical=False)
        header.setdimensions(tg.View.MATCH_PARENT, 52)
        header.setlinearlayoutparams(0)
        logo = tg.ImageView(activity, header)
        logo.setdimensions(28, 28)
        logo.setimage(load("apps"))
        logo.setmargin(10, "left")
        logo.setmargin(8, "right")
        logo.setmargin(12, "top")
        logo.setlinearlayoutparams(0)
        self.title = self._text(header, "Nexa Store", 0, 52, 20, weight=1)
        self._button(header, "refresh", "refresh", 76)

        tabs = tg.LinearLayout(activity, self.root, vertical=False)
        tabs.setdimensions(tg.View.MATCH_PARENT, 44)
        tabs.setlinearlayoutparams(0)
        self.tab_buttons = {
            name: self._button(tabs, label, f"tab:{name}", 0, weight=1)
            for name, label in (("discover", "discover"),
                                ("installed", "installed"), ("sources", "sources"))
        }

        self.search = tg.EditText(activity, "", self.root, singleline=True,
                                  line=False, inputtype="text")
        self.search.setdimensions(tg.View.MATCH_PARENT, 42)
        self.search.setlinearlayoutparams(0)
        self.search.setmargin(8, "left")
        self.search.setmargin(8, "right")
        self.search.setmargin(3, "top")
        self.search.setmargin(3, "bottom")
        self.search.setbackgroundcolor(palette.surface)
        self.search.settextcolor(palette.text)
        self.search.settextsize(14)
        self.search.sendtextevent(True)

        self.status = self._text(self.root, "loading repositories…", tg.View.MATCH_PARENT, 34, 11)
        self.status.setmargin(8, "left")
        self.status.setmargin(8, "right")
        self.status.settextcolor(palette.muted)
        self.rows_scroll = tg.NestedScrollView(activity, self.root, fillviewport=True, nobar=True)
        self.rows_scroll.setheight(0)
        self.rows_scroll.setlinearlayoutparams(1)
        self.rows = tg.LinearLayout(activity, self.rows_scroll, vertical=True)
        self.rows.setdimensions(tg.View.MATCH_PARENT, tg.View.WRAP_CONTENT)
        self.rows.setbackgroundcolor(palette.window_content)
        self.prompt = TextPrompt(activity, parent,
                                 lambda: tuple(round(size / self.density)
                                               for size in self.root.getdimensions()))
        self.confirm_menu = ContextMenu(
            activity, parent,
            lambda: tuple(round(size / self.density) for size in self.root.getdimensions()),
            actions=("install", "remove", "cancel"),
            labels={"install": "trust and install", "remove": "remove app",
                    "cancel": "cancel"})
        self.density = activity.getconfiguration()["density"]
        self._load_catalogs()
        return self

    def _text(self, parent, value, width, height, size, weight=0):
        view = tg.TextView(self.activity, value, parent)
        view.setdimensions(width, height)
        view.setlinearlayoutparams(weight)
        view.settextsize(size)
        view.settextcolor(palette.text)
        view.setgravity(0, 1)
        return view

    def _button(self, parent, label, action, width, weight=0):
        button = tg.TextView(self.activity, label, parent)
        button.setdimensions(width, 40)
        button.setlinearlayoutparams(weight)
        button.setmargin(3, "left")
        button.setmargin(3, "right")
        button.setbackgroundcolor(palette.surface)
        button.settextsize(12)
        button.settextcolor(palette.text)
        button.setgravity(1, 1)
        button.sendclickevent(True)
        self.actions[button.id] = action
        return button

    def _set_status(self, message):
        self.status.settext(message)

    def _run_background(self, message, operation, complete):
        if self.busy:
            return
        self.busy = True
        self._set_status(message)

        def finished(result, error):
            self.busy = False
            complete(result, error)

        file_actions.run_background(operation, finished)

    def _load_catalogs(self):
        self._run_background("loading app catalogs…", app_store.load_catalogs,
                             self._catalogs_loaded)

    def _catalogs_loaded(self, result, error):
        if self.closed:
            return
        if error is not None:
            self.errors = [str(error)]
        else:
            self.catalogs = result["catalogs"]
            self.errors = result["errors"]
        self._set_status("catalogs loaded" if not self.errors else "some catalogs could not load")
        self._render()

    def _render(self):
        self.actions = {view_id: action for view_id, action in self.actions.items()
                        if action == "refresh"
                        or isinstance(action, str) and action.startswith("tab:")}
        self.rows.clearchildren()
        self.catalog_cards.clear()
        for name, button in self.tab_buttons.items():
            button.setbackgroundcolor(palette.accent if name == self.tab else palette.surface)
        self.search.setvisibility(tg.View.VISIBLE if self.tab == "discover" else tg.View.GONE)
        if self.tab == "sources":
            self._add_intro("repositories", "choose where Nexa gets Python apps")
            self._render_sources()
            return
        if self.tab == "installed":
            self.installed = app_store.installed_apps()
            self._add_intro("installed apps", f"{len(self.installed)} apps added to this Nexa desktop")
            self._render_installed()
            return
        self._add_intro("discover apps", "small Python apps built for Nexa")
        self._render_discover()

    def _add_intro(self, title, detail):
        heading = self._text(self.rows, title, tg.View.MATCH_PARENT, 40, 20)
        heading.setmargin(8, "left")
        heading.setmargin(4, "top")
        subheading = self._text(self.rows, detail, tg.View.MATCH_PARENT, 32, 12)
        subheading.setmargin(8, "left")
        subheading.setmargin(3, "bottom")
        subheading.settextcolor(palette.muted)

    def _add_section(self, title):
        label = self._text(self.rows, title, tg.View.MATCH_PARENT, 38, 14)
        label.setmargin(8, "left")
        label.settextcolor(palette.muted)

    def _add_card(self, name, version, description, source, action, action_label,
                  icon_name=None):
        card = tg.LinearLayout(self.activity, self.rows, vertical=True)
        card.setdimensions(tg.View.MATCH_PARENT, tg.View.WRAP_CONTENT)
        card.setmargin(7, "left")
        card.setmargin(7, "right")
        card.setmargin(4, "top")
        card.setmargin(4, "bottom")
        card.setbackgroundcolor(palette.surface)
        summary = tg.LinearLayout(self.activity, card, vertical=False)
        summary.setdimensions(tg.View.MATCH_PARENT, 66)
        summary.setlinearlayoutparams(0)
        icon_view = tg.ImageView(self.activity, summary)
        icon_view.setdimensions(42, 42)
        icon_view.setimage(load(icon_name or "apps"))
        icon_view.setmargin(12, "left")
        icon_view.setmargin(10, "right")
        icon_view.setmargin(12, "top")
        icon_view.setlinearlayoutparams(0)
        labels = tg.LinearLayout(self.activity, summary, vertical=True)
        labels.setdimensions(0, 58)
        labels.setlinearlayoutparams(1)
        labels.setmargin(2, "left")
        heading = self._text(labels, name, tg.View.MATCH_PARENT, 34, 15)
        version_label = self._text(labels, version, tg.View.MATCH_PARENT, 22, 11)
        version_label.settextcolor(palette.muted)
        if action:
            button = self._button(summary, action_label, action, 104)
            button.setbackgroundcolor(palette.accent)
        elif action_label:
            state = self._text(summary, action_label, 112, 44, 11)
            state.setgravity(1, 1)
            state.settextcolor(palette.muted)
        details = self._text(card, description or "Nexa app", tg.View.MATCH_PARENT,
                             tg.View.WRAP_CONTENT, 13)
        details.setmargin(12, "left")
        details.setmargin(5, "right")
        details.setmargin(3, "bottom")
        details.settextcolor(palette.muted)
        origin = self._text(card, source, tg.View.MATCH_PARENT, 30, 11)
        origin.setmargin(12, "left")
        origin.setmargin(3, "bottom")
        origin.settextcolor(palette.muted)
        return card

    def _render_discover(self):
        if self.errors:
            self._add_section("repository errors")
            for error in self.errors:
                self._add_card("catalog unavailable", "", error, "check repository settings", "", "")
        found = set()
        for catalog in self.catalogs:
            self._add_section(catalog["repository"]["name"])
            for listing in catalog["apps"]:
                found.add(listing["id"])
                installed = self.installed.get(listing["id"])
                running = self.services.get("app_is_running", lambda _app_id: False)(listing["id"])
                if installed is None:
                    label, action = "install", ("install", listing)
                elif running:
                    label, action = "close app first", None
                elif installed.get("repository", {}).get("url") != listing["repository"]["url"]:
                    label, action = "from another source", None
                elif installed["version"] != listing["version"]:
                    label, action = "update", ("install", listing)
                else:
                    label, action = "open", ("open", listing["id"])
                source = catalog["repository"]["name"]
                if catalog["cached"]:
                    source += " · cached catalog"
                card = self._add_card(listing["name"], listing["version"], listing["description"],
                                      source, action, label,
                                      getattr(self.services["apps"].get(listing["id"]), "icon", None)
                                      if installed else None)
                self.catalog_cards[(listing["repository"]["url"], listing["id"])] = card
        self._filter_catalog(self.search.gettext())
        if not found and not self.errors:
            self._add_section("no apps in these repositories yet")

    def _render_installed(self):
        if not self.installed:
            self._add_section("no store apps installed")
            return
        self._add_section("installed apps")
        for app_id, manifest in sorted(self.installed.items(), key=lambda item: item[1]["name"].casefold()):
            source = manifest.get("repository", {}).get("name", "local package")
            if self.services.get("app_is_running", lambda _app_id: False)(app_id):
                action_label, action = "close app first", None
            else:
                action_label, action = "remove", ("remove", app_id)
            self._add_card(manifest["name"], manifest["version"],
                           manifest.get("description", ""), source,
                           action, action_label,
                           getattr(self.services["apps"].get(app_id), "icon", None))

    def _render_sources(self):
        self._add_section("app repositories")
        for repo in app_store.repositories():
            label = "Nexa official" if repo["official"] else repo["name"]
            action = None if repo["official"] else ("remove_repo", repo["url"])
            action_label = "official" if repo["official"] else "remove"
            self._add_card(label, "", repo["url"], "HTTPS repository", action, action_label)
        controls = tg.LinearLayout(self.activity, self.rows, vertical=False)
        controls.setdimensions(tg.View.MATCH_PARENT, 48)
        self._button(controls, "add repository", "add_repo", 156)

    def _install(self, listing):
        if not listing.get("repository", {}).get("official"):
            self.pending_install = listing
            self._set_status("custom Python apps run as you, with access to Termux files and commands")
            width, height = (round(size / self.density) for size in self.root.getdimensions())
            self.confirm_menu.show(("install", "cancel"), (width - self.confirm_menu.width) // 2,
                                   height // 2, self._confirm_install)
            return
        self._install_confirmed(listing)

    def _confirm_install(self, action):
        listing = self.pending_install
        self.pending_install = None
        if action == "install" and listing is not None:
            self._install_confirmed(listing)

    def _install_confirmed(self, listing):
        app_id = listing["id"]
        if self.services.get("app_is_running", lambda _app_id: False)(app_id):
            self.activity.c.toast("close this app before updating")
            return
        installed = app_store.installed_apps()
        reserved = set(self.services["apps"]) - set(installed)

        def complete(manifest, error):
            if error is not None:
                if self.closed:
                    return
                self._set_status(f"install failed: {error}")
                return
            self.installed = app_store.installed_apps()
            refresh = self.services.get("refresh_apps")
            if refresh is None:
                if self.closed:
                    return
                self._set_status("installed; restart Nexa to load it")
                self._render()
                return

            def refreshed(refresh_error):
                if self.closed:
                    return
                if refresh_error is None:
                    self._set_status(f"{manifest['name']} installed and added to All apps")
                else:
                    self._set_status(f"installed; couldn't refresh apps: {refresh_error}")
                self._render()

            if not self.closed:
                self._set_status("updating All apps…")
            refresh(refreshed)

        self._run_background(f"installing {listing['name']}…",
                             lambda: app_store.install_app(listing, reserved), complete)

    def _remove(self, app_id):
        if self.services.get("app_is_running", lambda _app_id: False)(app_id):
            self.activity.c.toast("close this app before removing it")
            return
        self.pending_remove = app_id
        width, height = (round(size / self.density) for size in self.root.getdimensions())
        self.confirm_menu.show(
            ("remove", "cancel"),
            ((width - self.confirm_menu.width) // 2, height // 2),
            self._confirm_remove)

    def _confirm_remove(self, action):
        app_id = self.pending_remove
        self.pending_remove = None
        if action == "remove" and app_id is not None:
            self._remove_confirmed(app_id)

    def _remove_confirmed(self, app_id):
        if self.services.get("app_is_running", lambda _app_id: False)(app_id):
            self.activity.c.toast("close this app before removing it")
            return
        reserved = set(self.services["apps"]) - set(app_store.installed_apps())

        def complete(_result, error):
            if error is not None:
                if self.closed:
                    return
                self._set_status(f"remove failed: {error}")
                return
            self.installed = app_store.installed_apps()
            refresh = self.services.get("refresh_apps")
            if refresh is None:
                if self.closed:
                    return
                self._set_status("removed; restart Nexa to update the app list")
                self._render()
                return

            def refreshed(refresh_error):
                if self.closed:
                    return
                self._set_status("app removed" if refresh_error is None
                                 else f"removed; couldn't refresh apps: {refresh_error}")
                self._render()

            refresh(refreshed)

        def remove():
            app_store.remove_app(app_id, reserved)
            file_associations.remove_app(app_id)

        self._run_background("removing app…", remove, complete)

    def handle_event(self, event):
        if self.confirm_menu.visible:
            if event.type == tg.Event.back:
                self.confirm_menu.hide()
                self.pending_install = None
                self.pending_remove = None
                return True
            return self.confirm_menu.handle_event(event) or True
        if self.confirm_menu.handle_event(event):
            return True
        if self.prompt.handle_event(event):
            return True
        if event.type == tg.Event.text and event.id == self.search.id:
            self._filter_catalog(event.value.get("text", ""))
            return True
        if event.type != tg.Event.click:
            return False
        action = self.actions.get(event.id)
        if action is None:
            return False
        if isinstance(action, str) and action.startswith("tab:"):
            self.tab = action.split(":", 1)[1]
            self._render()
            return True
        kind, value = action if isinstance(action, tuple) else (action, None)
        if kind == "refresh":
            self._load_catalogs()
        elif kind == "install":
            self._install(value)
        elif kind == "remove":
            self._remove(value)
        elif kind == "open":
            self.services["open_app"](value)
        elif kind == "add_repo":
            self.prompt.show("add https repository", "https://", self._add_repository)
        elif kind == "remove_repo":
            try:
                app_store.remove_repository(value)
                self._load_catalogs()
                self._render()
            except (OSError, ValueError) as error:
                self._set_status(str(error))
        return True

    def _filter_catalog(self, query):
        query = query.casefold().strip()
        for catalog in self.catalogs:
            for listing in catalog["apps"]:
                key = (catalog["repository"]["url"], listing["id"])
                card = self.catalog_cards.get(key)
                if card is None:
                    continue
                match = (not query or query in listing["name"].casefold()
                         or query in listing["description"].casefold())
                card.setvisibility(tg.View.VISIBLE if match else tg.View.GONE)

    def _add_repository(self, url):
        try:
            app_store.add_repository(url)
        except (OSError, ValueError) as error:
            self._set_status(f"couldn't add repository: {error}")
            return
        self.tab = "sources"
        self._render()
        self._load_catalogs()

    def close(self):
        self.closed = True
        self.prompt.hide()
        self.confirm_menu.hide()


APP = AppStoreApp()
