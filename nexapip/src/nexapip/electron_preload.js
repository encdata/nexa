(() => {
    if (typeof window.process === "undefined") {
        const listeners = new Map();
        const emitProcessEvent = (name, ...args) => {
            for (const listener of [...(listeners.get(name) || [])]) listener(...args);
        };
        const process = {
            argv: [],
            arch: window.__NEXA_ELECTRON_PROCESS__?.arch || "x64",
            env: Object.assign({HOME: String(window.__NEXA_ELECTRON_HOME__ || "/"),
                TMPDIR: "/tmp"}, window.__NEXA_ELECTRON_PROCESS__?.env || {}),
            isMainFrame: window.top === window,
            platform: "linux",
            versions: window.__NEXA_ELECTRON_PROCESS__?.versions || {},
            pid: window.__NEXA_ELECTRON_PROCESS__?.pid || 0,
            type: "renderer",
            cwd() {
                return String(window.__NEXA_ELECTRON_CWD__ || "/");
            },
            uptime() { return performance.now() / 1000; },
            memoryUsage() {
                const memory = performance.memory || {};
                return {rss: 0, heapTotal: memory.totalJSHeapSize || 0,
                    heapUsed: memory.usedJSHeapSize || 0, external: 0, arrayBuffers: 0};
            },
            getHeapStatistics() {
                const memory = performance.memory || {};
                return {totalHeapSize: memory.totalJSHeapSize || 0,
                    usedHeapSize: memory.usedJSHeapSize || 0,
                    heapSizeLimit: memory.jsHeapSizeLimit || 0};
            },
            getBlinkMemoryInfo() {
                const memory = performance.memory || {};
                return {allocated: memory.totalJSHeapSize || 0,
                    used: memory.usedJSHeapSize || 0};
            },
            nextTick(callback, ...args) {
                queueMicrotask(() => callback(...args));
            },
            on(name, listener) {
                const entries = listeners.get(name) || new Set();
                entries.add(listener);
                listeners.set(name, entries);
                return process;
            },
            once(name, listener) {
                if (name === "loaded") {
                    queueMicrotask(listener);
                    return process;
                }
                const once = (...args) => {
                    process.removeListener(name, once);
                    listener(...args);
                };
                return process.on(name, once);
            },
            removeListener(name, listener) {
                const entries = listeners.get(name);
                entries?.delete(listener);
                if (entries?.size === 0) listeners.delete(name);
                return process;
            },
            emit(name, ...args) {
                emitProcessEvent(name, ...args);
                return listeners.has(name);
            }
        };
        window.addEventListener("error", event => emitProcessEvent(
            "uncaughtException", event.error || new Error(event.message), "uncaughtException"));
        window.addEventListener("unhandledrejection", event => emitProcessEvent(
            "unhandledRejection", event.reason));
        Object.defineProperty(window, "process", {configurable: true, value: process});
    }
    if (typeof window.global === "undefined") {
        Object.defineProperty(window, "global", {configurable: true, value: window});
    }

    const native = window.nexaElectronNative;
    if (native) {
        native.postMessage(JSON.stringify({kind: "preload-start"}));
    }
    if (!native) {
        console.error("nexa electron preload: native WebView bridge is missing");
    }

    let rendererErrorCount = 0;
    const reportedErrors = new Set();
    function reportRendererError(type, message, source = "", line = 0, column = 0) {
        const detail = {
            type,
            message: String(message || type).slice(0, 1000),
            source: String(source || "").slice(0, 500),
            line: Number(line) || 0,
            column: Number(column) || 0
        };
        const key = `${detail.type}:${detail.message}:${detail.source}:${detail.line}`;
        if (rendererErrorCount >= 40 || reportedErrors.has(key)) return;
        reportedErrors.add(key);
        rendererErrorCount++;
        postBridgeMessage(JSON.stringify({
            kind: "send", channel: "__nexa:renderer-error", args: [detail]
        }));
    }

    window.addEventListener("error", event => {
        reportRendererError("error", event.message, event.filename,
            event.lineno, event.colno);
    }, true);
    window.addEventListener("unhandledrejection", event => {
        const reason = event.reason;
        reportRendererError("unhandledrejection",
            reason?.stack || reason?.message || reason,
            location.href);
    });

    let nextId = 1;
    const pending = new Map();
    const listeners = new Map();
    const insertedStyles = new Map();
    const queuedSends = [];
    let queuedSendChars = 0;
    let sendFlushQueued = false;

    function postBridgeMessage(data) {
        if (native) {
            native.postMessage(data);
            return;
        }
        const endpoint = window.__NEXA_ELECTRON_SYNC_ENDPOINT__;
        if (!endpoint) {
            console.error("nexa electron preload: IPC endpoint is missing");
            return;
        }
        const message = JSON.parse(data);
        message.token = window.__NEXA_ELECTRON_IPC_TOKEN__;
        message.windowId = window.__NEXA_ELECTRON_WINDOW_ID__;
        const xhr = new XMLHttpRequest();
        xhr.open("POST", endpoint, true);
        xhr.setRequestHeader("Content-Type", "text/plain;charset=UTF-8");
        xhr.onload = () => {
            let result;
            try { result = JSON.parse(xhr.responseText); }
            catch (_) { result = {error: `Electron IPC failed (${xhr.status})`}; }
            if (message.kind !== "invoke") {
                if (result.error !== undefined) console.error(result.error);
                return;
            }
            const call = pending.get(message.id);
            if (!call) return;
            pending.delete(message.id);
            if (xhr.status < 200 || xhr.status >= 300 || result.error !== undefined) {
                call.reject(new Error(result.error || `Electron IPC failed (${xhr.status})`));
            } else call.resolve(result.value);
        };
        xhr.onerror = () => {
            if (message.kind !== "invoke") return;
            const call = pending.get(message.id);
            if (!call) return;
            pending.delete(message.id);
            call.reject(new Error("Electron IPC request failed"));
        };
        xhr.send(JSON.stringify(message));
    }

    function flushSends() {
        sendFlushQueued = false;
        if (!queuedSends.length) return;
        const messages = queuedSends.splice(0, queuedSends.length);
        queuedSendChars = 0;
        postBridgeMessage(`{"kind":"batch","messages":[${messages.join(",")}]}`);
    }

    function request(kind, channel, args) {
        if (kind === "send") {
            const message = JSON.stringify({kind, channel, args});
            if (message.length > 256 * 1024) {
                flushSends();
                postBridgeMessage(message);
                return;
            }
            if (queuedSendChars + message.length > 256 * 1024 || queuedSends.length >= 128) {
                flushSends();
            }
            queuedSends.push(message);
            queuedSendChars += message.length;
            if (!sendFlushQueued) {
                sendFlushQueued = true;
                queueMicrotask(flushSends);
            }
            return;
        }
        flushSends();
        const id = kind === "invoke" ? nextId++ : null;
        if (id !== null) {
            return new Promise((resolve, reject) => {
                pending.set(id, {resolve, reject});
                postBridgeMessage(JSON.stringify({kind, id, channel, args}));
            });
        }
        postBridgeMessage(JSON.stringify({kind, channel, args}));
    }

    function addListener(channel, listener, once = false, prepend = false) {
        if (typeof listener !== "function") throw new TypeError("listener must be a function");
        const entries = listeners.get(channel) || [];
        const entry = {listener, once};
        ipcRenderer.emit("newListener", channel, listener);
        if (prepend) entries.unshift(entry);
        else entries.push(entry);
        listeners.set(channel, entries);
        return ipcRenderer;
    }

    function removeListener(channel, listener) {
        const entries = listeners.get(channel);
        if (!entries) return ipcRenderer;
        for (let index = entries.length - 1; index >= 0; index--) {
            if (entries[index].listener !== listener) continue;
            return removeListenerEntry(channel, entries[index]);
        }
        return ipcRenderer;
    }

    function removeListenerEntry(channel, entry) {
        const entries = listeners.get(channel);
        const index = entries?.indexOf(entry) ?? -1;
        if (index === -1) return ipcRenderer;
        entries.splice(index, 1);
        if (channel !== "removeListener") {
            ipcRenderer.emit("removeListener", channel, entry.listener);
        }
        if (!entries.length) listeners.delete(channel);
        return ipcRenderer;
    }

    const ipcRenderer = {
        send(channel, ...args) {
            request("send", channel, args);
        },
        sendSync(channel, ...args) {
            const endpoint = window.__NEXA_ELECTRON_SYNC_ENDPOINT__;
            if (!endpoint) throw new Error("Electron sync IPC is unavailable");
            const xhr = new XMLHttpRequest();
            xhr.open("POST", endpoint, false);
            xhr.setRequestHeader("Content-Type", "text/plain;charset=UTF-8");
            xhr.send(JSON.stringify({
                kind: "sendSync", channel, args,
                token: window.__NEXA_ELECTRON_IPC_TOKEN__,
                windowId: window.__NEXA_ELECTRON_WINDOW_ID__
            }));
            if (xhr.status < 200 || xhr.status >= 300) {
                throw new Error(`Electron sync IPC failed (${xhr.status})`);
            }
            const result = JSON.parse(xhr.responseText);
            if (result.error !== undefined) throw new Error(result.error);
            return result.value;
        },
        invoke(channel, ...args) {
            return request("invoke", channel, args);
        },
        on(channel, listener) {
            return addListener(channel, listener);
        },
        addListener(channel, listener) {
            return addListener(channel, listener);
        },
        once(channel, listener) {
            return addListener(channel, listener, true);
        },
        prependListener(channel, listener) {
            return addListener(channel, listener, false, true);
        },
        prependOnceListener(channel, listener) {
            return addListener(channel, listener, true, true);
        },
        off(channel, listener) {
            return removeListener(channel, listener);
        },
        removeListener(channel, listener) {
            return removeListener(channel, listener);
        },
        removeAllListeners(channel) {
            const channels = channel === undefined ? [...listeners.keys()] : [channel];
            for (const name of channels) {
                for (const entry of [...(listeners.get(name) || [])].reverse()) {
                    if (name !== "removeListener") {
                        ipcRenderer.emit("removeListener", name, entry.listener);
                    }
                }
                listeners.delete(name);
            }
            return ipcRenderer;
        },
        emit(channel, ...args) {
            const entries = listeners.get(channel);
            if (!entries?.length) return false;
            for (const entry of [...entries]) {
                if (entry.once) removeListenerEntry(channel, entry);
                entry.listener(...args);
            }
            return true;
        },
        listenerCount(channel, listener) {
            const entries = listeners.get(channel) || [];
            return listener === undefined ? entries.length
                : entries.filter(entry => entry.listener === listener).length;
        },
        eventNames() {
            return [...listeners.keys()];
        },
        listeners(channel) {
            return (listeners.get(channel) || []).map(entry => entry.listener);
        },
        rawListeners(channel) {
            return ipcRenderer.listeners(channel);
        },
        setMaxListeners() {
            return ipcRenderer;
        },
        getMaxListeners() {
            return 10;
        }
    };

    const openWindow = window.open.bind(window);
    window.open = (url = "", frameName = "", features = "") => {
        if (frameName === "_self" || frameName === "_parent" || frameName === "_top") {
            return openWindow(url, frameName, features);
        }
        let target;
        try {
            target = new URL(String(url || "about:blank"), location.href).href;
        } catch (_) {
            return null;
        }
        ipcRenderer.send("__nexa:window-open", {
            url: target,
            frameName: String(frameName || ""),
            features: String(features || "")
        });
        return null;
    };

    class NexaBuffer extends Uint8Array {
        static from(value, encoding) {
            if (typeof value === "string") {
                if (encoding === "base64") {
                    const binary = atob(value);
                    const buffer = new NexaBuffer(binary.length);
                    for (let index = 0; index < binary.length; index++) {
                        buffer[index] = binary.charCodeAt(index);
                    }
                    return buffer;
                }
                if (encoding === "hex") {
                    const bytes = [];
                    for (let index = 0; index + 1 < value.length; index += 2) {
                        bytes.push(parseInt(value.slice(index, index + 2), 16));
                    }
                    return new NexaBuffer(bytes);
                }
                return new NexaBuffer(new TextEncoder().encode(value));
            }
            if (ArrayBuffer.isView(value)) {
                return new NexaBuffer(value.buffer.slice(
                    value.byteOffset, value.byteOffset + value.byteLength));
            }
            return new NexaBuffer(value);
        }
        static alloc(size, fill = 0) {
            const buffer = new NexaBuffer(size);
            if (fill !== 0) buffer.fill(fill);
            return buffer;
        }
        static isBuffer(value) { return value instanceof NexaBuffer; }
        static byteLength(value, encoding = "utf8") {
            if (ArrayBuffer.isView(value)) return value.byteLength;
            if (value instanceof ArrayBuffer) return value.byteLength;
            return NexaBuffer.from(String(value), encoding).length;
        }
        static concat(values, totalLength) {
            const length = totalLength ?? values.reduce((sum, value) => sum + value.length, 0);
            const result = new NexaBuffer(length);
            let offset = 0;
            for (const value of values) {
                const part = value.subarray(0, Math.max(0, length - offset));
                result.set(part, offset);
                offset += part.length;
                if (offset >= length) break;
            }
            return result;
        }
        toString(encoding = "utf8", start = 0, end = this.length) {
            const bytes = this.subarray(start, end);
            if (encoding === "base64") {
                let binary = "";
                for (let offset = 0; offset < bytes.length; offset += 0x8000) {
                    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
                }
                return btoa(binary);
            }
            if (encoding === "hex") return [...bytes].map(byte => byte.toString(16).padStart(2, "0")).join("");
            return new TextDecoder(encoding === "utf8" ? "utf-8" : encoding).decode(bytes);
        }
        readUInt32LE(offset) {
            return new DataView(this.buffer, this.byteOffset, this.byteLength).getUint32(offset, true);
        }
        readUInt16LE(offset) {
            return new DataView(this.buffer, this.byteOffset, this.byteLength).getUint16(offset, true);
        }
        readInt32LE(offset) {
            return new DataView(this.buffer, this.byteOffset, this.byteLength).getInt32(offset, true);
        }
    }
    class NexaStats {
        isFile() { return this._isFile; }
        isDirectory() { return this._isDirectory; }
        isBlockDevice() { return this._isBlockDevice; }
        isCharacterDevice() { return this._isCharacterDevice; }
        isSymbolicLink() { return this._isSymbolicLink; }
        isFIFO() { return this._isFIFO; }
        isSocket() { return this._isSocket; }
    }
    class NexaDirent {
        isFile() { return this._isFile; }
        isDirectory() { return this._isDirectory; }
        isBlockDevice() { return this._isBlockDevice; }
        isCharacterDevice() { return this._isCharacterDevice; }
        isSymbolicLink() { return this._isSymbolicLink; }
        isFIFO() { return this._isFIFO; }
        isSocket() { return this._isSocket; }
    }
    const fsHandleFinalizer = typeof FinalizationRegistry === "function"
        ? new FinalizationRegistry(id => {
            try { request("send", "nexa:node:fs-handle:release", [id]); }
            catch (_) {}
        })
        : null;
    class NexaFileHandle {
        constructor(id, fd) {
            this.id = id;
            this.fd = fd;
            this.closed = false;
            this.closing = null;
            fsHandleFinalizer?.register(this, id, this);
        }
        call(method, args = []) {
            if (this.closed && method !== "close") {
                return Promise.reject(Object.assign(new Error("file handle is closed"), {code: "EBADF"}));
            }
            return ipcRenderer.invoke("nexa:node:fs-handle", this.id, method,
                encodeFsValue(args)).then(fsResult);
        }
        appendFile(...args) { return this.call("appendFile", args); }
        chmod(...args) { return this.call("chmod", args); }
        chown(...args) { return this.call("chown", args); }
        close() {
            if (this.closed) return Promise.resolve();
            if (this.closing) return this.closing;
            this.closing = this.call("close").then(result => {
                this.closed = true;
                this.closing = null;
                fsHandleFinalizer?.unregister(this);
                return result;
            }, error => {
                this.closing = null;
                throw error;
            });
            return this.closing;
        }
        datasync(...args) { return this.call("datasync", args); }
        read(buffer, ...args) {
            return this.call("read", [buffer, ...args]).then(result => {
                if (buffer instanceof Uint8Array && result.buffer instanceof Uint8Array) {
                    buffer.set(result.buffer.subarray(0, buffer.length));
                    result.buffer = buffer;
                }
                return result;
            });
        }
        readv(buffers, ...args) {
            return this.call("readv", [buffers, ...args]).then(result => {
                copyFsReadBuffers(buffers, result.buffers);
                result.buffers = buffers;
                return result;
            });
        }
        readFile(...args) { return this.call("readFile", args); }
        stat(...args) { return this.call("stat", args); }
        sync(...args) { return this.call("sync", args); }
        truncate(...args) { return this.call("truncate", args); }
        utimes(...args) { return this.call("utimes", args); }
        write(...args) { return this.call("write", args); }
        writeFile(...args) { return this.call("writeFile", args); }
        writev(...args) { return this.call("writev", args); }
    }

    function encodeFsValue(value) {
        if (typeof value === "bigint") return {__nexaType: "BigInt", value: value.toString()};
        if (value instanceof URL) return {__nexaType: "FileURL", value: value.href};
        if (value instanceof Date) return {__nexaType: "Date", value: value.toISOString()};
        if (value instanceof Uint8Array) {
            let binary = "";
            for (let offset = 0; offset < value.length; offset += 0x8000) {
                binary += String.fromCharCode(...value.subarray(offset, offset + 0x8000));
            }
            return {__nexaType: "Buffer", data: btoa(binary)};
        }
        if (Array.isArray(value)) return value.map(encodeFsValue);
        if (value && typeof value === "object") {
            const output = {};
            for (const [key, item] of Object.entries(value)) output[key] = encodeFsValue(item);
            return output;
        }
        return value;
    }

    function decodeFsValue(value) {
        if (Array.isArray(value)) return value.map(decodeFsValue);
        if (value && typeof value === "object") {
            if (value.__nexaType === "BigInt") return BigInt(value.value);
            if (value.__nexaType === "FileURL") return new URL(value.value);
            if (value.__nexaType === "Undefined") return undefined;
            if (value.__nexaType === "Date") return new Date(value.value);
            if (value.__nexaType === "Buffer") return NexaBuffer.from(value.data, "base64");
            if (value.__nexaType === "FsError") {
                const error = new Error(value.value?.message || "filesystem operation failed");
                Object.assign(error, value.value);
                return error;
            }
            if (value.__nexaType === "CallbackValues") {
                return {__nexaType: "CallbackValues", values: decodeFsValue(value.values)};
            }
            if (value.__nexaType === "FsReadResult") {
                return {__nexaType: "FsReadResult", value: value.value,
                    buffer: decodeFsValue(value.buffer)};
            }
            if (value.__nexaType === "FsReadvResult") {
                return {__nexaType: "FsReadvResult", value: value.value,
                    buffers: decodeFsValue(value.buffers)};
            }
            if (value.__nexaType === "Stats") {
                const stats = Object.assign(new NexaStats(), decodeFsValue(value.value));
                for (const method of ["isFile", "isDirectory", "isBlockDevice",
                    "isCharacterDevice", "isSymbolicLink", "isFIFO", "isSocket"]) {
                    stats[`_${method}`] = stats[method];
                    delete stats[method];
                }
                return stats;
            }
            if (value.__nexaType === "Dirent") {
                const dirent = Object.assign(new NexaDirent(), decodeFsValue(value.value));
                for (const method of ["isFile", "isDirectory", "isBlockDevice",
                    "isCharacterDevice", "isSymbolicLink", "isFIFO", "isSocket"]) {
                    dirent[`_${method}`] = dirent[method];
                    delete dirent[method];
                }
                return dirent;
            }
            const output = {};
            for (const [key, item] of Object.entries(value)) output[key] = decodeFsValue(item);
            return output;
        }
        return value;
    }
    function fsResult(value) {
        const result = decodeFsValue(value);
        if (result instanceof Error) throw result;
        return result;
    }
    function copyFsReadBuffer(target, source) {
        if (target instanceof Uint8Array && source instanceof Uint8Array) {
            target.set(source.subarray(0, target.length));
        }
    }
    function copyFsReadBuffers(targets, sources) {
        if (!Array.isArray(targets) || !Array.isArray(sources)) return;
        for (let index = 0; index < Math.min(targets.length, sources.length); index++) {
            copyFsReadBuffer(targets[index], sources[index]);
        }
    }

    let nextNativeCallbackId = 0;
    const nativeCallbacks = new Map();
    const nativeRefs = new WeakMap();
    const nativeFunctionOwners = new WeakMap();
    const nativeObjectFinalizer = typeof FinalizationRegistry === "function"
        ? new FinalizationRegistry(({moduleId, objectId}) => {
            try {
                request("send", "nexa:node:module:release-object", [moduleId, objectId]);
            } catch (_) {}
        })
        : null;
    function encodeNativeArgs(value) {
        if (typeof value === "function") {
            const id = ++nextNativeCallbackId;
            nativeCallbacks.set(id, value);
            return {__nexaType: "NativeCallback", id};
        }
        if (Array.isArray(value)) return value.map(encodeNativeArgs);
        const reference = value && typeof value === "object" ? nativeRefs.get(value) : null;
        if (reference) {
            return {__nexaType: "NativeReference", moduleId: reference.moduleId,
                objectId: reference.objectId};
        }
        if (value instanceof Uint8Array) return encodeFsValue(value);
        if (value && typeof value === "object") {
            return Object.fromEntries(Object.entries(value).map(([key, item]) =>
                [key, encodeNativeArgs(item)]));
        }
        return value;
    }
    function decodeNativeResult(moduleId, value) {
        const result = fsResult(value);
        if (result?.__nexaType !== "NativeResult") return result;
        return createNativeExports(moduleId, result.value);
    }
    function createNativeExports(moduleId, descriptor, propertyPath = [], objectId = null,
                                 ownerReference = null) {
        if (descriptor?.__nexaType === "NativeFunction") {
            const callable = function(...args) {
                const payload = encodeNativeArgs(args);
                if (new.target) {
                    if (!descriptor.constructable) {
                        throw new TypeError("native addon export is not a constructor");
                    }
                    const result = ipcRenderer.sendSync("nexa:node:module:construct",
                        moduleId, propertyPath, payload);
                    return decodeNativeResult(moduleId, result);
                }
                if (descriptor.async) {
                    return ipcRenderer.invoke("nexa:node:module:invoke", moduleId,
                        objectId, propertyPath, payload).then(value =>
                        decodeNativeResult(moduleId, value)).then(result => {
                        if (objectId !== null && propertyPath.at(-1) === "destroy") {
                            ipcRenderer.send("nexa:node:module:release-object", moduleId, objectId);
                        }
                        return result;
                    });
                }
                const result = decodeNativeResult(moduleId, ipcRenderer.sendSync(
                    "nexa:node:module:call", moduleId, objectId, propertyPath, payload));
                if (objectId !== null && propertyPath.at(-1) === "destroy") {
                    ipcRenderer.send("nexa:node:module:release-object", moduleId, objectId);
                }
                return result;
            };
            if (ownerReference) nativeFunctionOwners.set(callable, ownerReference);
            for (const [key, value] of Object.entries(descriptor.entries || {})) {
                callable[key] = createNativeExports(moduleId, value,
                    [...propertyPath, key], objectId, ownerReference);
            }
            return callable;
        }
        if (descriptor?.__nexaType === "NativeObject" ||
            descriptor?.__nexaType === "NativeRemoteObject") {
            const output = {};
            if (descriptor.__nexaType === "NativeRemoteObject") {
                const reference = {moduleId, objectId: descriptor.objectId};
                nativeRefs.set(output, reference);
                nativeObjectFinalizer?.register(output, reference, output);
                objectId = descriptor.objectId;
            }
            for (const [key, value] of Object.entries(descriptor.entries || {})) {
                output[key] = createNativeExports(moduleId, value,
                    [...propertyPath, key], objectId,
                    descriptor.__nexaType === "NativeRemoteObject" ? output : ownerReference);
            }
            return output;
        }
        if (Array.isArray(descriptor)) {
            return descriptor.map((value, index) =>
                createNativeExports(moduleId, value, [...propertyPath, index]));
        }
        return decodeFsValue(descriptor);
    }
    ipcRenderer.on("__nexa:node:module-callback", (_event, moduleId, callbackId, args, once) => {
        const callback = nativeCallbacks.get(callbackId);
        if (!callback) return;
        if (once) nativeCallbacks.delete(callbackId);
        try {
            callback(...decodeFsValue(args));
        } catch (error) {
            queueMicrotask(() => { throw error; });
        }
    });
    const nativeModuleCache = new Map();
    function loadNativeModule(specifier, baseFile = window.__NEXA_ELECTRON_PRELOAD_BASE__) {
        if (!baseFile) throw new Error("native modules can only be loaded from an app preload");
        const cacheKey = specifier.startsWith("node:") ? specifier.slice(5) : specifier;
        if (nativeModuleCache.has(cacheKey)) {
            const cached = nativeModuleCache.get(cacheKey);
            nativeModuleCache.delete(cacheKey);
            nativeModuleCache.set(cacheKey, cached);
            return cached;
        }
        const result = fsResult(ipcRenderer.sendSync("nexa:node:module:load",
            specifier, baseFile));
        const module = createNativeExports(result.id, result.exports);
        nativeModuleCache.set(cacheKey, module);
        if (nativeModuleCache.size > 64) {
            nativeModuleCache.delete(nativeModuleCache.keys().next().value);
        }
        return module;
    }

    const fsSyncMethods = new Set([
        "accessSync", "appendFileSync", "chmodSync", "chownSync", "closeSync",
        "copyFileSync", "cpSync", "existsSync", "fchmodSync", "fchownSync",
        "fdatasyncSync", "fstatSync", "fsyncSync", "ftruncateSync", "futimesSync",
        "lchmodSync", "lchownSync", "linkSync", "lstatSync", "lutimesSync",
        "mkdirSync", "mkdtempSync", "openSync", "readFileSync", "readdirSync",
        "readlinkSync", "realpathSync", "renameSync", "rmSync", "rmdirSync",
        "statSync", "symlinkSync", "truncateSync", "unlinkSync", "utimesSync",
        "readSync", "readvSync", "writeFileSync", "writeSync", "writevSync"
    ]);
    const fsAsyncMethods = new Set([
        "access", "appendFile", "chmod", "chown", "close", "copyFile", "cp",
        "fchmod", "fchown", "fdatasync", "fstat", "fsync", "ftruncate", "futimes",
        "link", "lstat", "lutimes", "mkdir", "mkdtemp", "open", "read", "readFile",
        "readv", "readdir", "readlink", "realpath", "rename", "rm", "rmdir", "stat",
        "symlink", "truncate", "unlink", "utimes", "write", "writeFile", "writev"
    ]);
    const fsPromiseMethodCache = new Map();
    const fsPromises = new Proxy({}, {
        get(_target, method) {
            if (typeof method !== "string" || !fsAsyncMethods.has(method)) return undefined;
            if (!fsPromiseMethodCache.has(method)) {
                fsPromiseMethodCache.set(method, (...args) =>
                    ipcRenderer.invoke("nexa:node:fs", "promises:" + method,
                        encodeFsValue(args)).then(fsResult).then(value =>
                        method === "open" ? new NexaFileHandle(value.id, value.fd) : value));
            }
            return fsPromiseMethodCache.get(method);
        }
    });
    function createFsEventTarget() {
        const events = new Map();
        return {
            on(name, listener) {
                const listeners = events.get(name) || new Set();
                listeners.add(listener);
                events.set(name, listeners);
                return this;
            },
            once(name, listener) {
                const once = (...args) => {
                    this.removeListener(name, once);
                    listener(...args);
                };
                return this.on(name, once);
            },
            removeListener(name, listener) {
                events.get(name)?.delete(listener);
                return this;
            },
            emit(name, ...args) {
                for (const listener of [...(events.get(name) || [])]) listener(...args);
            }
        };
    }
    function createReadStream(...args) {
        const target = createFsEventTarget();
        let streamId;
        let started = false;
        let paused = true;
        let closed = false;
        const encoding = args[1]?.encoding;
        const readNext = async () => {
            if (started || paused || closed) return;
            started = true;
            try {
                streamId = await ipcRenderer.invoke("nexa:node:fs-stream", "openRead", null,
                    encodeFsValue(args));
                if (closed) {
                    await ipcRenderer.invoke("nexa:node:fs-stream", "close", streamId, []);
                    return;
                }
                target.emit("open", streamId);
                target.emit("ready");
                while (!paused && !closed) {
                    const result = await ipcRenderer.invoke(
                        "nexa:node:fs-stream", "read", streamId, []);
                    if (result.done) {
                        closed = true;
                        target.emit("end");
                        target.emit("close");
                        return;
                    }
                    const value = decodeFsValue(result.value);
                    target.emit("data", encoding ? value.toString(encoding) : value);
                }
            } catch (error) {
                closed = true;
                if (streamId !== undefined) {
                    void ipcRenderer.invoke("nexa:node:fs-stream", "close", streamId, []);
                }
                target.emit("error", error);
                target.emit("close");
            } finally {
                started = false;
            }
        };
        const stream = {
            on(name, listener) {
                target.on(name, listener);
                if (name === "data") stream.resume();
                return stream;
            },
            once(name, listener) {
                target.once(name, listener);
                if (name === "data") stream.resume();
                return stream;
            },
            removeListener(name, listener) {
                target.removeListener(name, listener);
                return stream;
            },
            pause() { paused = true; return stream; },
            resume() { paused = false; void readNext(); return stream; },
            destroy(error) {
                if (closed) return stream;
                closed = true;
                if (streamId !== undefined) {
                    void ipcRenderer.invoke("nexa:node:fs-stream", "close", streamId, []);
                }
                if (error) target.emit("error", error);
                target.emit("close");
                return stream;
            },
            pipe(destination) {
                let ended = false;
                stream.on("data", chunk => {
                    if (!destination.write(chunk)) stream.pause();
                });
                stream.on("end", () => {
                    ended = true;
                    destination.end();
                });
                stream.on("error", error => destination.destroy?.(error));
                destination.on?.("drain", () => { if (!ended) stream.resume(); });
                stream.resume();
                return destination;
            }
        };
        return stream;
    }
    function createWriteStream(...args) {
        const target = createFsEventTarget();
        let streamId;
        let closed = false;
        let pendingWrites = 0;
        let queue = ipcRenderer.invoke("nexa:node:fs-stream", "openWrite", null,
            encodeFsValue(args)).then(id => {
                streamId = id;
                if (closed) return;
                target.emit("open", id);
                target.emit("ready");
            }, error => {
                closed = true;
                target.emit("error", error);
            });
        const stream = {
            on: target.on.bind(target),
            once: target.once.bind(target),
            removeListener: target.removeListener.bind(target),
            write(chunk, encoding, callback) {
                if (typeof encoding === "function") {
                    callback = encoding;
                    encoding = undefined;
                }
                if (typeof chunk === "string") chunk = NexaBuffer.from(chunk, encoding);
                const payload = encodeFsValue([chunk]);
                pendingWrites++;
                queue = queue.then(() => {
                    if (closed) throw new Error("filesystem stream is closed");
                    return ipcRenderer.invoke("nexa:node:fs-stream", "write", streamId, payload);
                }).then(() => callback?.(), error => {
                    closed = true;
                    target.emit("error", error);
                    callback?.(error);
                    if (streamId !== undefined) {
                        void ipcRenderer.invoke("nexa:node:fs-stream", "close", streamId, []);
                    }
                }).finally(() => {
                    pendingWrites--;
                    if (pendingWrites === 0) target.emit("drain");
                });
                return pendingWrites < 2;
            },
            end(chunk, encoding, callback) {
                if (typeof chunk === "function") {
                    callback = chunk;
                    chunk = undefined;
                } else if (typeof encoding === "function") {
                    callback = encoding;
                    encoding = undefined;
                }
                if (chunk !== undefined) stream.write(chunk, encoding);
                queue = queue.then(() => {
                    if (closed) throw new Error("filesystem stream is closed");
                    return ipcRenderer.invoke("nexa:node:fs-stream", "end", streamId, []);
                }).then(() => {
                    closed = true;
                    target.emit("finish");
                    target.emit("close");
                    callback?.();
                }, error => {
                    closed = true;
                    target.emit("error", error);
                    callback?.(error);
                });
                return stream;
            },
            destroy(error) {
                if (closed) return stream;
                closed = true;
                queue = queue.then(() => streamId === undefined ? undefined :
                    ipcRenderer.invoke("nexa:node:fs-stream", "close", streamId, []));
                if (error) target.emit("error", error);
                target.emit("close");
                return stream;
            },
            pipe() { throw new Error("filesystem write streams cannot be piped as sources"); }
        };
        return stream;
    }
    const fsMethodCache = new Map();
    const fsModule = new Proxy({}, {
        get(target, method) {
            if (Reflect.has(target, method)) return Reflect.get(target, method);
            if (method === "promises") return fsPromises;
            if (method === "createReadStream") return createReadStream;
            if (method === "createWriteStream") return createWriteStream;
            if (method === "constants") {
                if (!Object.hasOwn(fsModule, "_constants")) {
                    Object.defineProperty(fsModule, "_constants", {value: fsResult(
                        ipcRenderer.sendSync("nexa:node:fs-sync", "constants", []))});
                }
                return fsModule._constants;
            }
            if (method === "Stats") return NexaStats;
            if (method === "Dirent") return NexaDirent;
            if (typeof method !== "string") return undefined;
            if (/^[A-Z][A-Z0-9_]*$/.test(method)) {
                return fsModule.constants[method];
            }
            if (method === "realpathSync") {
                if (!fsMethodCache.has(method)) {
                    const realpath = (...args) => fsResult(ipcRenderer.sendSync(
                        "nexa:node:fs-sync", method, encodeFsValue(args)));
                    realpath.native = realpath;
                    fsMethodCache.set(method, realpath);
                }
                return fsMethodCache.get(method);
            }
            if (method === "exists") {
                if (!fsMethodCache.has(method)) {
                    fsMethodCache.set(method, (...args) => {
                        const callback = typeof args.at(-1) === "function" ? args.pop() : null;
                        if (!callback) {
                            return fsResult(ipcRenderer.sendSync(
                                "nexa:node:fs-sync", "exists", encodeFsValue(args)));
                        }
                        ipcRenderer.invoke("nexa:node:fs", "callback:exists", encodeFsValue(args))
                            .then(fsResult)
                            .then(result => callback(null, ...result.values), error => callback(error));
                    });
                }
                return fsMethodCache.get(method);
            }
            if (fsSyncMethods.has(method)) {
                if (!fsMethodCache.has(method)) {
                    fsMethodCache.set(method, (...args) => {
                        const result = fsResult(ipcRenderer.sendSync(
                            "nexa:node:fs-sync", method, encodeFsValue(args)));
                        if (method === "readSync" && result?.__nexaType === "FsReadResult") {
                            copyFsReadBuffer(args[1], result.buffer);
                            return result.value;
                        }
                        if (method === "readvSync" && result?.__nexaType === "FsReadvResult") {
                            copyFsReadBuffers(args[1], result.buffers);
                            return result.value;
                        }
                        return result;
                    });
                }
                return fsMethodCache.get(method);
            }
            if (fsAsyncMethods.has(method)) {
                if (!fsMethodCache.has(method)) {
                    fsMethodCache.set(method, (...args) => {
                        const callback = typeof args.at(-1) === "function" ? args.pop() : null;
                        const operation = callback
                            ? `callback:${method}` : `promises:${method}`;
                        const pending = ipcRenderer.invoke("nexa:node:fs", operation,
                            encodeFsValue(args)).then(fsResult);
                        if (!callback) return pending;
                        pending.then(value => {
                            if (method === "read" && value?.__nexaType === "FsReadResult") {
                                copyFsReadBuffer(args[1], value.buffer);
                                callback(null, value.value, args[1]);
                                return;
                            }
                            if (method === "readv" && value?.__nexaType === "FsReadvResult") {
                                copyFsReadBuffers(args[1], value.buffers);
                                callback(null, value.value, args[1]);
                                return;
                            }
                            const results = value?.__nexaType === "CallbackValues"
                                ? value.values : [value];
                            callback(null, ...results);
                        }, error => callback(error));
                        return undefined;
                    });
                }
                return fsMethodCache.get(method);
            }
            return undefined;
        }
    });
    function createPathModule() {
        function validate(value) {
            if (typeof value !== "string") {
                throw new TypeError("path must be a string");
            }
            return value;
        }
        function normalizeString(value, allowAboveRoot) {
            const segments = [];
            for (const segment of value.split("/")) {
                if (!segment || segment === ".") continue;
                if (segment === "..") {
                    if (segments.length && segments[segments.length - 1] !== "..") segments.pop();
                    else if (allowAboveRoot) segments.push("..");
                } else {
                    segments.push(segment);
                }
            }
            return segments.join("/");
        }
        function normalize(value) {
            value = validate(value);
            if (!value) return ".";
            const absolute = value.startsWith("/");
            const trailing = value.endsWith("/");
            let result = normalizeString(value, !absolute);
            if (!result && !absolute) result = ".";
            if (absolute) result = "/" + result;
            if (trailing && result !== "/") result += "/";
            return result;
        }
        function resolve(...values) {
            let combined = "";
            let absolute = false;
            for (let index = values.length - 1; index >= -1 && !absolute; index--) {
                const segment = index >= 0 ? validate(values[index]) : process.cwd();
                if (!segment) continue;
                combined = segment + "/" + combined;
                absolute = segment.startsWith("/");
            }
            const result = normalizeString(combined, !absolute);
            if (absolute) return "/" + result;
            return result || ".";
        }
        function isAbsolute(value) {
            return validate(value).startsWith("/");
        }
        function join(...values) {
            if (!values.length) return ".";
            return normalize(values.map(validate).filter(Boolean).join("/"));
        }
        function relative(from, to) {
            const source = resolve(validate(from)).split("/").filter(Boolean);
            const target = resolve(validate(to)).split("/").filter(Boolean);
            let shared = 0;
            while (shared < source.length && shared < target.length &&
                source[shared] === target[shared]) shared++;
            return [...Array(source.length - shared).fill(".."),
                ...target.slice(shared)].join("/");
        }
        function dirname(value) {
            value = validate(value);
            if (!value) return ".";
            let end = value.length - 1;
            while (end > 0 && value[end] === "/") end--;
            const slash = value.lastIndexOf("/", end);
            if (slash === -1) return ".";
            if (slash === 0) return "/";
            let start = slash - 1;
            while (start >= 0 && value[start] === "/") start--;
            return value.slice(0, start + 1);
        }
        function basename(value, suffix) {
            value = validate(value);
            if (suffix !== undefined) validate(suffix);
            let end = value.length;
            while (end > 0 && value[end - 1] === "/") end--;
            const slash = value.lastIndexOf("/", end - 1);
            let result = value.slice(slash + 1, end);
            if (suffix && result.endsWith(suffix)) result = result.slice(0, -suffix.length);
            return result;
        }
        function extname(value) {
            const base = basename(validate(value));
            if (!base || base === "." || base === "..") return "";
            const dot = base.lastIndexOf(".");
            return dot <= 0 ? "" : base.slice(dot);
        }
        function parse(value) {
            value = validate(value);
            const root = value.startsWith("/") ? "/" : "";
            const base = basename(value);
            const ext = extname(base);
            const dir = dirname(value);
            return {
                root,
                dir: dir === "." ? "" : dir,
                base,
                ext,
                name: ext ? base.slice(0, -ext.length) : base
            };
        }
        function format(value) {
            if (!value || typeof value !== "object") throw new TypeError("pathObject must be an object");
            const dir = value.dir || value.root || "";
            const extension = value.ext
                ? (value.ext.startsWith(".") ? value.ext : "." + value.ext) : "";
            const base = value.base || ((value.name || "") + extension);
            if (!dir) return base;
            return dir === value.root ? dir + base : dir + "/" + base;
        }
        const path = {
            resolve, normalize, isAbsolute, join, relative, dirname, basename, extname, parse, format,
            toNamespacedPath(value) { return value; },
            sep: "/",
            delimiter: ":"
        };
        path.posix = path;
        return path;
    }
    const pathModule = createPathModule();
    class NexaEventEmitter {
        constructor() {
            this._events = new Map();
            this._maxListeners = undefined;
        }
        addListener(name, listener) { return this.on(name, listener); }
        on(name, listener) {
            if (typeof listener !== "function") throw new TypeError("listener must be a function");
            const entries = this._events.get(name) || [];
            entries.push(listener);
            this._events.set(name, entries);
            return this;
        }
        prependListener(name, listener) {
            if (typeof listener !== "function") throw new TypeError("listener must be a function");
            const entries = this._events.get(name) || [];
            entries.unshift(listener);
            this._events.set(name, entries);
            return this;
        }
        once(name, listener) { return this._addOnce(name, listener, false); }
        prependOnceListener(name, listener) { return this._addOnce(name, listener, true); }
        _addOnce(name, listener, prepend) {
            if (typeof listener !== "function") throw new TypeError("listener must be a function");
            const wrapped = (...args) => {
                this.removeListener(name, wrapped);
                listener.apply(this, args);
            };
            wrapped.listener = listener;
            return prepend ? this.prependListener(name, wrapped) : this.on(name, wrapped);
        }
        emit(name, ...args) {
            const entries = this._events.get(name);
            if (!entries?.length) {
                if (name === "error") throw args[0] instanceof Error
                    ? args[0] : new Error(args[0] === undefined ? "Unhandled error event" : String(args[0]));
                return false;
            }
            for (const listener of [...entries]) listener.apply(this, args);
            return true;
        }
        removeListener(name, listener) {
            const entries = this._events.get(name);
            if (!entries) return this;
            for (let index = entries.length - 1; index >= 0; index--) {
                const current = entries[index];
                if (current === listener || current.listener === listener) {
                    entries.splice(index, 1);
                    if (!entries.length) this._events.delete(name);
                    if (name !== "removeListener") this.emit("removeListener", name, current.listener || current);
                    break;
                }
            }
            return this;
        }
        off(name, listener) { return this.removeListener(name, listener); }
        removeAllListeners(name) {
            if (name !== undefined) {
                if (name !== "removeListener") {
                    for (const listener of this.rawListeners(name).reverse()) {
                        this.emit("removeListener", name, listener.listener || listener);
                    }
                }
                this._events.delete(name);
                return this;
            }
            for (const event of this.eventNames()) this.removeAllListeners(event);
            this._events.clear();
            return this;
        }
        listeners(name) { return this.rawListeners(name).map(listener => listener.listener || listener); }
        rawListeners(name) { return [...(this._events.get(name) || [])]; }
        listenerCount(name, listener) {
            return listener ? this.listeners(name).filter(item => item === listener).length
                : (this._events.get(name)?.length || 0);
        }
        eventNames() { return [...this._events.keys()]; }
        setMaxListeners(count) {
            if (typeof count !== "number" || count < 0 || Number.isNaN(count)) {
                throw new RangeError("max listeners must be a non-negative number");
            }
            this._maxListeners = count;
            return this;
        }
        getMaxListeners() { return this._maxListeners ?? NexaEventEmitter.defaultMaxListeners; }
    }
    NexaEventEmitter.defaultMaxListeners = 10;
    NexaEventEmitter.EventEmitter = NexaEventEmitter;
    NexaEventEmitter.listenerCount = (emitter, name) => emitter.listenerCount(name);
    NexaEventEmitter.once = (emitter, name) => new Promise((resolve, reject) => {
        const cleanup = () => {
            emitter.removeListener(name, onEvent);
            emitter.removeListener("error", onError);
        };
        const onEvent = (...args) => { cleanup(); resolve(args); };
        const onError = error => { cleanup(); reject(error); };
        emitter.once(name, onEvent);
        if (name !== "error") emitter.once("error", onError);
    });
    NexaEventEmitter.on = (emitter, name) => {
        const queue = [];
        const waiting = [];
        let ended = false;
        let failure = null;
        const deliver = () => {
            while (waiting.length && queue.length) waiting.shift()({value: queue.shift(), done: false});
            if (ended) while (waiting.length) waiting.shift()(
                failure ? Promise.reject(failure) : {value: undefined, done: true});
        };
        const onEvent = (...args) => { queue.push(args); deliver(); };
        const onError = error => { failure = error; ended = true; cleanup(); deliver(); };
        const onClose = () => { ended = true; cleanup(); deliver(); };
        const cleanup = () => {
            emitter.removeListener(name, onEvent);
            emitter.removeListener("error", onError);
            emitter.removeListener("close", onClose);
        };
        emitter.on(name, onEvent);
        if (name !== "error") emitter.on("error", onError);
        emitter.on("close", onClose);
        return {
            [Symbol.asyncIterator]() { return this; },
            next() {
                if (queue.length) return Promise.resolve({value: queue.shift(), done: false});
                if (failure) return Promise.reject(failure);
                if (ended) return Promise.resolve({value: undefined, done: true});
                return new Promise(resolve => waiting.push(resolve));
            },
            return() {
                ended = true;
                cleanup();
                deliver();
                return Promise.resolve({value: undefined, done: true});
            }
        };
    };

    if (native) native.onmessage = event => {
        let message;
        try {
            message = JSON.parse(event.data);
        } catch (_) {
            return;
        }

        if (message.kind === "reply") {
            const call = pending.get(message.id);
            if (!call) return;
            pending.delete(message.id);
            if (message.error !== undefined) call.reject(new Error(message.error));
            else call.resolve(message.value);
            return;
        }

        if (message.kind !== "event") return;
        if (message.channel === "__nexa:execute-js") {
            const [requestId, code] = message.args || [];
            Promise.resolve().then(() => window.eval(String(code))).then(value => {
                try {
                    let encoded = encodeFsValue(value);
                    if (encoded === undefined) encoded = {__nexaType: "Undefined"};
                    JSON.stringify(encoded);
                    ipcRenderer.send("__nexa:execute-js-result", requestId, {value: encoded});
                } catch (error) {
                    ipcRenderer.send("__nexa:execute-js-result", requestId, {
                        error: `JavaScript result cannot cross the Electron bridge: ${error.message}`
                    });
                }
            }, error => ipcRenderer.send("__nexa:execute-js-result", requestId, {
                error: error?.message || String(error)
            }));
            return;
        }
        if (message.channel === "__nexa:insert-css") {
            const [key, css] = message.args || [];
            const style = document.createElement("style");
            style.textContent = String(css ?? "");
            (document.head || document.documentElement).appendChild(style);
            insertedStyles.set(String(key), style);
            return;
        }
        if (message.channel === "__nexa:remove-css") {
            const style = insertedStyles.get(String((message.args || [])[0]));
            if (style) {
                style.remove();
                insertedStyles.delete(String((message.args || [])[0]));
            }
            return;
        }
        if (message.channel === "__nexa:set-zoom") {
            const factor = Number((message.args || [])[0]);
            if (Number.isFinite(factor) && factor > 0) {
                document.documentElement.style.zoom = String(factor);
            }
            return;
        }
        const entries = listeners.get(message.channel);
        if (!entries) return;
        const ipcEvent = {
            sender: window.__NEXA_ELECTRON_WINDOW_ID__ === undefined ? null
                : remoteElectronTarget("webContents", window.__NEXA_ELECTRON_WINDOW_ID__),
            senderFrame: {url: location.href},
            frameId: 0,
            processId: window.process.pid,
            ports: [],
            returnValue: undefined,
            reply(channel, ...args) { ipcRenderer.send(channel, ...args); }
        };
        for (const entry of [...entries]) {
            if (entry.once) {
                removeListenerEntry(message.channel, entry);
            }
            entry.listener(ipcEvent, ...(message.args || []));
        }
    };

    const electron = {
        ipcRenderer,
        contextBridge: {
            exposeInMainWorld(name, value) {
                Object.defineProperty(window, name, {
                    configurable: false,
                    enumerable: true,
                    value
                });
            }
        },
        shell: {openExternal: url => ipcRenderer.invoke("shell.openExternal", url)},
        clipboard: {
            readText: () => ipcRenderer.invoke("clipboard.readText"),
            writeText: text => ipcRenderer.invoke("clipboard.writeText", text)
        },
        app: {
            getPath: name => fsResult(ipcRenderer.sendSync("nexa:electron:app:getPath", name)),
            getVersion: () => ipcRenderer.sendSync("nexa:electron:app:getVersion"),
            getName: () => ipcRenderer.sendSync("nexa:electron:app:getName"),
            getAppPath: () => ipcRenderer.sendSync("nexa:electron:app:getAppPath")
        },
        webFrame: {
            setZoomFactor: factor => ipcRenderer.sendSync(
                "nexa:electron:webFrame:setZoomFactor", factor),
            clearCache: () => ipcRenderer.send("nexa:electron:webFrame:clearCache")
        },
        crashReporter: {
            start: options => ipcRenderer.send("nexa:electron:crashReporter:start", options),
            addExtraParameter: (key, value) => ipcRenderer.send(
                "nexa:electron:crashReporter:addExtraParameter", key, value),
            removeExtraParameter: key => ipcRenderer.send(
                "nexa:electron:crashReporter:removeExtraParameter", key),
            getParameters: () => ipcRenderer.sendSync(
                "nexa:electron:crashReporter:getParameters"),
            getLastCrashReport: () => null,
            getUploadedReports: () => []
        },
        BrowserWindow: {
            getAllWindows: () => fsResult(ipcRenderer.sendSync(
                "nexa:electron:windows:list")).map(item =>
                remoteElectronTarget("window", item.id, item.title)),
            getFocusedWindow: () => {
                const windows = fsResult(ipcRenderer.sendSync("nexa:electron:windows:list"));
                const window = windows.find(item => item.focused);
                return window ? remoteElectronTarget("window", window.id, window.title) : null;
            },
            fromId: id => {
                const window = fsResult(ipcRenderer.sendSync("nexa:electron:windows:list"))
                    .find(item => item.id === Number(id));
                return window ? remoteElectronTarget("window", window.id, window.title) : null;
            },
            fromWebContents: webContents => webContents?.id
                ? remoteElectronTarget("window", webContents.id) : null
        },
        webContents: {
            getAllWebContents: () => fsResult(ipcRenderer.sendSync(
                "nexa:electron:webContents:list")).map(item =>
                remoteElectronTarget("webContents", item.id, item.title)),
            getFocusedWebContents: () => {
                const contents = fsResult(ipcRenderer.sendSync("nexa:electron:webContents:list"));
                const webContents = contents.find(item => item.focused);
                return webContents
                    ? remoteElectronTarget("webContents", webContents.id, webContents.title) : null;
            },
            fromId: id => {
                const contents = fsResult(ipcRenderer.sendSync("nexa:electron:webContents:list"))
                    .find(item => item.id === Number(id));
                return contents
                    ? remoteElectronTarget("webContents", contents.id, contents.title) : null;
            }
        }
    };

    const appEvents = new NexaEventEmitter();
    const appOn = appEvents.on;
    appEvents.on = function(name, listener) {
        const first = !this._events.has(name);
        appOn.call(this, name, listener);
        if (first) ipcRenderer.send("nexa:electron:event:subscribe", "app", null, name);
        return this;
    };
    electron.app.on = appEvents.on.bind(appEvents);
    electron.app.once = appEvents.once.bind(appEvents);
    electron.app.removeListener = appEvents.removeListener.bind(appEvents);
    electron.app.removeAllListeners = appEvents.removeAllListeners?.bind(appEvents);
    const remoteElectronTargets = new Map();
    function remoteElectronTarget(type, id, title) {
        const key = `${type}:${id}`;
        if (remoteElectronTargets.has(key)) return remoteElectronTargets.get(key);
        const target = new NexaEventEmitter();
        target.id = id;
        if (type === "window") target.title = title || "";
        if (type === "webContents") target.getTitle = () => title || "";
        const call = (method, ...args) => fsResult(ipcRenderer.sendSync(
            "nexa:electron:object:call", type, id, method, encodeFsValue(args)));
        const methods = type === "window" ? [
            "close", "destroy", "show", "hide", "focus", "blur", "minimize", "restore",
            "maximize", "unmaximize", "isMaximized", "isMinimized", "isVisible", "isFocused",
            "isDestroyed", "isFullScreen", "getBounds", "getSize", "getPosition", "getTitle",
            "setTitle", "setBounds", "setSize", "setPosition", "setFullScreen", "setResizable",
            "setMenuBarVisibility"
        ] : ["getURL", "getTitle", "isDestroyed", "send", "setBackgroundThrottling",
            "setZoomFactor", "openDevTools", "closeDevTools", "stop"];
        const asyncMethods = type === "window" ? ["loadURL", "loadFile"]
            : ["loadURL", "loadFile", "reload", "reloadIgnoringCache", "executeJavaScript",
                "executeJavaScriptInIsolatedWorld"];
        for (const method of methods) target[method] = (...args) => call(method, ...args);
        for (const method of asyncMethods) target[method] = (...args) => ipcRenderer.invoke(
            "nexa:electron:object:invoke", type, id, method, encodeFsValue(args)).then(fsResult);
        if (type === "window") target.webContents = remoteElectronTarget("webContents", id, title);
        const targetOn = target.on;
        target.on = function(name, listener) {
            const first = !this._events.has(name);
            targetOn.call(this, name, listener);
            if (first) ipcRenderer.send("nexa:electron:event:subscribe", type, id, name);
            return this;
        };
        remoteElectronTargets.set(key, target);
        return target;
    }
    function decodeElectronEventArg(value) {
        if (Array.isArray(value)) return value.map(decodeElectronEventArg);
        if (!value || typeof value !== "object") return value;
        if (value.__nexaType === "BrowserWindow") {
            return remoteElectronTarget("window", value.id, value.title);
        }
        if (value.__nexaType === "WebContents") {
            return remoteElectronTarget("webContents", value.id, value.title);
        }
        return decodeFsValue(value);
    }
    ipcRenderer.on("__nexa:electron:event", (_event, type, id, name, args) => {
        const target = type === "app" ? appEvents : remoteElectronTarget(type, id);
        target.emit(name, ...(args || []).map(decodeElectronEventArg));
        if ((type === "window" && name === "closed") ||
                (type === "webContents" && name === "destroyed")) {
            remoteElectronTargets.delete(`${type}:${id}`);
        }
    });

    if (!("electron" in window)) {
        Object.defineProperty(window, "electron", {configurable: true, value: electron});
    }
    Object.defineProperty(window, "nexaElectron", {configurable: true, value: electron});
    const commonJsModules = new Map();
    const moduleResolutionCache = new Map();
    function resolveCommonJsModule(specifier, parentFile) {
        const key = JSON.stringify([parentFile, specifier]);
        if (moduleResolutionCache.has(key)) {
            const cached = moduleResolutionCache.get(key);
            moduleResolutionCache.delete(key);
            moduleResolutionCache.set(key, cached);
            return cached;
        }
        const resolved = fsResult(ipcRenderer.sendSync("nexa:node:module:resolve",
            specifier, parentFile));
        moduleResolutionCache.set(key, resolved);
        if (moduleResolutionCache.size > 512) {
            moduleResolutionCache.delete(moduleResolutionCache.keys().next().value);
        }
        return resolved;
    }
    const promisifyCustom = Symbol.for("nodejs.util.promisify.custom");
    function utilModule() {
        function promisify(callbackFunction) {
            if (typeof callbackFunction !== "function") {
                throw new TypeError("promisify expects a function");
            }
            if (callbackFunction[promisifyCustom]) return callbackFunction[promisifyCustom];
            return (...args) => new Promise((resolve, reject) => {
                callbackFunction(...args, (error, ...values) => {
                    if (error) reject(error);
                    else resolve(values.length > 1 ? values : values[0]);
                });
            });
        }
        promisify.custom = promisifyCustom;
        return {promisify, inherits(child, parent) {
            child.super_ = parent;
            Object.setPrototypeOf(child.prototype, parent.prototype);
        }};
    }
    const builtinModules = new Map();
    function builtinModule(name, parentFile) {
        if (typeof name !== "string") return null;
        let key = name.startsWith("node:") ? name.slice(5) : name;
        if (key === "original-fs") key = "fs";
        if (key === "electron/main") key = "electron";
        if (builtinModules.has(key)) return builtinModules.get(key);

        let module = null;
        if (key === "electron") module = electron;
        else if (key === "fs") module = fsModule;
        else if (key === "fs/promises") module = fsPromises;
        else if (key === "path") module = pathModule;
        else if (key === "events") module = NexaEventEmitter;
        else if (key === "buffer") module = {Buffer: NexaBuffer};
        else if (key === "process") module = window.process;
        else if (key === "util") module = utilModule();
        else if (key === "os") module = {
            platform: () => "linux",
            homedir: () => String(window.__NEXA_ELECTRON_HOME__ || "/"),
            tmpdir: () => "/tmp",
            EOL: "\n",
            arch: () => window.process.arch,
            type: () => "Linux",
            endianness: () => "LE",
            cpus: () => Array.from({length: Math.max(1,
                Number(navigator.hardwareConcurrency) || 1)}, () => ({
                model: "Android WebView CPU", speed: 0,
                times: {user: 0, nice: 0, sys: 0, idle: 0, irq: 0}
            })),
            availableParallelism: () => Math.max(1,
                Number(navigator.hardwareConcurrency) || 1)
        };
        else if (key === "child_process") module = loadNativeModule(key, parentFile);
        else if (key === "url") module = {
            URL, URLSearchParams,
            pathToFileURL: value => new URL(`file://${pathModule.resolve(value)}`),
            fileURLToPath: value => decodeURIComponent(new URL(value).pathname)
        };
        else if (key === "assert") module = {
            ok(value, message) { if (!value) throw new Error(message || "assertion failed"); },
            strictEqual(actual, expected, message) {
                if (actual !== expected) throw new Error(message || "values are not equal");
            }
        };
        if (module !== null) builtinModules.set(key, module);
        return module;
    }
    function loadCommonJsModule(specifier, parentFile) {
        const builtin = builtinModule(specifier, parentFile);
        if (builtin !== null) return builtin;
        const resolved = resolveCommonJsModule(specifier, parentFile);
        if (resolved.builtin) {
            return loadNativeModule(resolved.builtin, parentFile);
        }
        const filename = resolved.filename;
        if (filename.endsWith(".node")) return loadNativeModule(filename, parentFile);
        if (commonJsModules.has(filename)) return commonJsModules.get(filename).exports;

        const source = fsResult(ipcRenderer.sendSync("nexa:node:module:source", filename));
        if (filename.endsWith(".json")) {
            const jsonModule = {exports: JSON.parse(source), filename, loaded: true};
            commonJsModules.set(filename, jsonModule);
            return jsonModule.exports;
        }
        const module = {exports: {}, filename, id: filename, loaded: false};
        commonJsModules.set(filename, module);
        const localRequire = request => loadCommonJsModule(request, filename);
        localRequire.resolve = request => {
            const result = resolveCommonJsModule(request, filename);
            return result.filename || result.builtin;
        };
        localRequire.cache = commonJsModules;
        localRequire.main = null;
        module.require = localRequire;
        try {
            const factory = new Function("require", "module", "exports", "__filename",
                "__dirname", source.replace(/^#!.*(?:\r?\n|$)/, ""));
            factory(localRequire, module, module.exports, filename,
                pathModule.dirname(filename));
            module.loaded = true;
            return module.exports;
        } catch (error) {
            commonJsModules.delete(filename);
            throw error;
        }
    }
    if (typeof window.require !== "function") {
        Object.defineProperty(window, "require", {
            configurable: true,
            value: name => loadCommonJsModule(name, window.__NEXA_ELECTRON_PRELOAD_BASE__)
        });
    }
    if (typeof window.Buffer === "undefined") {
        Object.defineProperty(window, "Buffer", {configurable: true, value: NexaBuffer});
    }
    if (native) {
        native.postMessage(JSON.stringify({kind: "preload-ready",
            hasRequire: typeof window.require === "function"}));
    }
})();
