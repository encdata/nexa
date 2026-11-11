RTL_LANGUAGES = {"ar", "fa", "he", "iw", "ps", "sd", "ug", "ur", "yi"}


def is_rtl(configuration):
    language = configuration.get("language", "").lower().replace("_", "-").split("-", 1)[0]
    return language in RTL_LANGUAGES
