from secrets import choice
from socket import AF_UNIX, SOCK_STREAM, SO_PEERCRED, SOL_SOCKET, socket, timeout
from string import ascii_letters, digits
from struct import unpack
from subprocess import DEVNULL, run
from threading import RLock
from os import getuid

from termuxgui.connection import Connection


class NexaConnection(Connection):
    def __init__(self):
        self._Connection__main_lock = RLock()
        self._Connection__event_lock = RLock()

        main_name = "nexa" + "".join(choice(ascii_letters + digits) for _ in range(46))
        event_name = "nexa" + "".join(choice(ascii_letters + digits) for _ in range(46))
        main_server = socket(AF_UNIX, SOCK_STREAM)
        event_server = socket(AF_UNIX, SOCK_STREAM)

        with main_server, event_server:
            main_server.bind("\0" + main_name)
            event_server.bind("\0" + event_name)
            main_server.listen(1)
            event_server.listen(1)
            main_server.settimeout(5)
            event_server.settimeout(5)

            broadcast = [
                "broadcast", "-n", "dev.encdata.nexa/com.termux.gui.NexaReceiver",
                "--es", "mainSocket", main_name,
                "--es", "eventSocket", event_name,
            ]
            result = run(["termux-am", *broadcast], stdout=DEVNULL, stderr=DEVNULL)
            if result.returncode != 0:
                result = run(["am", *broadcast], stdout=DEVNULL, stderr=DEVNULL)
            if result.returncode != 0:
                raise RuntimeError("could not request a Nexa connection")

            main = None
            event = None
            try:
                main = main_server.accept()[0]
                event = event_server.accept()[0]
                main_uid = unpack("III", main.getsockopt(SOL_SOCKET, SO_PEERCRED, 12))[1]
                event_uid = unpack("III", event.getsockopt(SOL_SOCKET, SO_PEERCRED, 12))[1]
                if main_uid != getuid() or event_uid != getuid():
                    raise RuntimeError("Nexa sockets are not owned by Termux")

                main.sendall(b"\x01")
                if main.recv(1) != b"\x00":
                    raise RuntimeError("Nexa connection protocol mismatch")

                self._main = main
                self._event = event
            except Exception as error:
                if main is not None:
                    main.close()
                if event is not None:
                    event.close()
                if isinstance(error, timeout):
                    raise RuntimeError("could not connect to Nexa Launcher") from error
                raise
