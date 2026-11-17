APP_API_VERSION = 1


class NexaApp:
    """Base contract for Python apps loaded by the Nexa desktop."""

    app_id = ""
    name = ""
    icon = ""
    version = "0.0.0"
    description = ""
    api_version = APP_API_VERSION
    preload_ui = False
    build_threaded = True

    def build(self, activity, parent, context=None):
        raise NotImplementedError

    def build_with_services(self, activity, parent, context, services):
        self.services = services
        return self.build(activity, parent, context)

    def get_title(self, context=None):
        return self.name

    def supports_file(self, path):
        return False
