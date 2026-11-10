RTL_LANGUAGES = {"ar", "fa", "he", "iw", "ps", "sd", "ug", "ur", "yi"}


def is_rtl(configuration):
    return configuration.get("language") in RTL_LANGUAGES
