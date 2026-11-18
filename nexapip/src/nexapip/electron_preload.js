(() => {
    const native = window.nexaElectronNative;
    if (!native) return;

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
        native.postMessage(JSON.stringify({
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

    function request(kind, channel, args) {
        const id = kind === "invoke" ? nextId++ : null;
        if (id !== null) {
            return new Promise((resolve, reject) => {
                pending.set(id, {resolve, reject});
                native.postMessage(JSON.stringify({kind, id, channel, args}));
            });
        }
        native.postMessage(JSON.stringify({kind, channel, args}));
    }

    function addListener(channel, listener, once = false) {
        const entries = listeners.get(channel) || new Map();
        entries.set(listener, {once});
        listeners.set(channel, entries);
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
        once(channel, listener) {
            return addListener(channel, listener, true);
        },
        removeListener(channel, listener) {
            listeners.get(channel)?.delete(listener);
            return ipcRenderer;
        },
        removeAllListeners(channel) {
            if (channel === undefined) listeners.clear();
            else listeners.delete(channel);
            return ipcRenderer;
        }
    };

    native.onmessage = event => {
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
        const entries = listeners.get(message.channel);
        if (!entries) return;
        for (const [listener, options] of [...entries]) {
            listener({sender: null}, ...(message.args || []));
            if (options.once) entries.delete(listener);
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
        app: {getPath: name => ipcRenderer.invoke("app.getPath", name)}
    };

    if (!("electron" in window)) {
        Object.defineProperty(window, "electron", {configurable: true, value: electron});
    }
    Object.defineProperty(window, "nexaElectron", {configurable: true, value: electron});
    if (typeof window.require !== "function") {
        Object.defineProperty(window, "require", {
            configurable: true,
            value: name => {
                if (name === "electron") return electron;
                throw new Error(`Nexa WebView does not provide Node module: ${name}`);
            }
        });
    }
})();
