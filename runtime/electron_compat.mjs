import {EventEmitter} from "node:events";
import dns from "node:dns/promises";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import Module from "node:module";
import {registerHooks} from "node:module";
import os from "node:os";
import path from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";

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
    const sessions = new Map();
    const handlers = new Map();
    const protocolHandlers = new WeakMap();
    const privilegedSchemes = new Map();
    const dnsLookups = new Map();
    const dnsCacheTtlMs = 30_000;
    const maxDnsCacheEntries = 128;
    let nextWindowId = 0;
    let nextCssId = 0;
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
            this.webContents = new EventEmitter();
            this.webContents.id = this.id;
            this.webContents.session = this.session;
            this.webContents.destroyed = false;
            this.webContents.backgroundThrottling =
                options.webPreferences?.backgroundThrottling !== false;
            this.webContents.getURL = () => this.url || "";
            this.webContents.getTitle = () => this.title || "";
            this.webContents.isDestroyed = () => this.webContents.destroyed;
            this.webContents.loadURL = (...args) => this.loadURL(...args);
            this.webContents.loadFile = (...args) => this.loadFile(...args);
            this.webContents.send = (channel, ...args) => {
                emit({event: "rendererMessage", windowId: this.id, channel, args});
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
            windows.set(this.id, this);
            activeWindow = this;
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
            emit({event: "renderer", windowId: this.id, url: this.url,
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

        getPreloadScript() {
            const paths = [...this.session.getPreloads(), this.preloadPath].filter(Boolean);
            const preloads = paths.map(readPreload).join("\n");
            const bridge = `(() => {
                const schemes = new Set(${JSON.stringify([...privilegedSchemes.keys()])});
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
            return preloads ? `${preloads}\n${bridge}` : bridge;
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
            this.notifyState("show");
        }

        hide() { this.visible = false; this.focused = false; this.emit("hide"); this.notifyState("hide"); }
        focus() {
            if (activeWindow && activeWindow !== this) activeWindow.focused = false;
            activeWindow = this;
            this.focused = true;
            this.emit("focus");
            this.notifyState("focus");
        }
        blur() { this.focused = false; this.emit("blur"); this.notifyState("blur"); }
        minimize() { this.minimized = true; this.focused = false; this.emit("minimize"); this.notifyState("minimize"); }
        restore() {
            this.minimized = false;
            this.visible = true;
            if (activeWindow && activeWindow !== this) activeWindow.focused = false;
            activeWindow = this;
            this.focused = true;
            this.emit("restore");
            this.notifyState("restore");
        }
        maximize() { this.maximized = true; this.emit("maximize"); this.notifyState("maximize"); }
        unmaximize() { this.maximized = false; this.emit("unmaximize"); this.notifyState("unmaximize"); }
        isMaximized() { return this.maximized; }
        isMinimized() { return this.minimized; }
        isVisible() { return this.visible; }
        isFocused() { return this.focused; }
        isDestroyed() { return this.destroyed; }
        getBounds() {
            return {x: this.options.x || 0, y: this.options.y || 0,
                width: this.options.width || 800, height: this.options.height || 600};
        }
        getSize() { return [this.options.width || 800, this.options.height || 600]; }
        getPosition() { return [0, 0]; }
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
            this.notifyState("resize", {
                width: this.options.width, height: this.options.height,
                x: this.options.x, y: this.options.y
            });
        }
        setSize(width, height) {
            this.options.width = width;
            this.options.height = height;
            this.notifyState("resize", {width, height});
        }
        setPosition(x, y) {
            this.options.x = x;
            this.options.y = y;
            this.notifyState("move", {x, y});
        }
        setResizable() {}
        setFullScreen() {}
        isFullScreen() { return false; }
        close() {
            let prevented = false;
            this.emit("close", {preventDefault() { prevented = true; }});
            if (!prevented) this.destroy();
        }
        destroy() {
            this.finishLoad(new Error("window was destroyed"));
            this.destroyed = true;
            windows.delete(this.id);
            this.webContents.destroyed = true;
            contents.delete(this.id);
            this.webContents.emit("destroyed");
            this.emit("closed");
            emit({event: "windowClosed", windowId: this.id});
            if (activeWindow === this) activeWindow = windows.values().next().value || null;
        }

        static getAllWindows() { return [...windows.values()]; }
        static getFocusedWindow() { return activeWindow; }
        static fromId(id) { return windows.get(id) || null; }
        static fromWebContents(webContents) {
            return windows.get(webContents?.id) || null;
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
    function readPreload(file) {
        const absolute = path.resolve(appRoot, file);
        let realFile;
        try {
            realFile = fs.realpathSync(absolute);
        } catch (_) {
            throw new Error(`Electron preload does not exist: ${file}`);
        }
        if (isTrustedPath(realFile)) return fs.readFileSync(realFile, "utf8");
        try {
            const realUserData = fs.realpathSync(userDataPath);
            if (realFile.startsWith(realUserData + path.sep)) {
                return fs.readFileSync(realFile, "utf8");
            }
        } catch (_) {}
        throw new Error(`Electron preload is outside the app and user data directories: ${file}`);
    }
    let previousCpu = process.cpuUsage();
    let previousCpuTime = process.hrtime.bigint();
    app.isReady = () => ready;
    app.whenReady = () => ready ? Promise.resolve(app) : new Promise(resolve => {
        app.once("ready", () => resolve(app));
    });
    app.quit = () => emit({event: "appQuit"});
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

    const shell = {
        openExternal: url => emit({event: "nativeCall", method: "shell.openExternal", args: [url]}),
        showItemInFolder: file => emit({event: "nativeCall", method: "shell.showItemInFolder", args: [file]})
    };
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
    const electron = {
        app,
        process,
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
        clipboard: {},
        Menu: {buildFromTemplate: template => template, setApplicationMenu() {}},
        protocol,
        powerMonitor: new EventEmitter(),
        crashReporter: createCrashReporter(),
        nativeImage: {createEmpty: () => ({isEmpty: () => true})}
    };
    globalThis[Symbol.for("nexa.electron.runtime")] = electron;

    async function dispatch(message) {
        const window = windows.get(message.windowId) || activeWindow;
        const event = {
            sender: window?.webContents || null,
            senderFrame: window ? {url: window.url || ""} : null,
            frameId: 0,
            processId: process.pid,
            returnValue: undefined,
            reply: (channel, ...args) => window?.webContents.send(channel, ...args)
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
        if (message.kind === "sendSync") {
            ipcMain.emit(message.channel, event, ...(message.args || []));
            return event.returnValue;
        }
        throw new Error(`unsupported Electron IPC kind: ${message.kind}`);
    }

    async function dispatchProtocolRequest(message) {
        const window = windows.get(message.windowId) || activeWindow;
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
        const window = windows.get(message.windowId) || activeWindow;
        if (!window) return;
        const webContents = window.webContents;
        const value = message.value || {};
        if (message.type === "webviewNavigation") {
            window.url = value.url || window.url;
            try {
                const parsed = new URL(window.url);
                if (parsed.protocol === "http:" || parsed.protocol === "https:") {
                    trustOrigin?.(parsed.origin);
                }
            } catch (_) {}
            webContents.emit("did-start-navigation", {}, value.url, false, true);
            webContents.emit("did-navigate", {}, value.url);
        } else if (message.type === "webviewProgress" && value.progress >= 100) {
            window.finishLoad();
            webContents.emit("did-finish-load");
            if (!window.readyToShow) {
                window.readyToShow = true;
                window.emit("ready-to-show");
            }
        } else if (message.type === "webviewError") {
            window.finishLoad(new Error(value.description || value.error || "load failed"));
            webContents.emit("did-fail-load", {}, value.code || -1,
                value.description || value.error || "load failed", value.url || "", true);
        } else if (message.type === "webviewDestroyed") {
            window.finishLoad(new Error("webview renderer was destroyed"));
            webContents.emit("render-process-gone", {}, {reason: "crashed", exitCode: 1});
        }
    }

    function runMain(entry) {
        const mainPath = path.resolve(appRoot, entry);
        if (!mainPath.startsWith(appRoot + path.sep) || !fs.existsSync(mainPath)) {
            throw new Error(`Electron main entry not found: ${entry}`);
        }
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
        getPreload(windowId) { return windows.get(windowId)?.getPreloadScript() ?? null; }
    };
}
