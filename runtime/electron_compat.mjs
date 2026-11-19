import {EventEmitter} from "node:events";
import {execFile, execFileSync} from "node:child_process";
import {createHash} from "node:crypto";
import dns from "node:dns/promises";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import Module from "node:module";
import {registerHooks} from "node:module";
import os from "node:os";
import path from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {promisify} from "node:util";

const execFileAsync = promisify(execFile);

export function createElectronCompat({appRoot, appPath = appRoot, resourcesPath = appRoot,
                                     localUrl, emit, resolveAsar, trustOrigin,
                                     isTrustedPath = () => false,
                                     executablePath = process.execPath}) {
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
    const handlers = new Map();
    const protocolHandlers = new WeakMap();
    const privilegedSchemes = new Map();
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
            visible: view.getVisible(), bounds, backgroundColor: view.backgroundColor,
            protocolSchemes: [...privilegedSchemes.keys()]});
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
        webContents.options = options;
        webContents.ownerWindowId = null;
        webContents.session = sessionForPartition(preferences.partition || "default");
        webContents.preloadPath = preferences.preload || null;
        webContents.destroyed = false;
        webContents.backgroundThrottling = preferences.backgroundThrottling !== false;
        webContents.getURL = () => webContents.url;
        webContents.getTitle = () => webContents.title;
        webContents.isDestroyed = () => webContents.destroyed;
        webContents.loadURL = url => {
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
            for (const [key, value] of Object.entries(loadOptions.query || {})) {
                url.searchParams.set(key, value);
            }
            if (loadOptions.hash) url.hash = loadOptions.hash;
            return webContents.loadURL(url.href);
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
        webContents.setZoomFactor = factor => {
            webContents.zoomFactor = Number(factor);
            webContents.send("__nexa:set-zoom", webContents.zoomFactor);
        };
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
            webContents.destroyed = true;
            webContents.pendingLoad?.reject(new Error("webContents was destroyed"));
            webContents.pendingLoad = null;
            for (const [key, pending] of pendingScriptExecutions) {
                if (!key.startsWith(`${id}:`)) continue;
                pendingScriptExecutions.delete(key);
                pending.reject(new Error("webContents was destroyed"));
            }
            contents.delete(id);
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
            this.resizable = options.resizable !== false;
            this.webContents = new EventEmitter();
            this.webContents.id = this.id;
            this.webContents.ownerWindowId = this.id;
            this.webContents.session = this.session;
            this.webContents.destroyed = false;
            this.webContents.backgroundThrottling =
                options.webPreferences?.backgroundThrottling !== false;
            this.webContents.zoomFactor = 1;
            this.webContents.getURL = () => this.url || "";
            this.webContents.getTitle = () => this.title || "";
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
            this.webContents.reload = () => this.loadURL(this.url || "about:blank");
            this.webContents.reloadIgnoringCache = () => this.webContents.reload();
            this.webContents.stop = () => this.finishLoad(new Error("navigation stopped"));
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
            this.webContents.openDevTools = () => {};
            this.webContents.closeDevTools = () => {};
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
            this.webContents.setZoomFactor = factor => {
                const zoomFactor = Number(factor);
                if (!Number.isFinite(zoomFactor) || zoomFactor <= 0) {
                    throw new TypeError("zoom factor must be a positive number");
                }
                this.webContents.zoomFactor = zoomFactor;
                this.webContents.send("__nexa:set-zoom", zoomFactor);
            };
            contents.set(this.id, this.webContents);
            contentsOwners.set(this.id, this.id);
            windows.set(this.id, this);
            activeWindow = this;
            this.contentView = new View({bounds: {
                x: 0, y: 0, width: options.width || 800, height: options.height || 600
            }});
            this.contentView._setOwnerWindow(this);
            this.webContentsView = new WebContentsView({webContents: this.webContents,
                bounds: {x: 0, y: 0, width: options.width || 800, height: options.height || 600}});
            this.contentView.addChildView(this.webContentsView);
            app.emit("browser-window-created", {}, this);
            app.emit("web-contents-created", {}, this.webContents);
        }

        notifyState(action, value = {}) {
            emit({event: "windowState", windowId: this.id, action, value});
        }

        loadURL(url) {
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
            emit({event: "renderer", windowId: this.id, contentsId: this.webContents.id,
                url: this.url,
                preloadUrl: localUrl(`__nexa/preload/${this.id}`), title: this.getTitle(),
                options: this.options, visible: this.visible,
                protocolSchemes: [...privilegedSchemes.keys()]});
            return load;
        }

        finishLoad(error = null) {
            const pending = this.pendingLoad;
            this.pendingLoad = null;
            if (!pending) return;
            if (error) pending.reject(error);
            else pending.resolve();
        }

        getPreloadResponse() {
            const paths = [...this.session.getPreloads(), this.preloadPath].filter(Boolean);
            const files = paths.map(preloadFileInfo);
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
            const cacheKey = JSON.stringify([files, schemes, rendererProcess]);
            const cachedEntry = preloadScriptCache.get(cacheKey);
            if (cachedEntry !== undefined) {
                preloadScriptCache.delete(cacheKey);
                preloadScriptCache.set(cacheKey, cachedEntry);
                return {source: cachedEntry.script, etag: cachedEntry.etag};
            }
            const preloads = paths.map((file, index) =>
                `window.__NEXA_ELECTRON_PRELOAD_BASE__=${JSON.stringify(files[index].absolute)};\n` +
                readPreload(file, files[index])).join("\n");
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
            const script = `${processSetup}\n${preloads}\n${bridge}`;
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
            if (options.query) {
                for (const [key, value] of Object.entries(options.query)) url.searchParams.set(key, value);
            }
            if (options.hash) url.hash = options.hash;
            return this.loadURL(url.href);
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
        getSize() { return [this.options.width || 800, this.options.height || 600]; }
        getPosition() { return [this.options.x || 0, this.options.y || 0]; }
        setTitle(title) {
            this.title = String(title);
            this.notifyState("title", {title: this.title});
        }
        getTitle() { return this.title || ""; }
        setMenuBarVisibility() {}
        setMenu() {}
        setProgressBar() {}
        setBounds(bounds) {
            Object.assign(this.options, bounds);
            this.contentView.setBounds({x: 0, y: 0,
                width: this.options.width || 800, height: this.options.height || 600});
            this.webContentsView.setBounds({x: 0, y: 0,
                width: this.options.width || 800, height: this.options.height || 600});
            this.notifyState("resize", {
                width: this.options.width, height: this.options.height,
                x: this.options.x, y: this.options.y
            });
        }
        setSize(width, height) {
            this.options.width = width;
            this.options.height = height;
            this.contentView.setBounds({x: 0, y: 0, width, height});
            this.webContentsView.setBounds({x: 0, y: 0, width, height});
            this.notifyState("resize", {width, height});
        }
        setPosition(x, y) {
            this.options.x = x;
            this.options.y = y;
            this.notifyState("move", {x, y});
        }
        setResizable(resizable) {
            this.resizable = Boolean(resizable);
            this.notifyState("resizable", {enabled: this.resizable});
        }
        isResizable() { return this.resizable; }
        getContentSize() { return this.getSize(); }
        setContentSize(width, height) { this.setSize(width, height); }
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
            contents.delete(this.id);
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
            getPosition: () => target.getPosition(), getTitle: () => target.getTitle(),
            setTitle: () => target.setTitle(args[0]), setBounds: () => target.setBounds(args[0]),
            setSize: () => target.setSize(args[0], args[1]),
            setPosition: () => target.setPosition(args[0], args[1]),
            setFullScreen: () => target.setFullScreen(args[0]),
            setResizable: () => target.setResizable(args[0]),
            setMenuBarVisibility: () => target.setMenuBarVisibility(args[0]),
            setBackgroundThrottling: () => target.webContents.setBackgroundThrottling(args[0])
        };
        const webContentsCalls = {
            getURL: () => target.getURL(), getTitle: () => target.getTitle(),
            isDestroyed: () => target.isDestroyed(),
            send: () => target.send(args[0], ...args.slice(1)),
            setBackgroundThrottling: () => target.setBackgroundThrottling(args[0]),
            setZoomFactor: () => target.setZoomFactor(args[0]),
            openDevTools: () => target.openDevTools(), closeDevTools: () => target.closeDevTools()
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
        if (method === "loadURL") return await target.loadURL(args[0]);
        if (method === "loadFile") return target.loadFile(args[0], args[1]);
        if (method === "reload" && type === "webContents") return target.reload();
        if (method === "reloadIgnoringCache" && type === "webContents") {
            return target.reloadIgnoringCache();
        }
        if (method === "executeJavaScript" && type === "webContents") {
            return target.executeJavaScript(args[0], args[1]);
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
        return execFileAsync("am", ["start", "-a", "android.intent.action.VIEW", "-d", parsed.href], {
            timeout: 10000,
            maxBuffer: 64 * 1024
        }).then(() => undefined);
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
                encoding: "utf8"
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
                input: String(text), timeout: 5000, maxBuffer: 64 * 1024
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
    function createSession() {
        const session = new EventEmitter();
        const cookies = new Map();
        const matchesUrl = (cookie, url) => {
            if (!url) return true;
            const parsed = new URL(url);
            const domain = cookie.domain.replace(/^\./, "");
            return (parsed.hostname === domain || parsed.hostname.endsWith(`.${domain}`)) &&
                parsed.pathname.startsWith(cookie.path || "/") &&
                (!cookie.secure || parsed.protocol === "https:");
        };
        session.cookies = {
            get: async (filter = {}) => [...cookies.values()].filter(cookie =>
                (!filter.name || cookie.name === filter.name) &&
                (!filter.domain || cookie.domain.endsWith(filter.domain)) &&
                (!filter.path || cookie.path === filter.path) &&
                (filter.secure === undefined || cookie.secure === filter.secure) &&
                matchesUrl(cookie, filter.url) &&
                (!cookie.expirationDate || cookie.expirationDate > Date.now() / 1000)
            ),
            set: async details => {
                const parsed = new URL(details.url);
                const cookie = {
                    name: details.name,
                    value: details.value,
                    domain: details.domain || parsed.hostname,
                    path: details.path || "/",
                    secure: details.secure ?? parsed.protocol === "https:",
                    httpOnly: details.httpOnly ?? false,
                    sameSite: details.sameSite || "unspecified",
                    session: details.expirationDate === undefined,
                    expirationDate: details.expirationDate
                };
                cookies.set(`${cookie.domain}:${cookie.path}:${cookie.name}`, cookie);
            },
            remove: async (url, name) => {
                for (const [key, cookie] of cookies) {
                    if (cookie.name === name && matchesUrl(cookie, url)) cookies.delete(key);
                }
            },
            getAll: async () => [...cookies.values()]
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
                if (typeof filter === "function") {
                    listener = filter;
                    filter = {};
                }
                if (listener === null) {
                    webRequestHandlers.delete(method);
                    return;
                }
                if (typeof listener !== "function") {
                    throw new TypeError(`${method} requires a listener`);
                }
                webRequestHandlers.set(method, {filter: filter || {}, listener});
            };
        }
        session.webRequest.dispatch = (method, details, callback) => {
            const registration = webRequestHandlers.get(method);
            if (!registration) return;
            if (callback) registration.listener(details, callback);
            else registration.listener(details);
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
        session.clearCache = async () => {};
        session.clearStorageData = async () => {};
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
        if (!sessions.has(partition)) {
            const session = createSession();
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
        Menu: {buildFromTemplate: template => template, setApplicationMenu() {}},
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
        if (message.type === "webviewNavigation") {
            rendererContents.url = value.url || rendererContents.url;
            if (rendererContents === window.webContents) window.url = rendererContents.url;
            try {
                const parsed = new URL(rendererContents.url);
                if (parsed.protocol === "http:" || parsed.protocol === "https:") {
                    trustOrigin?.(parsed.origin);
                }
            } catch (_) {}
            rendererContents.emit("did-start-navigation", {}, value.url, false, true);
            rendererContents.emit("did-navigate", {}, value.url);
        } else if (message.type === "webviewProgress" && value.progress >= 100) {
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
        } else if (message.type === "webviewError") {
            const error = new Error(value.description || value.error || "load failed");
            if (rendererContents === window.webContents) window.finishLoad(error);
            else {
                rendererContents.pendingLoad?.reject(error);
                rendererContents.pendingLoad = null;
            }
            rendererContents.emit("did-fail-load", {}, value.code || -1,
                value.description || value.error || "load failed", value.url || "", true);
        } else if (message.type === "webviewDestroyed") {
            const error = new Error("webview renderer was destroyed");
            if (rendererContents === window.webContents) window.finishLoad(error);
            else {
                rendererContents.pendingLoad?.reject(error);
                rendererContents.pendingLoad = null;
            }
            rendererContents.emit("render-process-gone", {}, {reason: "crashed", exitCode: 1});
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
        electron, dispatch, dispatchProtocolRequest, dispatchRendererEvent, runMain, windowCommand,
        getPreload(windowId) {
            return windows.get(windowId)?.getPreloadResponse() ??
                contents.get(windowId)?.getPreloadResponse?.() ?? null;
        }
    };
}
