const electron = globalThis[Symbol.for("nexa.electron.runtime")];
if (!electron) throw new Error("Nexa Electron runtime was not initialized");

export const {
    app,
    process,
    View,
    WebContentsView,
    BaseWindow,
    BrowserWindow,
    ipcMain,
    shell,
    net,
    webContents,
    nativeTheme,
    screen,
    session,
    dialog,
    clipboard,
    Menu,
    protocol,
    powerMonitor,
    crashReporter,
    nativeImage
} = electron;

export default electron;
