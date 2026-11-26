import {EventEmitter} from "node:events";
import {execFileSync} from "node:child_process";
import {createHash, randomBytes} from "node:crypto";
import dns from "node:dns/promises";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import Module from "node:module";
import {registerHooks} from "node:module";
import os from "node:os";
import path from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";

function termuxCommandEnvironment() {
    const environment = {...process.env};
    delete environment.LD_PRELOAD;
    delete environment.LD_LIBRARY_PATH;
    return environment;
}

export function createElectronCompat({appRoot, appPath = appRoot, resourcesPath = appRoot,
                                     localUrl, emit, resolveAsar, trustOrigin,
                                     isTrustedPath = () => false,
                                     executablePath = process.execPath,
                                     nativeCall = message => emit({event: "nativeCall", ...message})}) {
    if (process.platform === "android") {
        Object.defineProperty(process, "platform", {value: "linux", configurable: true});
        os.platform = () => "linux";
    }
    process.type = "browser";
    process.resourcesPath = resourcesPath;
    const windows = new Map();
    const contents = new Map();
    const contentsOwners = new Map();
    const preloadScriptCache = new Map();
    let preloadScriptCacheBytes = 0;
    const maxPreloadScriptCacheBytes = 8 * 1024 * 1024;
    const nativeModules = new Map();
    const nativeObjectHandles = new Map();
    const canonicalFsRoots = new Map();
    const electronEventSubscriptions = new Map();
    let nextElectronEventSubscriptionId = 0;
    let nextNativeModuleId = 0;
    let nextNativeObjectId = 0;
    const sessions = new Map();
    const sessionWebContents = new WeakMap();
    const handlers = new Map();
    const protocolHandlers = new WeakMap();
    const privilegedSchemes = new Map();
    const popupMenus = new Map();
    const rendererPopupMenus = new Map();
    let nextMenuId = 0;
    let nextMenuItemId = 0;
    let nextRendererPopupId = 0;

    function closeRendererPopupsForContents(contentsId) {
        for (const [menuId, popup] of rendererPopupMenus) {
            if (popup.contentsId === contentsId) rendererPopupMenus.delete(menuId);
        }
    }
    let applicationMenu = null;
    const dnsLookups = new Map();
    const dnsCacheTtlMs = 30_000;
    const maxDnsCacheEntries = 128;
    let nextWindowId = 0;
    let nextContentsId = 1000000;
    let nextCssId = 0;
    let nextScriptExecutionId = 0;
    const pendingScriptExecutions = new Map();
    let activeWindow = null;

    async function resolveHost(host, options = {}) {
        const family = options.queryType === "A" ? 4 : options.queryType === "AAAA" ? 6 : 0;
        const key = `${family}:${String(host).toLowerCase()}`;
        let entry = dnsLookups.get(key);
        if (entry && (entry.expiresAt === 0 || entry.expiresAt > Date.now())) {
            dnsLookups.delete(key);
            dnsLookups.set(key, entry);
        } else {
            if (entry) dnsLookups.delete(key);
            const lookup = dns.lookup(host, {all: true, family}).then(endpoints =>
                endpoints.map(endpoint => ({
                    address: endpoint.address,
                    family: endpoint.family === 4 ? "ipv4" : "ipv6"
                })));
            entry = {lookup, expiresAt: 0};
            dnsLookups.set(key, entry);
            while (dnsLookups.size > maxDnsCacheEntries) {
                dnsLookups.delete(dnsLookups.keys().next().value);
            }
        }
        try {
            const endpoints = await entry.lookup;
            entry.expiresAt = Date.now() + dnsCacheTtlMs;
            return {endpoints: endpoints.map(endpoint => ({...endpoint}))};
        } catch (error) {
            if (dnsLookups.get(key) === entry) dnsLookups.delete(key);
            throw error;
        }
    }

    function announceEmbeddedContents(webContents) {
        const windowId = webContents.ownerWindowId;
        const view = webContents.view;
        if (windowId == null || !view || webContents.destroyed) return;
        const bounds = view.getAbsoluteBounds();
        if (webContents.id === windowId) {
            emit({event: "rendererViewState", windowId, contentsId: webContents.id,
                visible: view.getVisible(), bounds, backgroundColor: view.backgroundColor});
            return;
        }
        if (!webContents.url) return;
        emit({event: "rendererView", windowId, contentsId: webContents.id,
            url: webContents.url, preloadUrl: localUrl(`__nexa/preload/${webContents.id}`),
            title: webContents.getTitle(), options: webContents.options,
            userAgent: webContents.getUserAgent(),
            loadOptions: webContents._loadOptions || {headers: {}},
            profileName: webContents.session.profileName,
            profileTemporary: webContents.session.temporary,
            webRequestEnabled: webContents.session.webRequestEnabled,
            visible: view.getVisible(), bounds, backgroundColor: view.backgroundColor,
            protocolSchemes: [...privilegedSchemes.keys()]});
    }

    function installNavigationHistory(webContents) {
        webContents._navigationHistory = {entries: [], index: -1, targetIndex: null};
        webContents.canGoBack = () => {
            const history = webContents._navigationHistory;
            return (history.targetIndex ?? history.index) > 0;
        };
        webContents.canGoForward = () => {
            const history = webContents._navigationHistory;
            return (history.targetIndex ?? history.index) < history.entries.length - 1;
        };
        webContents.getNavigationHistory = () => {
            const history = webContents._navigationHistory;
            return {entries: history.entries.map(entry => ({...entry})),
                index: history.targetIndex ?? history.index};
        };
        webContents.goBack = () => navigateHistory(webContents, -1);
        webContents.goForward = () => navigateHistory(webContents, 1);
        webContents.clearHistory = () => {
            const url = webContents.getURL?.() || webContents.url || "";
            const entry = url ? [{id: 0, url, title: webContents.getTitle?.() || ""}] : [];
            webContents._navigationHistory = {entries: entry, index: entry.length - 1,
                targetIndex: null};
            emit({event: "nativeCall", method: "webContents.clearHistory",
                contentsId: webContents.id});
        };
        webContents.navigationHistory = {
            canGoBack: webContents.canGoBack,
            canGoForward: webContents.canGoForward,
            canGoToOffset(offset) {
                const history = webContents._navigationHistory;
                const current = history.targetIndex ?? history.index;
                return Number.isInteger(offset) && current + offset >= 0 &&
                    current + offset < history.entries.length;
            },
            clear: webContents.clearHistory,
            getActiveIndex() {
                const history = webContents._navigationHistory;
                return history.targetIndex ?? history.index;
            },
            getEntryAtIndex(index) {
                if (!Number.isInteger(index)) return null;
                const entry = webContents._navigationHistory.entries[index];
                return entry ? {url: entry.url, title: entry.title} : null;
            },
            goBack: webContents.goBack,
            goForward: webContents.goForward,
            goToIndex(index) {
                if (!Number.isInteger(index)) throw new TypeError("navigation index must be an integer");
                navigateHistoryTo(webContents, index);
            },
            goToOffset(offset) {
                if (!Number.isInteger(offset)) throw new TypeError("navigation offset must be an integer");
                const history = webContents._navigationHistory;
                navigateHistoryTo(webContents,
                    (history.targetIndex ?? history.index) + offset);
            },
            length() { return webContents._navigationHistory.entries.length; },
            removeEntryAtIndex() {
                throw new Error("navigationHistory.removeEntryAtIndex is not supported by Android WebView");
            },
            getAllEntries() {
                return webContents._navigationHistory.entries.map(({url, title}) => ({url, title}));
            },
            restore() {
                throw new Error("navigationHistory.restore is not supported by Android WebView");
            }
        };
    }

    function installWebContentsUserAgent(webContents, userAgent) {
        if (userAgent !== undefined && typeof userAgent !== "string") {
            throw new TypeError("webPreferences.userAgent must be a string");
        }
        webContents._userAgent = userAgent ?? process.env.NEXA_ELECTRON_USER_AGENT ?? "";
        webContents.getUserAgent = () => webContents._userAgent;
        webContents.setUserAgent = value => {
            if (typeof value !== "string") throw new TypeError("Electron user agent must be a string");
            webContents._userAgent = value;
            emit({event: "nativeCall", method: "webContents.setUserAgent",
                contentsId: webContents.id, args: [value]});
        };
    }

    function installWebContentsZoom(webContents) {
        webContents._zoomFactor = 1;
        webContents.getZoomFactor = () => webContents._zoomFactor;
        webContents.getZoomLevel = () => Math.log(webContents._zoomFactor) / Math.log(1.2);
        webContents.setZoomFactor = factor => {
            if (typeof factor !== "number" || !Number.isFinite(factor) || factor <= 0) {
                throw new TypeError("zoom factor must be a positive number");
            }
            webContents._zoomFactor = Math.min(5, Math.max(0.25, factor));
            webContents.send("__nexa:set-zoom", webContents._zoomFactor);
        };
        webContents.setZoomLevel = level => {
            if (typeof level !== "number" || !Number.isFinite(level)) {
                throw new TypeError("zoom level must be a number");
            }
            const minimum = Math.log(0.25) / Math.log(1.2);
            const maximum = Math.log(5) / Math.log(1.2);
            const boundedLevel = Math.min(maximum, Math.max(minimum, level));
            webContents.setZoomFactor(Math.pow(1.2, boundedLevel));
        };
        Object.defineProperties(webContents, {
            zoomFactor: {
                enumerable: true,
                get: webContents.getZoomFactor,
                set: webContents.setZoomFactor
            },
            zoomLevel: {
                enumerable: true,
                get: webContents.getZoomLevel,
                set: webContents.setZoomLevel
            }
        });
    }

    function installWebContentsPageCommands(webContents) {
        const edit = (command, value) => webContents.send(
            "__nexa:edit-command", command, value);
        for (const command of ["undo", "redo", "cut", "copy", "paste",
            "pasteAndMatchStyle", "delete", "selectAll", "replace",
            "replaceMisspelling"]) {
            webContents[command] = value => {
                if ((command === "replace" || command === "replaceMisspelling") &&
                    typeof value !== "string") {
                    throw new TypeError(`${command} expects a string`);
                }
                edit(command, value);
            };
        }
        webContents.unselect = () => edit("unselect");
        webContents.scrollToTop = () => webContents.send("__nexa:scroll", "top");
        webContents.scrollToBottom = () => webContents.send("__nexa:scroll", "bottom");
        webContents.centerSelection = () => webContents.send("__nexa:edit-command", "centerSelection");
        webContents.insertText = text => {
            if (typeof text !== "string") throw new TypeError("insertText expects a string");
            return webContents.executeJavaScript(
                `document.execCommand("insertText", false, ${JSON.stringify(text)})`
            ).then(() => undefined);
        };
    }

    function normalizeLoadURLOptions(options = {}) {
        if (options == null || typeof options !== "object" || Array.isArray(options)) {
            throw new TypeError("loadURL options must be an object");
        }
        for (const key of ["postData", "baseURLForDataURL"]) {
            if (options[key] !== undefined) {
                throw new Error(`loadURL option ${key} is not supported by Android WebView`);
            }
        }
        const headers = new Map();
        const setHeader = (name, value) => {
            if (typeof name !== "string" || !/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(name) ||
                typeof value !== "string" || /[\r\n\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(value)) {
                throw new TypeError("loadURL contains an invalid HTTP header");
            }
            headers.set(name.toLowerCase(), {name, value});
        };
        if (options.extraHeaders !== undefined) {
            if (typeof options.extraHeaders !== "string") {
                throw new TypeError("loadURL extraHeaders must be a string");
            }
            for (const line of options.extraHeaders.split(/\r?\n/)) {
                if (!line.trim()) continue;
                const separator = line.indexOf(":");
                if (separator < 1) throw new TypeError("loadURL extraHeaders has an invalid line");
                setHeader(line.slice(0, separator).trim(), line.slice(separator + 1).trim());
            }
        }
        if (options.httpReferrer !== undefined) {
            const referrer = typeof options.httpReferrer === "string"
                ? options.httpReferrer : options.httpReferrer?.url;
            if (typeof referrer !== "string" || !referrer) {
                throw new TypeError("loadURL httpReferrer must contain a URL");
            }
            if (typeof options.httpReferrer === "object" &&
                options.httpReferrer.policy !== undefined) {
                throw new Error("loadURL httpReferrer policies are not supported by Android WebView");
            }
            setHeader("Referer", referrer);
        }
        const userAgent = options.userAgent ?? headers.get("user-agent")?.value;
        if (userAgent !== undefined && typeof userAgent !== "string") {
            throw new TypeError("loadURL userAgent must be a string");
        }
        headers.delete("user-agent");
        return {headers: Object.fromEntries([...headers.values()].map(({name, value}) => [name, value])),
            userAgent};
    }

    function applyLoadFileOptions(url, options = {}) {
        if (options == null || typeof options !== "object" || Array.isArray(options)) {
            throw new TypeError("loadFile options must be an object");
        }
        if (options.query !== undefined) {
            if (options.query == null || typeof options.query !== "object" ||
                Array.isArray(options.query)) {
                throw new TypeError("loadFile query must be an object");
            }
            for (const [key, value] of Object.entries(options.query)) {
                url.searchParams.set(key, String(value));
            }
        }
        if (options.search !== undefined) {
            if (typeof options.search !== "string") {
                throw new TypeError("loadFile search must be a string");
            }
            url.search = options.search;
        }
        if (options.hash !== undefined) {
            if (typeof options.hash !== "string") {
                throw new TypeError("loadFile hash must be a string");
            }
            url.hash = options.hash;
        }
    }

    function navigateHistory(webContents, offset) {
        const history = webContents._navigationHistory;
        const current = history.targetIndex ?? history.index;
        navigateHistoryTo(webContents, current + offset);
    }

    function navigateHistoryTo(webContents, index) {
        const history = webContents._navigationHistory;
        const current = history.targetIndex ?? history.index;
        if (!Number.isInteger(index) || index < 0 || index >= history.entries.length) return;
        const offset = index - current;
        if (!offset) return;
        history.targetIndex = index;
        emit({event: "nativeCall", method: "webContents.goToOffset",
            contentsId: webContents.id, args: [offset]});
    }

    function setWebContentsLoading(webContents, loading) {
        const next = Boolean(loading);
        if (webContents._isLoading === next) return;
        webContents._isLoading = next;
        webContents.emit(next ? "did-start-loading" : "did-stop-loading");
    }

    function setWebContentsMainFrameLoading(webContents, loading) {
        webContents._isLoadingMainFrame = Boolean(loading);
    }

    function setWebContentsWaitingForResponse(webContents, waiting) {
        webContents._isWaitingForResponse = Boolean(waiting);
    }

    function emitNavigationStart(webContents, url, isMainFrame) {
        if (isMainFrame) setWebContentsWaitingForResponse(webContents, true);
        webContents.emit("did-start-navigation", {}, url, false, isMainFrame);
    }

    function matchesRequestUrl(pattern, value) {
        if (pattern === "<all_urls>") return true;
        const match = /^(\*|https?|file|ftp):\/\/([^/]*)(\/.*)$/.exec(pattern);
        if (!match) return false;
        const [, scheme, authority, pathPattern] = match;
        let url;
        try { url = new URL(value); } catch (_) { return false; }
        if (scheme === "*") {
            if (!new Set(["http:", "https:"]).has(url.protocol)) return false;
        } else if (url.protocol !== `${scheme}:`) {
            return false;
        }
        const portIndex = authority.lastIndexOf(":");
        const hostPattern = portIndex > 0 ? authority.slice(0, portIndex) : authority;
        const portPattern = portIndex > 0 ? authority.slice(portIndex + 1) : "";
        const hostMatches = hostPattern === "*" ||
            (hostPattern.startsWith("*.")
                ? url.hostname === hostPattern.slice(2) ||
                    url.hostname.endsWith(`.${hostPattern.slice(2)}`)
                : url.hostname === hostPattern);
        const actualPort = url.port || (url.protocol === "https:" ? "443"
            : url.protocol === "ftp:" ? "21" : "80");
        if (!hostMatches || (portPattern && portPattern !== "*" &&
            actualPort !== portPattern)) return false;
        const pathRegex = new RegExp(`^${pathPattern.split("*")
            .map(part => part.replace(/[|\\{}()[\]^$+?.]/g, "\\$&"))
            .join(".*")}$`);
        return pathRegex.test(`${url.pathname}${url.search}`);
    }

    function installDevTools(webContents, id) {
        webContents.devToolsOpened = false;
        webContents._devToolsGeneration = 0;
        webContents.isDevToolsOpened = () => webContents.devToolsOpened &&
            !webContents.destroyed;
        webContents.openDevTools = options => {
            if (webContents.destroyed || !webContents.devTools) return;
            const generation = ++webContents._devToolsGeneration;
            webContents.devToolsOpened = true;
            Promise.resolve(nativeCall({method: "webContents.openDevTools",
                contentsId: id, args: [options || {}]})).then(() => {
                if (webContents._devToolsGeneration !== generation) return;
                webContents.emit("devtools-opened");
            }, error => {
                if (webContents._devToolsGeneration !== generation) return;
                webContents.devToolsOpened = false;
                console.warn(`could not open Electron DevTools: ${error.message}`);
            });
        };
        webContents.closeDevTools = () => {
            if (!webContents.devToolsOpened) return;
            const generation = ++webContents._devToolsGeneration;
            webContents.devToolsOpened = false;
            Promise.resolve(nativeCall({method: "webContents.closeDevTools",
                contentsId: id})).then(() => {
                if (webContents._devToolsGeneration === generation) {
                    webContents.emit("devtools-closed");
                }
            }, error => console.warn(`could not close Electron DevTools: ${error.message}`));
        };
    }

    function updateDevToolsState(contentsId, opened) {
        const webContents = contents.get(contentsId);
        if (!webContents || webContents.destroyed) return false;
        const wasOpened = webContents.devToolsOpened;
        webContents.devToolsOpened = Boolean(opened);
        if (wasOpened !== webContents.devToolsOpened) {
            webContents._devToolsGeneration++;
            webContents.emit(webContents.devToolsOpened
                ? "devtools-opened" : "devtools-closed");
        }
        return true;
    }

    function recordNavigation(webContents, url) {
        const history = webContents._navigationHistory;
        if (!history || typeof url !== "string" || !url) return;
        const targetIndex = history.targetIndex;
        if (targetIndex !== null && history.entries[targetIndex]?.url === url) {
            history.index = targetIndex;
        } else if (history.entries[history.index]?.url !== url) {
            history.entries.splice(history.index + 1);
            history.entries.push({id: history.entries.length, url,
                title: webContents.getTitle?.() || ""});
            history.index = history.entries.length - 1;
        }
        history.targetIndex = null;
    }

    function publishEmbeddedTree(view) {
        if (view instanceof WebContentsView) {
            const webContents = view.webContents;
            const windowId = webContents.ownerWindowId;
            if (windowId != null && !webContents.destroyed) {
                emit({event: "rendererViewState", windowId, contentsId: webContents.id,
                    bounds: view.getAbsoluteBounds(), visible: view.getVisible(),
                    backgroundColor: view.backgroundColor});
            }
        }
        for (const child of view.children) publishEmbeddedTree(child);
    }

    class View extends EventEmitter {
        constructor(options = {}) {
            super();
            this.parent = null;
            this.ownerWindow = null;
            this.children = [];
            this.bounds = {...(options.bounds || {x: 0, y: 0, width: 0, height: 0})};
            this.visible = options.visible !== false;
            this.backgroundColor = options.backgroundColor || "#ffffff";
        }

        addChildView(view, index = 0) {
            if (!(view instanceof View)) throw new TypeError("child must be an Electron View");
            if (view === this) throw new TypeError("a View cannot contain itself");
            for (let parent = this; parent; parent = parent.parent) {
                if (parent === view) throw new TypeError("adding this View would create a cycle");
            }
            view.parent?.removeChildView(view);
            const position = Math.max(0, Math.min(this.children.length, Number(index) || 0));
            this.children.splice(position, 0, view);
            view.parent = this;
            view._setOwnerWindow(this.ownerWindow);
            view.emit("did-attach");
            return view;
        }

        removeChildView(view) {
            const index = this.children.indexOf(view);
            if (index < 0) return false;
            this.children.splice(index, 1);
            view.parent = null;
            view._setOwnerWindow(null);
            publishEmbeddedTree(view);
            view.emit("did-detach");
            return true;
        }

        getChildAt(index) { return this.children[index] || null; }
        getChildCount() { return this.children.length; }
        getChildren() { return [...this.children]; }
        getParentView() { return this.parent; }
        getBounds() { return {...this.bounds}; }
        getVisible() {
            if (!this.parent) return Boolean(this.ownerWindow) && this.visible &&
                this.ownerWindow.visible && !this.ownerWindow.minimized;
            return this.visible && this.parent.getVisible();
        }
        getBackgroundColor() { return this.backgroundColor; }

        getAbsoluteBounds() {
            let x = this.bounds.x;
            let y = this.bounds.y;
            for (let parent = this.parent; parent; parent = parent.parent) {
                x += parent.bounds.x;
                y += parent.bounds.y;
            }
            return {...this.bounds, x, y};
        }

        _setOwnerWindow(window) {
            this.ownerWindow = window;
            if (this instanceof WebContentsView) {
                const webContents = this.webContents;
                if (window) {
                    this.detachedOwnerWindowId = window.id;
                    webContents.ownerWindowId = window.id;
                    contentsOwners.set(webContents.id, window.id);
                } else if (this.detachedOwnerWindowId !== undefined) {
                    webContents.ownerWindowId = this.detachedOwnerWindowId;
                }
                if (this.parent) announceEmbeddedContents(webContents);
            }
            for (const child of this.children) child._setOwnerWindow(window);
        }

        setBounds(bounds) {
            if (!bounds || typeof bounds !== "object") throw new TypeError("bounds must be an object");
            for (const key of ["x", "y", "width", "height"]) {
                if (bounds[key] !== undefined) {
                    const value = Number(bounds[key]);
                    if (!Number.isFinite(value)) throw new TypeError(`bounds.${key} must be finite`);
                    if ((key === "width" || key === "height") && value < 0) {
                        throw new RangeError(`bounds.${key} must not be negative`);
                    }
                    this.bounds[key] = value;
                }
            }
            publishEmbeddedTree(this);
        }

        setVisible(visible) {
            this.visible = Boolean(visible);
            publishEmbeddedTree(this);
        }

        setBackgroundColor(color) {
            this.backgroundColor = String(color);
            publishEmbeddedTree(this);
        }
    }

    function createEmbeddedContents(options = {}) {
        const preferences = options.webPreferences || {};
        const id = ++nextContentsId;
        const webContents = new EventEmitter();
        webContents.id = id;
        webContents.url = "";
        webContents.title = options.title || "";
        webContents.getURL = () => webContents.url;
        webContents.getTitle = () => webContents.title;
        webContents.getType = () => "window";
        webContents.getProcessId = () => {
            throw new Error("WebContents.getProcessId() is unavailable in Android WebView");
        };
        webContents.getOSProcessId = () => {
            throw new Error("WebContents.getOSProcessId() is unavailable in Android WebView");
        };
        webContents.isCrashed = () => webContents._isCrashed === true;
        webContents._isCrashed = false;
        installWebContentsZoom(webContents);
        webContents.focus = () => emit({event: "nativeCall", method: "webContents.focus",
            contentsId: id});
        installNavigationHistory(webContents);
        webContents.options = options;
        installWebContentsPageCommands(webContents);
        webContents.ownerWindowId = null;
        webContents.session = sessionForPartition(preferences.partition || "default");
        webContents.preloadPath = preferences.preload || null;
        webContents.destroyed = false;
        webContents.devTools = preferences.devTools !== false;
        installWebContentsUserAgent(webContents, preferences.userAgent);
        sessionWebContents.get(webContents.session).add(webContents);
        webContents.backgroundThrottling = preferences.backgroundThrottling !== false;
        webContents.getBackgroundThrottling = () => webContents.backgroundThrottling;
        webContents.getURL = () => webContents.url;
        webContents.getTitle = () => webContents.title;
        webContents.isDestroyed = () => webContents.destroyed;
        webContents._isLoading = false;
        webContents._isLoadingMainFrame = false;
        webContents._isWaitingForResponse = false;
        webContents.isLoading = () => webContents._isLoading;
        webContents.isLoadingMainFrame = () => webContents._isLoadingMainFrame;
        webContents.isWaitingForResponse = () => webContents._isWaitingForResponse;
        webContents.loadURL = (url, options = {}) => {
            let loadOptions;
            try {
                loadOptions = normalizeLoadURLOptions(options);
            } catch (error) {
                return Promise.reject(error);
            }
            if (loadOptions.userAgent !== undefined) {
                webContents.setUserAgent(loadOptions.userAgent);
            }
            webContents._loadOptions = loadOptions;
            webContents.url = String(url);
            if (webContents.url.startsWith("file:")) {
                const file = path.resolve(fileURLToPath(webContents.url));
                if (file !== appRoot && !file.startsWith(appRoot + path.sep)) {
                    return Promise.reject(new Error("renderer file escapes the app root"));
                }
                webContents.url = localUrl(path.relative(appRoot, file).split(path.sep)
                    .map(encodeURIComponent).join("/"));
            }
            try {
                const parsed = new URL(webContents.url);
                if (parsed.protocol === "http:" || parsed.protocol === "https:") {
                    trustOrigin?.(parsed.origin);
                }
            } catch (_) {}
            webContents.pendingLoad?.reject(new Error("navigation interrupted"));
            const pending = {};
            const load = new Promise((resolve, reject) => {
                pending.resolve = resolve;
                pending.reject = reject;
            });
            load.catch(() => {});
            webContents.pendingLoad = pending;
            setWebContentsMainFrameLoading(webContents, true);
            setWebContentsLoading(webContents, true);
            emitNavigationStart(webContents, webContents.url, true);
            if (webContents.ownerWindowId !== null) announceEmbeddedContents(webContents);
            return load;
        };
        webContents.loadFile = (file, loadOptions = {}) => {
            const absolute = path.resolve(appRoot, file);
            if (absolute !== appRoot && !absolute.startsWith(appRoot + path.sep)) {
                return Promise.reject(new Error("renderer file escapes the app root"));
            }
            if (!fs.existsSync(absolute)) return Promise.reject(new Error(`renderer file not found: ${file}`));
            const url = new URL(localUrl(path.relative(appRoot, absolute).split(path.sep)
                .map(encodeURIComponent).join("/")));
            try {
                applyLoadFileOptions(url, loadOptions);
            } catch (error) {
                return Promise.reject(error);
            }
            const {query, search, hash, ...navigationOptions} = loadOptions;
            return webContents.loadURL(url.href, navigationOptions);
        };
        webContents.reload = () => {
            setWebContentsMainFrameLoading(webContents, true);
            setWebContentsLoading(webContents, true);
            emitNavigationStart(webContents, webContents.url, true);
            emit({event: "nativeCall", method: "webContents.reload", contentsId: id});
        };
        webContents.reloadIgnoringCache = () => {
            setWebContentsMainFrameLoading(webContents, true);
            setWebContentsLoading(webContents, true);
            emitNavigationStart(webContents, webContents.url, true);
            emit({event: "nativeCall", method: "webContents.reloadIgnoringCache", contentsId: id});
        };
        webContents.stop = () => {
            const error = new Error("navigation stopped");
            webContents.pendingLoad?.reject(error);
            webContents.pendingLoad = null;
            setWebContentsMainFrameLoading(webContents, false);
            setWebContentsLoading(webContents, false);
            setWebContentsWaitingForResponse(webContents, false);
            emit({event: "nativeCall", method: "webContents.stop", contentsId: id});
        };
        webContents.send = (channel, ...args) => emit({event: "rendererMessage",
            windowId: webContents.ownerWindowId, contentsId: webContents.id, channel, args});
        webContents.executeJavaScript = (code, _userGesture = false) => {
            if (webContents.destroyed) return Promise.reject(new Error("webContents was destroyed"));
            const requestId = ++nextScriptExecutionId;
            return new Promise((resolve, reject) => {
                pendingScriptExecutions.set(`${id}:${requestId}`, {resolve, reject});
                webContents.send("__nexa:execute-js", requestId, String(code));
            });
        };
        webContents.getPreloadResponse = () =>
            BrowserWindow.prototype.getPreloadResponse.call(webContents);
        webContents.setWindowOpenHandler = handler => { webContents.windowOpenHandler = handler; };
        webContents.setBackgroundThrottling = enabled => {
            webContents.backgroundThrottling = Boolean(enabled);
            emit({event: "rendererViewState", windowId: webContents.ownerWindowId,
                contentsId: id, backgroundThrottling: webContents.backgroundThrottling});
        };
        webContents.clearCache = async () => {
            if (webContents.destroyed) throw new Error("webContents was destroyed");
            await nativeCall({method: "webContents.clearCache", contentsId: id});
        };
        installDevTools(webContents, id);
        webContents.insertCSS = css => {
            const key = `nexa-electron-css-${id}-${++nextCssId}`;
            webContents.send("__nexa:insert-css", key, String(css));
            return Promise.resolve(key);
        };
        webContents.removeInsertedCSS = key => {
            webContents.send("__nexa:remove-css", String(key));
            return Promise.resolve();
        };
        webContents.destroy = () => {
            if (webContents.destroyed) return;
            setWebContentsMainFrameLoading(webContents, false);
            setWebContentsLoading(webContents, false);
            setWebContentsWaitingForResponse(webContents, false);
            webContents.destroyed = true;
            sessionWebContents.get(webContents.session).delete(webContents);
            webContents.pendingLoad?.reject(new Error("webContents was destroyed"));
            webContents.pendingLoad = null;
            for (const [key, pending] of pendingScriptExecutions) {
                if (!key.startsWith(`${id}:`)) continue;
                pendingScriptExecutions.delete(key);
                pending.reject(new Error("webContents was destroyed"));
            }
            contents.delete(id);
            closeRendererPopupsForContents(id);
            if (webContents.ownerWindowId != null) {
                emit({event: "rendererViewClosed", windowId: webContents.ownerWindowId,
                    contentsId: id});
            }
            contentsOwners.delete(id);
            removeElectronEventSubscriptions(id);
            webContents.emit("destroyed");
        };
        contents.set(id, webContents);
        app.emit("web-contents-created", {}, webContents);
        return webContents;
    }

    class WebContentsView extends View {
        constructor(options = {}) {
            super(options);
            this.webContents = options.webContents || createEmbeddedContents(options);
            if (this.webContents.view && this.webContents.view !== this) {
                throw new Error("a WebContents can only belong to one WebContentsView");
            }
            this.webContents.view = this;
            this.backgroundColor = options.backgroundColor || "#ffffff";
        }
    }

    class BrowserWindow extends EventEmitter {
        constructor(options = {}) {
            super();
            this.id = ++nextWindowId;
            this.options = options;
            this.title = options.title || "";
            this.session = sessionForPartition(options.webPreferences?.partition || "default");
            this.preloadPath = options.webPreferences?.preload || null;
            this.destroyed = false;
            this.pendingLoad = null;
            this.visible = options.show !== false;
            this.focused = this.visible;
            this.minimized = false;
            this.maximized = false;
            this.nativeGeometryReady = false;
            this.resizable = options.resizable !== false;
            this.autoHideMenuBar = options.autoHideMenuBar === true;
            this.menuBarVisible = !this.autoHideMenuBar;
            this.menu = null;
            this._menuExplicit = false;
            this.webContents = new EventEmitter();
            this.webContents.id = this.id;
            this.webContents.ownerWindowId = this.id;
            this.webContents.session = this.session;
            sessionWebContents.get(this.session).add(this.webContents);
            this.webContents.destroyed = false;
            this.webContents._isLoading = false;
            this.webContents._isLoadingMainFrame = false;
            this.webContents._isWaitingForResponse = false;
            this.webContents.isLoading = () => this.webContents._isLoading;
            this.webContents.isLoadingMainFrame = () => this.webContents._isLoadingMainFrame;
            this.webContents.isWaitingForResponse = () =>
                this.webContents._isWaitingForResponse;
            installWebContentsPageCommands(this.webContents);
            this.webContents.backgroundThrottling =
                options.webPreferences?.backgroundThrottling !== false;
            this.webContents.getBackgroundThrottling = () =>
                this.webContents.backgroundThrottling;
            this.webContents.devTools = options.webPreferences?.devTools !== false;
            installWebContentsZoom(this.webContents);
            this.webContents.title = "";
            this.webContents.getURL = () => this.url || "";
            this.webContents.getTitle = () => this.webContents.title || "";
            this.webContents.getType = () => "window";
            this.webContents.getProcessId = () => {
                throw new Error("WebContents.getProcessId() is unavailable in Android WebView");
            };
            this.webContents.getOSProcessId = () => {
                throw new Error("WebContents.getOSProcessId() is unavailable in Android WebView");
            };
            this.webContents.isCrashed = () => this.webContents._isCrashed === true;
            this.webContents._isCrashed = false;
            installWebContentsUserAgent(this.webContents, options.webPreferences?.userAgent);
            this.webContents.focus = () => emit({event: "nativeCall", method: "webContents.focus",
                contentsId: this.webContents.id});
            installNavigationHistory(this.webContents);
            this.webContents.isDestroyed = () => this.webContents.destroyed;
            this.webContents.loadURL = (...args) => this.loadURL(...args);
            this.webContents.loadFile = (...args) => this.loadFile(...args);
            this.webContents.executeJavaScript = (code, _userGesture = false) => {
                if (this.webContents.destroyed) {
                    return Promise.reject(new Error("webContents was destroyed"));
                }
                const requestId = ++nextScriptExecutionId;
                return new Promise((resolve, reject) => {
                    pendingScriptExecutions.set(`${this.id}:${requestId}`, {resolve, reject});
                    this.webContents.send("__nexa:execute-js", requestId, String(code));
                });
            };
            this.webContents.reload = () => {
                setWebContentsMainFrameLoading(this.webContents, true);
                setWebContentsLoading(this.webContents, true);
                emitNavigationStart(this.webContents, this.url, true);
                emit({event: "nativeCall", method: "webContents.reload",
                    contentsId: this.webContents.id});
            };
            this.webContents.reloadIgnoringCache = () => {
                setWebContentsMainFrameLoading(this.webContents, true);
                setWebContentsLoading(this.webContents, true);
                emitNavigationStart(this.webContents, this.url, true);
                emit({event: "nativeCall", method: "webContents.reloadIgnoringCache",
                    contentsId: this.webContents.id});
            };
            this.webContents.stop = () => {
                this.finishLoad(new Error("navigation stopped"));
                setWebContentsMainFrameLoading(this.webContents, false);
                setWebContentsLoading(this.webContents, false);
                setWebContentsWaitingForResponse(this.webContents, false);
                emit({event: "nativeCall", method: "webContents.stop",
                    contentsId: this.webContents.id});
            };
            this.webContents.send = (channel, ...args) => {
                emit({event: "rendererMessage", windowId: this.id,
                    contentsId: this.webContents.id, channel, args});
            };
            this.webContents.insertCSS = css => {
                if (this.webContents.destroyed) {
                    return Promise.reject(new Error("webContents was destroyed"));
                }
                const key = `nexa-electron-css-${this.id}-${++nextCssId}`;
                this.webContents.send("__nexa:insert-css", key, String(css));
                return Promise.resolve(key);
            };
            this.webContents.removeInsertedCSS = key => {
                if (this.webContents.destroyed) {
                    return Promise.reject(new Error("webContents was destroyed"));
                }
                this.webContents.send("__nexa:remove-css", String(key));
                return Promise.resolve();
            };
            this.webContents.clearCache = async () => {
                if (this.webContents.destroyed) {
                    throw new Error("webContents was destroyed");
                }
                await nativeCall({method: "webContents.clearCache",
                    contentsId: this.webContents.id});
            };
            installDevTools(this.webContents, this.webContents.id);
            this.webContents.setWindowOpenHandler = handler => {
                this.webContents.windowOpenHandler = handler;
                this.windowOpenHandler = handler;
            };
            this.webContents.setBackgroundThrottling = enabled => {
                const allowed = Boolean(enabled);
                if (this.webContents.backgroundThrottling === allowed) return;
                this.webContents.backgroundThrottling = allowed;
                this.notifyState("backgroundThrottling", {
                    enabled: allowed
                });
            };
            contents.set(this.id, this.webContents);
            contentsOwners.set(this.id, this.id);
            windows.set(this.id, this);
            activeWindow = this;
            this.contentView = new View({bounds: {
                x: 0, y: 0, width: options.width || 800, height: options.height || 600
            }, backgroundColor: options.backgroundColor || "#ffffff"});
            this.contentView._setOwnerWindow(this);
            this.webContentsView = new WebContentsView({webContents: this.webContents,
                backgroundColor: options.backgroundColor || "#ffffff",
                bounds: {x: 0, y: 0, width: options.width || 800, height: options.height || 600}});
            this.contentView.addChildView(this.webContentsView);
            app.emit("browser-window-created", {}, this);
            app.emit("web-contents-created", {}, this.webContents);
            this._syncMenu();
        }

        _syncMenu() {
            const menu = this._menuExplicit ? this.menu : applicationMenu;
            this.webContents.send("__nexa:menu:application",
                this.menuBarVisible && menu ? serializeMenuItems(menu.items) : []);
        }

        notifyState(action, value = {}) {
            emit({event: "windowState", windowId: this.id, action, value});
        }

        loadURL(url, loadURLOptions = {}) {
            let loadOptions;
            try {
                loadOptions = normalizeLoadURLOptions(loadURLOptions);
            } catch (error) {
                return Promise.reject(error);
            }
            if (loadOptions.userAgent !== undefined) {
                this.webContents.setUserAgent(loadOptions.userAgent);
            }
            this.webContents._loadOptions = loadOptions;
            this.url = String(url);
            if (this.url.startsWith("file:")) {
                const file = path.resolve(fileURLToPath(this.url));
                if (file !== appRoot && !file.startsWith(appRoot + path.sep)) {
                    return Promise.reject(new Error("renderer file escapes the app root"));
                }
                this.url = localUrl(path.relative(appRoot, file).split(path.sep).map(encodeURIComponent).join("/"));
            }
            try {
                const parsed = new URL(this.url);
                if (parsed.protocol === "http:" || parsed.protocol === "https:") {
                    trustOrigin?.(parsed.origin);
                }
            } catch (_) {}
            activeWindow = this;
            this.finishLoad(new Error("navigation interrupted"));
            let resolveLoad;
            let rejectLoad;
            const load = new Promise((resolve, reject) => {
                resolveLoad = resolve;
                rejectLoad = reject;
            });
            load.catch(() => {});
            this.pendingLoad = {resolve: resolveLoad, reject: rejectLoad};
            setWebContentsMainFrameLoading(this.webContents, true);
            setWebContentsLoading(this.webContents, true);
            emitNavigationStart(this.webContents, this.url, true);
            emit({event: "renderer", windowId: this.id, contentsId: this.webContents.id,
                url: this.url,
                preloadUrl: localUrl(`__nexa/preload/${this.id}`), title: this.getTitle(),
                options: this.options, profileName: this.session.profileName,
                profileTemporary: this.session.temporary,
                webRequestEnabled: this.session.webRequestEnabled,
                userAgent: this.webContents.getUserAgent(),
                loadOptions,
                visible: this.visible,
                protocolSchemes: [...privilegedSchemes.keys()]});
            return load;
        }

        finishLoad(error = null) {
            const pending = this.pendingLoad;
            this.pendingLoad = null;
            setWebContentsMainFrameLoading(this.webContents, false);
            setWebContentsLoading(this.webContents, false);
            if (error) setWebContentsWaitingForResponse(this.webContents, false);
            if (!pending) return;
            if (error) pending.reject(error);
            else pending.resolve();
        }

        getPreloadResponse() {
            const paths = [...this.session.getPreloads(), this.preloadPath].filter(Boolean);
            const files = paths.map(preloadFileInfo);
            const preferences = this.options?.webPreferences || {};
            const contextIsolation = preferences.contextIsolation !== false;
            const nodeIntegration = preferences.nodeIntegration === true;
            const rendererProcess = {
                arch: process.arch,
                argv: process.argv,
                pid: process.pid,
                versions: {
                    node: process.versions.node,
                    electron: process.versions.electron || ""
                },
                env: Object.fromEntries(["HOME", "TMPDIR", "XDG_CONFIG_HOME",
                    "XDG_CACHE_HOME", "LANG", "TERM", "PATH"].filter(name =>
                    process.env[name] !== undefined).map(name => [name, process.env[name]]))
            };
            const schemes = [...privilegedSchemes.keys()];
            const cacheKey = JSON.stringify([files, schemes, rendererProcess,
                contextIsolation, nodeIntegration]);
            const cachedEntry = preloadScriptCache.get(cacheKey);
            if (cachedEntry !== undefined) {
                preloadScriptCache.delete(cacheKey);
                preloadScriptCache.set(cacheKey, cachedEntry);
                return {source: cachedEntry.script, etag: cachedEntry.etag};
            }
            const preloads = paths.map((file, index) => {
                const info = files[index];
                const filename = JSON.stringify(info.absolute);
                const dirname = JSON.stringify(path.dirname(info.absolute));
                const source = readPreload(file, info).replace(/^#!.*(?:\r?\n|$)/, "");
                const extension = path.extname(info.absolute).toLowerCase();
                const commonJs = extension === ".cjs" ||
                    (extension === ".js" && packageData.type !== "module");
                const script = commonJs
                    ? `(() => {
                        const process = window.process;
                        const global = window.global;
                        const Buffer = window.Buffer;
                        const require = window.__NEXA_ELECTRON_CREATE_REQUIRE__(${filename});
                        const module = {id: ${filename}, filename: ${filename},
                            exports: {}, loaded: false};
                        module.require = require;
                        const exports = module.exports;
                        try {
                            (function(exports, require, module, __filename, __dirname) {
${source}\n                        }).call(module.exports, exports, require, module,
                            ${filename}, ${dirname});
                            module.loaded = true;
                        } catch (error) {
                            console.error("nexa electron preload failed:", ${filename}, error);
                        }
                    })();`
                    : source;
                return `window.__NEXA_ELECTRON_PRELOAD_BASE__=${filename};\n${script}`;
            }).join("\n");
            const bridge = `(() => {
                const schemes = new Set(${JSON.stringify(schemes)});
                const ipc = window.nexaElectron?.ipcRenderer;
                if (!schemes.size || !ipc || typeof window.fetch !== "function") return;
                const originalFetch = window.fetch.bind(window);
                window.fetch = async (input, init) => {
                    const sourceRequest = input instanceof Request ? input : null;
                    const url = sourceRequest ? sourceRequest.url : String(input);
                    let parsed;
                    try { parsed = new URL(url); } catch (_) { return originalFetch(input, init); }
                    if (!schemes.has(parsed.protocol.slice(0, -1))) return originalFetch(input, init);
                    const method = String(init?.method || sourceRequest?.method || "GET").toUpperCase();
                    const headers = new Headers(init?.headers || sourceRequest?.headers);
                    let body = init && Object.prototype.hasOwnProperty.call(init, "body")
                        ? init.body : sourceRequest?.body;
                    if (method === "GET" || method === "HEAD") body = null;
                    const prepared = new Request("http://nexa.invalid/", {
                        method, headers, body, duplex: "half",
                        signal: init?.signal || sourceRequest?.signal
                    });
                    const bodyBytes = body === null ? undefined : await prepared.arrayBuffer();
                    const requestMetadata = {
                        url, method, headers: Object.fromEntries(prepared.headers),
                        initiatorOrigin: location.origin,
                        body: ""
                    };
                    if (bodyBytes) {
                        let binary = "";
                        const bytes = new Uint8Array(bodyBytes);
                        for (let offset = 0; offset < bytes.length; offset += 0x8000) {
                            binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
                        }
                        requestMetadata.body = btoa(binary);
                    }
                    const result = await ipc.invoke("nexa:electron:protocol", requestMetadata);
                    if (result.status === 0) return Response.error();
                    const responseBinary = atob(result.body || "");
                    const responseBytes = new Uint8Array(responseBinary.length);
                    for (let index = 0; index < responseBinary.length; index++) {
                        responseBytes[index] = responseBinary.charCodeAt(index);
                    }
                    const responseBody = responseBytes.length && result.status !== 204 && result.status !== 304
                        ? responseBytes : null;
                    return new Response(responseBody, {
                        status: result.status, statusText: result.statusText,
                        headers: result.headers
                    });
                };
            })();`;
            const processSetup = `window.__NEXA_ELECTRON_PROCESS__=${JSON.stringify(rendererProcess)};` +
                `if(window.process){Object.assign(window.process,window.__NEXA_ELECTRON_PROCESS__);` +
                `window.process.env=window.__NEXA_ELECTRON_PROCESS__.env;}`;
            const pageShimCleanup = contextIsolation && !nodeIntegration ? `(() => {
                for (const name of ["require", "process", "global", "Buffer", "electron",
                    "nexaElectron", "__NEXA_ELECTRON_PROCESS__",
                    "__NEXA_ELECTRON_CREATE_REQUIRE__"]) {
                    try { delete window[name]; } catch (_) {}
                }
            })();` : "";
            const script = `${processSetup}\n${preloads}\n${bridge}\n${pageShimCleanup}`;
            const scriptBytes = Buffer.byteLength(script);
            const etag = `"${createHash("sha256").update(script).digest("hex")}"`;
            if (scriptBytes <= maxPreloadScriptCacheBytes) {
                preloadScriptCache.set(cacheKey, {script, etag, bytes: scriptBytes});
                preloadScriptCacheBytes += scriptBytes;
                while (preloadScriptCache.size > 2 ||
                    preloadScriptCacheBytes > maxPreloadScriptCacheBytes) {
                    const oldestKey = preloadScriptCache.keys().next().value;
                    const oldest = preloadScriptCache.get(oldestKey);
                    preloadScriptCacheBytes -= oldest.bytes;
                    preloadScriptCache.delete(oldestKey);
                }
            }
            return {source: script, etag};
        }

        loadFile(file, options = {}) {
            const absolute = path.resolve(appRoot, file);
            if (absolute !== appRoot && !absolute.startsWith(appRoot + path.sep)) {
                return Promise.reject(new Error("loadFile path escapes the app root"));
            }
            if (!fs.existsSync(absolute)) return Promise.reject(new Error(`renderer file not found: ${file}`));
            const relative = path.relative(appRoot, absolute).split(path.sep).map(encodeURIComponent).join("/");
            const url = new URL(localUrl(relative));
            try {
                applyLoadFileOptions(url, options);
            } catch (error) {
                return Promise.reject(error);
            }
            const {query, search, hash, ...navigationOptions} = options;
            return this.loadURL(url.href, navigationOptions);
        }

        show() {
            this.visible = true;
            if (activeWindow && activeWindow !== this) activeWindow.focused = false;
            activeWindow = this;
            this.focused = true;
            this.emit("show");
            publishEmbeddedTree(this.contentView);
            this.notifyState("show");
        }

        hide() {
            this.visible = false;
            this.focused = false;
            this.emit("hide");
            publishEmbeddedTree(this.contentView);
            this.notifyState("hide");
        }
        focus() {
            if (activeWindow && activeWindow !== this) activeWindow.focused = false;
            activeWindow = this;
            this.focused = true;
            this.emit("focus");
            this.notifyState("focus");
        }
        blur() { this.focused = false; this.emit("blur"); this.notifyState("blur"); }
        minimize() {
            this.minimized = true;
            this.focused = false;
            this.emit("minimize");
            publishEmbeddedTree(this.contentView);
            this.notifyState("minimize");
        }
        restore() {
            this.minimized = false;
            this.visible = true;
            if (activeWindow && activeWindow !== this) activeWindow.focused = false;
            activeWindow = this;
            this.focused = true;
            this.emit("restore");
            publishEmbeddedTree(this.contentView);
            this.notifyState("restore");
        }
        maximize() { this.maximized = true; this.emit("maximize"); this.notifyState("maximize"); }
        unmaximize() { this.maximized = false; this.emit("unmaximize"); this.notifyState("unmaximize"); }
        isMaximized() { return this.maximized; }
        isMinimized() { return this.minimized; }
        isVisible() { return this.visible; }
        isFocused() { return this.focused; }
        isDestroyed() { return this.destroyed; }
        isNormal() { return !this.minimized && !this.maximized && !this.isFullScreen(); }
        getBounds() {
            return {x: this.options.x || 0, y: this.options.y || 0,
                width: this.options.width || 800, height: this.options.height || 600};
        }
        getContentBounds() { return this.getBounds(); }
        getSize() { return [this.options.width || 800, this.options.height || 600]; }
        getContentSize() { return this.getSize(); }
        getPosition() { return [this.options.x || 0, this.options.y || 0]; }
        center() {
            const area = display.workArea;
            const {width, height} = this.getBounds();
            this.setPosition(area.x + Math.round((area.width - width) / 2),
                area.y + Math.round((area.height - height) / 2));
        }
        setTitle(title) {
            this.title = String(title);
            this.notifyState("title", {title: this.title});
        }
        getTitle() { return this.title || ""; }
        setBackgroundColor(color) {
            this.contentView.setBackgroundColor(color);
            this.webContentsView.setBackgroundColor(color);
            this.options.backgroundColor = this.webContentsView.backgroundColor;
        }
        getBackgroundColor() { return this.webContentsView.getBackgroundColor(); }
        setAutoHideMenuBar(autoHide) {
            this.autoHideMenuBar = Boolean(autoHide);
            this.setMenuBarVisibility(!this.autoHideMenuBar);
        }
        isAutoHideMenuBar() { return Boolean(this.autoHideMenuBar); }
        isMenuBarVisible() { return this.menuBarVisible; }
        setMenuBarVisibility(visible) {
            this.menuBarVisible = Boolean(visible);
            this._syncMenu();
        }
        setMenu(menu) {
            if (menu !== null && !(menu instanceof Menu)) {
                throw new TypeError("window menu must be a Menu or null");
            }
            this._menuExplicit = true;
            this.menu = menu;
            this._syncMenu();
        }
        setProgressBar() {}
        setBounds(bounds) {
            const previous = this.getBounds();
            Object.assign(this.options, bounds);
            this.contentView.setBounds({x: 0, y: 0,
                width: this.options.width || 800, height: this.options.height || 600});
            this.webContentsView.setBounds({x: 0, y: 0,
                width: this.options.width || 800, height: this.options.height || 600});
            const next = this.getBounds();
            if (previous.width !== next.width || previous.height !== next.height) {
                this.emit("resize");
            }
            if (previous.x !== next.x || previous.y !== next.y) this.emit("move");
            this.notifyState("resize", {
                width: this.options.width, height: this.options.height,
                x: this.options.x, y: this.options.y
            });
        }
        setSize(width, height) {
            const changed = this.options.width !== width || this.options.height !== height;
            this.options.width = width;
            this.options.height = height;
            this.contentView.setBounds({x: 0, y: 0, width, height});
            this.webContentsView.setBounds({x: 0, y: 0, width, height});
            if (changed) this.emit("resize");
            this.notifyState("resize", {width, height});
        }
        setPosition(x, y) {
            const changed = this.options.x !== x || this.options.y !== y;
            this.options.x = x;
            this.options.y = y;
            if (changed) this.emit("move");
            this.notifyState("move", {x, y});
        }
        setResizable(resizable) {
            this.resizable = Boolean(resizable);
            this.notifyState("resizable", {enabled: this.resizable});
        }
        isResizable() { return this.resizable; }
        setContentSize(width, height) { this.setSize(width, height); }
        setContentBounds(bounds) { this.setBounds(bounds); }
        setContentView(view) {
            if (!(view instanceof View)) throw new TypeError("contentView must be an Electron View");
            if (this.contentView === view) return;
            this.contentView._setOwnerWindow(null);
            this.contentView = view;
            view.parent?.removeChildView(view);
            view._setOwnerWindow(this);
            publishEmbeddedTree(view);
        }
        setFullScreen(value) {
            this.fullScreen = Boolean(value);
            this.notifyState("fullscreen", {enabled: this.fullScreen});
        }
        isFullScreen() { return Boolean(this.fullScreen); }
        close() {
            let prevented = false;
            this.emit("close", {preventDefault() { prevented = true; }});
            if (!prevented) this.destroy();
        }
        destroy() {
            this.finishLoad(new Error("window was destroyed"));
            setWebContentsWaitingForResponse(this.webContents, false);
            this.destroyed = true;
            for (const webContents of [...contents.values()]) {
                if (webContents.id !== this.id && contentsOwners.get(webContents.id) === this.id) {
                    webContents.destroy?.();
                }
            }
            for (const child of [...this.contentView.children]) this.contentView.removeChildView(child);
            for (const [key, pending] of pendingScriptExecutions) {
                if (!key.startsWith(`${this.id}:`)) continue;
                pendingScriptExecutions.delete(key);
                pending.reject(new Error("webContents was destroyed"));
            }
            const fileHandleFds = new Set();
            for (const [id, entry] of fsHandles) {
                if (entry.windowId !== this.id) continue;
                fsHandles.delete(id);
                fileHandleFds.add(entry.handle.fd);
                fsFds.delete(entry.handle.fd);
                void entry.handle.close().catch(() => {});
            }
            for (const [fd, owner] of fsFds) {
                if (owner !== this.id) continue;
                fsFds.delete(fd);
                if (fileHandleFds.has(fd)) continue;
                try { fs.closeSync(fd); } catch (_) {}
            }
            for (const [id, entry] of fsStreams) {
                if (entry.windowId !== this.id) continue;
                fsStreams.delete(id);
                entry.stream.destroy();
            }
            for (const [id, entry] of nativeModules) {
                if (entry.windowId === this.id) nativeModules.delete(id);
            }
            for (const [id, entry] of nativeObjectHandles) {
                if (entry.windowId === this.id) nativeObjectHandles.delete(id);
            }
            windows.delete(this.id);
            contentsOwners.delete(this.id);
            this.webContents.destroyed = true;
            sessionWebContents.get(this.webContents.session).delete(this.webContents);
            contents.delete(this.id);
            closeRendererPopupsForContents(this.id);
            this.webContents.emit("destroyed");
            this.emit("closed");
            emit({event: "windowClosed", windowId: this.id});
            removeElectronEventSubscriptions(this.id);
            if (activeWindow === this) activeWindow = windows.values().next().value || null;
        }

        static getAllWindows() { return [...windows.values()]; }
        static getFocusedWindow() { return activeWindow; }
        static fromId(id) { return windows.get(id) || null; }
        static fromWebContents(webContents) {
            const id = webContents?.id;
            return windows.get(contentsOwners.get(id) ?? id) || null;
        }
    }

    function windowCommand(message) {
        const window = windows.get(message.windowId);
        if (!window) return false;
        if (message.action === "close") window.close();
        else if (message.action === "focus") window.focus();
        else return false;
        return true;
    }

    function updateWindowGeometry(message) {
        const window = windows.get(message.windowId);
        const {x, y, width, height} = message;
        const geometry = [x, y, width, height].map(Number);
        if (!window || window.destroyed ||
            !geometry.every(Number.isFinite) || geometry[2] <= 0 || geometry[3] <= 0) {
            return false;
        }
        const [nextX, nextY, nextWidth, nextHeight] = geometry;
        const moved = window.options.x !== nextX || window.options.y !== nextY;
        const resized = window.options.width !== nextWidth ||
            window.options.height !== nextHeight;
        const maximized = typeof message.maximized === "boolean"
            ? message.maximized : window.maximized;
        const notifyEvents = window.nativeGeometryReady;
        const maximizeChanged = window.maximized !== maximized;
        window.options.x = nextX;
        window.options.y = nextY;
        window.options.width = nextWidth;
        window.options.height = nextHeight;
        window.maximized = maximized;
        window.nativeGeometryReady = true;
        if (resized) {
            window.contentView.setBounds({x: 0, y: 0, width: nextWidth, height: nextHeight});
            window.webContentsView.setBounds({x: 0, y: 0, width: nextWidth, height: nextHeight});
            if (notifyEvents) window.emit("resize");
        }
        if (notifyEvents && moved) window.emit("move");
        if (notifyEvents && maximizeChanged) {
            window.emit(maximized ? "maximize" : "unmaximize");
        }
        return true;
    }

    const ipcMain = new EventEmitter();
    const autoUpdater = new EventEmitter();
    ipcMain.handle = (channel, callback) => {
        if (handlers.has(channel)) throw new Error(`duplicate ipcMain handler: ${channel}`);
        handlers.set(channel, callback);
    };
    ipcMain.handleOnce = (channel, callback) => {
        const once = async (event, ...args) => {
            handlers.delete(channel);
            return callback(event, ...args);
        };
        ipcMain.handle(channel, once);
    };
    ipcMain.removeHandler = channel => handlers.delete(channel);

    ipcMain.on("nexa:electron:object:call", (event, type, id, method, args = []) => {
        args = decodeFsValue(args);
        const window = windows.get(id);
        const target = type === "window" ? window
            : type === "webContents" ? contents.get(id) : null;
        if (!target || target.destroyed || !Array.isArray(args)) {
            event.returnValue = undefined;
            return;
        }
        const windowCalls = {
            close: () => target.close(), destroy: () => target.destroy(),
            show: () => target.show(), hide: () => target.hide(),
            focus: () => target.focus(), blur: () => target.blur(),
            minimize: () => target.minimize(), restore: () => target.restore(),
            maximize: () => target.maximize(), unmaximize: () => target.unmaximize(),
            isMaximized: () => target.isMaximized(), isMinimized: () => target.isMinimized(),
            isVisible: () => target.isVisible(), isFocused: () => target.isFocused(),
            isDestroyed: () => target.isDestroyed(), isFullScreen: () => target.isFullScreen(),
            isNormal: () => target.isNormal(), isResizable: () => target.isResizable(),
            getBounds: () => target.getBounds(), getSize: () => target.getSize(),
            getContentBounds: () => target.getContentBounds(),
            getContentSize: () => target.getContentSize(),
            getPosition: () => target.getPosition(), getTitle: () => target.getTitle(),
            getBackgroundColor: () => target.getBackgroundColor(),
            center: () => target.center(),
            setTitle: () => target.setTitle(args[0]), setBounds: () => target.setBounds(args[0]),
            setContentBounds: () => target.setContentBounds(args[0]),
            setSize: () => target.setSize(args[0], args[1]),
            setContentSize: () => target.setContentSize(args[0], args[1]),
            setPosition: () => target.setPosition(args[0], args[1]),
            setFullScreen: () => target.setFullScreen(args[0]),
            setResizable: () => target.setResizable(args[0]),
            setMenuBarVisibility: () => target.setMenuBarVisibility(args[0]),
            isMenuBarVisible: () => target.isMenuBarVisible(),
            setAutoHideMenuBar: () => target.setAutoHideMenuBar(args[0]),
            isAutoHideMenuBar: () => target.isAutoHideMenuBar(),
            setBackgroundColor: () => target.setBackgroundColor(args[0]),
            setBackgroundThrottling: () => target.webContents.setBackgroundThrottling(args[0])
        };
        const webContentsCalls = {
            getURL: () => target.getURL(), getTitle: () => target.getTitle(),
            getType: () => target.getType(), getProcessId: () => target.getProcessId(),
            getOSProcessId: () => target.getOSProcessId(),
            isCrashed: () => target.isCrashed(),
            getUserAgent: () => target.getUserAgent(),
            setUserAgent: () => target.setUserAgent(args[0]),
            getZoomFactor: () => target.getZoomFactor(),
            setZoomFactor: () => target.setZoomFactor(args[0]),
            getZoomLevel: () => target.getZoomLevel(),
            setZoomLevel: () => target.setZoomLevel(args[0]),
            getBackgroundThrottling: () => target.getBackgroundThrottling(),
            undo: () => target.undo(), redo: () => target.redo(),
            cut: () => target.cut(), copy: () => target.copy(),
            paste: () => target.paste(), pasteAndMatchStyle: () => target.pasteAndMatchStyle(),
            delete: () => target.delete(), selectAll: () => target.selectAll(),
            unselect: () => target.unselect(), centerSelection: () => target.centerSelection(),
            scrollToTop: () => target.scrollToTop(),
            scrollToBottom: () => target.scrollToBottom(),
            replace: () => target.replace(args[0]),
            replaceMisspelling: () => target.replaceMisspelling(args[0]),
            isLoading: () => target.isLoading(),
            isLoadingMainFrame: () => target.isLoadingMainFrame(),
            isWaitingForResponse: () => target.isWaitingForResponse(),
            "navigationHistory.canGoBack": () => target.navigationHistory.canGoBack(),
            "navigationHistory.canGoForward": () => target.navigationHistory.canGoForward(),
            "navigationHistory.canGoToOffset": () => target.navigationHistory.canGoToOffset(args[0]),
            "navigationHistory.clear": () => target.navigationHistory.clear(),
            "navigationHistory.getActiveIndex": () => target.navigationHistory.getActiveIndex(),
            "navigationHistory.getEntryAtIndex": () => target.navigationHistory.getEntryAtIndex(args[0]),
            "navigationHistory.goBack": () => target.navigationHistory.goBack(),
            "navigationHistory.goForward": () => target.navigationHistory.goForward(),
            "navigationHistory.goToIndex": () => target.navigationHistory.goToIndex(args[0]),
            "navigationHistory.goToOffset": () => target.navigationHistory.goToOffset(args[0]),
            "navigationHistory.length": () => target.navigationHistory.length(),
            "navigationHistory.removeEntryAtIndex": () => target.navigationHistory.removeEntryAtIndex(args[0]),
            "navigationHistory.getAllEntries": () => target.navigationHistory.getAllEntries(),
            "navigationHistory.restore": () => target.navigationHistory.restore(args[0]),
            focus: () => target.focus(),
            canGoBack: () => target.canGoBack(), canGoForward: () => target.canGoForward(),
            getNavigationHistory: () => target.getNavigationHistory(),
            goBack: () => target.goBack(), goForward: () => target.goForward(),
            isDevToolsOpened: () => target.isDevToolsOpened(),
            clearHistory: () => target.clearHistory(),
            isDestroyed: () => target.isDestroyed(),
            send: () => target.send(args[0], ...args.slice(1)),
            stop: () => target.stop(),
            clearCache: () => target.clearCache(),
            setBackgroundThrottling: () => target.setBackgroundThrottling(args[0]),
            openDevTools: () => target.openDevTools(args[0]),
            closeDevTools: () => target.closeDevTools()
        };
        const call = (type === "window" ? windowCalls : webContentsCalls)[method];
        if (typeof call !== "function") {
            event.returnValue = {__nexaType: "FsError", value: {
                message: `unsupported Electron ${type} method: ${method}`, code: "ENOSYS"
            }};
            return;
        }
        try {
            event.returnValue = encodeFsValue(call());
        } catch (error) {
            event.returnValue = {__nexaType: "FsError", value: {
                message: error.message, code: error.code || "ERR_ELECTRON_CALL"
            }};
        }
    });

    ipcMain.on("__nexa:execute-js-result", (event, requestId, result) => {
        const key = `${event.sender?.id}:${requestId}`;
        const pending = pendingScriptExecutions.get(key);
        if (!pending) return;
        pendingScriptExecutions.delete(key);
        if (result?.error) pending.reject(new Error(String(result.error)));
        else pending.resolve(decodeFsValue(result?.value));
    });

    ipcMain.handle("nexa:electron:object:invoke", async (_event, type, id, method, args = []) => {
        args = decodeFsValue(args);
        const target = type === "window" ? windows.get(id)
            : type === "webContents" ? contents.get(id) : null;
        if (!target || target.destroyed || !Array.isArray(args)) {
            throw new Error(`Electron ${type} was destroyed`);
        }
        if (method === "loadURL") return await target.loadURL(args[0], args[1]);
        if (method === "loadFile") return target.loadFile(args[0], args[1]);
        if (method === "reload" && type === "webContents") return target.reload();
        if (method === "reloadIgnoringCache" && type === "webContents") {
            return target.reloadIgnoringCache();
        }
        if (method === "executeJavaScript" && type === "webContents") {
            return target.executeJavaScript(args[0], args[1]);
        }
        if (method === "insertText" && type === "webContents") {
            return await target.insertText(args[0]);
        }
        if (method === "insertCSS" && type === "webContents") {
            return target.insertCSS(args[0]);
        }
        if (method === "removeInsertedCSS" && type === "webContents") {
            return target.removeInsertedCSS(args[0]);
        }
        if (method === "clearCache" && type === "webContents") {
            return await target.clearCache();
        }
        if (method === "executeJavaScriptInIsolatedWorld" && type === "webContents") {
            if (args[0] !== 0) throw new Error("isolated JavaScript worlds are unavailable in Nexa WebView");
            const scripts = args[1];
            return target.executeJavaScript((scripts || []).map(script => script.code || "").join("\n"),
                args[2]);
        }
        throw new Error(`unsupported Electron ${type} async method: ${method}`);
    });

    ipcMain.on("__nexa:window-open", (event, details) => {
        const opener = BrowserWindow.fromWebContents(event.sender);
        if (!opener || opener.isDestroyed() || !details ||
            typeof details.url !== "string") return;
        let url;
        try {
            url = new URL(details.url, opener.url);
        } catch (_) {
            return;
        }
        if (!["http:", "https:"].includes(url.protocol)) return;
        const request = {
            url: url.href,
            frameName: String(details.frameName || ""),
            features: String(details.features || "")
        };
        if (request.frameName && request.frameName !== "_blank" &&
            request.frameName[0] !== "_") {
            const namedWindow = [...windows.values()].find(window =>
                window !== opener && !window.destroyed && window.name === request.frameName);
            if (namedWindow) {
                namedWindow.loadURL(url.href).catch(() => {});
                namedWindow.show();
                namedWindow.focus();
                return;
            }
        }
        let decision = {action: "allow"};
        try {
            const handler = event.sender?.windowOpenHandler || opener.webContents.windowOpenHandler;
            if (typeof handler === "function") {
                decision = handler(request) || {action: "deny"};
            }
        } catch (error) {
            emit({event: "mainError", error: `window open handler failed: ${error.message}`});
            return;
        }
        if (decision.action !== "allow") return;

        const options = {...(decision.overrideBrowserWindowOptions || {})};
        for (const [key, option] of Object.entries({width: "width", height: "height",
            left: "x", top: "y"})) {
            const match = new RegExp(`(?:^|,)\\s*${key}\\s*=\\s*(\\d+)`, "i")
                .exec(request.features);
            if (match && options[option] === undefined) options[option] = Number(match[1]);
        }
        options.title ||= opener.title;
        if (typeof decision.createWindow === "function") {
            const preferences = {
                ...(opener.options.webPreferences || {}),
                ...(options.webPreferences || {})
            };
            const childContents = createEmbeddedContents({
                title: options.title, webPreferences: preferences
            });
            childContents.ownerWindowId = opener.id;
            contentsOwners.set(childContents.id, opener.id);
            try {
                const created = decision.createWindow({...options, webContents: childContents});
                const returnedContents = created?.webContents || created;
                if (returnedContents && contents.has(returnedContents.id)) {
                    returnedContents.ownerWindowId = opener.id;
                    contentsOwners.set(returnedContents.id, opener.id);
                    returnedContents.loadURL(url.href).catch(error =>
                        emit({event: "mainError", error: `child renderer failed: ${error.message}`}));
                } else {
                    childContents.destroy();
                }
            } catch (error) {
                childContents.destroy();
                emit({event: "mainError", error: `custom window creation failed: ${error.stack || error}`});
            }
            return;
        }
        const child = new BrowserWindow(options);
        child.name = request.frameName;
        child.loadURL(url.href).catch(() => {});
        child.show();
        opener.webContents.emit("did-create-window", child, {
            options, frameName: request.frameName
        });
    });

    const app = new EventEmitter();
    let ready = false;
    const markReady = () => {
        if (ready) return;
        ready = true;
        app.emit("ready");
    };
    let packageData = {};
    try {
        packageData = JSON.parse(fs.readFileSync(path.join(appRoot, "package.json"), "utf8"));
    } catch (_) {}
    if (!process.versions.electron) {
        const declaredElectron = packageData.electronVersion ||
            packageData.devDependencies?.electron || packageData.dependencies?.electron;
        const version = typeof declaredElectron === "string"
            ? declaredElectron.match(/\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?/)?.[0]
            : null;
        if (version) {
            Object.defineProperty(process.versions, "electron", {
                value: version,
                enumerable: true
            });
        }
    }
    let appName = packageData.productName || packageData.name || path.basename(appPath);
    let desktopName = "";
    let userDataPath = process.env.NEXA_ELECTRON_USER_DATA ||
        path.join(process.env.HOME || appRoot, ".config", "nexa", "apps", appName);
    const pathOverrides = new Map();
    function preloadFileInfo(file) {
        const absolute = path.resolve(appRoot, file);
        let realFile;
        try {
            realFile = fs.realpathSync(absolute);
        } catch (_) {
            throw new Error(`Electron preload does not exist: ${file}`);
        }
        let trusted = isTrustedPath(realFile);
        if (!trusted) {
            try {
                const realUserData = fs.realpathSync(userDataPath);
                trusted = realFile.startsWith(realUserData + path.sep);
            } catch (_) {}
        }
        if (!trusted) {
            throw new Error(`Electron preload is outside the app and user data directories: ${file}`);
        }
        const stat = fs.statSync(realFile);
        return {absolute, realFile, size: stat.size, mtimeMs: stat.mtimeMs};
    }

    function readPreload(file, info = preloadFileInfo(file)) {
        return fs.readFileSync(info.realFile, "utf8");
    }
    let previousCpu = process.cpuUsage();
    let previousCpuTime = process.hrtime.bigint();
    app.isReady = () => ready;
    app.whenReady = () => ready ? Promise.resolve(app) : new Promise(resolve => {
        app.once("ready", () => resolve(app));
    });
    app.quit = () => {
        let prevented = false;
        app.emit("before-quit", {preventDefault() { prevented = true; }});
        if (prevented) return;
        app.emit("will-quit", {preventDefault() { prevented = true; }});
        if (prevented) return;
        app.emit("quit", 0);
        emit({event: "appQuit"});
    };
    app.exit = code => emit({event: "appExit", code});
    app.relaunch = options => emit({event: "appRelaunch", options});
    app.name = appName;
    app.isPackaged = true;
    app.getName = () => appName;
    app.getVersion = () => packageData.version || "0.0.0";
    app.setVersion = version => { packageData.version = String(version); };
    app.getLocale = app.getSystemLocale = () =>
        Intl.DateTimeFormat().resolvedOptions().locale || "en-US";
    app.getAppPath = () => appPath;
    app.getAppMetrics = () => {
        const cpu = process.cpuUsage();
        const now = process.hrtime.bigint();
        const elapsed = Number(now - previousCpuTime) / 1000;
        const percentCPUUsage = elapsed > 0
            ? ((cpu.user - previousCpu.user) + (cpu.system - previousCpu.system)) / elapsed * 100
            : 0;
        previousCpu = cpu;
        previousCpuTime = now;
        return [{
            pid: process.pid,
            type: "Browser",
            memory: {workingSetSize: Math.round(process.memoryUsage().rss / 1024), peakWorkingSetSize: 0},
            cpu: {percentCPUUsage, idleWakeupsPerSecond: 0}
        }];
    };
    app.getGPUInfo = async () => ({gpuDevice: []});
    app.getPath = name => {
        if (pathOverrides.has(name)) return pathOverrides.get(name);
        const home = process.env.HOME || appRoot;
        const paths = {
            home,
            appData: process.env.XDG_CONFIG_HOME || path.join(home, ".config"),
            userData: userDataPath,
            sessionData: userDataPath,
            cache: process.env.XDG_CACHE_HOME || path.join(home, ".cache"),
            downloads: path.join(home, "Downloads"),
            documents: path.join(home, "Documents"),
            desktop: path.join(home, "Desktop"),
            pictures: path.join(home, "Pictures"),
            videos: path.join(home, "Videos"),
            music: path.join(home, "Music"),
            logs: path.join(userDataPath, "logs"),
            temp: process.env.TMPDIR || "/tmp",
            exe: executablePath,
            module: appPath,
            assets: resourcesPath,
            crashDumps: path.join(userDataPath, "Crashpad", "reports")
        };
        const value = paths[name];
        if (!value) throw new Error(`unknown Electron path: ${name}`);
        if (name === "userData" || name === "logs") {
            fs.mkdirSync(value, {recursive: true});
        }
        return value;
    };
    ipcMain.on("nexa:electron:app:getPath", (event, name) => {
        try { event.returnValue = app.getPath(name); }
        catch (error) { event.returnValue = encodeFsError(error); }
    });
    ipcMain.on("nexa:electron:app:getVersion", event => {
        event.returnValue = app.getVersion();
    });
    ipcMain.on("nexa:electron:app:getName", event => {
        event.returnValue = app.getName();
    });
    ipcMain.on("nexa:electron:app:getAppPath", event => {
        event.returnValue = app.getAppPath();
    });
    ipcMain.on("nexa:electron:webFrame:setZoomFactor", (event, factor) => {
        try { event.sender?.setZoomFactor(factor); }
        catch (error) { event.returnValue = encodeFsError(error); }
    });
    ipcMain.on("nexa:electron:webFrame:clearCache", event => {
        emit({event: "nativeCall", windowId: event.sender?.id,
            method: "webFrame.clearCache", args: []});
    });
    ipcMain.on("nexa:electron:crashReporter:getParameters", event => {
        event.returnValue = crashReporter.getParameters();
    });
    ipcMain.on("nexa:electron:crashReporter:addExtraParameter", (event, key, value) => {
        crashReporter.addExtraParameter(key, value);
    });
    ipcMain.on("nexa:electron:crashReporter:removeExtraParameter", (event, key) => {
        crashReporter.removeExtraParameter(key);
    });
    ipcMain.on("nexa:electron:crashReporter:start", (_event, options) => {
        crashReporter.start(options);
    });
    app.setPath = (name, value) => {
        if (!Object.hasOwn({home: 1, appData: 1, userData: 1, sessionData: 1,
            cache: 1, downloads: 1, documents: 1, desktop: 1, pictures: 1,
            videos: 1, music: 1, logs: 1, temp: 1, exe: 1, module: 1,
            assets: 1, crashDumps: 1}, name)) {
            throw new Error(`unknown Electron path: ${name}`);
        }
        if (typeof value !== "string" || !path.isAbsolute(value)) {
            throw new TypeError("Electron paths must be absolute");
        }
        const stat = fs.statSync(value);
        if (name === "exe" ? !stat.isFile() : !stat.isDirectory()) {
            throw new Error(`Electron path is not the expected file or directory: ${value}`);
        }
        pathOverrides.set(name, value);
        if (name === "userData") userDataPath = value;
        canonicalFsRoots.clear();
    };
    app.setName = name => {
        appName = String(name);
        app.name = appName;
    };
    app.setDesktopName = name => { desktopName = String(name); };
    app.getDesktopName = () => desktopName;
    app.setAppUserModelId = () => {};
    app.requestSingleInstanceLock = () => true;
    app.commandLine = {appendSwitch() {}, appendArgument() {}};

    function encodeElectronEventArg(value) {
        if (value instanceof BrowserWindow) {
            return {__nexaType: "BrowserWindow", id: value.id, title: value.getTitle()};
        }
        if (value && contents.get(value.id) === value) {
            return {__nexaType: "WebContents", id: value.id, title: value.getTitle()};
        }
        return encodeFsValue(value);
    }

    function removeElectronEventSubscriptions(ownerWindowId) {
        for (const [id, subscription] of electronEventSubscriptions) {
            if (subscription.ownerWindowId !== ownerWindowId) continue;
            subscription.target.removeListener(subscription.eventName, subscription.listener);
            electronEventSubscriptions.delete(id);
        }
    }

    ipcMain.on("nexa:electron:event:subscribe", (event, targetType, targetId, eventName) => {
        if (typeof eventName !== "string" || eventName.length > 128) return;
        const ownerWindowId = event.sender?.id;
        if (!Number.isInteger(ownerWindowId)) return;
        const target = targetType === "app" ? app
            : targetType === "window" ? windows.get(targetId)
            : targetType === "webContents" ? contents.get(targetId) : null;
        if (!target || typeof target.on !== "function") return;
        const id = ++nextElectronEventSubscriptionId;
        const listener = (...args) => event.sender?.send("__nexa:electron:event",
            targetType, targetId, eventName, args.map(encodeElectronEventArg));
        electronEventSubscriptions.set(id, {
            ownerWindowId, targetType, targetId, target, eventName, listener
        });
        target.on(eventName, listener);
    });

    ipcMain.on("nexa:electron:event:unsubscribe", (event, targetType, targetId, eventName) => {
        const ownerWindowId = event.sender?.id;
        for (const [id, subscription] of electronEventSubscriptions) {
            if (subscription.ownerWindowId !== ownerWindowId ||
                subscription.targetType !== targetType || subscription.targetId !== targetId ||
                subscription.eventName !== eventName) continue;
            subscription.target.removeListener(eventName, subscription.listener);
            electronEventSubscriptions.delete(id);
        }
    });

    ipcMain.on("nexa:electron:windows:list", event => {
        event.returnValue = [...windows.values()].map(window => ({
            id: window.id, title: window.getTitle(), focused: window.isFocused(),
            visible: window.isVisible()
        }));
    });
    ipcMain.on("nexa:electron:windows:fromWebContents", (event, id) => {
        const window = BrowserWindow.fromWebContents(contents.get(Number(id)));
        event.returnValue = window?.id ?? null;
    });
    ipcMain.on("nexa:electron:webContents:list", event => {
        event.returnValue = [...contents.values()].map(webContents => ({
            id: webContents.id, title: webContents.getTitle(),
            focused: webContents === activeWindow?.webContents
        }));
    });

    function openExternal(url) {
        let parsed;
        try {
            parsed = new URL(url);
        } catch (_) {
            throw new TypeError("shell.openExternal expects a valid URL");
        }
        if (["file:", "javascript:", "data:"].includes(parsed.protocol)) {
            throw new TypeError(`shell.openExternal does not support ${parsed.protocol}`);
        }
        return nativeCall({method: "shell.openExternal", args: [parsed.href]});
    }

    const shell = {
        openExternal,
        showItemInFolder: file => emit({event: "nativeCall", method: "shell.showItemInFolder", args: [file]})
    };

    function requireClipboardType(type = "clipboard") {
        if (type !== "clipboard") {
            throw new Error("Android only provides the system clipboard; selection clipboard is unavailable");
        }
    }

    function readClipboardText() {
        try {
            return execFileSync("termux-clipboard-get", [], {
                timeout: 5000,
                maxBuffer: 2 * 1024 * 1024,
                encoding: "utf8",
                env: termuxCommandEnvironment()
            });
        } catch (error) {
            if (error.code === "ENOENT") {
                throw new Error("clipboard access needs the Termux:API package and app");
            }
            throw error;
        }
    }

    function writeClipboardText(text) {
        try {
            execFileSync("termux-clipboard-set", [], {
                input: String(text), timeout: 5000, maxBuffer: 64 * 1024,
                env: termuxCommandEnvironment()
            });
        } catch (error) {
            if (error.code === "ENOENT") {
                throw new Error("clipboard access needs the Termux:API package and app");
            }
            throw error;
        }
    }

    const clipboard = {
        readText(type = "clipboard") {
            requireClipboardType(type);
            return readClipboardText();
        },
        writeText(text, type = "clipboard") {
            requireClipboardType(type);
            writeClipboardText(text);
        },
        clear(type = "clipboard") {
            requireClipboardType(type);
            writeClipboardText("");
        },
        availableFormats(type = "clipboard") {
            requireClipboardType(type);
            return readClipboardText() ? ["text/plain"] : [];
        },
        has(format, type = "clipboard") {
            requireClipboardType(type);
            return format === "text/plain" && Boolean(readClipboardText());
        }
    };
    ipcMain.handle("shell.openExternal", (_event, url) => openExternal(url));
    ipcMain.on("clipboard.readText", (event, type) => {
        event.returnValue = clipboard.readText(type);
    });
    ipcMain.on("clipboard.writeText", (event, text, type) => {
        clipboard.writeText(text, type);
        event.returnValue = undefined;
    });
    ipcMain.on("clipboard.clear", (event, type) => {
        clipboard.clear(type);
        event.returnValue = undefined;
    });
    ipcMain.on("clipboard.availableFormats", (event, type) => {
        event.returnValue = clipboard.availableFormats(type);
    });
    ipcMain.on("clipboard.has", (event, format, type) => {
        event.returnValue = clipboard.has(format, type);
    });
    function createSession(partition = "default") {
        const session = new EventEmitter();
        const temporary = partition !== "default" && !partition.startsWith("persist:");
        const profileKey = createHash("sha256")
            .update(`${path.resolve(appPath)}\0${partition}`)
            .digest("hex").slice(0, 32);
        session.partition = partition;
        session.temporary = temporary;
        session.webRequestEnabled = false;
        session.profileName = `nexa_${profileKey}${temporary
            ? `_${randomBytes(8).toString("hex")}` : ""}`;
        sessionWebContents.set(session, new Set());
        const cookieCall = async (action, values = {}) => {
            const result = await nativeCall({method: "session.cookies", action,
                profileName: session.profileName, ...values});
            if (result && typeof result === "object" && result.error) {
                throw new Error(result.error);
            }
            return result;
        };
        session.cookies = {
            get: async (filter = {}) => {
                if (!filter.url) {
                    throw new Error("Electron cookie queries need a URL on Nexa WebView");
                }
                if (filter.domain || filter.path || filter.secure !== undefined ||
                    filter.session !== undefined) {
                    throw new Error("this Android WebView provider cannot filter cookie attributes");
                }
                return cookieCall("get", {url: filter.url, name: filter.name});
            },
            set: async details => {
                if (!details || typeof details !== "object" ||
                    typeof details.url !== "string") {
                    throw new TypeError("Electron cookies.set needs a URL");
                }
                const supported = new Set(["url", "name", "value", "domain", "path",
                    "secure", "httpOnly", "sameSite", "expirationDate"]);
                const unsupported = Object.keys(details).find(key => !supported.has(key));
                if (unsupported) {
                    throw new Error(`Electron cookie attribute is not supported: ${unsupported}`);
                }
                await cookieCall("set", {url: details.url, details});
            },
            remove: async (url, name) => {
                await cookieCall("remove", {url, name});
            },
            getAll: async () => {
                throw new Error("enumerating all Electron cookies is not supported by Android WebView");
            },
        };
        session.resolveHost = resolveHost;
        session.preconnect = ({url}) => {
            resolveHost(new URL(url).hostname).catch(() => {});
        };
        session.webRequest = new EventEmitter();
        const webRequestHandlers = new Map();
        for (const method of ["onBeforeRequest", "onBeforeSendHeaders", "onSendHeaders",
            "onHeadersReceived", "onResponseStarted", "onBeforeRedirect",
            "onCompleted", "onErrorOccurred"]) {
            session.webRequest[method] = (filter, listener) => {
                if (filter === null && listener === undefined) {
                    listener = null;
                    filter = undefined;
                }
                if (typeof filter === "function") {
                    listener = filter;
                    filter = {};
                }
                if (listener === null) {
                    webRequestHandlers.delete(method);
                    if (method === "onBeforeRequest" && session.webRequestEnabled) {
                        session.webRequestEnabled = false;
                        void Promise.resolve(nativeCall({method: "session.setWebRequestEnabled",
                            profileName: session.profileName, enabled: false})).catch(error =>
                            console.warn(`could not disable Electron request interception: ${error.message}`));
                    }
                    return;
                }
                if (method !== "onBeforeRequest" && method !== "onErrorOccurred" &&
                    method !== "onCompleted" && method !== "onHeadersReceived") {
                    throw new Error(`Electron webRequest.${method} is not supported by Nexa WebView yet`);
                }
                if (typeof listener !== "function") {
                    throw new TypeError(`${method} requires a listener`);
                }
                const requestFilter = filter || {};
                if (typeof requestFilter !== "object" || Array.isArray(requestFilter)) {
                    throw new TypeError("webRequest filter must be an object");
                }
                if (requestFilter.resourceTypes !== undefined) {
                    throw new Error("Android WebView does not expose Electron resource types");
                }
                if (requestFilter.urls !== undefined &&
                    (!Array.isArray(requestFilter.urls) ||
                        requestFilter.urls.some(pattern => typeof pattern !== "string"))) {
                    throw new TypeError("webRequest filter urls must be an array of patterns");
                }
                webRequestHandlers.set(method, {filter: requestFilter, listener});
                if (method === "onHeadersReceived") {
                    console.warn("Electron webRequest.onHeadersReceived cannot modify Android WebView response headers");
                }
                if (method === "onBeforeRequest" && !session.webRequestEnabled) {
                    session.webRequestEnabled = true;
                    void Promise.resolve(nativeCall({method: "session.setWebRequestEnabled",
                        profileName: session.profileName, enabled: true})).catch(error =>
                        console.warn(`could not enable Electron request interception: ${error.message}`));
                }
            };
        }
        session.webRequest.dispatchEvent = (method, details) => {
            const registration = webRequestHandlers.get(method);
            if (!registration) return;
            const patterns = registration.filter.urls || ["<all_urls>"];
            if (!patterns.some(pattern => matchesRequestUrl(pattern, details.url))) return;
            try {
                registration.listener(details);
            } catch (error) {
                console.error(`Electron webRequest.${method} listener failed: ${error.message}`);
            }
        };
        session.webRequest.dispatch = async (method, details) => {
            const registration = webRequestHandlers.get(method);
            if (!registration) return {};
            const patterns = registration.filter.urls || ["<all_urls>"];
            if (!patterns.some(pattern => matchesRequestUrl(pattern, details.url))) return {};
            return await new Promise((resolve, reject) => {
                let settled = false;
                const timeout = setTimeout(() => {
                    if (settled) return;
                    settled = true;
                    reject(new Error("Electron onBeforeRequest listener did not call its callback"));
                }, 30_000);
                const finish = result => {
                    if (settled) return;
                    if (result !== undefined && result !== null &&
                        (typeof result !== "object" || Array.isArray(result))) {
                        settled = true;
                        clearTimeout(timeout);
                        reject(new TypeError("webRequest callback response must be an object"));
                        return;
                    }
                    settled = true;
                    clearTimeout(timeout);
                    resolve(result || {});
                };
                try {
                    const result = registration.listener(details, finish);
                    if (result && typeof result.then === "function") {
                        result.then(finish, error => {
                            if (settled) return;
                            settled = true;
                            clearTimeout(timeout);
                            reject(error);
                        });
                    }
                } catch (error) {
                    settled = true;
                    clearTimeout(timeout);
                    reject(error);
                }
            });
        };
        const registeredProtocols = new Map();
        protocolHandlers.set(session, registeredProtocols);
        session.protocol = {
            handle(scheme, callback) {
                if (typeof callback !== "function") throw new TypeError("protocol handler must be a function");
                scheme = String(scheme).toLowerCase();
                if (registeredProtocols.has(scheme)) throw new Error(`protocol already handled: ${scheme}`);
                registeredProtocols.set(scheme, callback);
                return Promise.resolve();
            },
            unhandle(scheme) { registeredProtocols.delete(String(scheme).toLowerCase()); },
            isProtocolHandled(scheme) { return registeredProtocols.has(String(scheme).toLowerCase()); }
        };
        let preloads = [];
        session.getPreloads = () => [...preloads];
        session.setPreloads = paths => {
            if (!Array.isArray(paths) || paths.some(file => typeof file !== "string")) {
                throw new TypeError("session.setPreloads expects an array of paths");
            }
            preloads = [...paths];
        };
        session.getCacheSize = async () => 0;
        session.getSharedDictionaryUsageInfo = async () => [];
        session.clearCache = async () => {
            const webContents = [...sessionWebContents.get(session)]
                .find(contents => !contents.destroyed);
            if (webContents) await webContents.clearCache();
        };
        session.clearStorageData = async options => {
            if (options !== undefined && options !== null &&
                (typeof options !== "object" || Array.isArray(options))) {
                throw new TypeError("Electron session.clearStorageData options must be an object");
            }
            if (options && Object.keys(options).length === 1 &&
                Array.isArray(options.storages) && options.storages.length === 1 &&
                options.storages[0] === "shadercache") {
                // WebView only exposes a broader resource-cache clear for shadercache.
                await session.clearCache();
                return;
            }
            if (options && Object.keys(options).length) {
                throw new Error("filtered Electron session.clearStorageData is not supported yet");
            }
            await nativeCall({method: "session.clearStorageData",
                profileName: session.profileName});
        };
        // Android WebView owns this cache; clearing it on each launch defeats its warm start.
        session.clearCodeCaches = async () => {};
        session.flushStorageData = async () => {};
        session.closeAllConnections = async () => {};
        session.setPermissionRequestHandler = handler => {
            session.permissionRequestHandler = handler;
        };
        session.setPermissionCheckHandler = handler => {
            session.permissionCheckHandler = handler;
        };
        return session;
    }
    const defaultSession = createSession();
    sessions.set("default", defaultSession);
    const protocol = {
        ...defaultSession.protocol,
        registerSchemesAsPrivileged(schemes) {
            if (ready) throw new Error("protocol schemes must be registered before app ready");
            if (!Array.isArray(schemes)) throw new TypeError("protocol schemes must be an array");
            for (const item of schemes) {
                const scheme = String(item?.scheme || "").toLowerCase();
                if (!/^[a-z][a-z0-9+.-]*$/.test(scheme)) {
                    throw new TypeError(`invalid protocol scheme: ${scheme}`);
                }
                privilegedSchemes.set(scheme, {...(privilegedSchemes.get(scheme) || {}),
                    ...(item.privileges || {})});
            }
        }
    };
    function sessionForPartition(partition) {
        if (partition === "persist:default") partition = "default";
        if (!sessions.has(partition)) {
            const session = createSession(partition);
            sessions.set(partition, session);
            app.emit("session-created", {}, session);
        }
        return sessions.get(partition);
    }
    let onlineState;
    let onlineCheckedAt = 0;
    const online = () => {
        const now = Date.now();
        if (onlineState !== undefined && now >= onlineCheckedAt &&
            now - onlineCheckedAt < 1000) return onlineState;
        onlineCheckedAt = now;
        onlineState = Object.values(os.networkInterfaces()).some(addresses =>
            addresses?.some(address => !address.internal));
        return onlineState;
    };
    const screenWidth = Math.max(1, Number(process.env.NEXA_ELECTRON_SCREEN_WIDTH) || 800);
    const screenHeight = Math.max(1, Number(process.env.NEXA_ELECTRON_SCREEN_HEIGHT) || 600);
    const displayBounds = {x: 0, y: 0, width: screenWidth, height: screenHeight};
    const display = {
        id: 1,
        bounds: {...displayBounds},
        workArea: {...displayBounds},
        size: {width: screenWidth, height: screenHeight},
        workAreaSize: {width: screenWidth, height: screenHeight},
        scaleFactor: Math.max(0.1, Number(process.env.NEXA_ELECTRON_SCALE_FACTOR) || 1),
        rotation: 0,
        internal: true,
        touchSupport: "available"
    };
    const screen = new EventEmitter();
    screen.getPrimaryDisplay = () => display;
    screen.getAllDisplays = () => [display];
    screen.getDisplayNearestPoint = () => display;
    screen.getDisplayMatching = () => display;
    screen.getCursorScreenPoint = () => ({x: 0, y: 0});

    function updateScreenMetrics(value = {}) {
        const changed = [];
        const width = Number(value.width);
        const height = Number(value.height);
        const scaleFactor = Number(value.scaleFactor);
        if (Number.isFinite(width) && width > 0 && width !== display.bounds.width) {
            display.bounds.width = width;
            display.workArea.width = width;
            display.size.width = width;
            display.workAreaSize.width = width;
            changed.push("bounds", "workArea");
        }
        if (Number.isFinite(height) && height > 0 && height !== display.bounds.height) {
            display.bounds.height = height;
            display.workArea.height = height;
            display.size.height = height;
            display.workAreaSize.height = height;
            changed.push("bounds", "workArea");
        }
        if (Number.isFinite(scaleFactor) && scaleFactor > 0 &&
            scaleFactor !== display.scaleFactor) {
            display.scaleFactor = scaleFactor;
            changed.push("scaleFactor");
        }
        if (changed.length) screen.emit("display-metrics-changed", {}, display,
            [...new Set(changed)]);
    }
    const net = {
        isOnline: online,
        request(options, callback) {
            if (typeof options === "string") options = new URL(options);
            const url = options instanceof URL ? options : options.url
                ? new URL(options.url)
                : new URL(`${options.protocol || "https:"}//${options.hostname || options.host}` +
                    `${options.port ? `:${options.port}` : ""}${options.path || "/"}`);
            const transport = url.protocol === "https:" ? https : http;
            const request = transport.request(url, {
                method: options.method || "GET",
                headers: options.headers,
                auth: options.auth || (options.username
                    ? `${options.username}:${options.password || ""}` : undefined)
            }, response => {
                response.statusLine = `HTTP/${response.httpVersion} ${response.statusCode} ${response.statusMessage}`;
                callback?.(response);
            });
            return request;
        },
        fetch: (...args) => globalThis.fetch(...args),
        WebSocket: globalThis.WebSocket
    };
    Object.defineProperty(net, "online", {enumerable: true, get: online});
    const nativeTheme = new EventEmitter();
    nativeTheme.themeSource = "system";
    nativeTheme.shouldUseDarkColors = false;
    function createCrashReporter() {
        const reporter = new EventEmitter();
        const extraParameters = new Map();
        reporter.start = options => {
            reporter.options = {...options};
            reporter.started = true;
        };
        reporter.addExtraParameter = (key, value) => extraParameters.set(key, String(value));
        reporter.removeExtraParameter = key => extraParameters.delete(key);
        reporter.getLastCrashReport = () => null;
        reporter.getUploadedReports = () => [];
        reporter.getParameters = () => Object.fromEntries(extraParameters);
        return reporter;
    }
    function showMessageBox(parentOrOptions, maybeOptions) {
        const options = maybeOptions || parentOrOptions || {};
        emit({event: "nativeCall", method: "dialog.showMessageBox", args: [options]});
        return Promise.resolve({response: 0, checkboxChecked: false});
    }
    const dialog = {
        showErrorBox(title, content) {
            emit({event: "nativeCall", method: "dialog.showErrorBox", args: [title, content]});
        },
        showMessageBox,
        showMessageBoxSync(parentOrOptions, maybeOptions) {
            const options = maybeOptions || parentOrOptions || {};
            emit({event: "nativeCall", method: "dialog.showMessageBox", args: [options]});
            return 0;
        }
    };
    function normalizeMenuItem(option) {
        return option instanceof MenuItem ? option : new MenuItem(option);
    }
    function serializeMenuItems(items) {
        return items.map(item => ({
            id: item._nexaId, label: item.label, type: item.type,
            enabled: item.enabled, visible: item.visible, checked: item.checked,
            accelerator: item.accelerator,
            submenu: item.submenu ? serializeMenuItems(item.submenu.items) : []
        }));
    }
    class MenuItem {
        constructor(options = {}) {
            if (!options || typeof options !== "object") {
                throw new TypeError("MenuItem options must be an object");
            }
            this._nexaId = ++nextMenuItemId;
            this.id = options.id;
            this.label = String(options.label || "");
            this.type = options.type || "normal";
            this.role = options.role;
            this.enabled = options.enabled !== false;
            this.visible = options.visible !== false;
            this.checked = Boolean(options.checked);
            this.accelerator = options.accelerator || "";
            this.toolTip = options.toolTip || "";
            this.click = typeof options.click === "function" ? options.click : null;
            this.submenu = options.submenu
                ? options.submenu instanceof Menu ? options.submenu
                    : Menu.buildFromTemplate(options.submenu)
                : null;
            this.registerAccelerator = options.registerAccelerator !== false;
        }
    }
    class Menu extends EventEmitter {
        constructor() {
            super();
            this.items = [];
            this.popupContents = null;
            this.popupId = null;
            this.popupCallback = null;
        }
        static buildFromTemplate(template) {
            if (!Array.isArray(template)) throw new TypeError("menu template must be an array");
            const menu = new Menu();
            for (const item of template) menu.append(normalizeMenuItem(item));
            return menu;
        }
        static setApplicationMenu(menu) {
            if (menu !== null && !(menu instanceof Menu)) {
                throw new TypeError("application menu must be a Menu or null");
            }
            applicationMenu = menu;
            for (const window of windows.values()) {
                if (!window._menuExplicit) window._syncMenu();
            }
        }
        static getApplicationMenu() { return applicationMenu; }
        append(item) { this.insert(this.items.length, item); }
        insert(position, item) {
            if (!(item instanceof MenuItem)) item = normalizeMenuItem(item);
            const index = Math.max(0, Math.min(this.items.length, Number(position) || 0));
            this.items.splice(index, 0, item);
        }
        getMenuItemById(id) {
            for (const item of this.items) {
                if (item.id === id || item._nexaId === id) return item;
                const nested = item.submenu?.getMenuItemById(id);
                if (nested) return nested;
            }
            return null;
        }
        popup(options = {}) {
            const target = options.webContents || options.window?.webContents ||
                activeWindow?.webContents;
            if (!target || target.isDestroyed?.()) return;
            this.closePopup();
            this.popupContents = target;
            this.popupId = ++nextMenuId;
            this.popupCallback = options.callback || null;
            popupMenus.set(this.popupId, this);
            target.send("__nexa:menu:popup", this.popupId,
                serializeMenuItems(this.items), {
                    x: Number.isFinite(options.x) ? options.x : 0,
                    y: Number.isFinite(options.y) ? options.y : 0
                });
            this.emit("menu-will-show");
        }
        closePopup() {
            if (this.popupId === null) return;
            this.popupContents?.send("__nexa:menu:close", this.popupId);
            popupMenus.delete(this.popupId);
            this.popupContents = null;
            this.popupId = null;
            const callback = this.popupCallback;
            this.popupCallback = null;
            this.emit("menu-will-close");
            callback?.();
        }
    }
    ipcMain.on("nexa:electron:menu:click", (event, menuId, itemId) => {
        const rendererPopup = rendererPopupMenus.get(menuId);
        if (rendererPopup) {
            const findItem = items => {
                for (const item of items) {
                    if (item.id === itemId) return item;
                    const nested = findItem(item.submenu || []);
                    if (nested) return nested;
                }
                return null;
            };
            const item = findItem(rendererPopup.items);
            if (!item || item.enabled === false || item.visible === false ||
                item.type === "separator") return;
            if (item.type === "checkbox") item.checked = !item.checked;
            if (item.type === "radio") item.checked = true;
            event.sender?.send("__nexa:menu:renderer-click", rendererPopup.localId,
                itemId, item.checked);
            rendererPopupMenus.delete(menuId);
            return;
        }
        const ownerWindow = windows.get(contentsOwners.get(event.sender?.id));
        const menu = popupMenus.get(menuId) || (menuId === "application"
            ? ownerWindow?._menuExplicit ? ownerWindow.menu : applicationMenu : null);
        const item = menu?.getMenuItemById(itemId);
        if (!item || !item.enabled || !item.visible || item.type === "separator") return;
        if (item.type === "checkbox") item.checked = !item.checked;
        if (item.type === "radio") item.checked = true;
        item.click?.(item, ownerWindow || null, {
            preventDefault() { this.defaultPrevented = true; },
            defaultPrevented: false
        });
        menu.closePopup();
    });
    ipcMain.on("nexa:electron:menu:renderer-popup", (event, localId, items, bounds) => {
        if (!Array.isArray(items) || !items.length || !event.sender) return;
        const menuId = `renderer-menu-${event.sender.id}-${++nextRendererPopupId}`;
        const popup = {contentsId: event.sender.id, localId, items};
        rendererPopupMenus.set(menuId, popup);
        event.sender.send("__nexa:menu:popup", menuId, items, bounds || {});
    });
    ipcMain.on("nexa:electron:menu:renderer-close", (event, localId) => {
        for (const [menuId, popup] of rendererPopupMenus) {
            if (popup.contentsId !== event.sender?.id || popup.localId !== localId) continue;
            rendererPopupMenus.delete(menuId);
            event.sender.send("__nexa:menu:close", menuId);
        }
    });
    ipcMain.on("nexa:electron:menu:open-submenu", (event, itemId, x, y) => {
        const ownerWindow = windows.get(contentsOwners.get(event.sender?.id));
        const menu = ownerWindow?._menuExplicit ? ownerWindow.menu : applicationMenu;
        const item = menu?.getMenuItemById(itemId);
        if (!item?.enabled || !item.visible || !item.submenu) return;
        item.submenu.popup({webContents: event.sender, x, y});
    });
    ipcMain.on("nexa:electron:menu:closed", (event, menuId) => {
        popupMenus.get(menuId)?.closePopup();
        const rendererPopup = rendererPopupMenus.get(menuId);
        if (rendererPopup) {
            rendererPopupMenus.delete(menuId);
            event.sender?.send("__nexa:menu:renderer-close", rendererPopup.localId);
        }
    });
    const crashReporter = createCrashReporter();
    const electron = {
        app,
        process,
        View,
        WebContentsView,
        BaseWindow: BrowserWindow,
        BrowserWindow,
        ipcMain,
        shell,
        net,
        webContents: {
            getAllWebContents: () => [...contents.values()],
            getFocusedWebContents: () => activeWindow?.webContents || null,
            fromId: id => contents.get(id)
        },
        nativeTheme,
        screen,
        autoUpdater,
        session: {
            defaultSession,
            fromPartition: sessionForPartition
        },
        dialog,
        clipboard,
        Menu,
        MenuItem,
        protocol,
        powerMonitor: new EventEmitter(),
        crashReporter,
        nativeImage: {createEmpty: () => ({isEmpty: () => true})}
    };
    globalThis[Symbol.for("nexa.electron.runtime")] = electron;

    async function dispatch(message) {
        const sender = contents.get(message.contentsId ?? message.windowId) || null;
        const ownerWindowId = sender ? contentsOwners.get(sender.id) : message.windowId;
        const window = windows.get(ownerWindowId) || windows.get(message.windowId) || activeWindow;
        const event = {
            sender: sender || window?.webContents || null,
            senderFrame: sender ? {url: sender.getURL?.() || ""}
                : window ? {url: window.url || ""} : null,
            frameId: 0,
            processId: process.pid,
            returnValue: undefined,
            reply: (channel, ...args) => (sender || window?.webContents)?.send(channel, ...args)
        };
        if (message.kind === "invoke") {
            const callback = handlers.get(message.channel);
            if (!callback) throw new Error(`no ipcMain.handle for ${message.channel}`);
            return await callback(event, ...(message.args || []));
        }
        if (message.kind === "send") {
            ipcMain.emit(message.channel, event, ...(message.args || []));
            return undefined;
        }
        if (message.kind === "batch") {
            if (!Array.isArray(message.messages) || message.messages.length > 128 ||
                message.messages.some(item => !item || item.kind !== "send" ||
                    typeof item.channel !== "string" || !Array.isArray(item.args))) {
                throw new TypeError("invalid Electron IPC send batch");
            }
            let firstError;
            let hasError = false;
            for (const item of message.messages) {
                try {
                    ipcMain.emit(item.channel, event, ...item.args);
                } catch (error) {
                    if (!hasError) {
                        firstError = error instanceof Error ? error : new Error(String(error));
                        hasError = true;
                    }
                }
            }
            if (hasError) throw firstError;
            return undefined;
        }
        if (message.kind === "sendSync") {
            ipcMain.emit(message.channel, event, ...(message.args || []));
            return event.returnValue;
        }
        throw new Error(`unsupported Electron IPC kind: ${message.kind}`);
    }

    function decodeFsValue(value) {
        if (Array.isArray(value)) return value.map(decodeFsValue);
        if (value && typeof value === "object") {
            if (value.__nexaType === "BigInt") return BigInt(value.value);
            if (value.__nexaType === "FileURL") return new URL(value.value);
            if (value.__nexaType === "Undefined") return undefined;
            if (value.__nexaType === "Buffer") return Buffer.from(value.data, "base64");
            if (value.__nexaType === "Uint8Array") return Buffer.from(value.data, "base64");
            const decoded = {};
            for (const [key, item] of Object.entries(value)) {
                if (key !== "__proto__") decoded[key] = decodeFsValue(item);
            }
            return decoded;
        }
        return value;
    }

    function encodeFsValue(value) {
        if (typeof value === "bigint") return {__nexaType: "BigInt", value: value.toString()};
        if (value instanceof Date) return {__nexaType: "Date", value: value.toISOString()};
        if (Buffer.isBuffer(value)) return {__nexaType: "Buffer", data: value.toString("base64")};
        if (ArrayBuffer.isView(value)) {
            return {__nexaType: "Buffer", data: Buffer.from(value.buffer,
                value.byteOffset, value.byteLength).toString("base64")};
        }
        if (Array.isArray(value)) return value.map(encodeFsValue);
        if (value instanceof fs.Stats) {
            const stats = {};
            for (const key of ["dev", "mode", "nlink", "uid", "gid", "rdev", "ino",
                "size", "blocks", "atimeMs", "mtimeMs", "ctimeMs", "birthtimeMs",
                "atimeNs", "mtimeNs", "ctimeNs", "birthtimeNs",
                "atime", "mtime", "ctime", "birthtime"]) {
                if (value[key] !== undefined) stats[key] = encodeFsValue(value[key]);
            }
            for (const key of ["isFile", "isDirectory", "isBlockDevice", "isCharacterDevice",
                "isSymbolicLink", "isFIFO", "isSocket"]) stats[key] = value[key]();
            return {__nexaType: "Stats", value: stats};
        }
        if (value instanceof fs.Dirent) {
            const entry = {name: encodeFsValue(value.name), parentPath: value.parentPath};
            for (const key of ["isFile", "isDirectory", "isBlockDevice", "isCharacterDevice",
                "isSymbolicLink", "isFIFO", "isSocket"]) entry[key] = value[key]();
            return {__nexaType: "Dirent", value: entry};
        }
        if (value && typeof value === "object") {
            const encoded = {};
            for (const [key, item] of Object.entries(value)) encoded[key] = encodeFsValue(item);
            return encoded;
        }
        return value;
    }

    function encodeFsError(error) {
        return {
            __nexaType: "FsError",
            value: Object.fromEntries(["name", "message", "code", "errno", "syscall", "path", "dest"]
                .filter(key => error?.[key] !== undefined)
                .map(key => [key, String(error[key])]))
        };
    }

    function fsAllowedRoots() {
        const home = process.env.HOME || appRoot;
        const roots = [appRoot, appPath, resourcesPath, userDataPath,
            process.env.XDG_CONFIG_HOME || path.join(home, ".config"),
            process.env.XDG_CACHE_HOME || path.join(home, ".cache"),
            process.env.TMPDIR || "/tmp", ...pathOverrides.values()];
        const appModulesPath = path.resolve(path.dirname(appPath), "..", "modules");
        if (isTrustedPath(appModulesPath)) roots.push(appModulesPath);
        for (const name of ["downloads", "documents", "desktop", "pictures", "videos", "music"]) {
            roots.push(pathOverrides.get(name) || path.join(home,
                name[0].toUpperCase() + name.slice(1)));
        }
        return [...new Set(roots.map(value => path.resolve(value)))];
    }

    function canonicalFsRoot(root) {
        if (canonicalFsRoots.has(root)) return canonicalFsRoots.get(root);
        let canonical = root;
        try { canonical = fs.realpathSync(root); }
        catch (_) {}
        canonicalFsRoots.set(root, canonical);
        return canonical;
    }

    function assertFsPath(value) {
        const target = value instanceof URL ? fileURLToPath(value)
            : Buffer.isBuffer(value) ? value.toString() : value;
        if (typeof target !== "string" || !target) {
            throw new TypeError("filesystem paths must be strings or buffers");
        }
        const absolute = path.resolve(target);
        const roots = fsAllowedRoots();
        const within = (candidate, root) => {
            const relative = path.relative(root, candidate);
            return relative === "" || (!relative.startsWith(`..${path.sep}`) &&
                relative !== ".." && !path.isAbsolute(relative));
        };
        if (!roots.some(root => within(absolute, root))) {
            const error = new Error(`filesystem access outside app data is blocked: ${target}`);
            error.code = "EACCES";
            error.path = target;
            throw error;
        }

        let existing = absolute;
        while (!fs.existsSync(existing)) {
            const parent = path.dirname(existing);
            if (parent === existing) break;
            existing = parent;
        }
        let canonical;
        try {
            canonical = path.resolve(fs.realpathSync(existing), path.relative(existing, absolute));
        } catch (_) {
            canonical = existing;
        }
        const canonicalRoots = roots.map(canonicalFsRoot);
        if (!canonicalRoots.some(root => within(canonical, root))) {
            const error = new Error(`filesystem path resolves outside app data: ${target}`);
            error.code = "EACCES";
            error.path = target;
            throw error;
        }
        return value;
    }

    function checkFsPaths(method, args) {
        const baseMethod = method.replace(/^(?:promises:|callback:)/, "");
        const pathIndexes = new Set(["copyFile", "copyFileSync", "cp", "cpSync", "link",
            "linkSync", "rename", "renameSync"].includes(baseMethod) ? [0, 1]
            : baseMethod === "symlink" || baseMethod === "symlinkSync" ? [1]
            : ["close", "closeSync", "read", "readSync", "write", "writeSync",
                "fchmod", "fchmodSync", "fchown", "fchownSync", "fdatasync",
                "fdatasyncSync", "fstat", "fstatSync", "fsync", "fsyncSync",
                "ftruncate", "ftruncateSync", "futimes", "futimesSync"].includes(baseMethod)
                ? [] : [0]);
        for (const index of pathIndexes) {
            if (args[index] !== undefined && typeof args[index] !== "number") {
                args[index] = assertFsPath(args[index]);
            }
        }
        return args;
    }

    let nextFsHandleId = 0;
    const fsHandles = new Map();
    const fsFds = new Map();
    function trackFsFd(fd, windowId) {
        fsFds.set(fd, windowId);
    }
    function assertFsFd(fd, windowId) {
        if (!Number.isInteger(fd) || !fsFds.has(fd) || fsFds.get(fd) !== windowId) {
            const error = new Error("filesystem descriptor is not owned by this window");
            error.code = "EBADF";
            throw error;
        }
    }
    const fsFdMethods = new Set(["close", "closeSync", "fchmod", "fchmodSync", "fchown",
        "fchownSync", "fdatasync", "fdatasyncSync", "fstat", "fstatSync", "fsync",
        "fsyncSync", "ftruncate", "ftruncateSync", "futimes", "futimesSync", "read",
        "readSync", "readv", "readvSync", "write", "writeSync", "writev", "writevSync"]);
    function fsMethod(method, args = [], windowId = null) {
        if (typeof method === "string" && method.startsWith("callback:")) {
            const callbackMethod = method.slice("callback:".length);
            if (callbackMethod === "exists") {
                return fs.promises.access(...checkFsPaths(method, decodeFsValue(args))).then(
                    () => ({__nexaType: "CallbackValues", values: [true]}),
                    () => ({__nexaType: "CallbackValues", values: [false]}));
            }
            if (typeof fs[callbackMethod] !== "function") {
                throw new TypeError(`unsupported Node fs callback method: ${callbackMethod}`);
            }
            const decoded = checkFsPaths(method, decodeFsValue(args));
            if (fsFdMethods.has(callbackMethod)) assertFsFd(decoded[0], windowId);
            return new Promise((resolve, reject) => {
                fs[callbackMethod](...decoded, (error, ...values) => {
                    if (error) reject(error);
                    else if (callbackMethod === "open") {
                        trackFsFd(values[0], windowId);
                        resolve({__nexaType: "CallbackValues", values});
                    }
                    else if (callbackMethod === "close") {
                        fsFds.delete(decoded[0]);
                        resolve({__nexaType: "CallbackValues", values});
                    }
                    else if (callbackMethod === "read") {
                        resolve({__nexaType: "FsReadResult", value: values[0],
                            buffer: encodeFsValue(values[1])});
                    }
                    else if (callbackMethod === "readv") {
                        resolve({__nexaType: "FsReadvResult", value: values[0],
                            buffers: encodeFsValue(decoded[1])});
                    }
                    else resolve({__nexaType: "CallbackValues", values});
                });
            });
        }
        if (typeof method !== "string" || method.startsWith("_") ||
            method === "promises" || method === "constants") {
            throw new TypeError(`unsupported Node fs method: ${method}`);
        }
        const decoded = checkFsPaths(method, decodeFsValue(args));
        if (method.startsWith("promises:")) {
            const promiseMethod = method.slice("promises:".length);
            if (typeof fs.promises[promiseMethod] !== "function") {
                throw new TypeError(`unsupported Node fs promise method: ${promiseMethod}`);
            }
            if (promiseMethod === "open") {
                return fs.promises.open(...decoded).then(handle => {
                    const id = ++nextFsHandleId;
                    trackFsFd(handle.fd, windowId);
                    fsHandles.set(id, {handle, windowId});
                    return {__nexaType: "FileHandle", id, fd: handle.fd};
                });
            }
            return fs.promises[promiseMethod](...decoded);
        }
        if (method === "exists") return fs.promises.access(...decoded).then(() => true, () => false);
        if (typeof fs[method] !== "function") {
            throw new TypeError(`unsupported Node fs method: ${method}`);
        }
        if (fsFdMethods.has(method)) assertFsFd(decoded[0], windowId);
        const result = fs[method](...decoded);
        if (method === "openSync") trackFsFd(result, windowId);
        else if (method === "closeSync") fsFds.delete(decoded[0]);
        return result;
    }

    function nativeDescriptor(value, moduleId, windowId, propertyPath = [], depth = 0) {
        if (typeof value === "function") {
            let constructable = false;
            try { Reflect.construct(function() {}, [], value); constructable = true; }
            catch (_) {}
            const descriptor = {__nexaType: "NativeFunction", path: propertyPath,
                async: value.constructor?.name === "AsyncFunction", constructable, entries: {}};
            if (depth > 4) return descriptor;
            for (const key of Object.keys(value).slice(0, 256)) {
                try {
                    descriptor.entries[key] = nativeDescriptor(value[key], moduleId, windowId,
                        [...propertyPath, key], depth + 1);
                } catch (_) {}
            }
            return descriptor;
        }
        if (value === null || typeof value !== "object") {
            return encodeFsValue(value);
        }
        if (Buffer.isBuffer(value) || value instanceof Date || ArrayBuffer.isView(value)) {
            return encodeFsValue(value);
        }
        if (depth > 4) {
            const objectId = ++nextNativeObjectId;
            nativeObjectHandles.set(objectId, {moduleId, windowId, value});
            return {__nexaType: "NativeRemoteObject", objectId, entries: {}};
        }
        if (Array.isArray(value)) {
            return value.slice(0, 256).map((item, index) =>
                nativeDescriptor(item, moduleId, windowId,
                    [...propertyPath, index], depth + 1));
        }
        const prototype = Object.getPrototypeOf(value);
        if (prototype && prototype !== Object.prototype) {
            const objectId = ++nextNativeObjectId;
            nativeObjectHandles.set(objectId, {moduleId, windowId, value});
            const entries = {};
            const keys = new Set();
            for (let current = value; current && current !== Object.prototype;
                 current = Object.getPrototypeOf(current)) {
                for (const key of Object.getOwnPropertyNames(current)) {
                    if (key !== "constructor") keys.add(key);
                }
            }
            for (const key of [...keys].slice(0, 256)) {
                try {
                    entries[key] = nativeDescriptor(value[key], moduleId, windowId,
                        [key], depth + 1);
                } catch (_) {}
            }
            return {__nexaType: "NativeRemoteObject", objectId, entries};
        }
        const descriptor = {__nexaType: "NativeObject", entries: {}};
        for (const key of Object.keys(value).slice(0, 256)) {
            try {
                descriptor.entries[key] = nativeDescriptor(value[key], moduleId, windowId,
                    [...propertyPath, key], depth + 1);
            } catch (_) {}
        }
        return descriptor;
    }

    function nativeArguments(value, event, moduleId, onceCallback = false) {
        if (Array.isArray(value)) return value.map(item =>
            nativeArguments(item, event, moduleId, onceCallback));
        if (value && typeof value === "object" && value.__nexaType === "NativeReference") {
            const reference = nativeObjectHandles.get(value.objectId);
            if (!reference || reference.windowId !== event.sender?.id ||
                reference.moduleId !== value.moduleId) {
                throw new Error("native object reference is closed");
            }
            return reference.value;
        }
        if (value && typeof value === "object" && value.__nexaType === "NativeCallback") {
            return (...args) => {
                try {
                    event.sender?.send("__nexa:node:module-callback", moduleId,
                        value.id, encodeFsValue(args), onceCallback);
                } catch (_) {}
            };
        }
        if (value && typeof value === "object") {
            if (value.__nexaType) return decodeFsValue(value);
            return Object.fromEntries(Object.entries(value).map(([key, item]) =>
                [key, nativeArguments(item, event, moduleId, onceCallback)]));
        }
        return value;
    }

    function nativeMethod(entry, objectId, propertyPath, windowId) {
        let owner = null;
        let value = entry.exports;
        if (objectId !== null && objectId !== undefined) {
            const reference = nativeObjectHandles.get(objectId);
            if (!reference || reference.windowId !== windowId ||
                reference.moduleId !== entry.id) {
                throw new Error("native object reference is closed");
            }
            value = reference.value;
        }
        for (const key of propertyPath) {
            owner = value;
            value = value?.[key];
        }
        if (typeof value !== "function") throw new TypeError("native module export is not callable");
        return {owner, value};
    }

    ipcMain.on("nexa:node:module:load", (event, specifier, baseFile) => {
        try {
            if (typeof specifier !== "string" || specifier.length > 4096 ||
                typeof baseFile !== "string") {
                throw new TypeError("native module request is invalid");
            }
            const resolved = resolveRendererModule(specifier, baseFile);
            const windowId = event.sender?.id;
            let id = [...nativeModules].find(([, item]) =>
                item.windowId === windowId && item.filename === resolved)?.[0];
            if (id === undefined) {
                id = ++nextNativeModuleId;
                nativeModules.set(id, {
                    id, windowId, filename: resolved, exports: require(resolved)
                });
            }
            event.returnValue = {id, exports: nativeDescriptor(
                nativeModules.get(id).exports, id, windowId)};
        } catch (error) {
            event.returnValue = encodeFsError(error);
        }
    });

    function resolveRendererModule(specifier, baseFile) {
        let base = path.resolve(baseFile);
        const archiveMatch = /^(.*?\.asar)(?:[\\/](.*))?$/.exec(base);
        if (archiveMatch && fs.existsSync(archiveMatch[1])) {
            base = path.join(resolveAsar(archiveMatch[1]), archiveMatch[2] || "");
        }
        base = fs.realpathSync(base);
        if (!isTrustedPath(base)) {
            throw new Error("renderer module base is outside the installed app");
        }
        const requireFrom = Module.createRequire(base);
        const resolved = requireFrom.resolve(specifier);
        if (Module.builtinModules.includes(specifier) || specifier.startsWith("node:")) {
            return specifier;
        }
        const realPath = fs.realpathSync(resolved);
        if (!isTrustedPath(realPath)) {
            throw new Error("renderer module is outside the installed app");
        }
        return realPath;
    }

    ipcMain.on("nexa:node:module:resolve", (event, specifier, baseFile) => {
        try {
            if (typeof specifier !== "string" || specifier.length > 4096 ||
                typeof baseFile !== "string") {
                throw new TypeError("module resolution request is invalid");
            }
            const resolved = resolveRendererModule(specifier, baseFile);
            if (Module.builtinModules.includes(resolved) || resolved.startsWith("node:")) {
                event.returnValue = {builtin: resolved};
            } else {
                event.returnValue = {filename: resolved};
            }
        } catch (error) {
            event.returnValue = encodeFsError(error);
        }
    });

    ipcMain.on("nexa:node:module:source", (event, filename) => {
        try {
            if (typeof filename !== "string" || !isTrustedPath(filename)) {
                throw new Error("renderer module source is outside the installed app");
            }
            const resolved = fs.realpathSync(filename);
            if (!isTrustedPath(resolved) || !/\.(?:c?js|json)$/i.test(resolved)) {
                throw new Error("renderer module source type is unsupported");
            }
            event.returnValue = fs.readFileSync(resolved, "utf8");
        } catch (error) {
            event.returnValue = encodeFsError(error);
        }
    });

    ipcMain.on("nexa:node:module:call", (event, id, objectId, propertyPath, args) => {
        const entry = nativeModules.get(id);
        if (!entry || entry.windowId !== event.sender?.id) {
            event.returnValue = encodeFsError(Object.assign(
                new Error("native module is closed"), {code: "ERR_NATIVE_MODULE_CLOSED"}));
            return;
        }
        try {
            const {owner, value} = nativeMethod(entry, objectId, propertyPath, event.sender?.id);
            const onceCallback = propertyPath.at(-1) === "once" ||
                propertyPath.at(-1) === "prependOnceListener";
            const result = value.apply(owner,
                nativeArguments(args, event, id, onceCallback));
            if (result && typeof result.then === "function") {
                throw new TypeError("native addon returned a promise from a synchronous export");
            }
            event.returnValue = {__nexaType: "NativeResult",
                value: nativeDescriptor(result, id, entry.windowId)};
        } catch (error) {
            event.returnValue = encodeFsError(error);
        }
    });

    ipcMain.on("nexa:node:module:release-object", (event, id, objectId) => {
        const entry = nativeObjectHandles.get(objectId);
        if (entry?.moduleId === id && entry.windowId === event.sender?.id) {
            nativeObjectHandles.delete(objectId);
        }
    });

    ipcMain.on("nexa:node:module:construct", (event, id, propertyPath, args) => {
        const entry = nativeModules.get(id);
        if (!entry || entry.windowId !== event.sender?.id) {
            event.returnValue = encodeFsError(Object.assign(
                new Error("native module is closed"), {code: "ERR_NATIVE_MODULE_CLOSED"}));
            return;
        }
        try {
            const {value} = nativeMethod(entry, null, propertyPath, event.sender?.id);
            const instance = Reflect.construct(value, nativeArguments(args, event, id));
            event.returnValue = {__nexaType: "NativeResult",
                value: nativeDescriptor(instance, id, entry.windowId)};
        } catch (error) {
            event.returnValue = encodeFsError(error);
        }
    });

    ipcMain.handle("nexa:node:module:invoke", async (event, id, objectId, propertyPath, args) => {
        const entry = nativeModules.get(id);
        if (!entry || entry.windowId !== event.sender?.id) {
            return encodeFsError(Object.assign(
                new Error("native module is closed"), {code: "ERR_NATIVE_MODULE_CLOSED"}));
        }
        try {
            const {owner, value} = nativeMethod(entry, objectId, propertyPath, event.sender?.id);
            const onceCallback = propertyPath.at(-1) === "once" ||
                propertyPath.at(-1) === "prependOnceListener";
            const result = await value.apply(owner,
                nativeArguments(args, event, id, onceCallback));
            return {__nexaType: "NativeResult",
                value: nativeDescriptor(result, id, entry.windowId)};
        } catch (error) {
            return encodeFsError(error);
        }
    });

    ipcMain.handle("nexa:node:fs", async (_event, method, args) => {
        try {
            return encodeFsValue(await fsMethod(method, args, _event.sender?.id ?? null));
        } catch (error) {
            return encodeFsError(error);
        }
    });
    ipcMain.on("nexa:node:fs-handle:release", (event, id) => {
        const entry = fsHandles.get(id);
        if (!entry || entry.windowId !== (event.sender?.id ?? null)) return;
        fsHandles.delete(id);
        fsFds.delete(entry.handle.fd);
        void entry.handle.close().catch(() => {});
    });
    ipcMain.handle("nexa:node:fs-handle", async (event, id, method, args) => {
        const entry = fsHandles.get(id);
        if (!entry || entry.windowId !== (event.sender?.id ?? null)) {
            return encodeFsError(Object.assign(new Error("filesystem handle is closed"), {
                code: "EBADF"
            }));
        }
        if (typeof method !== "string" || method.startsWith("_") ||
            !["appendFile", "chmod", "chown", "close", "datasync", "read", "readFile",
                "readv", "stat", "sync", "truncate", "utimes", "write",
                "writeFile", "writev"].includes(method) || typeof entry.handle[method] !== "function") {
            return encodeFsError(new TypeError(`unsupported Node FileHandle method: ${method}`));
        }
        try {
            const result = encodeFsValue(await entry.handle[method](...decodeFsValue(args)));
            if (method === "close") {
                fsHandles.delete(id);
                fsFds.delete(entry.handle.fd);
            }
            return result;
        } catch (error) {
            return encodeFsError(error);
        }
    });
    ipcMain.on("nexa:node:fs-sync", (event, method, args) => {
        if (method === "constants") {
            event.returnValue = encodeFsValue(fs.constants);
            return;
        }
        if (method === "exists") {
            event.returnValue = fs.existsSync(...checkFsPaths(method, decodeFsValue(args)));
            return;
        }
        if (method === "readSync") {
            const decoded = checkFsPaths(method, decodeFsValue(args));
            try {
                assertFsFd(decoded[0], event.sender?.id ?? null);
                const value = fs.readSync(...decoded);
                event.returnValue = {__nexaType: "FsReadResult", value,
                    buffer: encodeFsValue(decoded[1])};
            } catch (error) {
                event.returnValue = encodeFsError(error);
            }
            return;
        }
        if (method === "readvSync") {
            const decoded = checkFsPaths(method, decodeFsValue(args));
            try {
                assertFsFd(decoded[0], event.sender?.id ?? null);
                const value = fs.readvSync(...decoded);
                event.returnValue = {__nexaType: "FsReadvResult", value,
                    buffers: encodeFsValue(decoded[1])};
            } catch (error) {
                event.returnValue = encodeFsError(error);
            }
            return;
        }
        if (typeof method !== "string" || !method.endsWith("Sync")) {
            throw new TypeError(`unsupported synchronous Node fs method: ${method}`);
        }
        try {
            event.returnValue = encodeFsValue(fsMethod(method, args, event.sender?.id ?? null));
        } catch (error) {
            event.returnValue = encodeFsError(error);
        }
    });
    let nextFsStreamId = 0;
    const fsStreams = new Map();
    ipcMain.handle("nexa:node:fs-stream", async (event, action, id, args) => {
        if (action === "openRead") {
            const streamArgs = checkFsPaths("createReadStream", decodeFsValue(args));
            if (streamArgs[1]?.fd !== undefined) {
                assertFsFd(streamArgs[1].fd, event.sender?.id ?? null);
            }
            const stream = fs.createReadStream(...streamArgs);
            try {
                if (streamArgs[1]?.fd === undefined) {
                await new Promise((resolve, reject) => {
                    stream.once("open", resolve);
                    stream.once("error", reject);
                });
                }
            } catch (error) {
                stream.destroy();
                throw error;
            }
            const streamId = ++nextFsStreamId;
            fsStreams.set(streamId, {type: "read", stream,
                windowId: event.sender?.id ?? null,
                iterator: stream[Symbol.asyncIterator]()});
            stream.on("error", () => fsStreams.delete(streamId));
            if (streamArgs[1]?.fd !== undefined && streamArgs[1]?.autoClose !== false) {
                stream.once("close", () => fsFds.delete(streamArgs[1].fd));
            }
            return streamId;
        }
        if (action === "openWrite") {
            const streamArgs = checkFsPaths("createWriteStream", decodeFsValue(args));
            if (streamArgs[1]?.fd !== undefined) {
                assertFsFd(streamArgs[1].fd, event.sender?.id ?? null);
            }
            const stream = fs.createWriteStream(...streamArgs);
            try {
                if (streamArgs[1]?.fd === undefined) {
                await new Promise((resolve, reject) => {
                    stream.once("open", resolve);
                    stream.once("error", reject);
                });
                }
            } catch (error) {
                stream.destroy();
                throw error;
            }
            const streamId = ++nextFsStreamId;
            fsStreams.set(streamId, {type: "write", stream,
                windowId: event.sender?.id ?? null});
            stream.on("error", () => fsStreams.delete(streamId));
            if (streamArgs[1]?.fd !== undefined && streamArgs[1]?.autoClose !== false) {
                stream.once("close", () => fsFds.delete(streamArgs[1].fd));
            }
            return streamId;
        }
        const entry = fsStreams.get(id);
        if (!entry) {
            if (action === "close") return true;
            throw new Error("filesystem stream is closed");
        }
        if (entry.windowId !== (event.sender?.id ?? null)) {
            throw new Error("filesystem stream belongs to another window");
        }
        if (action === "read" && entry.type === "read") {
            const result = await entry.iterator.next();
            if (result.done) {
                fsStreams.delete(id);
                return {done: true};
            }
            return {done: false, value: encodeFsValue(result.value)};
        }
        if (action === "write" && entry.type === "write") {
            await new Promise((resolve, reject) => entry.stream.write(
                decodeFsValue(args)[0], error => error ? reject(error) : resolve()));
            return true;
        }
        if (action === "end" && entry.type === "write") {
            await new Promise((resolve, reject) => {
                entry.stream.once("error", reject);
                entry.stream.end(error => error ? reject(error) : resolve());
            });
            fsStreams.delete(id);
            return true;
        }
        if (action === "close") {
            fsStreams.delete(id);
            entry.stream.destroy();
            return true;
        }
        throw new TypeError(`unsupported filesystem stream action: ${action}`);
    });

    async function dispatchProtocolRequest(message) {
        const ownerWindowId = contentsOwners.get(message.windowId) ?? message.windowId;
        const window = windows.get(ownerWindowId) || activeWindow;
        const url = new URL(message.url);
        const callback = protocolHandlers.get(window?.session)?.get(url.protocol.slice(0, -1));
        if (!callback) return new Response("protocol handler not found", {status: 404});
        const method = String(message.method || "GET").toUpperCase();
        const init = {method, headers: message.headers || {}};
        if (message.body?.length && method !== "GET" && method !== "HEAD") {
            init.body = Buffer.from(message.body, "base64");
            init.duplex = "half";
        }
        const request = new Request(url, init);
        Object.defineProperty(request, "initiatorOrigin", {
            value: message.initiatorOrigin || null
        });
        const response = await callback(request);
        if (!(response instanceof Response)) {
            throw new TypeError("protocol.handle must return a Response");
        }
        return response;
    }

    ipcMain.handle("nexa:electron:protocol", async (event, message) => {
        const response = await dispatchProtocolRequest({
            ...message,
            windowId: event.sender?.id ?? activeWindow?.id
        });
        const body = Buffer.from(await response.arrayBuffer());
        return {
            status: response.status,
            statusText: response.statusText,
            headers: Object.fromEntries(response.headers),
            body: body.toString("base64")
        };
    });

    ipcMain.handle("nexa:electron:webRequest", async (event, details) => {
        const webContents = event.sender;
        if (!webContents || webContents.destroyed) {
            throw new Error("Electron WebContents was destroyed during request interception");
        }
        details.webContents = webContents;
        details.webContentsId = webContents.id;
        return webContents.session.webRequest.dispatch("onBeforeRequest", details);
    });

    function dispatchRendererEvent(message) {
        if (message.type === "screenMetricsChanged") {
            updateScreenMetrics(message.value);
            return;
        }
        const webContents = contents.get(message.contentsId ?? message.windowId);
        const ownerWindowId = webContents
            ? contentsOwners.get(webContents.id) : message.windowId;
        const window = windows.get(ownerWindowId) || activeWindow;
        if (!window) return;
        const rendererContents = webContents || window.webContents;
        const value = message.value || {};
        if (message.type === "webviewTitleChanged") {
            const title = String(value.title ?? "");
            const explicitSet = value.explicitSet === true;
            rendererContents.title = title;
            const titleEvent = {defaultPrevented: false,
                preventDefault() { this.defaultPrevented = true; }};
            rendererContents.emit("page-title-updated", titleEvent, title, explicitSet);
            if (rendererContents === window.webContents) {
                window.emit("page-title-updated", titleEvent, title, explicitSet);
            }
            if (rendererContents === window.webContents && !titleEvent.defaultPrevented) {
                window.title = title || window.options.title || "";
                window.notifyState("title", {title: window.title});
            }
        } else if (message.type === "webviewNavigation") {
            if (value.isMainFrame === true) {
                setWebContentsMainFrameLoading(rendererContents, true);
                setWebContentsLoading(rendererContents, true);
                setWebContentsWaitingForResponse(rendererContents, true);
            }
            rendererContents.emit("did-start-navigation", {}, value.url, false,
                value.isMainFrame ?? null);
        } else if (message.type === "webviewInPageNavigation") {
            const url = String(value.url || "");
            rendererContents.url = url;
            if (rendererContents === window.webContents) window.url = url;
            const snapshot = value.navigationHistory;
            if (Array.isArray(snapshot?.entries) &&
                Number.isInteger(snapshot.index) && snapshot.index >= 0 &&
                snapshot.index < snapshot.entries.length &&
                snapshot.entries[snapshot.index]?.url === url &&
                snapshot.entries.every(entry => typeof entry?.url === "string")) {
                rendererContents._navigationHistory = {
                    entries: snapshot.entries.map((entry, index) => ({
                        id: index, url: entry.url, title: String(entry.title || "")
                    })),
                    index: snapshot.index,
                    targetIndex: null
                };
            } else {
                recordNavigation(rendererContents, url);
            }
            rendererContents.emit("did-start-navigation", {}, url, true, true);
            rendererContents.emit("did-navigate-in-page", {}, url, true);
        } else if (message.type === "webviewLoadStarted") {
            setWebContentsMainFrameLoading(rendererContents, true);
            setWebContentsLoading(rendererContents, true);
            setWebContentsWaitingForResponse(rendererContents, true);
        } else if (message.type === "webviewCommitVisible") {
            setWebContentsWaitingForResponse(rendererContents, false);
            rendererContents.url = value.url || rendererContents.url;
            if (rendererContents === window.webContents) window.url = rendererContents.url;
            recordNavigation(rendererContents, rendererContents.url);
            try {
                const parsed = new URL(rendererContents.url);
                if (parsed.protocol === "http:" || parsed.protocol === "https:") {
                    trustOrigin?.(parsed.origin);
                }
            } catch (_) {}
            rendererContents.emit("did-navigate", {}, rendererContents.url);
        } else if (message.type === "webviewHTTPError") {
            const url = String(value.url || "");
            rendererContents._webRequestHttpErrors ||= new Set();
            rendererContents._webRequestHttpErrors.add(url);
            rendererContents.session.webRequest.dispatchEvent("onCompleted", {
                id: 0, url, statusCode: Number(value.code) || 0,
                timestamp: Date.now() / 1000, webContentsId: rendererContents.id,
                resourceType: "mainFrame"
            });
        } else if (message.type === "webviewLoadFinished") {
            const url = String(value.url || rendererContents.url || "");
            const httpErrors = rendererContents._webRequestHttpErrors;
            if (!httpErrors?.has(url)) {
                rendererContents.session.webRequest.dispatchEvent("onCompleted", {
                    id: 0, url, statusCode: 200, timestamp: Date.now() / 1000,
                    webContentsId: rendererContents.id, resourceType: "mainFrame"
                });
            }
            httpErrors?.delete(url);
            setWebContentsMainFrameLoading(rendererContents, false);
            rendererContents.emit("did-frame-finish-load", {}, true, process.pid,
                rendererContents.id);
        } else if (message.type === "webviewProgress") {
            if (value.progress >= 100) {
                setWebContentsWaitingForResponse(rendererContents, false);
                setWebContentsMainFrameLoading(rendererContents, false);
                setWebContentsLoading(rendererContents, false);
                if (rendererContents === window.webContents) window.finishLoad();
                else {
                    const pending = rendererContents.pendingLoad;
                    rendererContents.pendingLoad = null;
                    pending?.resolve();
                }
                rendererContents.emit("did-finish-load");
                if (rendererContents === window.webContents && !window.readyToShow) {
                    window.readyToShow = true;
                    window.emit("ready-to-show");
                }
            } else {
                setWebContentsLoading(rendererContents, true);
            }
        } else if (message.type === "webviewRequestError") {
            rendererContents.session.webRequest.dispatchEvent("onErrorOccurred", {
                id: Number(value.requestId) || 0,
                url: String(value.url || ""),
                error: String(value.error || value.description || "net::ERR_FAILED"),
                method: value.method,
                timestamp: Number(value.timestamp) || Date.now() / 1000,
                webContentsId: rendererContents.id,
                resourceType: value.isMainFrame === true ? "mainFrame" : "other"
            });
        } else if (message.type === "webviewError") {
            rendererContents.session.webRequest.dispatchEvent("onErrorOccurred", {
                id: Number(value.requestId) || 0,
                url: String(value.url || ""),
                error: String(value.error || value.description || "net::ERR_FAILED"),
                method: value.method,
                timestamp: Number(value.timestamp) || Date.now() / 1000,
                webContentsId: rendererContents.id,
                resourceType: "mainFrame"
            });
            if (value.isMainFrame !== false) {
                setWebContentsWaitingForResponse(rendererContents, false);
            }
            setWebContentsMainFrameLoading(rendererContents, false);
            setWebContentsLoading(rendererContents, false);
            rendererContents._navigationHistory.targetIndex = null;
            const error = new Error(value.description || value.error || "load failed");
            if (rendererContents === window.webContents) window.finishLoad(error);
            else {
                rendererContents.pendingLoad?.reject(error);
                rendererContents.pendingLoad = null;
            }
            rendererContents.emit("did-fail-load", {
                platformErrorCode: value.platformErrorCode
            }, value.code ?? -2,
                value.description || value.error || "load failed", value.url || "",
                value.isMainFrame ?? true);
        } else if (message.type === "webviewDestroyed") {
            rendererContents._isCrashed = value.crashed === true;
            setWebContentsWaitingForResponse(rendererContents, false);
            setWebContentsMainFrameLoading(rendererContents, false);
            setWebContentsLoading(rendererContents, false);
            const error = new Error("webview renderer was destroyed");
            if (rendererContents === window.webContents) window.finishLoad(error);
            else {
                rendererContents.pendingLoad?.reject(error);
                rendererContents.pendingLoad = null;
            }
            const details = {
                reason: rendererContents._isCrashed ? "crashed" : "killed"
            };
            app.emit("render-process-gone", {}, rendererContents, details);
            rendererContents.emit("render-process-gone", {}, details);
        }
    }

    function runMain(entry) {
        const mainPath = path.resolve(appRoot, entry);
        if (!mainPath.startsWith(appRoot + path.sep) || !fs.existsSync(mainPath)) {
            throw new Error(`Electron main entry not found: ${entry}`);
        }
        const originalExit = process.exit;
        process.exit = function(code = 0) {
            emit({event: "processExit", code,
                stack: new Error("process.exit called").stack});
            return originalExit.call(process, code);
        };
        process.argv = [executablePath, appPath];
        process.defaultApp = false;
        const appModulesPath = path.join(appRoot, "node_modules");
        if (fs.existsSync(appModulesPath) && !Module.globalPaths.includes(appModulesPath)) {
            Module.globalPaths.unshift(appModulesPath);
        }
        const isEsm = path.extname(mainPath) === ".mjs" ||
            (path.extname(mainPath) === ".js" && packageData.type === "module");
        if (isEsm) {
            if (typeof registerHooks !== "function") {
                throw new Error("ES module Electron apps need a Node.js runtime with registerHooks support");
            }
            const electronModule = pathToFileURL(
                path.join(path.dirname(fileURLToPath(import.meta.url)), "electron_esm.mjs")
            ).href;
            registerHooks({
                resolve(specifier, context, nextResolve) {
                    if (specifier === "electron" || specifier === "electron/main") {
                        return {url: electronModule, shortCircuit: true};
                    }
                    return nextResolve(specifier, context);
                }
            });
        }
        const originalLoad = Module._load;
        Module._load = function(request, parent, isMain) {
            if (request === "electron" || request === "electron/main") return electron;
            if (request === "original-fs") return fs;
            if (typeof request === "string") {
                const requestPath = path.resolve(path.dirname(parent?.filename || appRoot), request);
                const archiveMatch = /^(.*?\.asar)(?:[\\/](.*))?$/.exec(requestPath);
                if (archiveMatch && fs.existsSync(archiveMatch[1])) {
                    const extracted = resolveAsar(archiveMatch[1]);
                    const target = archiveMatch[2]
                        ? path.join(extracted, archiveMatch[2]) : extracted;
                    return originalLoad.call(this, target, parent, isMain);
                }
            }
            return originalLoad.call(this, request, parent, isMain);
        };
        try {
            if (isEsm) {
                const loading = import(pathToFileURL(mainPath).href);
                setImmediate(markReady);
                return loading.catch(error => {
                    emit({event: "mainError", error: error.stack || String(error)});
                });
            }
            Module._load(mainPath, null, true);
            process.nextTick(markReady);
        } catch (error) {
            emit({event: "mainError", error: error.stack || String(error)});
        }
    }

    return {
        electron, dispatch, dispatchProtocolRequest, dispatchRendererEvent, runMain,
        updateDevToolsState, updateWindowGeometry, windowCommand,
        getPreload(windowId) {
            return windows.get(windowId)?.getPreloadResponse() ??
                contents.get(windowId)?.getPreloadResponse?.() ?? null;
        }
    };
}
