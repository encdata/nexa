import fs from "node:fs";
import crypto from "node:crypto";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import {Readable} from "node:stream";
import {createElectronCompat} from "./electron_compat.mjs";

const [rootArg, entryArg = "index.html", portArg = "0", mainArg = ""] = process.argv.slice(2);
if (!rootArg) throw new Error("usage: electron_host.mjs <app-root-or-app.asar> [entry] [port] [main-entry]");
const ipcToken = process.env.NEXA_ELECTRON_IPC_TOKEN || "";
const eventFd = Number(process.env.NEXA_ELECTRON_EVENT_FD);
if (!Number.isInteger(eventFd) || eventFd < 3) throw new Error("Nexa event pipe is unavailable");
const parentFd = Number(process.env.NEXA_ELECTRON_PARENT_FD);
let electronHost = null;
if (Number.isInteger(parentFd) && parentFd >= 3) {
    const parentControl = fs.createReadStream(null, {fd: parentFd});
    let controlBuffer = "";
    parentControl.setEncoding("utf8");
    parentControl.on("data", chunk => {
        controlBuffer += chunk;
        let newline;
        while ((newline = controlBuffer.indexOf("\n")) !== -1) {
            const line = controlBuffer.slice(0, newline);
            controlBuffer = controlBuffer.slice(newline + 1);
            if (Buffer.byteLength(line) > 1024 * 1024) continue;
            try {
                const message = JSON.parse(line);
                if (message.kind === "rendererEvent") {
                    electronHost?.dispatchRendererEvent(message);
                }
            } catch (error) {
                process.stderr.write(`nexa electron: invalid parent control message: ${error.message}\n`);
            }
        }
        if (Buffer.byteLength(controlBuffer) > 1024 * 1024) {
            controlBuffer = "";
            process.stderr.write("nexa electron: parent control message exceeds 1 MiB\n");
        }
    });
    parentControl.on("end", () => {
        try {
            process.kill(-process.pid, "SIGTERM");
        } catch (_) {
            process.exit(0);
        }
    });
    parentControl.on("error", () => {
        try {
            process.kill(-process.pid, "SIGTERM");
        } catch (_) {
            process.exit(0);
        }
    });
    parentControl.resume();
}
const maxIpcPayloadBytes = 1024 * 1024;
const maxRendererEventBytes = 8 * 1024 * 1024;

let root = path.resolve(rootArg);
let realRoot = fs.realpathSync(root);
const sourceRoot = root;
let applicationExecutable = process.execPath;
const entry = entryArg.replace(/^\/+/, "");
let baseUrl = "";
const trustedOrigins = new Set();
const mimeTypes = new Map([
    [".css", "text/css; charset=utf-8"],
    [".html", "text/html; charset=utf-8"],
    [".ico", "image/x-icon"],
    [".jpeg", "image/jpeg"],
    [".jpg", "image/jpeg"],
    [".js", "text/javascript; charset=utf-8"],
    [".json", "application/json; charset=utf-8"],
    [".mjs", "text/javascript; charset=utf-8"],
    [".png", "image/png"],
    [".svg", "image/svg+xml"],
    [".wasm", "application/wasm"],
    [".webp", "image/webp"],
    [".woff", "font/woff"],
    [".woff2", "font/woff2"]
]);

function readAsar(file) {
    const fd = fs.openSync(file, "r");
    try {
        const headerPrefix = Buffer.alloc(16);
        fs.readSync(fd, headerPrefix, 0, headerPrefix.length, 0);
        const headerSize = headerPrefix.readUInt32LE(4);
        const jsonSize = headerPrefix.readUInt32LE(12);
        if (jsonSize <= 0 || jsonSize + 16 > headerSize + 8) {
            throw new Error("invalid asar header");
        }
        const json = Buffer.alloc(jsonSize);
        fs.readSync(fd, json, 0, jsonSize, 16);
        const header = JSON.parse(json.toString("utf8"));
        return {fd, base: 8 + headerSize, header};
    } catch (error) {
        fs.closeSync(fd);
        throw error;
    }
}

function* walkAsar(directory, prefix = "") {
    for (const [name, node] of Object.entries(directory.files || {})) {
        const relative = prefix ? `${prefix}/${name}` : name;
        if (node.files) yield* walkAsar(node, relative);
        else yield [relative, node];
    }
}

function getAsarItem(header, relative) {
    let node = header;
    for (const part of relative.split("/")) {
        node = node?.files?.[part];
        if (!node) return null;
    }
    return node.files ? null : node;
}

let archive = fs.statSync(root).isFile() ? readAsar(root) : null;
if (archive && !root.endsWith(".asar")) throw new Error("app archive must end in .asar");
const rendererArchive = archive ? {base: archive.base, header: archive.header} : null;
const extractedArchives = new Map();
const extractedRoots = new Set([realRoot]);
const linkedModuleArchives = new Set();

function findApplicationExecutable(archivePath) {
    const resourcesDirectory = path.dirname(archivePath);
    const appDirectory = path.basename(resourcesDirectory) === "resources"
        ? path.dirname(resourcesDirectory) : resourcesDirectory;
    let candidates;
    try {
        candidates = fs.readdirSync(appDirectory, {withFileTypes: true});
    } catch (_) {
        return null;
    }
    const binaries = [];
    for (const candidate of candidates) {
        if (!candidate.isFile() || candidate.name === "chrome-sandbox" ||
            candidate.name === "chrome_crashpad_handler" || /\.(?:so|node)(?:\.|$)/i.test(candidate.name)) {
            continue;
        }
        const file = path.join(appDirectory, candidate.name);
        try {
            const stat = fs.statSync(file);
            if (!(stat.mode & 0o111)) continue;
            const fd = fs.openSync(file, "r");
            const header = Buffer.alloc(20);
            try {
                if (fs.readSync(fd, header, 0, header.length, 0) !== header.length) continue;
            } finally {
                fs.closeSync(fd);
            }
            if (header.subarray(0, 4).equals(Buffer.from([0x7f, 0x45, 0x4c, 0x46])) &&
                header[5] === 1 && header.readUInt16LE(18) === 62) {
                binaries.push({file, size: stat.size});
            }
        } catch (_) {}
    }
    binaries.sort((left, right) => right.size - left.size);
    return binaries[0]?.file || null;
}

function extractAsar(file, parsed = readAsar(file)) {
    file = path.resolve(file);
    if (extractedArchives.has(file)) {
        fs.closeSync(parsed.fd);
        return extractedArchives.get(file);
    }
    const stat = fs.statSync(file);
    const key = crypto.createHash("sha256")
        .update(file).update(String(stat.size)).update(String(stat.mtimeMs)).digest("hex");
    const cacheRoot = path.join(process.env.HOME || os.tmpdir(), ".cache", "nexa", "electron", key);
    const complete = path.join(cacheRoot, ".nexa-extracted");
    if (fs.existsSync(complete)) {
        fs.closeSync(parsed.fd);
        extractedArchives.set(file, cacheRoot);
        return cacheRoot;
    }

    fs.rmSync(cacheRoot, {recursive: true, force: true});
    fs.mkdirSync(cacheRoot, {recursive: true});
    try {
        const chunk = Buffer.allocUnsafe(64 * 1024);
        for (const [relative, item] of walkAsar(parsed.header)) {
            const destination = path.resolve(cacheRoot, relative);
            if (destination !== cacheRoot && !destination.startsWith(cacheRoot + path.sep)) continue;
            fs.mkdirSync(path.dirname(destination), {recursive: true});
            if (item.link) {
                const target = path.posix.normalize(path.posix.join(path.posix.dirname(relative), item.link));
                if (target.startsWith("../") || path.posix.isAbsolute(target)) continue;
                fs.symlinkSync(path.relative(path.dirname(destination), path.join(cacheRoot, target)), destination);
                continue;
            }
            if (item.unpacked) {
                const source = `${file}.unpacked/${relative}`;
                const realSource = fs.realpathSync(source);
                const unpackedRoot = fs.realpathSync(`${file}.unpacked`);
                if (realSource !== unpackedRoot && !realSource.startsWith(unpackedRoot + path.sep)) continue;
                fs.copyFileSync(realSource, destination);
                continue;
            }

            const sourceOffset = parsed.base + Number(item.offset);
            const output = fs.openSync(destination, "w", item.executable ? 0o755 : 0o644);
            let offset = sourceOffset;
            let remaining = item.size;
            try {
                while (remaining > 0) {
                    const size = Math.min(chunk.length, remaining);
                    let read = 0;
                    while (read < size) {
                        const count = fs.readSync(parsed.fd, chunk, read, size - read, offset + read);
                        if (count === 0) throw new Error(`truncated asar file: ${relative}`);
                        read += count;
                    }
                    let written = 0;
                    while (written < read) {
                        written += fs.writeSync(output, chunk, written, read - written);
                    }
                    offset += read;
                    remaining -= read;
                }
            } finally {
                fs.closeSync(output);
            }
        }
        fs.writeFileSync(complete, "ok");
    } catch (error) {
        fs.rmSync(cacheRoot, {recursive: true, force: true});
        throw error;
    } finally {
        fs.closeSync(parsed.fd);
    }
    extractedArchives.set(file, cacheRoot);
    return cacheRoot;
}

function linkElectronModules(archivePath, extractedRoot) {
    const key = path.resolve(archivePath);
    if (linkedModuleArchives.has(key)) return;
    const modulesRoot = path.resolve(path.dirname(archivePath), "..", "modules");
    if (!fs.existsSync(modulesRoot)) {
        linkedModuleArchives.add(key);
        return;
    }
    extractedRoots.add(fs.realpathSync(modulesRoot));
    const nodeModules = path.join(extractedRoot, "node_modules");
    fs.mkdirSync(nodeModules, {recursive: true});

    function linkPackage(directory) {
        const manifest = path.join(directory, "package.json");
        if (!fs.existsSync(manifest)) return;
        try {
            const name = JSON.parse(fs.readFileSync(manifest, "utf8")).name;
            if (typeof name !== "string" || !name || name.startsWith("/") || name.includes("..")) return;
            const destination = path.join(nodeModules, ...name.split("/"));
            if (!destination.startsWith(nodeModules + path.sep) || fs.existsSync(destination)) return;
            fs.mkdirSync(path.dirname(destination), {recursive: true});
            fs.symlinkSync(directory, destination, "dir");
        } catch (_) {}
    }

    for (const entry of fs.readdirSync(modulesRoot, {withFileTypes: true})) {
        if (!entry.isDirectory()) continue;
        const directory = path.join(modulesRoot, entry.name);
        linkPackage(directory);
        for (const nested of fs.readdirSync(directory, {withFileTypes: true})) {
            if (nested.isDirectory()) linkPackage(path.join(directory, nested.name));
        }
    }
    linkedModuleArchives.add(key);
}

if (archive && mainArg) {
    applicationExecutable = findApplicationExecutable(sourceRoot) || applicationExecutable;
    root = extractAsar(root, archive);
    linkElectronModules(sourceRoot, root);
    archive = null;
    realRoot = fs.realpathSync(root);
    extractedRoots.add(realRoot);
} else if (archive) {
    fs.closeSync(archive.fd);
}

function extractElectronArchive(file) {
    const extracted = extractAsar(file);
    linkElectronModules(file, extracted);
    extractedRoots.add(fs.realpathSync(extracted));
    return extracted;
}

function isTrustedPath(file) {
    const absolute = path.resolve(file);
    return [...extractedRoots].some(root => absolute === root || absolute.startsWith(root + path.sep));
}

let eventPipeClosed = false;
let oversizedEventWarned = false;
function emit(event) {
    if (eventPipeClosed) return;
    const buffer = Buffer.from(JSON.stringify(event) + "\n");
    const maxEventBytes = event.event === "renderer"
        ? maxRendererEventBytes : maxIpcPayloadBytes;
    if (buffer.length - 1 > maxEventBytes) {
        if (!oversizedEventWarned) {
            process.stderr.write(`nexa electron: host event exceeds ${maxEventBytes / 1024 / 1024} MiB and was dropped\n`);
            oversizedEventWarned = true;
        }
        return;
    }
    let offset = 0;
    try {
        while (offset < buffer.length) {
            const written = fs.writeSync(eventFd, buffer, offset, buffer.length - offset);
            if (written <= 0) {
                eventPipeClosed = true;
                return;
            }
            offset += written;
        }
    } catch (_) {
        // a closed Nexa pipe means the owning desktop session is gone
        eventPipeClosed = true;
    }
}

function localUrl(relative) {
    const url = new URL(baseUrl);
    url.pathname = "/" + relative.split("/").map(encodeURIComponent).join("/");
    return url.href;
}

electronHost = mainArg && !archive
    ? createElectronCompat({
        appRoot: root,
        appPath: sourceRoot,
        resourcesPath: path.dirname(sourceRoot),
        localUrl,
        emit,
        resolveAsar: extractElectronArchive,
        trustOrigin: origin => trustedOrigins.add(origin),
        isTrustedPath,
        executablePath: applicationExecutable
    })
    : null;
const electron = electronHost?.electron || null;

function getAsset(relative, headOnly = false) {
    const safe = path.posix.normalize(relative).replace(/^\.\//, "");
    if (!safe || safe === "." || safe.startsWith("../") || path.posix.isAbsolute(safe)) return null;

    if (rendererArchive) {
        const item = getAsarItem(rendererArchive.header, safe);
        if (!item || item.link) return null;
        if (item.unpacked) {
            const unpackedRoot = `${sourceRoot}.unpacked`;
            const file = path.resolve(unpackedRoot, safe);
            if (file !== unpackedRoot && !file.startsWith(unpackedRoot + path.sep)) return null;
            try {
                const realFile = fs.realpathSync(file);
                const realUnpackedRoot = fs.realpathSync(unpackedRoot);
                if (realFile !== realUnpackedRoot && !realFile.startsWith(realUnpackedRoot + path.sep)) return null;
                const size = fs.statSync(realFile).size;
                return {stream: headOnly ? null : fs.createReadStream(realFile), size};
            } catch (_) {
                return null;
            }
        }
        const start = rendererArchive.base + Number(item.offset);
        if (headOnly) return {stream: null, size: item.size};
        if (item.size === 0) return {stream: Readable.from([]), size: 0};
        return {stream: fs.createReadStream(sourceRoot, {start, end: start + item.size - 1}), size: item.size};
    }

    const file = path.resolve(root, safe);
    if (file !== root && !file.startsWith(root + path.sep)) return null;
    try {
        const realFile = fs.realpathSync(file);
        if (realFile !== realRoot && !realFile.startsWith(realRoot + path.sep)) return null;
        const stat = fs.statSync(realFile);
        if (!stat.isFile()) return null;
        return {stream: headOnly ? null : fs.createReadStream(realFile), size: stat.size};
    } catch (_) {
        return null;
    }
}

const server = http.createServer((request, response) => {
    const endpoint = request.url?.split("?", 1)[0];
    const preloadMatch = /^\/__nexa\/preload\/(\d+)$/.exec(endpoint || "");
    if (preloadMatch && request.method === "GET" && electronHost) {
        const suppliedToken = Buffer.from(String(request.headers["x-nexa-token"] || ""));
        const expectedToken = Buffer.from(ipcToken);
        if (!expectedToken.length || suppliedToken.length !== expectedToken.length ||
            !crypto.timingSafeEqual(suppliedToken, expectedToken)) {
            response.writeHead(403).end();
            return;
        }
        let preloadResponse;
        try {
            preloadResponse = electronHost.getPreload(Number(preloadMatch[1]));
        } catch (error) {
            response.writeHead(500, {"Content-Type": "application/json; charset=utf-8"});
            response.end(JSON.stringify({error: error.message || String(error)}));
            return;
        }
        if (preloadResponse === null) {
            response.writeHead(404).end();
            return;
        }
        const {source: preload, etag} = preloadResponse;
        const headers = {
            "Content-Type": "text/javascript; charset=utf-8",
            "Cache-Control": "private, no-cache",
            "ETag": etag,
            "X-Content-Type-Options": "nosniff"
        };
        if (request.headers["if-none-match"] === etag) {
            response.writeHead(304, headers).end();
            return;
        }
        headers["Content-Length"] = Buffer.byteLength(preload);
        response.writeHead(200, headers);
        response.end(preload);
        return;
    }
    if ((endpoint === "/__nexa/ipc" || endpoint === "/__nexa/renderer-event" ||
         endpoint === "/__nexa/window-command") &&
        electron && (request.method === "POST" || request.method === "OPTIONS")) {
        const origin = request.headers.origin;
        if (origin && !trustedOrigins.has(origin)) {
            response.writeHead(403).end();
            return;
        }
        if (origin) {
            response.setHeader("Access-Control-Allow-Origin", origin);
            response.setHeader("Vary", "Origin");
        }
        if (request.method === "OPTIONS") {
            response.writeHead(204, {
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "content-type"
            }).end();
            return;
        }
        const contentLength = Number(request.headers["content-length"]);
        if (Number.isFinite(contentLength) && contentLength > maxIpcPayloadBytes) {
            response.writeHead(413, {"Content-Type": "application/json; charset=utf-8"});
            response.end(JSON.stringify({error: "Electron IPC message exceeds 1 MiB"}));
            request.resume();
            return;
        }
        const chunks = [];
        let bodySize = 0;
        let bodyTooLarge = false;
        request.on("data", chunk => {
            if (bodyTooLarge) return;
            bodySize += chunk.length;
            if (bodySize > maxIpcPayloadBytes) {
                bodyTooLarge = true;
                chunks.length = 0;
                response.writeHead(413, {"Content-Type": "application/json; charset=utf-8"});
                response.end(JSON.stringify({error: "Electron IPC message exceeds 1 MiB"}));
                return;
            }
            chunks.push(chunk);
        });
        request.on("end", async () => {
            if (bodyTooLarge) return;
            try {
                const message = JSON.parse(Buffer.concat(chunks, bodySize).toString("utf8"));
                const suppliedToken = Buffer.from(String(
                    request.headers["x-nexa-token"] || message.token || ""));
                const expectedToken = Buffer.from(ipcToken);
                if (!expectedToken.length || suppliedToken.length !== expectedToken.length ||
                    !crypto.timingSafeEqual(suppliedToken, expectedToken)) {
                    response.writeHead(403).end();
                    return;
                }
                delete message.token;
                const windowHeader = request.headers["x-nexa-window-id"];
                if (windowHeader !== undefined) {
                    const windowId = Number(windowHeader);
                    if (!Number.isSafeInteger(windowId) || windowId < 0) {
                        response.writeHead(400).end();
                        return;
                    }
                    message.windowId = windowId;
                }
                let value;
                if (endpoint === "/__nexa/ipc") value = await electronHost.dispatch(message);
                else if (endpoint === "/__nexa/renderer-event") {
                    value = electronHost.dispatchRendererEvent(message);
                } else {
                    value = electronHost.windowCommand(message);
                }
                const payload = JSON.stringify({value: value === undefined ? null : value});
                if (Buffer.byteLength(payload) > maxIpcPayloadBytes) {
                    response.writeHead(413, {"Content-Type": "application/json; charset=utf-8"});
                    response.end(JSON.stringify({error: "Electron IPC response exceeds 1 MiB"}));
                    return;
                }
                response.writeHead(200, {"Content-Type": "application/json; charset=utf-8"});
                response.end(payload);
            } catch (error) {
                response.writeHead(500, {"Content-Type": "application/json; charset=utf-8"});
                response.end(JSON.stringify({error: error.message || String(error)}));
            }
        });
        return;
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
        response.writeHead(405, {Allow: "GET, HEAD"}).end();
        return;
    }

    let pathname;
    try {
        pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
    } catch (_) {
        response.writeHead(400).end();
        return;
    }
    const relative = pathname === "/" ? entry : pathname.replace(/^\/+/, "");
    const asset = getAsset(relative, request.method === "HEAD");
    if (!asset) {
        response.writeHead(404).end();
        return;
    }

    const headers = {
        "Content-Length": asset.size,
        "Content-Type": mimeTypes.get(path.extname(relative).toLowerCase()) || "application/octet-stream",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": relative.endsWith(".html") ? "no-store"
            : rendererArchive ? "public, max-age=31536000, immutable"
            : "public, max-age=86400"
    };
    response.writeHead(200, headers);
    if (request.method === "HEAD") response.end();
    else asset.stream.pipe(response);
});

server.listen(Number(portArg), "127.0.0.1", () => {
    const {port} = server.address();
    baseUrl = `http://127.0.0.1:${port}`;
    emit({event: "ready", url: localUrl(entry)});
    if (electronHost) setImmediate(() => electronHost.runMain(mainArg));
});

for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => server.close(() => process.exit(0)));
}
