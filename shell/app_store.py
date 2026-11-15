import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from nexapip import APP_API_VERSION


OFFICIAL_REPOSITORY = {
    "name": "Nexa official",
    "url": "https://raw.githubusercontent.com/encdata/nexa-apps/main/index.json",
    "official": True,
}
API_VERSION = APP_API_VERSION
MAX_BUNDLE_SIZE = 32 * 1024 * 1024
MAX_UNPACKED_SIZE = 96 * 1024 * 1024
APP_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
DATA_ROOT = Path(os.path.expanduser("~/.local/share/nexa"))
APP_ROOT = DATA_ROOT / "apps"
CONFIG_PATH = Path(os.path.expanduser("~/.config/nexa/repositories.json"))
CACHE_ROOT = Path(os.path.expanduser("~/.cache/nexa/store"))


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _https_url(value):
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("repository and package links must use https")
    return urllib.parse.urlunsplit(parsed)


def repositories():
    configured = []
    try:
        with CONFIG_PATH.open(encoding="utf-8") as source:
            saved = json.load(source)
        configured = saved.get("repositories", []) if isinstance(saved, dict) else []
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as error:
        print(f"nexa: couldn't read app repositories: {error}")

    result = [dict(OFFICIAL_REPOSITORY)]
    seen = {OFFICIAL_REPOSITORY["url"]}
    for repo in configured:
        if not isinstance(repo, dict) or not isinstance(repo.get("url"), str):
            continue
        try:
            url = _https_url(repo["url"])
        except ValueError:
            continue
        if url in seen:
            continue
        seen.add(url)
        result.append({"name": str(repo.get("name") or urllib.parse.urlsplit(url).hostname),
                       "url": url, "official": False})
    return result


def add_repository(url, name=None):
    url = _https_url(url)
    if url == OFFICIAL_REPOSITORY["url"]:
        return repositories()
    current = repositories()
    if any(repo["url"] == url for repo in current):
        return current
    custom = [{"name": repo["name"], "url": repo["url"]}
              for repo in current if not repo["official"]]
    hostname = urllib.parse.urlsplit(url).hostname or url
    label = (name or hostname).strip()
    custom.append({"name": label or hostname, "url": url})
    _atomic_json(CONFIG_PATH, {"repositories": custom})
    return repositories()


def remove_repository(url):
    if url == OFFICIAL_REPOSITORY["url"]:
        raise ValueError("the Nexa official repository can't be removed")
    custom = [{"name": repo["name"], "url": repo["url"]}
              for repo in repositories() if not repo["official"] and repo["url"] != url]
    _atomic_json(CONFIG_PATH, {"repositories": custom})
    return repositories()


def _read_catalog(repo):
    cache_key = hashlib.sha256(repo["url"].encode()).hexdigest() + ".json"
    cache_path = CACHE_ROOT / cache_key
    try:
        request = urllib.request.Request(repo["url"], headers={"User-Agent": "Nexa-App-Store/1"})
        with urllib.request.urlopen(request, timeout=15) as response:
            _https_url(response.geturl())
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("repository catalog is too large")
        catalog = json.loads(raw.decode("utf-8"))
        _validate_catalog(catalog)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{cache_path.name}.", dir=cache_path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
            os.replace(temporary, cache_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return catalog, False
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as network_error:
        try:
            catalog = json.loads(cache_path.read_text(encoding="utf-8"))
            _validate_catalog(catalog)
            return catalog, True
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            raise RuntimeError(f"couldn't load {repo['name']}: {network_error}") from network_error


def _validate_catalog(catalog):
    if not isinstance(catalog, dict) or catalog.get("schema_version") != 1:
        raise ValueError("unsupported catalog format")
    if not isinstance(catalog.get("apps"), list):
        raise ValueError("catalog has no app list")
    ids = set()
    for app in catalog["apps"]:
        _validate_listing(app)
        if app["id"] in ids:
            raise ValueError(f"duplicate app id {app['id']}")
        ids.add(app["id"])


def _validate_listing(app):
    if not isinstance(app, dict):
        raise ValueError("invalid app listing")
    for key in ("id", "name", "version", "description", "download_url", "sha256"):
        if not isinstance(app.get(key), str) or not app[key].strip():
            raise ValueError(f"app listing is missing {key}")
    if not APP_ID.fullmatch(app["id"]):
        raise ValueError("app id has invalid characters")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", app["sha256"]):
        raise ValueError("app package has an invalid sha256")
    _https_url(app["download_url"])
    if type(app.get("api_version")) is not int or app["api_version"] != API_VERSION:
        raise ValueError(f"{app['name']} needs an unsupported Nexa app API")


def load_catalogs():
    catalogs = []
    errors = []
    for repo in repositories():
        try:
            catalog, cached = _read_catalog(repo)
            entries = []
            for listing in catalog["apps"]:
                entry = dict(listing)
                entry["repository"] = repo
                entry["cached"] = cached
                entries.append(entry)
            catalogs.append({"repository": repo, "apps": entries, "cached": cached})
        except RuntimeError as error:
            errors.append(str(error))
    return {"catalogs": catalogs, "errors": errors}


def installed_apps():
    result = {}
    try:
        entries = tuple(APP_ROOT.iterdir())
    except FileNotFoundError:
        return result
    for entry in entries:
        if not entry.is_dir() or not APP_ID.fullmatch(entry.name):
            continue
        try:
            manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
            source_path = entry / "source.json"
            source = json.loads(source_path.read_text(encoding="utf-8")) if source_path.exists() else {}
            if (isinstance(manifest, dict) and manifest.get("id") == entry.name
                    and isinstance(source, dict)):
                result[entry.name] = {**manifest, "repository": source}
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            continue
    return result


def install_app(listing, reserved_ids=()):
    _validate_listing(listing)
    app_id = listing["id"]
    installed = installed_apps()
    previous = installed.get(app_id)
    if app_id in set(reserved_ids):
        raise ValueError("this app id belongs to Nexa")
    repo = listing.get("repository") or OFFICIAL_REPOSITORY
    _https_url(repo["url"])
    if previous and previous.get("repository", {}).get("url") != repo.get("url"):
        raise ValueError("remove this app before installing the same id from another repository")

    request = urllib.request.Request(listing["download_url"], headers={"User-Agent": "Nexa-App-Store/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        _https_url(response.geturl())
        archive = response.read(MAX_BUNDLE_SIZE + 1)
    if not archive or len(archive) > MAX_BUNDLE_SIZE:
        raise ValueError("app package is too large")
    if hashlib.sha256(archive).hexdigest().lower() != listing["sha256"].lower():
        raise ValueError("app package checksum doesn't match the catalog")

    APP_ROOT.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{app_id}-", dir=APP_ROOT))
    destination = APP_ROOT / app_id
    backup = APP_ROOT / f".{app_id}-previous"
    try:
        unpacked = 0
        members = set()
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            for member in package.infolist():
                path = PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise ValueError("app package contains an unsafe path")
                if member.filename in members:
                    raise ValueError("app package contains duplicate paths")
                members.add(member.filename)
                if (mode & 0o170000) == 0o120000:
                    raise ValueError("app packages can't contain symbolic links")
                unpacked += member.file_size
                if unpacked > MAX_UNPACKED_SIZE:
                    raise ValueError("unpacked app package is too large")
                output = stage.joinpath(*path.parts)
                if member.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(member) as source, output.open("wb") as target:
                        shutil.copyfileobj(source, target)

        manifest_path = stage / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("package manifest must be an object")
        if (manifest.get("id") != app_id or manifest.get("name") != listing["name"]
                or manifest.get("version") != listing["version"]
                or type(manifest.get("api_version")) is not int
                or manifest.get("api_version") != API_VERSION
                or not isinstance(manifest.get("license"), str) or not manifest["license"]):
            raise ValueError("package manifest doesn't match the catalog")
        if not (stage / "LICENSE").is_file():
            raise ValueError("app package is missing its license")
        entrypoint = manifest.get("entrypoint", "app:APP")
        module_name, separator, object_name = entrypoint.partition(":")
        if (not separator or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.]*", module_name)
                or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", object_name)):
            raise ValueError("package has an invalid entry point")
        module_path = stage.joinpath(*module_name.split("."))
        if not (module_path.with_suffix(".py").is_file()
                or (module_path / "__init__.py").is_file()):
            raise ValueError("app entry point is missing")
        icon = manifest.get("icon")
        if not isinstance(icon, str):
            raise ValueError("package manifest is missing its icon")
        icon_path = (stage / icon).resolve()
        if not icon_path.is_relative_to(stage.resolve()) or not icon_path.is_file():
            raise ValueError("app icon is missing or unsafe")
        _atomic_json(stage / "source.json", {"name": repo.get("name", "repository"),
                                              "url": repo["url"],
                                              "official": bool(repo.get("official")),
                                              "sha256": listing["sha256"].lower()})
        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(stage, destination)
        except OSError:
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def remove_app(app_id, reserved_ids=()):
    if not APP_ID.fullmatch(app_id):
        raise ValueError("invalid app id")
    if app_id in set(reserved_ids):
        raise ValueError("Nexa system apps can't be removed from the store")
    path = APP_ROOT / app_id
    if not path.is_dir():
        raise FileNotFoundError(app_id)
    shutil.rmtree(path)
